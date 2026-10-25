"""The run as an explicit state machine over one event log.

    PLANNING -> GENERATING -> VERIFYING -> PASSED
                                 | fail
                              REPAIRING -> VERIFYING      (bounded by max_repair_rounds)
                                 | rounds or budget exhausted
                              EXHAUSTED
    PASSED | EXHAUSTED -> EXPLAINING -> DONE              (any state -> FAILED on error)

Every transition is validated against ``TRANSITIONS`` and appended to the event log.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import structlog

from fabloom import metrics
from fabloom.agent.budget import BudgetExceededError, BudgetTracker
from fabloom.agent.events import EventLog, new_run_id
from fabloom.agent.explain import deterministic_explanation
from fabloom.agent.steps import LlmSteps
from fabloom.checks import allocate, compile_checks
from fabloom.config import RunConfig
from fabloom.contracts import (
    CheckSet,
    FabricPlan,
    Intent,
    Platform,
    RunState,
    Snapshot,
    Usage,
    VerificationReport,
    Verifier,
)
from fabloom.contracts.run import (
    BudgetExhausted,
    CandidateGenerated,
    ChecksCompiled,
    ExplanationReady,
    IntentPlanned,
    RunError,
    RunFinished,
    RunStarted,
    StateChanged,
    VerificationCompleted,
)
from fabloom.llm import ChatClient, ModelRouter
from fabloom.llm.grounding import Grounder
from fabloom.provenance import content_key

log = structlog.get_logger(__name__)

S = RunState
TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    S.PLANNING: frozenset({S.GENERATING, S.EXHAUSTED, S.FAILED}),
    S.GENERATING: frozenset({S.VERIFYING, S.EXHAUSTED, S.FAILED}),
    S.VERIFYING: frozenset({S.PASSED, S.REPAIRING, S.EXHAUSTED, S.FAILED}),
    S.REPAIRING: frozenset({S.VERIFYING, S.EXHAUSTED, S.FAILED}),
    S.PASSED: frozenset({S.EXPLAINING, S.FAILED}),
    S.EXHAUSTED: frozenset({S.EXPLAINING, S.FAILED}),
    S.EXPLAINING: frozenset({S.DONE, S.FAILED}),
    S.DONE: frozenset(),
    S.FAILED: frozenset(),
}

Outcome = Literal["passed", "exhausted", "failed"]


class InvalidTransitionError(RuntimeError):
    pass


@dataclass(frozen=True)
class RunRequest:
    request: str
    platform: Platform | None = None
    # Skip the planner and use this intent (benchmark arms that isolate generation).
    intent: Intent | None = None


@dataclass
class Candidate:
    configs: dict[str, str]
    key: str
    report: VerificationReport


@dataclass
class RunResult:
    run_id: str
    run_dir: Path
    outcome: Outcome
    state: RunState
    repair_rounds: int
    usage: Usage
    intent: Intent | None = None
    fabric: FabricPlan | None = None
    checkset: CheckSet | None = None
    best: Candidate | None = None
    explanation: str = ""
    error: str | None = None
    history: list[float] = field(default_factory=list)  # best score after each round


class _Run:
    """One run's mutable state. ``RunMachine`` is stateless across runs; this is not."""

    def __init__(
        self,
        machine: RunMachine,
        run_id: str,
        run_dir: Path,
    ) -> None:
        self.m = machine
        self.run_id = run_id
        self.log = EventLog(run_dir, run_id)
        self.budget = BudgetTracker(machine.run_config.budget)
        rc = machine.run_config
        grounder = machine.grounder if rc.grounding.enabled else None
        docs = (machine.grounder, rc.docs) if rc.docs.enabled and machine.grounder else None
        self.steps = LlmSteps(
            machine.client, machine.router, self.log, self.budget, grounder, docs=docs
        )
        self.cache: dict[str, VerificationReport] = {}
        self.result = RunResult(
            run_id=run_id,
            run_dir=run_dir,
            outcome="failed",
            state=RunState.PLANNING,
            repair_rounds=0,
            usage=self.budget.usage,
        )

    def transition(self, to: RunState, reason: str = "") -> None:
        frm = self.log.state
        if to not in TRANSITIONS[frm]:
            raise InvalidTransitionError(f"{frm} -> {to} is not a valid transition")
        self.log.emit(StateChanged, from_state=frm, to_state=to, reason=reason)
        self.log.state = to
        self.result.state = to

    async def verify_candidate(
        self, round_: int, index: int, configs: dict[str, str], snapshot: Snapshot
    ) -> Candidate:
        assert self.result.checkset is not None  # noqa: S101 - set before any verification
        key = content_key(configs)
        self.log.emit(
            CandidateGenerated,
            round=round_,
            candidate=index,
            content_key=key,
            devices=sorted(configs),
        )
        cached = key in self.cache
        if cached:
            # Repair loops often regenerate an identical candidate; never pay twice for it.
            self.budget.record_cache_hit()
            report = self.cache[key]
        else:
            self.budget.reserve_verification()
            executed = False
            try:
                with metrics.VERIFY_DURATION.labels(self.m.verifier.name).time():
                    report = await self.m.verifier.verify(
                        snapshot.model_copy(update={"name": f"{self.run_id}-{key}"}),
                        configs,
                        self.result.checkset.checks,
                    )
                executed = report.backend_executed
                metrics.SANDBOX_EXECUTIONS.labels("ok" if executed else "gated").inc()
            except Exception:
                metrics.SANDBOX_EXECUTIONS.labels("error").inc()
                executed = True
                raise
            finally:
                self.budget.settle_verification(executed=executed)
            self.cache[key] = report
            for c in report.checks:
                metrics.CHECKS.labels(c.kind.value, str(c.passed).lower()).inc()
        self.log.emit(
            VerificationCompleted,
            round=round_,
            candidate=index,
            content_key=key,
            cached=cached,
            summary=report.summary(),
        )
        return Candidate(configs=configs, key=key, report=report)

    async def loop(self, req: RunRequest) -> tuple[Outcome, str | None]:
        """Plan, generate, verify, repair. Returns the outcome and a stop reason."""
        r = self.result
        if req.intent is not None:
            r.intent, source = req.intent, "provided"
        else:
            r.intent, source = await self.steps.plan(req.request, req.platform), "planner"
        self.log.emit(IntentPlanned, source=source, intent=r.intent.model_dump(mode="json"))
        r.fabric = allocate(r.intent)
        r.checkset = compile_checks(r.intent, r.fabric)
        self.log.emit(
            ChecksCompiled, count=len(r.checkset.checks), unverifiable=r.checkset.unverifiable
        )
        snapshot = Snapshot(name=self.run_id, fabric=r.fabric)

        self.transition(S.GENERATING)
        configs = await self.steps.generate(r.intent, r.fabric)
        self.transition(S.VERIFYING)
        r.best = await self.verify_candidate(0, 0, configs, snapshot)
        r.history.append(r.best.report.score)

        max_rounds = self.budget.budget.max_repair_rounds
        while not r.best.report.passed:
            if r.repair_rounds >= max_rounds:
                return "exhausted", f"{max_rounds} repair rounds used"
            r.repair_rounds += 1
            self.transition(S.REPAIRING, reason=f"round {r.repair_rounds}")
            best = r.best
            candidates = await asyncio.gather(*[
                self.steps.repair(
                    round_=r.repair_rounds,
                    intent=r.intent,
                    fabric=r.fabric,
                    configs=best.configs,
                    report=best.report,
                    checkset=r.checkset,
                )
                for _ in range(self.m.run_config.repair_candidates)
            ])  # fmt: skip
            self.transition(S.VERIFYING, reason=f"round {r.repair_rounds}")
            verified = await asyncio.gather(*[
                self.verify_candidate(r.repair_rounds, i, c, snapshot)
                for i, c in enumerate(candidates)
            ])  # fmt: skip
            # Keep the best candidate seen so far; a worse repair never replaces a better one.
            r.best = max([best, *verified], key=lambda c: c.report.score)
            r.history.append(r.best.report.score)
        return "passed", None

    async def explain(self, outcome: Outcome, stop_reason: str | None, *, use_model: bool) -> None:
        r = self.result
        report = r.best.report if r.best else None
        fallback = deterministic_explanation(
            outcome, report, r.checkset, r.repair_rounds, stop_reason
        )
        text, source = fallback, "deterministic"
        if r.intent is not None and use_model:
            try:
                await self.steps.find_docs(r.intent)
            except Exception as exc:  # links are a nicety, like the explanation
                log.warning("docs_lookup_failed", run_id=self.run_id, error=str(exc))
        if report is not None and use_model:
            try:
                text = await self.steps.explain(outcome, report, r.checkset, r.repair_rounds)
                source = "model"
            except BudgetExceededError:
                pass
            except Exception as exc:  # the explanation is a nicety; the report is the product
                log.warning("explainer_failed", run_id=self.run_id, error=str(exc))
        r.explanation = text
        self.log.emit(ExplanationReady, text=text, source=source)


