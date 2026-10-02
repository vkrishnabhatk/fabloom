"""``fabloom`` CLI, and the composition root: where a concrete verifier meets the agent."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer

from fabloom import __version__
from fabloom.agent import RunMachine, RunRequest, RunResult, tail_events
from fabloom.agent.events import EVENTS_FILE, read_events
from fabloom.checks import allocate, compile_checks
from fabloom.config import REPO_ROOT, Settings, bench_config, models_config, run_config
from fabloom.contracts import Platform, RunBudget, Verifier
from fabloom.contracts.run import RunEvent
from fabloom.eval import (
    BenchMismatchError,
    BenchResults,
    BenchRunner,
    MlflowTracker,
    RunRecord,
    all_passed,
    collect,
    evaluate,
    resolve_arms,
    select_scenarios,
    worst_case_usd,
    write_report,
)
from fabloom.eval.gates import Gate
from fabloom.eval.runner import RESULTS_FILE
from fabloom.llm import ModelRouter, TokenFactoryClient
from fabloom.llm.prompts import render_plan
from fabloom.log import configure_logging
from fabloom.provenance import git_sha
from fabloom.scenarios import DEFAULT_BENCHMARK_DIR, load_scenario, load_scenarios

app = typer.Typer(no_args_is_help=True, add_completion=False, help=__doc__)
scenarios_app = typer.Typer(no_args_is_help=True, help="Benchmark scenario tools.")
app.add_typer(scenarios_app, name="scenarios")
bench_app = typer.Typer(
    no_args_is_help=True, help="Benchmark: arms x scenarios, tracked, gated and reported."
)
app.add_typer(bench_app, name="bench")

BASELINES_DIR = REPO_ROOT / "benchmark" / "baselines"
REPORT_FILE = "report.md"


def _echo_json(obj: object) -> None:
    typer.echo(json.dumps(obj, indent=2, default=str))


def build_verifier(settings: Settings) -> Verifier:
    from fabloom.verify import PrelintGate  # noqa: PLC0415 - composition root only
    from fabloom.verify.batfish_local import local_verifier  # noqa: PLC0415

    return PrelintGate(local_verifier(settings.batfish_host))


def build_client(settings: Settings) -> TokenFactoryClient:
    if settings.token_factory_api_key is None:
        typer.secho(
            "No Token Factory key. Set FABLOOM_TOKEN_FACTORY_API_KEY (or NEBIUS_API_KEY).",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(2)
    return TokenFactoryClient(
        settings.token_factory_api_key.get_secret_value(), settings.token_factory_base_url
    )


def format_event(e: RunEvent) -> str:  # noqa: PLR0911 - one arm per event type
    match e.type:
        case "state_changed":
            reason = f" ({e.reason})" if e.reason else ""
            return f"[{e.seq:>3}] {e.from_state} -> {e.to_state}{reason}"
        case "llm_call":
            return (
                f"[{e.seq:>3}]   {e.role}: {e.model} {e.prompt_tokens}+{e.completion_tokens} tok "
                f"${e.cost_usd:.4f} {e.duration_ms}ms"
            )
        case "verification_completed":
            s = e.summary
            cached = " (cached)" if e.cached else ""
            return (
                f"[{e.seq:>3}]   round {e.round} candidate {e.candidate}: "
                f"{s['total'] - len(s['failed'])}/{s['total']} checks, "
                f"score {s['score']:.3f}, {s['level']}{cached}"
            )
        case "checks_compiled":
            return f"[{e.seq:>3}]   compiled {e.count} checks"
        case "budget_exhausted":
            return f"[{e.seq:>3}]   budget exhausted: {e.limit} ({e.detail})"
        case "explanation_ready":
            return f"\n{e.text}\n"
        case "run_error":
            return f"[{e.seq:>3}]   error: {e.error_type}: {e.message}"
        case "run_finished":
            return (
                f"[{e.seq:>3}] finished: {e.outcome}, score {e.score:.3f}, "
                f"{e.repair_rounds} repair rounds, ${e.usage.cost_usd:.4f}"
            )
        case _:
            return f"[{e.seq:>3}]   {e.type}"


@app.command()
def version() -> None:
    """Print the package version and the commit it was built from."""
    typer.echo(f"fabloom {__version__} ({git_sha()})")


@app.command()
def plan(
    scenario: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    show_checks: Annotated[bool, typer.Option("--checks/--no-checks")] = True,
) -> None:
    """Show the deterministic fabric plan and check set for a scenario. No model, no network."""
    sc = load_scenario(scenario)
    fabric = allocate(sc.intent)
    checkset = compile_checks(sc.intent, fabric)
    typer.echo(render_plan(sc.intent, fabric))
    if show_checks:
        typer.echo(f"\n{len(checkset.checks)} checks:")
        for c in checkset.checks:
            typer.echo(f"  [{c.severity:<8}] {c.id}")
    for u in checkset.unverifiable:
        typer.secho(f"  not verifiable: {u}", fg=typer.colors.YELLOW)


@scenarios_app.command("validate")
def scenarios_validate(
    directory: Annotated[Path, typer.Argument(file_okay=False)] = DEFAULT_BENCHMARK_DIR,
) -> None:
    """Load every scenario, allocate its fabric and compile its checks."""
    scenarios = load_scenarios(directory)
    for sc in scenarios:
        checkset = compile_checks(sc.intent, allocate(sc.intent))
        typer.echo(f"{sc.id:<40} {sc.intent.platform:<11} {len(checkset.checks):>4} checks")
    typer.echo(f"{len(scenarios)} scenarios OK")


@app.command()
def generate(
    request: Annotated[str, typer.Argument(help="The network, in plain language.")],
    platform: Annotated[Platform | None, typer.Option(help="Override the platform.")] = None,
    runs_dir: Annotated[Path | None, typer.Option(help="Where run directories go.")] = None,
    max_repair_rounds: Annotated[int | None, typer.Option(min=0)] = None,
    watch: Annotated[bool, typer.Option(help="Tail the run's event log.")] = True,
) -> None:
    """Plan, generate, verify and repair configs for REQUEST."""
    settings = Settings()
    configure_logging(settings.log_level, settings.log_json)
    rc = run_config(settings.conf_dir)
    if max_repair_rounds is not None:
        budget = RunBudget.model_validate(
            {**rc.budget.model_dump(), "max_repair_rounds": max_repair_rounds}
        )
        rc = rc.model_copy(update={"budget": budget})
    machine = RunMachine(
        client=build_client(settings),
        router=ModelRouter(models_config(settings.conf_dir)),
        verifier=build_verifier(settings),
        run_config=rc,
        runs_dir=runs_dir or settings.runs_dir,
    )
    result = asyncio.run(_run_and_watch(machine, RunRequest(request, platform), watch))
    typer.echo(f"run dir: {result.run_dir}")
    raise typer.Exit(0 if result.outcome == "passed" else 1)


async def _run_and_watch(machine: RunMachine, req: RunRequest, watch: bool) -> RunResult:
    run_id, run_dir = machine.new_run_dir()
    task = asyncio.create_task(machine.run(req, run_id=run_id))
    if watch:
        # The CLI is just another reader of the event log, like the web UI will be.
        async for event in tail_events(run_dir / EVENTS_FILE):
            typer.echo(format_event(event))
    return await task


@app.command()
def events(
    run_dir: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    follow: Annotated[bool, typer.Option("--follow", "-f")] = False,
) -> None:
    """Print a run's event log; with --follow, keep tailing until it finishes."""
    path = run_dir / EVENTS_FILE
    if follow:

        async def _follow() -> None:
            async for e in tail_events(path):
                typer.echo(format_event(e))

        asyncio.run(_follow())
    else:
        for e in read_events(path):
            typer.echo(format_event(e))


