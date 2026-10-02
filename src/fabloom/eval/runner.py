"""The benchmark runner: every (arm, scenario) pair through the real run state machine.

Arms differ only in routing and run limits (``conf/bench.yaml``); every arm grades against the
scenario's ground-truth intent, so the planner never decides what a run is graded on. The
verifier is handed in by the composition root, as for any run (ADR-0001).

Layout of a bench directory::

    runs/bench/<bench_id>/
        bench.json        what was run: arms, models, prompts, scenarios, fingerprint
        results.json      one record per finished run, derived from the event logs
        runs/<arm>--<scenario>/events.jsonl, configs/, report.json, result.json

A finished run is never paid for twice: running into an existing directory reuses every run
that has a ``run_finished`` event and re-runs only the rest.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from itertools import zip_longest
from pathlib import Path

from fabloom.agent import RunMachine, RunRequest, read_events
from fabloom.agent.events import EVENTS_FILE
from fabloom.config import ArmSpec, BenchConfig, ModelsConfig, RunConfig
from fabloom.contracts import ModelRole, Scenario, Verifier
from fabloom.eval.results import (
    ArmMeta,
    BenchMeta,
    BenchResults,
    RunRecord,
    record_from_events,
)
from fabloom.llm import ChatClient, ModelRouter
from fabloom.llm.prompts import prompt_hash
from fabloom.provenance import content_key, git_sha

BENCH_FILE = "bench.json"
RESULTS_FILE = "results.json"
RUNS_SUBDIR = "runs"


class BenchMismatchError(RuntimeError):
    """A bench directory was produced by a different configuration than the one resuming it."""


def select_scenarios(
    scenarios: Sequence[Scenario],
    *,
    ids: Collection[str] = (),
    families: Collection[str] = (),
    limit: int | None = None,
) -> list[Scenario]:
    """Filter by ID and family glob, then cap at ``limit`` taking families in turn, so a
    10-scenario nightly still touches every family rather than the alphabetically first."""
    unknown = set(ids) - {s.id for s in scenarios}
    if unknown:
        raise ValueError(f"unknown scenario ids: {sorted(unknown)}")
    chosen = [
        s
        for s in scenarios
        if (not ids or s.id in ids)
        and (not families or any(fnmatchcase(s.family, f) for f in families))
    ]
    if limit is None or limit >= len(chosen):
        return chosen
    by_family: dict[str, list[Scenario]] = {}
    for s in sorted(chosen, key=lambda s: s.id):
        by_family.setdefault(s.family, []).append(s)
    interleaved = [
        s for row in zip_longest(*(by_family[f] for f in sorted(by_family))) for s in row if s
    ]
    return interleaved[:limit]


@dataclass(frozen=True)
class Arm:
    name: str
    spec: ArmSpec
    models: ModelsConfig
    run_config: RunConfig
    router: ModelRouter

    def meta(self) -> ArmMeta:
        specs = {role: self.models.for_role(role) for role in ModelRole}
        return ArmMeta(
            description=self.spec.description,
            models={role.value: s.id for role, s in specs.items()},
            temperatures={role.value: s.temperature for role, s in specs.items()},
            max_repair_rounds=self.run_config.budget.max_repair_rounds,
            repair_candidates=self.run_config.repair_candidates,
            max_usd=self.run_config.budget.max_usd,
        )

    def roles_used(self) -> set[ModelRole]:
        """Roles a run of this arm can call; the planner and explainer are never called."""
        if self.run_config.budget.max_repair_rounds == 0:
            return {ModelRole.GENERATOR}
        return {ModelRole.GENERATOR, ModelRole.REPAIRER, ModelRole.REPAIRER_ESCALATED}


def resolve_arms(
    bench: BenchConfig,
    models: ModelsConfig,
    base: RunConfig,
    names: Sequence[str] = (),
) -> list[Arm]:
    unknown = [n for n in names if n not in bench.arms]
    if unknown:
        raise ValueError(f"unknown arms {unknown}; conf/bench.yaml has {sorted(bench.arms)}")
    arms = []
    for name in names or list(bench.arms):
        spec = bench.arms[name]
        routed = spec.models(models)
        # The router validates the routing table, so a broken arm fails before any run starts.
        arms.append(Arm(name, spec, routed, spec.run_config(base), ModelRouter(routed)))
    return arms


def run_id_for(arm: str, scenario_id: str) -> str:
    return f"{arm}--{scenario_id}"


def worst_case_usd(arms: Sequence[Arm], scenarios: Sequence[Scenario]) -> float:
    """What the run budgets allow at most: the number to see before spending anything."""
    return sum(a.run_config.budget.max_usd for a in arms) * len(scenarios)


def build_meta(
    arms: Sequence[Arm],
    scenarios: Sequence[Scenario],
    verifier: Verifier,
    benchmark: str,
    now: datetime | None = None,
) -> BenchMeta:
    arm_meta = {a.name: a.meta() for a in arms}
    scenarios_key = content_key([s.model_dump(mode="json") for s in scenarios])
    ph = prompt_hash()
    fingerprint = content_key({
        "arms": {n: m.model_dump() for n, m in arm_meta.items()},
        "scenarios": scenarios_key,
        "verifier": verifier.name,
        "prompts": ph,
    })  # fmt: skip
    created = now or datetime.now(UTC)
    return BenchMeta(
        bench_id=f"{created:%Y%m%dT%H%M%SZ}-{fingerprint[:6]}",
        created=created,
        git_sha=git_sha(),
        benchmark=benchmark,
        scenarios={s.id: s.family for s in scenarios},
        scenarios_key=scenarios_key,
        verifier=verifier.name,
        prompt_hash=ph,
        prices_verified=all(
            a.models.for_role(role).price_verified for a in arms for role in a.roles_used()
        ),
        arms=arm_meta,
        fingerprint=fingerprint,
    )


def load_record(path: Path, *, arm: str, scenario_id: str, family: str) -> RunRecord | None:
    """The run's record, or None if it never finished: no log, no ``run_finished``, or a
    last line cut off by a killed process (which fails to parse)."""
    if not path.exists():
        return None
    try:
        return record_from_events(
            read_events(path), arm=arm, scenario_id=scenario_id, family=family
        )
    except ValueError:  # IncompleteRunError, or pydantic's ValidationError on a torn line
        return None


def collect(out_dir: Path) -> BenchResults:
    """Rebuild results from a bench directory's event logs. No model, no verifier."""
    meta = BenchMeta.model_validate_json((out_dir / BENCH_FILE).read_text(encoding="utf-8"))
    records: list[RunRecord] = []
    incomplete: list[str] = []
    for arm in meta.arms:
        for scenario_id, family in meta.scenarios.items():
            rid = run_id_for(arm, scenario_id)
            path = out_dir / RUNS_SUBDIR / rid / EVENTS_FILE
            rec = load_record(path, arm=arm, scenario_id=scenario_id, family=family)
            if rec is None:
                incomplete.append(rid)
            else:
                records.append(rec)
    return BenchResults(meta=meta, records=records, incomplete=incomplete)


