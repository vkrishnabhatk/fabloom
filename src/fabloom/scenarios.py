"""Load benchmark scenarios from ``benchmark/<version>/*.yaml``."""

from __future__ import annotations

from pathlib import Path

import yaml

from fabloom.config import REPO_ROOT
from fabloom.contracts import Scenario

DEFAULT_BENCHMARK_DIR = REPO_ROOT / "benchmark" / "v0.1"
# Five scenarios per platform added after v0.1, measured separately (ADR-0009).
VENDOR_BENCHMARK_DIR = REPO_ROOT / "benchmark" / "vendors-v0.1"


def load_scenario(path: Path) -> Scenario:
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    scenario = Scenario.model_validate(data)
    if scenario.id != path.stem:
        raise ValueError(f"{path.name}: id {scenario.id!r} must match the file name")
    return scenario


def load_scenarios(directory: Path = DEFAULT_BENCHMARK_DIR) -> list[Scenario]:
    scenarios = [load_scenario(p) for p in sorted(directory.glob("*.yaml"))]
    ids = [s.id for s in scenarios]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"duplicate scenario ids: {sorted(dupes)}")
    return scenarios
