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

## v0.1 results (23 October 2026)

The full generated report is [v0.1.md](v0.1.md). It comes from the event logs of 294 runs
(49 scenarios × 6 arms, one run each) against live Nemotron models on Token Factory and
Batfish 2026.08.27 running locally. Total spend was $2.04 at list prices.

| Arm | Final pass | $/scenario |
|---|---|---|
| `nano-raw` | 10/49 (20%) | $0.0020 |
| `super-raw` | 33/49 (67%) | $0.0039 |
| `ultra-raw` | 47/49 (96%) | $0.0127 |
| `super-repair` | 46/49 (94%) | $0.0067 |
| `super-repair-grounding` | 42/49 (86%) | $0.0081 |
| `routed` (the product) | 47/49 (96%) | $0.0082 |

### What the numbers show

*This section is a hand-written reading of the generated report.*

- **The verify-and-repair loop is what makes Super usable.** In the `super-repair` arm, 37
  of 49 first candidates passed; after Batfish-guided repair, 46 did (+18 points on the
  same runs). Comparing with `super-raw` instead (67%) mixes in run-to-run variation: the
  two arms' first candidates come from identical prompts, yet passed at 67% and 76%.
- **The routed product matches Ultra at about two-thirds of the cost.** Both reached 47/49.
  Routed spent $0.0082 per scenario against Ultra's $0.0127, because Ultra is called only
  when a repair has already failed. Super plus repair alone reached 46/49 at $0.0067.
- **Doc grounding did not help, so it stays off.** The grounding gate failed: 42/49 against
  46/49 for the same arm without grounding. The gap is within run-to-run noise, but there
  is no sign of a gain either. The arm made only one Tavily search, because every
  unrecognised line was the same `exit-address-family`, and that line is where Batfish
  *notices* the error, not where it *is* (see below). Grounding searched for the wrong
  thing.
- **Nano is not a config generator.** It passed 10/49. Twelve runs ran out of output tokens
  while reasoning (about 5,000 reasoning tokens for about 600 tokens of config on one
  probe). In the product Nano only plans and explains.
- **One error dominates Super's failures.** All 187 rejected `exit-address-family` lines
  came from `neighbor … remote-as` written inside `address-family ipv4 unicast` instead
  of directly under `router bgp`. BGP still comes up, but the config is malformed, and the
  repair hint points at the closing line rather than the misplaced ones. A deterministic
  pre-lint rule for this is the obvious next improvement.

### How far to trust them

- **The checker was validated against real Batfish before these numbers were kept.** The
  known-good reference config of every one of the 49 scenarios passes every check. Getting
  there found and fixed four checker bugs: FRR files were not parsed at all, reachability
  counted intended drops as failures, FRR's `exit` was flagged as unrecognised, and the
  reference ACL over-blocked transit traffic.
- **One run per arm and scenario.** The 95% Wilson intervals in the report are wide (for
  example 86% to 99% for 47/49). Differences of a few scenarios between arms are not
  evidence by themselves.
- **Costs are tokens × list price** (confirmed against Token Factory, ADR-0005), not an
  invoice, and exclude Tavily credits.
- **Static verification only (L2).** Batfish checks parse, BGP, routes, reachability and
  filters. Nothing was booted. FRR packet filters cannot be verified at all (ADR-0006).
- A connection outage during the first attempt was discarded rather than counted, and the
  bench resumed from the runs that had finished (ADR-0007).