def _baseline_path(benchmark: str) -> Path:
    return BASELINES_DIR / f"{benchmark}.json"


def _load_baseline(path: Path | None, benchmark: str) -> BenchResults | None:
    path = path or _baseline_path(benchmark)
    return BenchResults.read(path) if path.exists() else None


def _print_gates(gates: list[Gate]) -> None:
    colours = {"passed": typer.colors.GREEN, "failed": typer.colors.RED}
    for g in gates:
        mark = typer.style(f"{g.status:<7}", fg=colours.get(g.status, typer.colors.YELLOW))
        typer.echo(f"{mark} {g.label}: {g.detail}")


def _finish_bench(
    out: Path,
    results: BenchResults,
    *,
    settings: Settings,
    baseline: Path | None,
    report: Path | None,
    track: bool,
    check_gates: bool,
) -> None:
    """Shared tail of `bench run` and `bench report`: report, gates, tracking, exit code."""
    gates = evaluate(
        results,
        _load_baseline(baseline, results.meta.benchmark),
        bench_config(settings.conf_dir).gates,
    )
    report_path = report or out / REPORT_FILE
    write_report(report_path, results, gates, source=str(out))
    for arm, s in results.summaries().items():
        typer.echo(
            f"{arm:<16} first-pass {s.first_passed}/{s.n}  final {s.passed}/{s.n}  "
            f"${s.usd_total:.4f}"
        )
    _print_gates(gates)
    typer.echo(f"report: {report_path}")
    if track:
        try:
            tracker = MlflowTracker(settings.mlflow_tracking_uri, settings.mlflow_artifact_root)
        except ImportError as exc:
            typer.secho(f"not tracked: {exc}", fg=typer.colors.YELLOW, err=True)
        else:
            created = tracker.log_bench(results, report_path)
            typer.echo(f"mlflow: {created} run(s) logged to {settings.mlflow_tracking_uri}")
    if check_gates and not all_passed(gates):
        raise typer.Exit(1)