class RunMachine:
    def __init__(
        self,
        client: ChatClient,
        router: ModelRouter,
        verifier: Verifier,
        run_config: RunConfig,
        runs_dir: Path,
        *,
        grounder: Grounder | None = None,
    ) -> None:
        self.client = client
        self.router = router
        self.verifier = verifier
        self.run_config = run_config
        self.runs_dir = runs_dir
        # Used only when run_config.grounding.enabled, so one grounder can serve every arm.
        self.grounder = grounder

    def new_run_dir(self, run_id: str | None = None) -> tuple[str, Path]:
        rid = run_id or new_run_id()
        return rid, self.runs_dir / rid

    async def run(self, req: RunRequest, run_id: str | None = None) -> RunResult:
        rid, run_dir = self.new_run_dir(run_id)
        run = _Run(self, rid, run_dir)
        r = run.result
        run.log.emit(
            RunStarted,
            request=req.request,
            platform=req.platform,
            budget=self.run_config.budget,
            verifier=self.verifier.name,
        )
        stop_reason: str | None = None
        budget_stop = False
        try:
            try:
                outcome, stop_reason = await run.loop(req)
            except BudgetExceededError as exc:
                metrics.BUDGET_EXHAUSTED.inc()
                run.log.emit(BudgetExhausted, limit=exc.limit, detail=exc.detail)
                outcome, stop_reason, budget_stop = "exhausted", f"budget {exc}", True
            final = S.PASSED if outcome == "passed" else S.EXHAUSTED
            run.transition(final, reason=stop_reason or "")
            run.transition(S.EXPLAINING)
            # Once the budget has stopped the run, explaining it must not spend more.
            use_model = self.run_config.explain_with_model and not budget_stop
            await run.explain(outcome, stop_reason, use_model=use_model)
            run.transition(S.DONE)
            r.outcome = outcome
        except Exception as exc:
            log.exception("run_failed", run_id=rid)
            run.log.emit(RunError, error_type=type(exc).__name__, message=str(exc))
            if run.log.state is not S.FAILED:
                run.transition(S.FAILED, reason=type(exc).__name__)
            r.outcome, r.error = "failed", f"{type(exc).__name__}: {exc}"

        metrics.RUNS.labels(r.outcome).inc()
        metrics.REPAIR_ROUNDS.observe(r.repair_rounds)
        run.log.emit(
            RunFinished,
            outcome=r.outcome,
            score=r.best.report.score if r.best else 0.0,
            repair_rounds=r.repair_rounds,
            usage=r.usage,
        )
        write_artifacts(r)
        return r


def write_artifacts(r: RunResult) -> None:
    """Configs and the report next to the event log, so a run dir is self-contained."""
    if r.best is not None:
        cfg_dir = r.run_dir / "configs"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        for device, text in sorted(r.best.configs.items()):
            (cfg_dir / f"{device}.cfg").write_text(text, encoding="utf-8")
        (r.run_dir / "report.json").write_text(
            r.best.report.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
    summary: Mapping[str, object] = {
        "run_id": r.run_id,
        "outcome": r.outcome,
        "repair_rounds": r.repair_rounds,
        "score_history": r.history,
        "usage": r.usage.model_dump(),
        "explanation": r.explanation,
        "error": r.error,
        "intent": r.intent.model_dump(mode="json") if r.intent else None,
        "unverifiable": r.checkset.unverifiable if r.checkset else [],
    }
    (r.run_dir / "result.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
