"""Replay real Batfish recordings (scripts/record_batfish_fixtures.py), when present.

Known-good configs must pass every check against real answers. Until the week-0 spike records
fixtures, this module skips; the synthetic answers in tests/fakes/batfish.py stand in.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fabloom.checks import allocate, compile_checks
from fabloom.config import REPO_ROOT
from fabloom.scenarios import load_scenario
from fabloom.verify.batfish_local import run_checks
from fabloom.verify.recorded import RecordedAnswerer

pytestmark = pytest.mark.integration

FIXTURES = sorted((REPO_ROOT / "tests" / "fixtures" / "batfish").glob("*.json"))


@pytest.mark.skipif(not FIXTURES, reason="no recorded Batfish fixtures yet (week-0 spike)")
@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda p: p.stem)
def test_known_good_configs_pass_on_real_answers(fixture: Path) -> None:
    folder = "vendors-v0.1" if fixture.stem.startswith("vendor-") else "v0.1"
    sc = load_scenario(REPO_ROOT / "benchmark" / folder / f"{fixture.stem}.yaml")
    checks = compile_checks(sc.intent, allocate(sc.intent)).checks
    facts = run_checks(RecordedAnswerer.from_file(fixture), checks)
    assert [r.id for r in facts["checks"] if not r.passed] == []
