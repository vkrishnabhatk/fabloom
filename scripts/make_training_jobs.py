"""Regenerate docker/verifier/training/*.json: the jobs the verifier image's AOT bake replays.

One job per FRR/EOS mode, rendered with the known-good test renderer, so the training run
exercises those parsers and every question fabloom asks. The image backs the deferred
Sandboxes backend (ADR-0003), so later platforms are not added yet. Re-run after changing
the check compiler.

    PYTHONPATH=. uv run python scripts/make_training_jobs.py
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.fakes.render import render_good_configs

from fabloom.checks import allocate, compile_checks
from fabloom.config import REPO_ROOT
from fabloom.contracts import Snapshot
from fabloom.scenarios import load_scenario

SCENARIOS = ["leaf-spine-ebgp-0002", "leaf-spine-unnumbered-0001", "leaf-spine-filters-0002"]
OUT = REPO_ROOT / "docker" / "verifier" / "training"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for sid in SCENARIOS:
        sc = load_scenario(REPO_ROOT / "benchmark" / "v0.1" / f"{sid}.yaml")
        fabric = allocate(sc.intent)
        job = {
            "snapshot": Snapshot(name=f"training-{sid}", fabric=fabric).model_dump(mode="json"),
            "configs": render_good_configs(sc.intent, fabric),
            "checks": [c.model_dump(mode="json") for c in compile_checks(sc.intent, fabric).checks],
        }
        path: Path = OUT / f"{sid}.json"
        path.write_text(json.dumps(job, indent=1) + "\n", encoding="utf-8")
        print(f"wrote {path.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
