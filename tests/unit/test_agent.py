"""The state machine and repair loop, fully offline: FakeChatClient + FakeVerifier."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from fabloom.agent import (
    TRANSITIONS,
    BudgetExceededError,
    BudgetTracker,
    EventLog,
    InvalidTransitionError,
    RunMachine,
    RunRequest,
    read_events,
    tail_events,
)
from fabloom.agent.events import EVENTS_FILE, new_run_id
from fabloom.agent.explain import deterministic_explanation
from fabloom.agent.state_machine import _Run
from fabloom.config import ModelSpec, RunConfig
from fabloom.contracts import (
    CheckSet,
    FabricPlan,
    Intent,
    ModelRole,
    Platform,
    RunBudget,
    RunState,
)
from fabloom.contracts.run import RunFinished, StateChanged
from fabloom.llm import LlmOutputError, ModelRouter
from tests.fakes import FakeChatClient, FakeVerifier
from tests.fakes.render import as_device_blocks, render_good_configs

pytestmark = pytest.mark.unit

S = RunState


def _machine(
    script: list[Any],
    verifier: FakeVerifier,
    router: ModelRouter,
    runs_dir: Path,
    run_config: RunConfig,
) -> tuple[RunMachine, FakeChatClient]:
    client = FakeChatClient(script=script)
    return RunMachine(client, router, verifier, run_config, runs_dir), client


def _states(run_dir: Path) -> list[RunState]:
    return [e.to_state for e in read_events(run_dir / EVENTS_FILE) if isinstance(e, StateChanged)]


def _blocks(intent: Intent, fabric: FabricPlan) -> str:
    return as_device_blocks(render_good_configs(intent, fabric), fabric.platform)


async def test_passes_first_time(
    frr_intent: Intent,
    frr_fabric: FabricPlan,
    router: ModelRouter,
    runs_dir: Path,
    run_config: RunConfig,
) -> None:
    script = [frr_intent.model_dump(mode="json"), _blocks(frr_intent, frr_fabric), "All good."]
    machine, client = _machine(script, FakeVerifier([0]), router, runs_dir, run_config)
    result = await machine.run(RunRequest("2 spines 2 leaves"))
    assert result.outcome == "passed"
    assert result.repair_rounds == 0
    assert result.explanation == "All good."
    assert _states(result.run_dir) == [S.GENERATING, S.VERIFYING, S.PASSED, S.EXPLAINING, S.DONE]
    assert [m for m, _ in client.calls] == [
        router.spec(r).id for r in (ModelRole.PLANNER, ModelRole.GENERATOR, ModelRole.EXPLAINER)
    ]
    assert (result.run_dir / "configs" / "leaf1.cfg").read_text().startswith("frr version")
    summary = json.loads((result.run_dir / "result.json").read_text())
    assert summary["outcome"] == "passed"
    assert summary["usage"]["llm_calls"] == 3


async def test_repairs_until_pass_and_keeps_best(
    frr_intent: Intent,
    frr_fabric: FabricPlan,
    router: ModelRouter,
    runs_dir: Path,
    run_config: RunConfig,
) -> None:
    good = render_good_configs(frr_intent, frr_fabric)
    broken = {**good, "leaf1": good["leaf1"].replace("65000", "65999")}
    script = [
        as_device_blocks(broken, Platform.FRR),  # generator
        f'<device name="leaf1">{good["leaf1"]}</device>',  # repair round 1: only leaf1 changes
        "Fixed.",
    ]
    verifier = FakeVerifier([5, 0])
    machine, client = _machine(script, verifier, router, runs_dir, run_config)
    result = await machine.run(RunRequest("x" * 10, intent=frr_intent))
    assert result.outcome == "passed"
    assert result.repair_rounds == 1
    assert result.history[0] < result.history[1] == 1.0
    # partial repair output is merged: the verifier saw every device
    assert set(verifier.calls[1]) == set(frr_fabric.device_names)
    assert verifier.calls[1]["leaf1"] == good["leaf1"]
    assert _states(result.run_dir) == [
        S.GENERATING, S.VERIFYING, S.REPAIRING, S.VERIFYING, S.PASSED, S.EXPLAINING, S.DONE,
    ]  # fmt: skip
    assert "Failed checks" in client.calls[1][1][1]["content"]


async def test_exhausts_after_max_rounds_with_deterministic_fallback(
    frr_intent: Intent,
    frr_fabric: FabricPlan,
    router: ModelRouter,
    runs_dir: Path,
) -> None:
    rc = RunConfig(budget=RunBudget(max_repair_rounds=2, max_usd=1.0))
    good = _blocks(frr_intent, frr_fabric)
    script = [
        good,
        good.replace("65101", "65111"),
        good.replace("65101", "65112"),
        RuntimeError("explainer down"),
    ]
    machine, client = _machine(script, FakeVerifier([3]), router, runs_dir, rc)
    result = await machine.run(RunRequest("x" * 10, intent=frr_intent))
    assert result.outcome == "exhausted"
    assert result.repair_rounds == 2
    assert "2 repair rounds used" in result.explanation
    assert "3 still fail" in result.explanation
    # round 2 had a critical failure -> escalated repairer
    assert client.calls[2][0] == router.spec(ModelRole.REPAIRER_ESCALATED).id


async def test_identical_candidates_are_not_reverified(
    frr_intent: Intent,
    frr_fabric: FabricPlan,
    router: ModelRouter,
    runs_dir: Path,
) -> None:
    rc = RunConfig(budget=RunBudget(max_repair_rounds=2, max_usd=1.0), repair_candidates=2)
    good = _blocks(frr_intent, frr_fabric)
    script = [good, good, good, good, good, "Done."]
    verifier = FakeVerifier([2])
    machine, _ = _machine(script, verifier, router, runs_dir, rc)
    result = await machine.run(RunRequest("x" * 10, intent=frr_intent))
    assert len(verifier.calls) == 1
    assert result.usage.verifications == 1
    assert result.usage.verification_cache_hits == 4


async def test_budget_stop_is_exhausted_not_failed(
    frr_intent: Intent,
    frr_fabric: FabricPlan,
    router: ModelRouter,
    runs_dir: Path,
) -> None:
    rc = RunConfig(budget=RunBudget(max_repair_rounds=5, max_usd=1.0, max_verifications=2))
    good = _blocks(frr_intent, frr_fabric)
    script = [good, good.replace("65101", "65121"), good.replace("65101", "65122")]
    machine, client = _machine(script, FakeVerifier([1]), router, runs_dir, rc)
    result = await machine.run(RunRequest("x" * 10, intent=frr_intent))
    assert result.outcome == "exhausted"
    assert "max_verifications" in result.explanation
    assert len(client.calls) == 3  # no explainer call once the budget stopped the run
    events = list(read_events(result.run_dir / EVENTS_FILE))
    assert any(e.type == "budget_exhausted" for e in events)


async def test_usd_budget_blocks_the_call_before_it_happens(
    frr_intent: Intent, router: ModelRouter, runs_dir: Path
) -> None:
    rc = RunConfig(budget=RunBudget(max_usd=0.000001))
    machine, client = _machine([], FakeVerifier([0]), router, runs_dir, rc)
    result = await machine.run(RunRequest("x" * 10, intent=frr_intent))
    assert result.outcome == "exhausted"
    assert client.calls == []
    assert result.best is None
    assert "No configuration was verified" in result.explanation


async def test_planner_retries_once_on_invalid_output(
    frr_intent: Intent,
    frr_fabric: FabricPlan,
    router: ModelRouter,
    runs_dir: Path,
    run_config: RunConfig,
) -> None:
    script = [
        LlmOutputError("Intent did not validate"),
        frr_intent.model_dump(mode="json"),
        _blocks(frr_intent, frr_fabric),
        "ok",
    ]
    machine, client = _machine(script, FakeVerifier([0]), router, runs_dir, run_config)
    result = await machine.run(RunRequest("x" * 10, platform=Platform.FRR))
    assert result.outcome == "passed"
    assert "previous answer was invalid" in client.calls[1][1][-1]["content"]


async def test_platform_override_wins(
    frr_intent: Intent, router: ModelRouter, runs_dir: Path, run_config: RunConfig
) -> None:
    eos = frr_intent.model_copy(update={"platform": Platform.ARISTA_EOS})
    script = [eos.model_dump(mode="json"), RuntimeError("stop here")]
    machine, _ = _machine(script, FakeVerifier([0]), router, runs_dir, run_config)
    result = await machine.run(RunRequest("x" * 10, platform=Platform.FRR))
    assert result.intent is not None
    assert result.intent.platform is Platform.FRR


async def test_unexpected_error_fails_the_run(
    frr_intent: Intent, router: ModelRouter, runs_dir: Path, run_config: RunConfig
) -> None:
    machine, _ = _machine([RuntimeError("boom")], FakeVerifier([0]), router, runs_dir, run_config)
    result = await machine.run(RunRequest("x" * 10, intent=frr_intent))
    assert result.outcome == "failed"
    assert result.error == "RuntimeError: boom"
    assert result.state is S.FAILED
    last = list(read_events(result.run_dir / EVENTS_FILE))[-1]
    assert isinstance(last, RunFinished)
    assert last.outcome == "failed"


def test_transition_table_is_closed() -> None:
    assert set(TRANSITIONS) == set(RunState)
    for targets in TRANSITIONS.values():
        assert targets <= set(RunState)
    assert TRANSITIONS[S.DONE] == TRANSITIONS[S.FAILED] == frozenset()


def test_invalid_transition_raises(
    router: ModelRouter, runs_dir: Path, run_config: RunConfig
) -> None:
    machine, _ = _machine([], FakeVerifier([0]), router, runs_dir, run_config)
    run = _Run(machine, "r1", runs_dir / "r1")
    with pytest.raises(InvalidTransitionError, match="PLANNING -> DONE"):
        run.transition(S.DONE)


# --- budget ------------------------------------------------------------------------------

SPEC = ModelSpec(id="m", usd_per_m_input=1.0, usd_per_m_output=1.0, max_output_tokens=1000)


def test_budget_tokens_and_wall_clock() -> None:
    now = [0.0]
    b = BudgetTracker(RunBudget(max_tokens=2000, max_wall_clock_s=10), clock=lambda: now[0])
    msgs = [{"role": "user", "content": "x" * 300}]
    b.check_llm(SPEC, msgs)
    b.record_llm(500, 400, 0.001)
    with pytest.raises(BudgetExceededError, match="max_tokens"):
        b.check_llm(SPEC, msgs)
    now[0] = 11
    with pytest.raises(BudgetExceededError, match="max_wall_clock_s"):
        b.reserve_verification()


def test_budget_reservations_block_parallel_overshoot() -> None:
    b = BudgetTracker(RunBudget(max_verifications=2))
    b.reserve_verification()
    b.reserve_verification()
    with pytest.raises(BudgetExceededError, match="max_verifications"):
        b.reserve_verification()
    b.settle_verification(executed=False)  # a gated candidate gives its slot back
    b.reserve_verification()
    b.settle_verification(executed=True)
    b.settle_verification(executed=True)
    assert b.usage.verifications == 2


# --- events ------------------------------------------------------------------------------


def test_run_ids_sort_by_time() -> None:
    a = new_run_id(datetime(2026, 10, 1, tzinfo=UTC))
    b = new_run_id(datetime(2026, 10, 2, tzinfo=UTC))
    assert a < b


async def test_tail_follows_a_live_writer(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "r", "r")

    async def writer() -> None:
        await asyncio.sleep(0.05)
        log.emit(StateChanged, from_state=S.PLANNING, to_state=S.GENERATING)
        await asyncio.sleep(0.05)
        log.emit(
            RunFinished,
            outcome="failed",
            score=0.0,
            repair_rounds=0,
            usage=BudgetTracker(RunBudget()).usage,
        )

    task = asyncio.create_task(writer())
    seen = [e.type async for e in tail_events(log.path, poll_s=0.01, timeout_s=5)]
    await task
    assert seen == ["state_changed", "run_finished"]


async def test_tail_times_out_without_a_writer(tmp_path: Path) -> None:
    seen = [e async for e in tail_events(tmp_path / "none.jsonl", poll_s=0.01, timeout_s=0.05)]
    assert seen == []


def test_deterministic_explanation_lists_unverifiable(frr_checkset: CheckSet) -> None:
    cs = frr_checkset.model_copy(update={"unverifiable": ["deny tcp/22 on frr"]})
    text = deterministic_explanation("passed", None, cs, 0, "budget")
    assert text == "No configuration was verified: budget."
