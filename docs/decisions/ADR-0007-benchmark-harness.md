# ADR-0007: The benchmark replays event logs, gates on shared scenarios, tracks in MLflow

## Status

Accepted — 2026-10-02. Built and tested offline only: no arm has run against Token Factory
or a real Batfish yet, so nothing below is measured.

## Context

The ablation matrix is the submission's main evidence (architecture §8): it has to show what
the verify-and-repair loop adds over raw Nemotron, with every number traceable and negative
results published. It also has to catch a hosted model changing behaviour during the
1–15 December judging window, on a 10-scenario nightly that costs little. Credits are finite,
so a crashed bench must not pay again for runs that already finished.

## Decision

- **Arms are config.** `conf/bench.yaml` defines each arm as a routing override on
  `conf/models.yaml` plus a repair cap. Every arm passes the scenario's ground-truth intent to
  the run (`RunRequest.intent`), so the planner never decides what a run is graded on, and
  arms differ only in who writes and repairs configs. Benchmark runs skip the model explainer
  (`RunConfig.explain_with_model`), because nothing grades it.
- **No separate verify-only arm.** Its pass rate is exactly the repair arm's first-pass rate,
  which the report shows. The grounding arm is added with `llm/grounding.py`.
- **Records come from event logs.** `eval/results.py` derives each run's record from its
  `events.jsonl` (ADR-0004), never from in-memory state. `fabloom bench report` rebuilds
  every number, gate and report from a bench directory with no model call.
- **Resume, never re-pay.** A bench directory keeps each run under `runs/<arm>--<scenario>`.
  Running into it again reuses every run that has `run_finished` and deletes and re-runs any
  half-written log. `bench.json` holds a fingerprint of arms, models, prompts, scenarios and
  verifier; a run with a different fingerprint refuses to resume, so one report never mixes
  two configurations.
- **Gates compare like with like.** `eval/gates.py` compares each arm with the frozen
  baseline (`benchmark/baselines/<version>.json`) only on the scenarios both share. Without
  this, a 10-scenario nightly would be compared with a 60-scenario average. Regression gates
  accept only higher-is-better metrics. A gate with missing input is *skipped* and says why,
  and `all_passed` is false when no gate was evaluated. The report and `--check-gates` use
  the same gate list.
- **Small samples stay honest.** Pass rates in the report carry a 95% Wilson interval, and
  percentiles are nearest-rank (always an observed value). Costs are marked as estimates
  while any price the bench used is `price_verified: false`.
- **Tracking.** One MLflow run per (bench, arm), written through `eval/tracking.py` only,
  using a SQLite store (`mlflow.db`) and `mlruns/` artifacts. It is idempotent per
  bench and arm. The dependency is `mlflow-skinny` plus SQLAlchemy and Alembic, as the
  optional `eval` extra. Full `mlflow` would add pandas, scikit-learn, Flask and more, only to
  write `mlflow.db`. Skinny on its own has only the file store, which MLflow 3 has put in
  maintenance mode. To view results, run `uvx mlflow ui --backend-store-uri sqlite:///mlflow.db`.
- **Boundary.** `fabloom.eval` never imports `fabloom.verify`. Like the agent, it is handed
  a `Verifier` by the composition root (`cli.py`). Nothing below it imports it. Both rules
  are enforced in `.importlinter`.

## Alternatives considered

- **Records from `RunResult` in memory.** Simpler, but a crash loses them, resume becomes
  impossible, and the report could drift from what the event log says happened.
- **Regression against the baseline's overall pass rate.** A subset with different
  difficulty would trip or mask the gate by sampling alone.
- **A fresh bench directory per invocation.** Safe, but a crash at run 90 of 100 would pay
  for all 100 again.
- **Full `mlflow`.** Brings the UI in-process, at the cost of a dependency tree several times
  larger for a CLI that only writes runs.
