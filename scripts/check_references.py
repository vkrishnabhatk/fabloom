"""Check the checker: every scenario's known-good config must pass every check in live Batfish.

A failure here means the reference renderer, the snapshot packaging or a check is wrong, not
a model: nothing is generated. Run it after touching any of them, and before trusting a
benchmark on a new platform.

    make batfish-up
    PYTHONPATH=. uv run python scripts/check_references.py [scenario dirs...]
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from tests.fakes.render import render_good_configs

from fabloom.checks import allocate, compile_checks
from fabloom.config import REPO_ROOT, Settings
from fabloom.contracts import Snapshot
from fabloom.scenarios import load_scenarios
from fabloom.verify.batfish_local import BatfishVerifier, PybatfishAnswerer

DEFAULT_DIRS = [REPO_ROOT / "benchmark" / "v0.1", REPO_ROOT / "benchmark" / "vendors-v0.1"]


async def check(directory: Path, host: str) -> int:
    failures = 0
    for sc in load_scenarios(directory):
        fabric = allocate(sc.intent)
        checks = compile_checks(sc.intent, fabric).checks
        verifier = BatfishVerifier(lambda: PybatfishAnswerer(host), name="batfish-references")
        report = await verifier.verify(
            Snapshot(name=f"ref-{sc.id}", fabric=fabric),
            render_good_configs(sc.intent, fabric),
            checks,
        )
        failed = [c.id for c in report.failed_checks]
        status = "pass" if not failed else f"FAIL {len(failed)}/{len(checks)}: {failed[:4]}"
        print(f"{sc.id:<34} {sc.intent.platform:<12} {len(checks):>4} checks  {status}")
        failures += bool(failed)
    return failures


def main() -> None:
    dirs = [Path(a) for a in sys.argv[1:]] or DEFAULT_DIRS
    host = Settings().batfish_host
    failed = sum(asyncio.run(check(d, host)) for d in dirs)
    print(f"\n{'all references pass' if not failed else f'{failed} scenario(s) FAILED'}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