@bench_app.command("run")
def bench_run(  # noqa: PLR0917 - one parameter per CLI option
    arm: Annotated[
        list[str] | None, typer.Option(help="Arm from conf/bench.yaml (repeatable). Default: all.")
    ] = None,
    scenario: Annotated[list[str] | None, typer.Option(help="Scenario ID (repeatable).")] = None,
    family: Annotated[list[str] | None, typer.Option(help="Family glob (repeatable).")] = None,
    limit: Annotated[
        int | None, typer.Option(min=1, help="At most N scenarios, taking families in turn.")
    ] = None,
    nightly: Annotated[
        bool, typer.Option(help="The nightly subset and arms from conf/bench.yaml.")
    ] = False,
    scenarios_dir: Annotated[Path, typer.Option(file_okay=False)] = DEFAULT_BENCHMARK_DIR,
    out: Annotated[
        Path | None, typer.Option(help="Bench directory; an existing one is resumed.")
    ] = None,
    concurrency: Annotated[
        int, typer.Option(min=1, help="Runs in flight. Keep 1 for a single local Batfish.")
    ] = 1,
    baseline: Annotated[Path | None, typer.Option(dir_okay=False)] = None,
    report: Annotated[Path | None, typer.Option(dir_okay=False)] = None,
    track: Annotated[bool, typer.Option(help="Log to MLflow.")] = True,
    check_gates: Annotated[bool, typer.Option(help="Exit 1 unless every gate passes.")] = False,
    dry_run: Annotated[
        bool, typer.Option(help="Show the matrix and worst-case spend, run nothing.")
    ] = False,
) -> None:
    """Run arms x scenarios through the real loop, then report, gate and track."""
    settings = Settings()
    configure_logging(settings.log_level, settings.log_json)
    bench = bench_config(settings.conf_dir)
    arms_wanted = arm or []
    if nightly:
        arms_wanted = arms_wanted or bench.nightly.arms
        limit = limit or bench.nightly.scenarios
    try:
        arms = resolve_arms(
            bench, models_config(settings.conf_dir), run_config(settings.conf_dir), arms_wanted
        )
        scenarios = select_scenarios(
            load_scenarios(scenarios_dir), ids=scenario or (), families=family or (), limit=limit
        )
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from exc
    if not scenarios:
        typer.secho("no scenarios selected", fg=typer.colors.RED, err=True)
        raise typer.Exit(2)
    typer.echo(
        f"{len(arms)} arm(s) x {len(scenarios)} scenario(s) = {len(arms) * len(scenarios)} runs; "
        f"worst case ${worst_case_usd(arms, scenarios):.2f} (sum of run caps)"
    )
    if dry_run:
        for a in arms:
            typer.echo(f"  {a.name:<16} {a.spec.description}")
        for s in scenarios:
            typer.echo(f"  {s.id}")
        return

    done = 0
    total = len(arms) * len(scenarios)

    def progress(r: RunRecord, reused: bool) -> None:
        nonlocal done
        done += 1
        tag = " (reused)" if reused else ""
        typer.echo(
            f"[{done:>3}/{total}] {r.arm:<16} {r.scenario_id:<36} {r.outcome:<9} "
            f"first-pass={str(r.first_pass).lower():<5} rounds={r.repair_rounds} "
            f"${r.cost_usd:.4f}{tag}"
        )

    runner = BenchRunner(
        client=build_client(settings),
        verifier=build_verifier(settings),
        arms=arms,
        scenarios=scenarios,
        out_dir=out,
        bench_root=settings.runs_dir / "bench",
        benchmark=scenarios_dir.name,
        concurrency=concurrency,
        on_record=progress,
    )
    try:
        results = asyncio.run(runner.run())
    except BenchMismatchError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from exc
    typer.echo(f"bench dir: {runner.out_dir}")
    _finish_bench(
        runner.out_dir,
        results,
        settings=settings,
        baseline=baseline,
        report=report,
        track=track,
        check_gates=check_gates,
    )


