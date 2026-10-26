"""Batfish verifier over any ``Answerer``, and the pybatfish-backed answerer.

``BatfishVerifier`` owns the snapshot -> answers -> report pipeline. Where Batfish runs is the
answerer's business: a local ``batfish/allinone`` container (this module), the same thing
inside a Nebius Sandbox (``fabloom-verify`` in the verifier image), or recorded fixtures in
tests. The benchmark runs on more than one backend so their numbers are comparable.
"""

from __future__ import annotations

import asyncio
import math
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from fabloom import __version__
from fabloom.contracts import (
    Check,
    Platform,
    Snapshot,
    VerificationLevel,
    VerificationReport,
    score_checks,
)
from fabloom.verify.batfish_questions import Answerer, Evaluator, Question, Row
from fabloom.verify.protocol import VerifierUnavailableError
from fabloom.verify.snapshot import write_snapshot


class SnapshotAnswerer(Answerer, Protocol):
    def init_snapshot(self, path: Path, name: str) -> None: ...

    def batfish_version(self) -> str | None: ...


def run_checks(answerer: Answerer, checks: Sequence[Check]) -> dict[str, Any]:
    """Evaluate ``checks`` against an initialised snapshot. Pure over the answerer."""
    ev = Evaluator(answerer)
    results = [ev.evaluate(c) for c in checks]
    return {
        "checks": results,
        "parse_status": ev.parse_status(),
        "unrecognized_lines": ev.unrecognized_lines(),
        "undefined_references": ev.undefined_references(),
        "score": score_checks(results),
    }


class BatfishVerifier:
    level = VerificationLevel.L2
    platforms = frozenset(Platform)

    def __init__(self, answerer_factory: Callable[[], SnapshotAnswerer], name: str) -> None:
        self._factory = answerer_factory
        self.name = name

    def _verify_sync(
        self, snapshot: Snapshot, configs: Mapping[str, str], checks: Sequence[Check]
    ) -> VerificationReport:
        start = time.perf_counter()
        answerer = self._factory()
        with tempfile.TemporaryDirectory(prefix="fabloom-snap-") as tmp:
            root = write_snapshot(Path(tmp), snapshot.fabric, configs)
            answerer.init_snapshot(root, snapshot.name)
            facts = run_checks(answerer, checks)
        return VerificationReport(
            level=self.level,
            duration_ms=int((time.perf_counter() - start) * 1000),
            verifier_name=self.name,
            verifier_version=__version__,
            batfish_version=answerer.batfish_version(),
            **facts,
        )

    async def verify(
        self, snapshot: Snapshot, configs: Mapping[str, str], checks: Sequence[Check]
    ) -> VerificationReport:
        # pybatfish is synchronous; keep the event loop free for SSE and parallel candidates.
        return await asyncio.to_thread(self._verify_sync, snapshot, configs, checks)


# --- pybatfish adapter -------------------------------------------------------------------


def to_cell(value: Any) -> Any:
    """Render a pybatfish frame cell as JSON-able data the evaluator can compare."""
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        return None if math.isnan(value) else value
    if isinstance(value, list | tuple | set | frozenset):
        return [to_cell(v) for v in value]
    if isinstance(value, dict):
        return {str(k): to_cell(v) for k, v in value.items()}
    hostname, interface = getattr(value, "hostname", None), getattr(value, "interface", None)
    if hostname is not None and interface is not None:
        return f"{hostname}[{interface}]"
    return str(value)


class PybatfishAnswerer:
    """Talks to a Batfish server with pybatfish (``pip install 'fabloom[batfish]'``)."""

    def __init__(self, host: str, network: str = "fabloom") -> None:
        try:
            from pybatfish.client.session import Session  # noqa: PLC0415 - optional extra
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise VerifierUnavailableError(
                "pybatfish is not installed; install with: uv sync --extra batfish"
            ) from exc
        try:
            self._bf = Session(host=host)
            self._bf.set_network(network)
        except Exception as exc:
            raise VerifierUnavailableError(
                f"cannot reach Batfish at {host}; start it with: make batfish-up"
            ) from exc

    def init_snapshot(self, path: Path, name: str) -> None:
        self._bf.init_snapshot(str(path), name=name, overwrite=True)

    def batfish_version(self) -> str | None:
        versions: dict[str, str] = self._bf.get_component_versions()
        return versions.get("Batfish")

    def ask(self, question: Question) -> list[Row]:
        from pybatfish.datamodel.flow import (  # noqa: PLC0415 - optional extra
            HeaderConstraints,
            PathConstraints,
        )

        params = dict(question.params)
        if "headers" in params:
            params["headers"] = HeaderConstraints(**params["headers"])
        if "pathConstraints" in params:
            params["pathConstraints"] = PathConstraints(**params["pathConstraints"])
        frame = getattr(self._bf.q, question.name)(**params).answer().frame()
        records: list[dict[str, Any]] = frame.to_dict("records")
        return [{str(k): to_cell(v) for k, v in rec.items()} for rec in records]


def local_verifier(host: str) -> BatfishVerifier:
    return BatfishVerifier(lambda: PybatfishAnswerer(host), name="batfish-local")
