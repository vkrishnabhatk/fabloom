# Benchmark

Two scenario sets, both CC BY 4.0: the frozen v0.1 benchmark, 49 scenarios in 7 families
on FRR and Arista EOS (`benchmark/v0.1/`, 23 on EOS), and the vendor set, 5 scenarios each on
Junos, NX-OS, IOS-XE, IOS-XR and SONiC (`benchmark/vendors-v0.1/`). Each arm in `conf/bench.yaml` runs every scenario through the real loop
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

## Nemotron 3.5 results (4 November 2026)

Token Factory retires Nemotron 3 Nano and Super on 9 November 2026. Both sets were re-run with
their replacements, Nemotron 3.5 Lightning (planner, explainer, thinking off) and Super-VL
(generator, repairer, thinking on, 32,000-token output cap), on the `super-repair` and
`routed` arms only ([ADR-0010](../decisions/ADR-0010-nemotron-3.5-migration.md)). Generated
reports: [v0.1-nemotron35.md](v0.1-nemotron35.md) and
[vendors-v0.1-nemotron35.md](vendors-v0.1-nemotron35.md). Same Batfish, prompts and
scenario sets as below.

| Arm | v0.1 first → final | Vendors first → final | $/scenario (v0.1 / vendors) | Nemotron 3, final (v0.1 / vendors) |
|---|---|---|---|---|
| `super-repair` | 44 → 49/49 | 19 → 25/25 | $0.0087 / $0.0107 | 46/49 / 23/25 |
| `routed` | 41 → 49/49 | 20 → 25/25 | $0.0096 / $0.0109 | 47/49 / 25/25 |

### What the numbers show

*Hand-written reading of the two reports and the runs that led to them.*

- **No regression; every gate against the frozen v0.1 baseline passed.** Both arms passed
  every scenario on both sets, and first-pass rates rose (super-repair 76% → 90% on v0.1).
- **About 20–35% more per scenario**, still about one cent: Super-VL writes more reasoning
  tokens than Super did, at the same list price.
- **Super-VL needs room to think.** With the old 12,000-token cap it truncated every
  10-device vendor fabric (routed 20/25): about 9,300 reasoning tokens plus 4,000 of config.
  With thinking off the configs fit, but `super-repair` failed all five SONiC scenarios and
  `routed` reached 23/25 by paying for Ultra. Both negative runs are in ADR-0010.
- **The planner is now measured too.** The benchmark grades against ground-truth intent, so
  `spikes/planner_sweep.py` checks the planner separately: Lightning with thinking off read
  69 of 74 requests exactly in ~2 s, against 65 of 74 in ~12 s for Nano. Two of its misses
  chose BGP unnumbered for a request that said "/31", an error this benchmark cannot see.

### How far to trust them

- One run per arm and scenario. 49/49 has a 95% interval of 93–100% and 46/49 one of
  83–98%: the overlap means "did not get worse", not "got better".
- The reports name commit `c5ffba1`; the routing change was uncommitted at the time. The
  routing they list per arm is the one in `conf/models.yaml` from v0.2.4.
- One call timed out four times during a slow spell on Token Factory; the harness stopped on
  the infrastructure error and the resumed bench re-ran only the unfinished runs.

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

## Vendor results (26 October 2026)

The generated report is [vendors-v0.1.md](vendors-v0.1.md): 25 scenarios, 5 each on Juniper
Junos, Cisco NX-OS, IOS-XE, IOS-XR and SONiC, through the same loop with the same grading,
4 arms, one run each, $0.98 in total.

| Arm | Final pass | $/scenario |
|---|---|---|
| Nemotron Super, one attempt | 15/25 (60%) | $0.0047 |
| Nemotron Ultra, one attempt | 20/25 (80%) | $0.0186 |
| Super + Batfish repair loop | 23/25 (92%) | $0.0077 |
| **Fabloom routed** | **25/25 (100%)** | **$0.0081** |

### What the numbers show

- **The loop matters more on unfamiliar platforms.** On v0.1 (FRR, EOS) routed matched
  Ultra; here it beats Ultra alone by five scenarios at under half its cost.
- **NX-OS is where single attempts fail.** Super alone got 1/5 and Ultra 2/5; with repair,
  4/5 and 5/5. Every platform reached 5/5 routed.
- **The prompts were written before these runs and not tuned on them.** The scenarios were
  generated from five templates per platform; the only fix made after a live run was in the
  checker (Junos pre-lint), before the benchmark started.

### How far to trust them

- **Five scenarios per platform** is a small sample: the 95% interval for 25/25 is 87% to
  100%, and per-platform differences of one scenario are noise.
- **The checker was validated first.** Every vendor reference config passes the pre-lint
  gate and every check in live Batfish (`scripts/check_references.py`); doing that found
  that Junos needs `local-address` and that filter checks must ignore non-initial
  fragments ([ADR-0009](../decisions/ADR-0009-more-platforms.md)).
- **Numbered eBGP only on the new platforms**, and SONiC filters are not verified.
