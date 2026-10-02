"""Benchmark records, summaries, gates, report and arm config: pure, no runs."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from fabloom.agent import BudgetTracker, EventLog, read_events
from fabloom.config import (
    ArmSpec,
    BenchConfig,
    GatesSpec,
    GroundingGateSpec,
    ModelsConfig,
    RegressionGateSpec,
    RunConfig,
    bench_config,
    run_config,
)
from fabloom.contracts import ModelRole, Platform, RunBudget, RunState
from fabloom.contracts.run import LlmCall, RunFinished, RunStarted, VerificationCompleted
from fabloom.eval import (
    BenchResults,
    NullTracker,
    RunRecord,
    all_passed,
    evaluate,
    render,
    resolve_arms,
    select_scenarios,
    summarize,
    worst_case_usd,
)
from fabloom.eval.gates import Gate
from fabloom.eval.results import (
    ArmMeta,
    BenchMeta,
    IncompleteRunError,
    metric_value,
    nearest_rank,
    record_from_events,
    wilson_interval,
)
from fabloom.eval.tracking import arm_params, log_flat_params
from fabloom.scenarios import load_scenarios
from tests.conftest import CONF_DIR

pytestmark = pytest.mark.unit


# --- helpers ------------------------------------------------------------------------------


def rec(
    arm: str,
    sid: str,
    *,
    passed: bool = True,
    first: bool | None = None,
    family: str = "fam-a",
    cost: float = 0.01,
    unrec: int | None = 0,
    wall: float = 10.0,
    rounds: int = 0,
    error: str | None = None,
    budget: str | None = None,
) -> RunRecord:
    return RunRecord(
        arm=arm,
        scenario_id=sid,
        family=family,
        run_id=f"{arm}--{sid}",
        outcome="passed" if passed else ("failed" if error else "exhausted"),
        first_pass=passed if first is None else first,
        first_score=1.0 if passed else 0.5,
        final_score=1.0 if passed else 0.5,
        repair_rounds=rounds,
        first_prelint_rejected=False,
        first_unrecognized_lines=unrec,
        failed_checks=[] if passed else ["parse_clean:leaf1"],
        cost_usd=cost,
        prompt_tokens=100,
        completion_tokens=50,
        llm_calls=1 + rounds,
        verifications=1 + rounds,
        cache_hits=0,
        escalations=0,
        wall_s=wall,
        budget_limit=budget,
        error=error,
    )


def arm_meta(desc: str = "test arm") -> ArmMeta:
    return ArmMeta(
        description=desc,
        models={"generator": "m-gen"},
        temperatures={"generator": 0.2},
        max_repair_rounds=3,
        repair_candidates=1,
        max_usd=0.25,
    )


def bench(
    records: list[RunRecord],
    *,
    bench_id: str = "b1",
    prices_verified: bool = False,
    incomplete: list[str] | None = None,
) -> BenchResults:
    arms = list(dict.fromkeys(r.arm for r in records))
    scenarios = {r.scenario_id: r.family for r in records}
    meta = BenchMeta(
        bench_id=bench_id,
        created=datetime(2026, 10, 2, 12, 0, tzinfo=UTC),
        git_sha="abc1234",
        benchmark="v0.1",
        scenarios=scenarios,
        scenarios_key="k",
        verifier="fake",
        prompt_hash="p",
        prices_verified=prices_verified,
        arms={a: arm_meta() for a in arms},
        fingerprint="f",
    )
    return BenchResults(meta=meta, records=records, incomplete=incomplete or [])


def _summary(
    *, passed: bool = False, score: float = 0.5, unrec: int = 0, failed: list[str] | None = None
) -> dict[str, Any]:
    return {
        "level": "L2",
        "passed": passed,
        "score": score,
        "total": 4,
        "failed": failed or [],
        "unrecognized_lines": unrec,
        "verifier": "batfish",
        "backend_executed": True,
        "duration_ms": 5,
    }


def _log(tmp_path: Path) -> EventLog:
    log = EventLog(tmp_path / "r", "r")
    log.emit(RunStarted, request="x", platform=Platform.FRR, budget=RunBudget(), verifier="v")
    return log


def _finish(log: EventLog, *, outcome: str = "passed", score: float = 1.0, rounds: int = 0) -> None:
    usage = BudgetTracker(RunBudget()).usage
    usage.cost_usd = 0.02
    log.emit(RunFinished, outcome=outcome, score=score, repair_rounds=rounds, usage=usage)


# --- records from event logs ---------------------------------------------------------------


def test_record_replays_candidates_in_order_and_counts_escalations(tmp_path: Path) -> None:
    log = _log(tmp_path)
    log.emit(VerificationCompleted, round=0, candidate=0, content_key="a", cached=False,
             summary=_summary(score=0.5, unrec=2, failed=["parse_clean:leaf1"]))  # fmt: skip
    log.emit(LlmCall, role=ModelRole.REPAIRER_ESCALATED, model="u", prompt_tokens=1,
             completion_tokens=1, cost_usd=0.01, duration_ms=1, redactions=0)  # fmt: skip
    # Parallel candidates finish out of order; the tie must go to candidate 0, as in the run.
    log.emit(VerificationCompleted, round=1, candidate=1, content_key="c", cached=False,
             summary=_summary(score=0.9, failed=["route_present:x"]))  # fmt: skip
    log.emit(VerificationCompleted, round=1, candidate=0, content_key="b", cached=False,
             summary=_summary(score=0.9, failed=["bgp_established:y"]))  # fmt: skip
    _finish(log, outcome="exhausted", score=0.9, rounds=1)

    r = record_from_events(read_events(log.path), arm="a", scenario_id="s-0001", family="f")
    assert r.outcome == "exhausted"
    assert not r.passed
    assert not r.first_pass
    assert r.first_score == 0.5
    assert r.final_score == 0.9
    assert r.first_unrecognized_lines == 2
    assert not r.first_prelint_rejected
    assert r.failed_checks == ["bgp_established:y"]
    assert r.escalations == 1
    assert r.cost_usd == 0.02
    assert r.wall_s >= 0


def test_record_of_a_prelint_rejected_first_candidate(tmp_path: Path) -> None:
    log = _log(tmp_path)
    s = _summary(score=0.0) | {"backend_executed": False, "verifier": "prelint"}
    log.emit(VerificationCompleted, round=0, candidate=0, content_key="a", cached=False, summary=s)
    _finish(log)
    r = record_from_events(read_events(log.path), arm="a", scenario_id="s-0001", family="f")
    assert r.first_prelint_rejected
    assert r.first_unrecognized_lines is None  # no parser saw it


def test_record_reads_logs_written_before_backend_executed_existed(tmp_path: Path) -> None:
    log = _log(tmp_path)
    s = {k: v for k, v in _summary(passed=True, score=1.0).items() if k != "backend_executed"}
    log.emit(VerificationCompleted, round=0, candidate=0, content_key="a", cached=False, summary=s)
    _finish(log)
    r = record_from_events(read_events(log.path), arm="a", scenario_id="s-0001", family="f")
    assert r.first_pass
    assert r.first_unrecognized_lines == 0


def test_record_without_any_verification(tmp_path: Path) -> None:
    log = _log(tmp_path)
    _finish(log, outcome="failed", score=0.0)
    r = record_from_events(read_events(log.path), arm="a", scenario_id="s-0001", family="f")
    assert r.first_unrecognized_lines is None
    assert r.failed_checks == []
    assert r.first_score == 0.0


def test_record_without_run_finished_is_incomplete(tmp_path: Path) -> None:
    log = _log(tmp_path)
    log.state = RunState.GENERATING
    with pytest.raises(IncompleteRunError):
        record_from_events(read_events(log.path), arm="a", scenario_id="s-0001", family="f")


# --- summaries -------------------------------------------------------------------------------


def test_summarize_rates_and_denominators() -> None:
    records = [
        rec("a", "s-0001", wall=1),
        rec("a", "s-0002", passed=False, unrec=3, wall=2, rounds=3, budget="max_usd"),
        rec("a", "s-0003", first=False, rounds=1, wall=3),
        rec("a", "s-0004", passed=False, unrec=None, wall=40, error="LlmError: down"),
    ]
    s = summarize("a", records)
    assert (s.n, s.passed, s.first_passed) == (4, 2, 1)
    assert s.final_pass_rate == 0.5
    assert s.first_pass_rate == 0.25
    assert s.mean_repair_rounds == 1.0
    # s-0004 never reached a parser: it is out of the denominator, not counted as clean.
    assert s.unrecognized_line_rate == pytest.approx(1 / 3)
    assert s.usd_total == pytest.approx(0.04)
    assert (s.wall_p50_s, s.wall_p95_s) == (2, 40)
    assert (s.errors, s.budget_stops) == (1, 1)
    assert "arm" not in s.metrics()
    assert s.metrics()["final_pass_rate"] == 0.5


def test_summarize_nothing_gives_missing_metrics_not_zeros() -> None:
    s = summarize("a", [])
    assert s.n == 0
    assert s.final_pass_rate is None
    assert s.unrecognized_line_rate is None
    assert s.wall_p95_s is None
    assert "final_pass_rate" not in s.metrics()


def test_percentile_and_interval() -> None:
    assert nearest_rank([], 0.5) is None
    assert nearest_rank([5, 1, 3], 0.5) == 3
    assert nearest_rank(range(1, 21), 0.95) == 19
    assert wilson_interval(0, 0) is None
    lo, hi = wilson_interval(0, 10) or (1, 1)
    assert lo == 0.0
    assert 0.27 < hi < 0.29
    lo, hi = wilson_interval(10, 10) or (0, 0)
    assert 0.71 < lo < 0.73
    assert hi == 1.0


def test_metric_value_rejects_unknown_names() -> None:
    s = summarize("a", [rec("a", "s-0001")])
    assert metric_value(s, "final_pass_rate") == 1.0
    with pytest.raises(ValueError, match="unknown metric"):
        metric_value(s, "pass_rate")


# --- gates -------------------------------------------------------------------------------------


SPEC = GatesSpec(
    regression=RegressionGateSpec(metrics=["final_pass_rate"], max_drop=0.10),
    grounding=GroundingGateSpec(arm="g", control="c", min_gain=0.05, max_cost_ratio=1.5),
)


def _status(gates: list[Gate]) -> dict[str, str]:
    return {g.label: g.status for g in gates}


def test_no_baseline_skips_and_does_not_pass() -> None:
    gates = evaluate(bench([rec("a", "s-0001")]), None, SPEC)
    assert {g.status for g in gates} == {"skipped"}
    assert gates[0].detail == "no frozen baseline"
    assert not all_passed(gates)


def test_regression_compares_only_shared_scenarios() -> None:
    champion = bench(
        [rec("a", f"s-000{i}") for i in range(1, 5)] + [rec("a", "s-0009", passed=False)]
    )
    # Nightly subset: 2 scenarios, one of which now fails. s-0009 must not dilute the drop.
    now = bench([rec("a", "s-0001"), rec("a", "s-0002", passed=False)], bench_id="b2")
    [gate, *_] = evaluate(now, champion, SPEC)
    assert gate.status == "failed"
    assert gate.value == pytest.approx(-0.5)
    assert "on 2 shared scenarios" in gate.detail
    assert not all_passed([gate])


def test_regression_tolerates_exactly_max_drop() -> None:
    ten = [rec("a", f"s-{i:04d}") for i in range(10)]
    worse = [*ten[:9], rec("a", "s-0009", passed=False)]
    [gate, *_] = evaluate(bench(worse, bench_id="b2"), bench(ten), SPEC)
    assert gate.status == "passed"
    assert gate.value == pytest.approx(-0.1)


def test_regression_skips_arms_the_baseline_lacks() -> None:
    gates = evaluate(bench([rec("new", "s-0001")]), bench([rec("old", "s-0001")]), SPEC)
    assert gates[0].status == "skipped"
    assert "not in baseline" in gates[0].detail


def test_regression_metric_must_be_higher_is_better() -> None:
    spec = GatesSpec(regression=RegressionGateSpec(metrics=["usd_per_scenario"]))
    with pytest.raises(ValueError, match="usd_per_scenario"):
        evaluate(bench([rec("a", "s-0001")]), None, spec)


def test_grounding_ships_only_with_lift_at_bounded_cost() -> None:
    control = [rec("c", "s-0001"), rec("c", "s-0002", passed=False)]
    helps = [rec("g", "s-0001", cost=0.012), rec("g", "s-0002", cost=0.012)]
    gates = evaluate(bench(control + helps), None, SPEC)
    lift, cost = gates[-2:]
    assert lift.status == cost.status == "passed"
    assert lift.value == pytest.approx(0.5)
    assert cost.value == pytest.approx(1.2)

    pricey = [rec("g", "s-0001", cost=0.05), rec("g", "s-0002", cost=0.05)]
    lift, cost = evaluate(bench(control + pricey), None, SPEC)[-2:]
    assert lift.status == "passed"
    assert cost.status == "failed"


def test_grounding_skipped_until_its_arm_runs() -> None:
    gates = evaluate(bench([rec("c", "s-0001")]), None, SPEC)
    assert [g.status for g in gates[-2:]] == ["skipped", "skipped"]
    assert "arm not run: g" in gates[-1].detail


def test_grounding_cost_gate_skips_without_spend() -> None:
    records = [rec("c", "s-0001", cost=0.0), rec("g", "s-0001", cost=0.0)]
    lift, cost = evaluate(bench(records), None, SPEC)[-2:]
    assert lift.status == "failed"  # no lift at all
    assert cost.status == "skipped"


def test_all_passed_needs_one_evaluated_gate() -> None:
    ok = Gate("x", True, 0.0, 0.0, ">=", "")
    skipped = Gate("y", None, None, 0.0, ">=", "")
    bad = Gate("z", False, -1.0, 0.0, ">=", "")
    assert all_passed([ok, skipped])
    assert not all_passed([skipped])
    assert not all_passed([ok, bad])


# --- report ----------------------------------------------------------------------------------


def test_report_says_how_it_was_measured_and_lists_failures() -> None:
    records = [
        rec("raw", "s-0001", family="fam-a"),
        rec("raw", "s-0002", family="fam-b", passed=False, unrec=2),
        rec("loop", "s-0001", family="fam-a"),
        rec("loop", "s-0002", family="fam-b", passed=False, error="LlmError: timeout"),
    ]
    results = bench(records, incomplete=["loop--s-0003"])
    text = render(results, evaluate(results, None, SPEC), source="runs/bench/b1")
    assert text.startswith("# Fabloom benchmark v0.1")
    assert "do not edit by hand" in text
    assert "2 scenarios x 2 arms" in text
    assert "commit `abc1234`" in text
    assert "from `runs/bench/b1`" in text
    assert "Costs are estimates" in text
    assert "`loop--s-0003`" in text
    assert "| `raw` | 50% | 1/2 (50%, 9% to 91%) |" in text
    assert "| `fam-b` | 0/1 | 0/1 |" in text
    assert (
        "- `s-0002`: exhausted, score 0.500 after 0 repair round(s); failing: `parse_clean:leaf1`"
        in text
    )
    assert "error: LlmError: timeout" in text
    assert "SKIPPED" in text
    assert "Total spend for this bench: $0.0400." in text


def test_report_without_failures_or_caveats() -> None:
    results = bench([rec("a", "s-0001")], prices_verified=True)
    text = render(results, [])
    assert "Costs are estimates" not in text
    assert "Incomplete" not in text
    assert (
        "## Failures\n\nEvery run that did not pass, with its best candidate's failures.\n\nNone."
        in text
    )


def test_report_lists_long_failures_and_budget_stops() -> None:
    r = rec("a", "s-0001", passed=False, budget="max_usd").model_copy(
        update={"failed_checks": [f"c{i}" for i in range(8)]}
    )
    text = render(bench([r]), [])
    assert "budget stop (max_usd)" in text
    assert "`c4` and 3 more" in text


# --- arms, selection, tracking helpers ---------------------------------------------------------


def test_every_configured_arm_resolves(models: ModelsConfig) -> None:
    cfg = bench_config(CONF_DIR)
    arms = resolve_arms(cfg, models, run_config(CONF_DIR))
    assert [a.name for a in arms] == list(cfg.arms)
    by_name = {a.name: a for a in arms}
    nano = by_name["nano-raw"]
    assert nano.router.spec(ModelRole.GENERATOR).id == models.models["nano"].id
    assert nano.run_config.budget.max_repair_rounds == 0
    assert nano.roles_used() == {ModelRole.GENERATOR}
    assert not nano.run_config.explain_with_model
    loop = by_name["super-repair"]
    assert loop.router.spec(ModelRole.REPAIRER_ESCALATED).id == models.models["super"].id
    assert loop.run_config.budget.max_repair_rounds == run_config(CONF_DIR).budget.max_repair_rounds
    assert ModelRole.REPAIRER_ESCALATED in loop.roles_used()
    assert set(cfg.nightly.arms) <= set(cfg.arms)
    assert worst_case_usd(arms, load_scenarios()[:2]) == pytest.approx(
        2 * sum(a.run_config.budget.max_usd for a in arms)
    )


def test_unknown_arms_and_models_are_refused(models: ModelsConfig) -> None:
    cfg = bench_config(CONF_DIR)
    with pytest.raises(ValueError, match="unknown arms"):
        resolve_arms(cfg, models, RunConfig(), ["nope"])
    bad = BenchConfig(arms={"x": ArmSpec(description="d", roles={ModelRole.GENERATOR: "gpt"})})
    with pytest.raises(ValueError, match=r"not in conf/models\.yaml"):
        resolve_arms(bad, models, RunConfig())


def test_selection_takes_families_in_turn() -> None:
    scenarios = load_scenarios()
    families = {s.family for s in scenarios}
    picked = select_scenarios(scenarios, limit=len(families))
    assert {s.family for s in picked} == families
    assert select_scenarios(scenarios, limit=1000) == scenarios
    eos = select_scenarios(scenarios, families=["*-eos"])
    assert eos
    assert {s.intent.platform for s in eos} == {Platform.ARISTA_EOS}
    one = select_scenarios(scenarios, ids=["leaf-spine-ebgp-0002"])
    assert [s.id for s in one] == ["leaf-spine-ebgp-0002"]
    with pytest.raises(ValueError, match="unknown scenario ids"):
        select_scenarios(scenarios, ids=["nope-0001"])


def test_params_are_flat_strings() -> None:
    assert log_flat_params({"a": {"b": 1, "c": {"d": True}}, "e": "x"}) == {
        "a.b": "1",
        "a.c.d": "True",
        "e": "x",
    }
    params = arm_params(bench([rec("a", "s-0001")]), "a")
    assert params["models.generator"] == "m-gen"
    assert params["max_repair_rounds"] == "3"
    assert params["prompt_hash"] == "p"
    assert NullTracker().log_bench(bench([rec("a", "s-0001")])) == 0
