"""The benchmark end to end, offline: real scenarios, real arms from conf/bench.yaml, the real
run state machine; content-aware fakes stand in for Token Factory and Batfish."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

from fabloom import cli
from fabloom.agent.events import EVENTS_FILE
from fabloom.config import ModelsConfig, RunConfig, Settings, bench_config, run_config
from fabloom.contracts import Intent, Platform, Scenario
from fabloom.eval import (
    BenchMismatchError,
    BenchResults,
    BenchRunner,
    MlflowTracker,
    collect,
    resolve_arms,
)
from fabloom.eval.runner import BENCH_FILE, RESULTS_FILE, RUNS_SUBDIR
from fabloom.llm.grounding import DocGrounder
from fabloom.scenarios import load_scenarios
from tests.conftest import CONF_DIR
from tests.fakes.bench import MarkerVerifier, PlanAwareChatClient

pytestmark = pytest.mark.integration

ARMS = ["nano-raw", "super-raw", "super-repair", "routed"]
SCENARIO_IDS = [
    "leaf-spine-ebgp-0002",
    "leaf-spine-unnumbered-0001",
    "leaf-spine-eos-0001",
    "leaf-spine-filters-0001",
]


@pytest.fixture
def scenarios() -> list[Scenario]:
    by_id = {s.id: s for s in load_scenarios()}
    return [by_id[i] for i in SCENARIO_IDS]


def _client(scenarios: list[Scenario], models: ModelsConfig) -> PlanAwareChatClient:
    nano, super_ = models.models["nano"].id, models.models["super"].id

    def breaks(model: str, kind: str, intent: Intent) -> bool:
        # Nano always invents a command; Super does on a first EOS draft, then fixes it.
        if model == nano:
            return True
        return model == super_ and kind == "generate" and intent.platform is Platform.ARISTA_EOS

    return PlanAwareChatClient([s.intent for s in scenarios], breaks)


def _runner(
    scenarios: list[Scenario],
    models: ModelsConfig,
    out: Path,
    *,
    arms: list[str] = ARMS,
    client: PlanAwareChatClient | None = None,
    concurrency: int = 1,
) -> tuple[BenchRunner, PlanAwareChatClient]:
    client = client or _client(scenarios, models)
    resolved = resolve_arms(
        bench_config(CONF_DIR), models, RunConfig.model_validate({"budget": {"max_usd": 1.0}}), arms
    )
    runner = BenchRunner(
        client=client,
        verifier=MarkerVerifier(),
        arms=resolved,
        scenarios=scenarios,
        out_dir=out,
        concurrency=concurrency,
    )
    return runner, client


def _outcomes(results: BenchResults) -> list[tuple[str, str, str, int]]:
    return sorted((r.arm, r.scenario_id, r.outcome, r.repair_rounds) for r in results.records)


def _eos_count(scenarios: list[Scenario]) -> int:
    return sum(s.intent.platform is Platform.ARISTA_EOS for s in scenarios)


async def test_bench_measures_what_each_arm_does(
    tmp_path: Path, scenarios: list[Scenario], models: ModelsConfig
) -> None:
    runner, client = _runner(scenarios, models, tmp_path / "b")
    results = await runner.run()
    eos, n = _eos_count(scenarios), len(scenarios)
    assert 0 < eos < n
    s = results.summaries()

    assert (s["nano-raw"].passed, s["nano-raw"].first_passed) == (0, 0)
    assert s["nano-raw"].unrecognized_line_rate == 1.0
    assert s["nano-raw"].mean_repair_rounds == 0

    assert (s["super-raw"].first_passed, s["super-raw"].passed) == (n - eos, n - eos)

    for arm in ("super-repair", "routed"):
        assert (s[arm].first_passed, s[arm].passed) == (n - eos, n)
        assert s[arm].mean_repair_rounds == pytest.approx(eos / n)
        assert s[arm].escalated_runs == 0  # one round fixes it; Ultra is never needed

    assert not results.incomplete
    assert results.meta.arms["nano-raw"].models["generator"] == models.models["nano"].id
    assert not results.meta.prices_verified  # conf/models.yaml prices are unconfirmed
    # Benchmark runs neither plan (ground-truth intent) nor explain (nothing grades it).
    assert {kind for _, kind in client.calls} == {"generate", "repair"}
    assert len(client.calls) == len(ARMS) * n + 2 * eos

    # results.json is exactly what the event logs say.
    on_disk = BenchResults.read(runner.out_dir / RESULTS_FILE)
    assert on_disk == collect(runner.out_dir) == results
    nano_run = runner.out_dir / RUNS_SUBDIR / "nano-raw--leaf-spine-ebgp-0002"
    assert (nano_run / EVENTS_FILE).exists()
    assert (nano_run / "report.json").exists()


async def test_parallel_runs_give_the_same_records(
    tmp_path: Path, scenarios: list[Scenario], models: ModelsConfig
) -> None:
    serial, _ = _runner(scenarios, models, tmp_path / "serial")
    parallel, _ = _runner(scenarios, models, tmp_path / "parallel", concurrency=8)
    assert _outcomes(await serial.run()) == _outcomes(await parallel.run())


async def test_resume_pays_only_for_unfinished_runs(
    tmp_path: Path, scenarios: list[Scenario], models: ModelsConfig
) -> None:
    out = tmp_path / "b"
    first, _ = _runner(scenarios, models, out)
    before = await first.run()

    again, client = _runner(scenarios, models, out)
    assert await again.run() == before
    assert client.calls == []  # every run reused
    assert again.meta.bench_id == before.meta.bench_id

    # A process killed mid-run leaves a log without run_finished: only that run is redone.
    events = out / RUNS_SUBDIR / "super-raw--leaf-spine-ebgp-0002" / EVENTS_FILE
    events.write_text("".join(events.read_text().splitlines(keepends=True)[:-1]))
    # ...or one killed mid-write leaves a torn last line that does not parse.
    torn = out / RUNS_SUBDIR / "nano-raw--leaf-spine-ebgp-0002" / EVENTS_FILE
    torn.write_text(torn.read_text()[:-40])
    assert {"super-raw--leaf-spine-ebgp-0002", "nano-raw--leaf-spine-ebgp-0002"} == set(
        collect(out).incomplete
    )
    third, client = _runner(scenarios, models, out)
    after = await third.run()
    assert sorted(client.calls) == sorted([
        (models.models["super"].id, "generate"),
        (models.models["nano"].id, "generate"),
    ])  # fmt: skip
    assert _outcomes(after) == _outcomes(before)
    assert not after.incomplete


async def test_resume_refuses_a_different_configuration(
    tmp_path: Path, scenarios: list[Scenario], models: ModelsConfig
) -> None:
    out = tmp_path / "b"
    first, _ = _runner(scenarios, models, out, arms=["super-raw"])
    await first.run()
    other, _ = _runner(scenarios, models, out, arms=["nano-raw"])
    with pytest.raises(BenchMismatchError, match="different configuration"):
        await other.run()


def test_collect_reports_runs_that_never_started(
    tmp_path: Path, scenarios: list[Scenario], models: ModelsConfig
) -> None:
    runner, _ = _runner(scenarios, models, tmp_path / "b", arms=["super-raw"])
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / BENCH_FILE).write_text(runner.meta.model_dump_json())
    results = collect(tmp_path / "b")
    assert results.records == []
    assert len(results.incomplete) == len(scenarios)


# MLflow's SQL store still uses a loader strategy SQLAlchemy 2.1 deprecated; not ours to fix.
@pytest.mark.filterwarnings("ignore:The ``noload`` loader strategy is deprecated")
async def test_mlflow_logs_one_run_per_arm_once(
    tmp_path: Path, scenarios: list[Scenario], models: ModelsConfig
) -> None:
    runner, _ = _runner(scenarios, models, tmp_path / "b", arms=["super-raw", "super-repair"])
    results = await runner.run()
    report = tmp_path / "report.md"
    report.write_text("# report\n")
    tracker = MlflowTracker(f"sqlite:///{tmp_path / 'mlflow.db'}", tmp_path / "mlruns")
    assert tracker.log_bench(results, report) == 2
    assert tracker.log_bench(results, report) == 0  # re-reporting does not duplicate

    from mlflow.tracking import MlflowClient  # noqa: PLC0415 - optional extra

    client = MlflowClient(tracking_uri=f"sqlite:///{tmp_path / 'mlflow.db'}")
    exp = client.get_experiment_by_name("fabloom-bench-v0.1")
    assert exp is not None
    runs = client.search_runs([exp.experiment_id], filter_string="tags.arm = 'super-repair'")
    [run] = runs
    assert run.data.metrics["final_pass_rate"] == 1.0
    assert run.data.params["models.repairer_escalated"] == models.models["super"].id
    assert run.data.tags["bench_id"] == results.meta.bench_id
    artifacts = {a.path for a in client.list_artifacts(run.info.run_id)}
    assert artifacts == {"records.json", "report.md"}
    assert (tmp_path / "mlruns").is_dir()  # artifacts land where configured, not in cwd


# --- CLI -------------------------------------------------------------------------------------

runner_cli = CliRunner()


def test_cli_dry_run_shows_the_bill_and_spends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_client(_: object) -> None:
        raise AssertionError("a dry run must not build a client")

    monkeypatch.setattr(cli, "build_client", no_client)
    result = runner_cli.invoke(cli.app, ["bench", "run", "--nightly", "--dry-run"])
    assert result.exit_code == 0, result.output
    nightly = bench_config(CONF_DIR).nightly
    assert f"{len(nightly.arms)} arm(s) x {nightly.scenarios} scenario(s)" in result.output
    assert "worst case $" in result.output


def test_cli_grounding_arm_needs_a_tavily_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # Blank reads as unset, so a real key in the developer's environment cannot leak in.
    monkeypatch.setenv("FABLOOM_TAVILY_API_KEY", "")
    monkeypatch.setenv("TAVILY_API_KEY", "")
    args = ["bench", "run", "--arm", "super-repair-grounding", "--nightly"]
    result = runner_cli.invoke(cli.app, [*args, "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "[needs FABLOOM_TAVILY_API_KEY]" in result.output

    def no_client(_: object) -> None:
        raise AssertionError("a run that cannot ground must stop before building a client")

    monkeypatch.setattr(cli, "build_client", no_client)
    result = runner_cli.invoke(cli.app, args)
    assert result.exit_code == 2
    assert "need FABLOOM_TAVILY_API_KEY" in result.output


def test_build_grounder_only_with_a_key() -> None:
    config = run_config(CONF_DIR).grounding
    assert cli.build_grounder(Settings(_env_file=None, tavily_api_key=None), config) is None
    with_key = Settings(_env_file=None, tavily_api_key=SecretStr("tvly-k"))
    assert isinstance(cli.build_grounder(with_key, config), DocGrounder)


def test_runner_refuses_a_grounding_arm_without_a_grounder(
    scenarios: list[Scenario], models: ModelsConfig
) -> None:
    [arm] = resolve_arms(bench_config(CONF_DIR), models, RunConfig(), ["super-repair-grounding"])
    assert arm.meta().grounding
    with pytest.raises(ValueError, match="TAVILY"):
        BenchRunner(
            client=_client(scenarios, models), verifier=MarkerVerifier(), arms=[arm],
            scenarios=scenarios[:1],
        )  # fmt: skip


def test_cli_refuses_unknown_arms_and_empty_selections() -> None:
    result = runner_cli.invoke(cli.app, ["bench", "run", "--arm", "gpt-raw", "--dry-run"])
    assert result.exit_code == 2
    assert "unknown arms" in result.output
    result = runner_cli.invoke(cli.app, ["bench", "run", "--family", "nope*", "--dry-run"])
    assert result.exit_code == 2
    assert "no scenarios selected" in result.output


def test_cli_run_report_baseline_and_gates(
    tmp_path: Path,
    scenarios: list[Scenario],
    models: ModelsConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "build_client", lambda s: _client(scenarios, models))
    monkeypatch.setattr(cli, "build_verifier", lambda s: MarkerVerifier())
    out, baseline = tmp_path / "bench", tmp_path / "baseline.json"
    ids = [arg for s in scenarios[:2] for arg in ("--scenario", s.id)]
    args = ["bench", "run", "--arm", "super-raw", *ids, "--out", str(out), "--no-track"]
    gate_args = ["--baseline", str(baseline), "--check-gates"]

    result = runner_cli.invoke(cli.app, [*args, *gate_args])
    # No baseline yet: every gate is skipped, and skipped is not passed.
    assert result.exit_code == 1, result.output
    assert "[  2/2] super-raw" in result.output
    assert "skipped no regression: super-raw final_pass_rate: no frozen baseline" in result.output
    assert (out / "report.md").read_text().startswith("# Fabloom benchmark v0.1")

    result = runner_cli.invoke(cli.app, ["bench", "baseline", str(out), "--to", str(baseline)])
    assert result.exit_code == 0, result.output
    assert json.loads(baseline.read_text())["meta"]["arms"].keys() == {"super-raw"}
    result = runner_cli.invoke(cli.app, ["bench", "baseline", str(out), "--to", str(baseline)])
    assert result.exit_code == 2
    assert "--force" in result.output

    published = tmp_path / "docs" / "v0.1.md"
    result = runner_cli.invoke(
        cli.app, ["bench", "report", str(out), "--report", str(published), *gate_args]
    )
    assert result.exit_code == 0, result.output
    assert "passed  no regression: super-raw final_pass_rate" in result.output
    assert "PASSED" in published.read_text()

    # Resuming through the CLI reuses both runs.
    result = runner_cli.invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    assert result.output.count("(reused)") == 2


def test_cli_baseline_refuses_an_unfinished_bench(
    tmp_path: Path, scenarios: list[Scenario], models: ModelsConfig
) -> None:
    runner, _ = _runner(scenarios, models, tmp_path / "b", arms=["super-raw"])
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / BENCH_FILE).write_text(runner.meta.model_dump_json())
    result = runner_cli.invoke(cli.app, ["bench", "baseline", str(tmp_path / "b")])
    assert result.exit_code == 2
    assert "unfinished" in result.output