@bench_app.command("report")
def bench_report(
    bench_dir: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    baseline: Annotated[Path | None, typer.Option(dir_okay=False)] = None,
    report: Annotated[
        Path | None,
        typer.Option(dir_okay=False, help="e.g. docs/benchmark/v0.1.md to publish."),
    ] = None,
    track: Annotated[bool, typer.Option(help="Log to MLflow (skips arms already logged).")] = False,
    check_gates: Annotated[bool, typer.Option(help="Exit 1 unless every gate passes.")] = False,
) -> None:
    """Rebuild results, gates and the report from a bench directory's event logs. No model."""
    settings = Settings()
    results = collect(bench_dir)
    results.write(bench_dir / RESULTS_FILE)
    _finish_bench(
        bench_dir,
        results,
        settings=settings,
        baseline=baseline,
        report=report,
        track=track,
        check_gates=check_gates,
    )


@bench_app.command("baseline")
def bench_baseline(
    bench_dir: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    to: Annotated[Path | None, typer.Option(dir_okay=False)] = None,
    force: Annotated[bool, typer.Option(help="Replace an existing baseline.")] = False,
) -> None:
    """Freeze a finished bench as the baseline the regression gate compares against."""
    results = collect(bench_dir)
    if results.incomplete:
        typer.secho(
            f"{len(results.incomplete)} run(s) unfinished; resume the bench first",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(2)
    path = to or _baseline_path(results.meta.benchmark)
    if path.exists() and not force:
        typer.secho(f"{path} exists; pass --force to replace it", fg=typer.colors.RED, err=True)
        raise typer.Exit(2)
    results.write(path)
    typer.echo(f"baseline {results.meta.bench_id} frozen at {path}")


@app.command()
def doctor() -> None:
    """Check every configured model exists on Token Factory and answers in JSON mode."""
    settings = Settings()
    cfg = models_config(settings.conf_dir)
    client = build_client(settings)

    async def _check() -> list[tuple[str, str, str | None]]:
        available = await client.list_models()
        rows: list[tuple[str, str, str | None]] = []
        for name, spec in cfg.models.items():
            if spec.id not in available:
                rows.append((name, spec.id, "not listed by /v1/models"))
            else:
                rows.append((name, spec.id, await client.probe(spec)))
        return rows

    rows = asyncio.run(_check())
    failed = False
    for name, model_id, error in rows:
        ok = error is None
        failed |= not ok
        mark = typer.style("ok  " if ok else "FAIL", fg=typer.colors.GREEN if ok else "red")
        typer.echo(f"{mark} {name:<10} {model_id}" + ("" if ok else f"  {error}"))
    raise typer.Exit(1 if failed else 0)


if __name__ == "__main__":  # pragma: no cover
    app()
