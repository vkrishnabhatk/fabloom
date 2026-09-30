"""The run event log: ``runs/<run_id>/events.jsonl``, append-only, one typed event per line.

A single writer (the run) appends; any number of readers tail. Readers never need the
writer's cooperation, which is what lets the CLI, the web SSE endpoint and the benchmark
consume the same run the same way (ADR-0004).
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from fabloom.contracts import RUN_EVENT_ADAPTER, RunEvent, RunState
from fabloom.contracts.run import RunError, RunEventBase, RunFinished

E = TypeVar("E", bound=RunEventBase)

EVENTS_FILE = "events.jsonl"
TERMINAL_EVENTS = (RunFinished,)


def new_run_id(now: datetime | None = None) -> str:
    """Sortable and collision-resistant: ``20261005T141502Z-3fa9c1``."""
    ts = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    return f"{ts}-{secrets.token_hex(3)}"


class EventLog:
    def __init__(self, run_dir: Path, run_id: str) -> None:
        self.run_id = run_id
        self.run_dir = run_dir
        self.path = run_dir / EVENTS_FILE
        self.state = RunState.PLANNING
        self._seq = 0
        run_dir.mkdir(parents=True, exist_ok=True)

    def emit(self, event_type: type[E], **fields: Any) -> E:
        event = event_type(
            seq=self._seq,
            ts=datetime.now(UTC),
            run_id=self.run_id,
            state=self.state,
            **fields,
        )
        self._seq += 1
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(event.model_dump_json() + "\n")
            fh.flush()
        return event


def parse_line(line: str) -> RunEvent:
    return RUN_EVENT_ADAPTER.validate_json(line)


def read_events(path: Path) -> Iterator[RunEvent]:
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield parse_line(line)


def _read_from(path: Path, offset: int) -> bytes:
    if not path.exists():
        return b""
    with path.open("rb") as fh:
        fh.seek(offset)
        return fh.read()


def _is_terminal(event: RunEvent) -> bool:
    return isinstance(event, TERMINAL_EVENTS)


async def tail_events(
    path: Path, *, poll_s: float = 0.2, timeout_s: float | None = None
) -> AsyncIterator[RunEvent]:
    """Yield events as they are appended, stopping after ``run_finished``.

    Safe against a writer that has not created the file yet and against partial lines.
    """
    loop = asyncio.get_running_loop()
    deadline = None if timeout_s is None else loop.time() + timeout_s
    offset = 0
    pending = b""
    while True:
        chunk = await asyncio.to_thread(_read_from, path, offset)
        offset += len(chunk)
        pending += chunk
        *lines, pending = pending.split(b"\n")
        for raw in lines:
            if raw.strip():
                event = parse_line(raw.decode("utf-8"))
                yield event
                if _is_terminal(event):
                    return
        if deadline is not None and loop.time() > deadline:
            return
        await asyncio.sleep(poll_s)


__all__ = [
    "EVENTS_FILE",
    "EventLog",
    "RunError",
    "new_run_id",
    "parse_line",
    "read_events",
    "tail_events",
]