OnRecord = Callable[[RunRecord, bool], None]  # (record, reused from an earlier run)


class BenchRunner:
    def __init__(
        self,
        *,
        client: ChatClient,
        verifier: Verifier,
        arms: Sequence[Arm],
        scenarios: Sequence[Scenario],
        out_dir: Path | None = None,
        bench_root: Path = Path("runs/bench"),
        benchmark: str = "v0.1",
        concurrency: int = 1,
        on_record: OnRecord | None = None,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        self.client = client
        self.verifier = verifier
        self.arms = list(arms)
        self.scenarios = list(scenarios)
        self.meta = build_meta(self.arms, self.scenarios, verifier, benchmark)
        self.out_dir = out_dir or bench_root / self.meta.bench_id
        self._sem = asyncio.Semaphore(concurrency)
        self._on_record = on_record

    def _prepare(self) -> None:
        bench_file = self.out_dir / BENCH_FILE
        if bench_file.exists():
            existing = BenchMeta.model_validate_json(bench_file.read_text(encoding="utf-8"))
            if existing.fingerprint != self.meta.fingerprint:
                raise BenchMismatchError(
                    f"{self.out_dir} holds runs from a different configuration "
                    f"(arms, models, prompts, scenarios or verifier changed); "
                    "use a new output directory"
                )
            self.meta = existing  # resuming: keep the original bench_id and timestamp
            return
        self.out_dir.mkdir(parents=True, exist_ok=True)
        bench_file.write_text(self.meta.model_dump_json(indent=2) + "\n", encoding="utf-8")

    async def _one(self, arm: Arm, scenario: Scenario) -> None:
        rid = run_id_for(arm.name, scenario.id)
        runs_dir = self.out_dir / RUNS_SUBDIR
        events = runs_dir / rid / EVENTS_FILE

        def record() -> RunRecord | None:
            return load_record(
                events, arm=arm.name, scenario_id=scenario.id, family=scenario.family
            )

        if (rec := record()) is not None:
            if self._on_record:
                self._on_record(rec, True)
            return
        if events.parent.exists():
            # A killed process left half a log; appending to it would corrupt the run.
            shutil.rmtree(events.parent)
        async with self._sem:
            machine = RunMachine(self.client, arm.router, self.verifier, arm.run_config, runs_dir)
            req = RunRequest(scenario.request, scenario.intent.platform, intent=scenario.intent)
            await machine.run(req, run_id=rid)
        if self._on_record and (rec := record()) is not None:
            self._on_record(rec, False)

    async def run(self) -> BenchResults:
        self._prepare()
        await asyncio.gather(*[self._one(a, s) for a in self.arms for s in self.scenarios])
        results = collect(self.out_dir)
        results.write(self.out_dir / RESULTS_FILE)
        return results
