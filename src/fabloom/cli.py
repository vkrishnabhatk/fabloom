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
from fabloom.config import Settings, models_config, run_config
from fabloom.contracts import Platform, RunBudget, Verifier
from fabloom.contracts.run import RunEvent
from fabloom.llm import ModelRouter, TokenFactoryClient
from fabloom.llm.prompts import render_plan
from fabloom.log import configure_logging
from fabloom.provenance import git_sha
from fabloom.scenarios import DEFAULT_BENCHMARK_DIR, load_scenario, load_scenarios

app = typer.Typer(no_args_is_help=True, add_completion=False, help=__doc__)
scenarios_app = typer.Typer(no_args_is_help=True, help="Benchmark scenario tools.")
app.add_typer(scenarios_app, name="scenarios")


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
