"""Per-run records and per-arm summaries, derived only from run event logs.

The event log is the single source of truth (ADR-0004): a bench directory can be summarised,
gated and reported again at any time without a model call, and every number in a report
traces back to lines in some ``events.jsonl``.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Iterable
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from fabloom.contracts import ModelRole, RunEvent
from fabloom.contracts.run import (
    BudgetExhausted,
    LlmCall,
    RunError,
    RunFinished,
    RunStarted,
    VerificationCompleted,
)


class IncompleteRunError(ValueError):
    """The event log has no ``run_finished``: the process died mid-run."""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RunRecord(_Frozen):
    arm: str
    scenario_id: str
    family: str
    run_id: str
    outcome: Literal["passed", "exhausted", "failed"]
    first_pass: bool  # the first candidate passed every check, before any repair
    first_score: float
    final_score: float
    repair_rounds: int
    # A deterministic pre-lint rejected the first candidate before any backend saw it.
    first_prelint_rejected: bool
    # Unrecognised lines in the first candidate; None when no backend parsed it.
    first_unrecognized_lines: int | None
    failed_checks: list[str]  # of the best candidate
    cost_usd: float
    prompt_tokens: int
    completion_tokens: int
    llm_calls: int
    verifications: int
    cache_hits: int
    escalations: int  # calls routed to repairer_escalated
    wall_s: float
    budget_limit: str | None = None
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.outcome == "passed"


def record_from_events(
    events: Iterable[RunEvent], *, arm: str, scenario_id: str, family: str
) -> RunRecord:
    started: RunStarted | None = None
    finished: RunFinished | None = None
    verifications: list[VerificationCompleted] = []
    escalations = 0
    budget_limit: str | None = None
    error: str | None = None
    for e in events:
        if isinstance(e, RunStarted):
            started = e
        elif isinstance(e, VerificationCompleted):
            verifications.append(e)
        elif isinstance(e, LlmCall) and e.role is ModelRole.REPAIRER_ESCALATED:
            escalations += 1
        elif isinstance(e, BudgetExhausted):
            budget_limit = e.limit
        elif isinstance(e, RunError):
            error = f"{e.error_type}: {e.message}"
        elif isinstance(e, RunFinished):
            finished = e
    if started is None or finished is None:
        raise IncompleteRunError(f"{arm}/{scenario_id}: no run_finished event")

    # Parallel candidates log in completion order; replay them in candidate order so the best
    # candidate is the one the state machine kept (a tie never replaces the earlier best).
    verifications.sort(key=lambda v: (v.round, v.candidate))
    first = verifications[0].summary if verifications else None
    # Summaries logged before backend_executed existed all came from a backend.
    first_backend = bool(first and first.get("backend_executed", True))
    best = first
    for v in verifications[1:]:
        assert best is not None  # noqa: S101 - first is set whenever there is a later one
        if v.summary["score"] > best["score"]:
            best = v.summary
    usage = finished.usage
    return RunRecord(
        arm=arm,
        scenario_id=scenario_id,
        family=family,
        run_id=finished.run_id,
        outcome=finished.outcome,
        first_pass=bool(first and first["passed"]),
        first_score=float(first["score"]) if first else 0.0,
        final_score=finished.score,
        repair_rounds=finished.repair_rounds,
        first_prelint_rejected=bool(first) and not first_backend,
        first_unrecognized_lines=(
            int(first["unrecognized_lines"]) if first and first_backend else None
        ),
        failed_checks=list(best["failed"]) if best else [],
        cost_usd=usage.cost_usd,
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        llm_calls=usage.llm_calls,
        verifications=usage.verifications,
        cache_hits=usage.verification_cache_hits,
        escalations=escalations,
        wall_s=(finished.ts - started.ts).total_seconds(),
        budget_limit=budget_limit,
        error=error,
    )


def nearest_rank(values: Iterable[float], q: float) -> float | None:
    """Nearest-rank percentile: always an observed value, honest for a handful of runs."""
    ordered = sorted(values)
    if not ordered:
        return None
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """95% Wilson score interval for a pass rate. Unlike the normal approximation it stays
    inside [0, 1] and does not collapse to zero width at 0/n or n/n."""
    if n == 0:
        return None
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


class ArmSummary(_Frozen):
    """Metrics are None when there is nothing to compute them from, never a made-up 0."""

    arm: str
    n: int
    passed: int
    first_passed: int
    final_pass_rate: float | None
    first_pass_rate: float | None
    mean_final_score: float | None
    mean_repair_rounds: float | None
    # Share of runs whose first candidate had a line the parser did not recognise: the model
    # invented a command. Over runs whose first candidate reached a backend.
    unrecognized_line_rate: float | None
    # Share of runs whose first candidate the deterministic pre-lint rejected outright.
    prelint_reject_rate: float | None
    usd_per_scenario: float | None
    usd_total: float
    wall_p50_s: float | None
    wall_p95_s: float | None
    errors: int
    budget_stops: int
    escalated_runs: int

    def metrics(self) -> dict[str, float]:
        """The numeric metrics that exist, for tracking."""
        skip = {"arm"}
        return {
            k: float(v)
            for k, v in self.model_dump().items()
            if k not in skip and isinstance(v, int | float) and not isinstance(v, bool)
        }


METRIC_NAMES = frozenset(ArmSummary.model_fields) - {"arm"}


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def summarize(arm: str, records: Collection[RunRecord]) -> ArmSummary:
    n = len(records)
    passed = sum(r.passed for r in records)
    first = sum(r.first_pass for r in records)
    verified = [r for r in records if r.first_unrecognized_lines is not None]
    walls = [r.wall_s for r in records]
    return ArmSummary(
        arm=arm,
        n=n,
        passed=passed,
        first_passed=first,
        final_pass_rate=passed / n if n else None,
        first_pass_rate=first / n if n else None,
        mean_final_score=_mean([r.final_score for r in records]),
        mean_repair_rounds=_mean([float(r.repair_rounds) for r in records]),
        unrecognized_line_rate=(
            sum(bool(r.first_unrecognized_lines) for r in verified) / len(verified)
            if verified
            else None
        ),
        prelint_reject_rate=sum(r.first_prelint_rejected for r in records) / n if n else None,
        usd_per_scenario=_mean([r.cost_usd for r in records]),
        usd_total=sum(r.cost_usd for r in records),
        wall_p50_s=nearest_rank(walls, 0.50),
        wall_p95_s=nearest_rank(walls, 0.95),
        errors=sum(r.error is not None for r in records),
        budget_stops=sum(r.budget_limit is not None for r in records),
        escalated_runs=sum(r.escalations > 0 for r in records),
    )


class ArmMeta(_Frozen):
    description: str
    models: dict[str, str]  # role -> model ID
    temperatures: dict[str, float]  # role -> temperature
    max_repair_rounds: int
    repair_candidates: int
    max_usd: float


class BenchMeta(_Frozen):
    bench_id: str
    created: datetime
    git_sha: str
    benchmark: str
    scenarios: dict[str, str]  # id -> family, in run order
    scenarios_key: str  # content hash of the scenario definitions
    verifier: str
    prompt_hash: str
    prices_verified: bool  # every price used comes from the probe, not a placeholder
    arms: dict[str, ArmMeta]
    # Hash of everything that changes a result. A bench directory is only ever resumed by a
    # run with the same fingerprint, so one report never mixes two configurations.
    fingerprint: str


class BenchResults(_Frozen):
    meta: BenchMeta
    records: list[RunRecord]
    incomplete: list[str] = Field(default_factory=list)  # run IDs with no run_finished

    @property
    def arm_names(self) -> list[str]:
        return list(self.meta.arms)

    @property
    def families(self) -> list[str]:
        return sorted(set(self.meta.scenarios.values()))

    def records_for(self, arm: str, scenario_ids: Collection[str] | None = None) -> list[RunRecord]:
        return [
            r
            for r in self.records
            if r.arm == arm and (scenario_ids is None or r.scenario_id in scenario_ids)
        ]

    def scenario_ids(self, arm: str) -> set[str]:
        return {r.scenario_id for r in self.records_for(arm)}

    def summary(self, arm: str, scenario_ids: Collection[str] | None = None) -> ArmSummary:
        return summarize(arm, self.records_for(arm, scenario_ids))

    def summaries(self) -> dict[str, ArmSummary]:
        return {arm: self.summary(arm) for arm in self.arm_names}

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2) + "\n", encoding="utf-8")

    @classmethod
    def read(cls, path: Path) -> BenchResults:
        return cls.model_validate_json(path.read_text(encoding="utf-8"))


def metric_value(summary: ArmSummary, metric: str) -> Any:
    if metric not in METRIC_NAMES:
        raise ValueError(f"unknown metric {metric!r}; one of {sorted(METRIC_NAMES)}")
    return getattr(summary, metric)
