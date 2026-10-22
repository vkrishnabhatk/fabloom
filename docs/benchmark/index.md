# Benchmark

The v0.1 benchmark is 49 scenarios in 7 families (`benchmark/v0.1/`, CC BY 4.0), 23 of them
on Arista EOS. Each arm in `conf/bench.yaml` runs every scenario through the real loop
against the scenario's ground-truth intent, so arms differ only in who writes and repairs
the configs ([ADR-0007](../decisions/ADR-0007-benchmark-harness.md)).

| Arm | What it isolates |
|---|---|
| `nano-raw`, `super-raw`, `ultra-raw` | one model, one attempt, no repair |
| `super-repair` | the verify-and-repair loop; its first-pass rate is the verify-only result |
| `super-repair-grounding` | the same plus vendor-doc grounding: the only difference, so the grounding gate measures grounding alone |
| `routed` | the product's routing, escalating to Ultra on repeated failure |

Every number in a report is derived from run event logs. `fabloom bench report` rebuilds a
report from a bench directory without a model call. Pass rates carry 95% Wilson intervals,
percentiles are nearest-rank, and negative results are published.

!!! warning "No results yet"
    The harness has only run against scripted models and verifiers. The first real report
    will be generated into `docs/benchmark/v0.1.md` once Token Factory credits are available.
