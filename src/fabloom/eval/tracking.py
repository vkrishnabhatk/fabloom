"""MLflow tracking for the benchmark: one MLflow run per (bench, arm).

The benchmark is the experiment (architecture §3). Params are what was run (model IDs,
temperatures, prompt hash, repair cap, verifier); metrics are the arm summary; the per-run
records and the report are artifacts. Tracking only: nothing is registered, because Fabloom
does not own the models it calls.

All MLflow calls go through this module, and ``mlflow`` is imported lazily, so the rest of
Fabloom (and ``pip install fabloom`` without the ``eval`` extra) never needs it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from fabloom.eval.results import BenchResults


class Tracker(Protocol):
    def log_bench(self, results: BenchResults, report: Path | None = None) -> int:
        """Log every arm of ``results``; return how many MLflow runs were created."""
        ...


class NullTracker:
    def log_bench(self, results: BenchResults, report: Path | None = None) -> int:
        return 0


def log_flat_params(params: Mapping[str, Any], prefix: str = "") -> dict[str, str]:
    """Flatten nested mappings into dotted keys: ``{"models": {"generator": x}}`` becomes
    ``{"models.generator": x}``. MLflow params are flat strings."""
    flat: dict[str, str] = {}
    for key, value in params.items():
        name = f"{prefix}{key}"
        if isinstance(value, Mapping):
            flat.update(log_flat_params(value, f"{name}."))
        else:
            flat[name] = str(value)
    return flat


def arm_params(results: BenchResults, arm: str) -> dict[str, str]:
    meta = results.meta
    return log_flat_params({
        "arm": arm,
        **meta.arms[arm].model_dump(),
        "benchmark": meta.benchmark,
        "n_scenarios": len(meta.scenarios),
        "verifier": meta.verifier,
        "prompt_hash": meta.prompt_hash,
        "prices_verified": meta.prices_verified,
    })  # fmt: skip


class MlflowTracker:
    def __init__(self, tracking_uri: str, artifact_root: Path) -> None:
        # MLflow 3 prints a hint for coding agents on import; it is noise in a CLI.
        os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")
        try:
            from mlflow.tracking import MlflowClient  # noqa: PLC0415 - optional extra
        except ImportError as exc:
            raise ImportError("MLflow tracking needs the eval extra: uv sync --extra eval") from exc
        self._client = MlflowClient(tracking_uri=tracking_uri)
        self._artifact_root = artifact_root

    def _experiment(self, name: str) -> str:
        found = self._client.get_experiment_by_name(name)
        if found is not None:
            return str(found.experiment_id)
        # Explicit artifact location: MLflow's default is ./mlruns relative to wherever the
        # process happens to run.
        location = (self._artifact_root / name).resolve().as_uri()
        return str(self._client.create_experiment(name, artifact_location=location))

    def log_bench(self, results: BenchResults, report: Path | None = None) -> int:
        from mlflow.entities import Metric, Param, RunTag  # noqa: PLC0415 - optional extra

        meta = results.meta
        experiment = self._experiment(f"fabloom-bench-{meta.benchmark}")
        created = 0
        for arm in results.arm_names:
            # Logging the same bench twice (a re-run report) must not duplicate MLflow runs.
            existing = self._client.search_runs(
                [experiment],
                filter_string=f"tags.bench_id = '{meta.bench_id}' and tags.arm = '{arm}'",
                max_results=1,
            )
            if existing:
                continue
            tags = {
                "bench_id": meta.bench_id,
                "arm": arm,
                "git_sha": meta.git_sha,
                "data.scenarios.sha256": meta.scenarios_key,
                "fingerprint": meta.fingerprint,
            }
            run = self._client.create_run(experiment, run_name=f"{arm} {meta.bench_id}", tags=tags)
            rid = run.info.run_id
            ts = int(meta.created.timestamp() * 1000)
            summary = results.summary(arm)
            self._client.log_batch(
                rid,
                metrics=[Metric(k, v, ts, 0) for k, v in sorted(summary.metrics().items())],
                params=[Param(k, v) for k, v in sorted(arm_params(results, arm).items())],
                tags=[RunTag("incomplete_runs", str(len(results.incomplete)))],
            )
            records = [r.model_dump(mode="json") for r in results.records_for(arm)]
            self._client.log_dict(rid, {"records": records}, "records.json")
            if report is not None:
                self._client.log_artifact(rid, str(report))
            self._client.set_terminated(rid)
            created += 1
        return created
