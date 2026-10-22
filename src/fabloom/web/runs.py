"""Starting runs for visitors, and reading any run back from its directory.

The web app is one more reader of the event log (ADR-0004): a live run, a finished run and
a frozen gallery run are all just directories with an ``events.jsonl``, read the same way.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from fabloom.agent import RunMachine, RunRequest, read_events
from fabloom.agent.events import EVENTS_FILE
from fabloom.contracts import Platform
from fabloom.contracts.run import RunFinished, RunStarted
from fabloom.web.guards import SpendBreaker

log = structlog.get_logger(__name__)

# new_run_id() output, and nothing that could walk out of the runs directory.
RUN_ID = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$")


class ReadOnlyError(Exception):
    """No model is configured, or today's spend cap is reached: only the gallery is served."""


class BusyError(Exception):
    """Every run slot is in use."""


@dataclass(frozen=True)
class RunLocation:
    run_id: str
    run_dir: Path
    gallery: bool

    @property
    def events(self) -> Path:
        return self.run_dir / EVENTS_FILE


class RunService:
    def __init__(
        self,
        machine: RunMachine | None,
        runs_dir: Path,
        gallery_dir: Path,
        breaker: SpendBreaker,
        max_active: int,
    ) -> None:
        self.machine = machine
        self.runs_dir = runs_dir
        self.gallery_dir = gallery_dir
        self.breaker = breaker
        self._max_active = max_active
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def read_only_reason(self) -> str | None:
        if self.machine is None:
            return "no model is configured on this server"
        if self.breaker.tripped:
            return "today's demo budget is used up"
        return None

    @property
    def active(self) -> int:
        return len(self._tasks)

    def start(self, request: str, platform: Platform | None) -> str:
        if (reason := self.read_only_reason) is not None or self.machine is None:
            raise ReadOnlyError(reason)
        if self.active >= self._max_active:
            raise BusyError(f"{self.active} runs in progress; try again shortly")
        worst = self.machine.run_config.budget.max_usd
        if not self.breaker.reserve(worst):
            raise ReadOnlyError("today's demo budget is used up")
        run_id, _ = self.machine.new_run_dir()
        task = asyncio.create_task(self._run(RunRequest(request, platform), run_id, worst))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return run_id

    async def _run(self, req: RunRequest, run_id: str, worst: float) -> None:
        assert self.machine is not None  # noqa: S101 - checked in start()
        cost = 0.0
        try:
            result = await self.machine.run(req, run_id=run_id)
            cost = result.usage.cost_usd
        except Exception:  # RunMachine logs its own failures; never kill the server loop
            log.exception("web_run_crashed", run_id=run_id)
        finally:
            self.breaker.settle(worst, cost)

    async def drain(self) -> None:
        """Let in-flight runs finish (shutdown, and tests)."""
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    def locate(self, run_id: str) -> RunLocation | None:
        if not RUN_ID.match(run_id):
            return None
        for base, gallery in ((self.runs_dir, False), (self.gallery_dir, True)):
            d = base / run_id
            if (d / EVENTS_FILE).exists():
                return RunLocation(run_id, d, gallery)
        return None

    def gallery(self) -> list[dict[str, Any]]:
        if not self.gallery_dir.is_dir():
            return []
        items = []
        for d in sorted(self.gallery_dir.iterdir(), reverse=True):
            loc = self.locate(d.name)
            if loc is not None:
                items.append(summarize(loc, with_configs=False))
        return items


def summarize(loc: RunLocation, *, with_configs: bool = True) -> dict[str, Any]:
    """A run as JSON: what the API returns and what every HTML view renders."""
    started: RunStarted | None = None
    finished: RunFinished | None = None
    state = None
    for e in read_events(loc.events):
        state = e.state
        if isinstance(e, RunStarted):
            started = e
        elif isinstance(e, RunFinished):
            finished = e
    out: dict[str, Any] = {
        "run_id": loc.run_id,
        "gallery": loc.gallery,
        "request": started.request if started else None,
        "verifier": started.verifier if started else None,
        "state": state,
        "finished": finished is not None,
        "outcome": finished.outcome if finished else None,
        "score": finished.score if finished else None,
        "repair_rounds": finished.repair_rounds if finished else None,
        "cost_usd": finished.usage.cost_usd if finished else None,
    }
    result_file = loc.run_dir / "result.json"
    if finished and result_file.exists():
        result = json.loads(result_file.read_text(encoding="utf-8"))
        out["explanation"] = result.get("explanation", "")
        out["unverifiable"] = result.get("unverifiable", [])
    report_file = loc.run_dir / "report.json"
    if with_configs and finished and report_file.exists():
        report = json.loads(report_file.read_text(encoding="utf-8"))
        out["level"] = report.get("level")
        out["checks"] = [
            {"id": c["id"], "kind": c["kind"], "passed": c["passed"]}
            for c in report.get("checks", [])
        ]
    cfg_dir = loc.run_dir / "configs"
    if with_configs and cfg_dir.is_dir():
        out["configs"] = {
            p.stem: p.read_text(encoding="utf-8") for p in sorted(cfg_dir.glob("*.cfg"))
        }
    return out
