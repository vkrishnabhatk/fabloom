"""Record real Batfish answers for known-good pilot configs into tests/fixtures/batfish/.

The evaluator's column assumptions (tests/fakes/batfish.py) are synthetic until this runs.
Once recorded, tests/integration/test_recorded_fixtures.py replays every fixture and asserts
known-good configs pass every check: the first test of the evaluator against the real thing.

    make batfish-up
    PYTHONPATH=. uv run python scripts/record_batfish_fixtures.py
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime

from tests.fakes.render import render_good_configs

from fabloom.checks import allocate, compile_checks
from fabloom.config import REPO_ROOT, Settings
from fabloom.contracts import Snapshot
from fabloom.scenarios import load_scenario
from fabloom.verify.batfish_local import BatfishVerifier, PybatfishAnswerer
from fabloom.verify.recorded import RecordingAnswerer

SCENARIOS = [
    "leaf-spine-ebgp-0002",
    "leaf-spine-unnumbered-0001",
    "leaf-spine-eos-0001",
    "leaf-spine-filters-0002",
    # Spine-only filter: leaf-to-leaf transit through the spines must stay open.
    "leaf-spine-filters-0006",
]
OUT = REPO_ROOT / "tests" / "fixtures" / "batfish"


async def record(sid: str, host: str) -> bool:
    sc = load_scenario(REPO_ROOT / "benchmark" / "v0.1" / f"{sid}.yaml")
    fabric = allocate(sc.intent)
    checks = compile_checks(sc.intent, fabric).checks
    rec = RecordingAnswerer(PybatfishAnswerer(host))
    verifier = BatfishVerifier(lambda: rec, name="batfish-record")
    report = await verifier.verify(
        Snapshot(name=f"record-{sid}", fabric=fabric),
        render_good_configs(sc.intent, fabric),
        checks,
    )
    OUT.mkdir(parents=True, exist_ok=True)
    rec.dump(
        OUT / f"{sid}.json",
        batfish_version=report.batfish_version,
        provenance=f"recorded from Batfish {report.batfish_version} "
        f"on {datetime.now(UTC):%Y-%m-%d}",
    )
    failed = [c.id for c in report.failed_checks]
    print(f"{sid:<32} {'all passed' if not failed else f'{len(failed)} FAILED: {failed[:5]}'}")
    return not failed


def main() -> None:
    host = Settings().batfish_host
    ok = [asyncio.run(record(sid, host)) for sid in SCENARIOS]
    # A known-good config failing means the renderer, the snapshot packaging or the question
    # mapping is wrong. That is the finding this script exists to surface.
    sys.exit(0 if all(ok) else 1)


if __name__ == "__main__":
    main()
