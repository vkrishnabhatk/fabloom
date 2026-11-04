# ADR-0010: Nemotron 3.5 replaces the retired Nano and Super

## Status

Accepted — 2026-11-05. Changes the `nano` and `super` entries of ADR-0005's findings table;
the routing policy of ADR-0005 is unchanged.

## Context

Token Factory retires `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` and
`nvidia/nemotron-3-super-120b-a12b` from Serverless on 9 November 2026, in the middle of the
1–15 December judging window, and does not reroute requests
([notice](https://docs.tokenfactory.nebius.com/model-deprecations-november-2026.md)). Every
run starts with a planner call to Nano, so without a change every demo run would fail. The
recommended replacements are `nvidia/Nemotron-3_5-Lightning` (for Nano) and
`nvidia/Nemotron-3.5-Super-VL` (for Super). Ultra is not affected.

## Decision

- The `nano` slot runs **Lightning with thinking off**
  (`extra_body: {chat_template_kwargs: {enable_thinking: false}}`).
- The `super` slot runs **Super-VL with thinking on and `max_output_tokens: 32000`**.
- The slot names stay `nano` and `super`, so the benchmark arms, roles and tests keep their
  names; the IDs live in `conf/models.yaml` only, as ADR-0005 intends.
- `test_doctor` reads the IDs from the config instead of repeating them.

## Findings (2026-11-04)

All measured against the live Token Factory API on 4 November 2026, one run per arm and
scenario. Spend for everything below: about $2.60.

### Listing and probe

`GET /v1/models?verbose=true` and one structured call per model
(`spikes/results/migration_probe-20261104T193443Z.json`):

| Slot | Token Factory ID | $/M input | $/M output | Context |
|---|---|---|---|---|
| nano | `nvidia/Nemotron-3_5-Lightning` | 0.06 | 0.24 | 1M |
| super | `nvidia/Nemotron-3.5-Super-VL` | 0.30 | 0.90 | 1M |

- Same base URL and the same list prices as the models they replace. Both answer a strict
  `json_schema` call and advertise `tools` and `reasoning`.
- Super-VL advertises no sampling parameters, so its `temperature` is probably ignored.
- `reasoning_effort: low` did not shorten Lightning's reasoning; `enable_thinking: false`
  removed it.

### Planner accuracy

The benchmark grades against each scenario's ground-truth intent, so it cannot see a
planner that misreads a request. `spikes/planner_sweep.py` makes one planner call per
scenario (benchmark/v0.1 + vendors-v0.1, 74 scenarios) and compares the intent field by field
(`spikes/results/planner_sweep-20261104T200855Z.json`, `…T201506Z.json`). Latencies are
with 8 calls in flight.

| Planner | Exact intent | Median / max latency | Misses |
|---|---|---|---|
| Nano (retiring) | 65/74 | 12.5 s / 31 s | 7 × `leaf_asn_base` 65001 instead of 65101, 1 pool, 1 filter glob |
| **Lightning, thinking off** | **69/74** | **2.1 s / 6.6 s** | 2 × `unnumbered` for a request that says "/31"; 3 invalid intents (the agent retries those once) |
| Lightning, thinking on | 72/74 | 31 s / 139 s | 1 truncated at 12000 tokens, 1 filter glob |

Thinking on is three intents better but fifteen times slower, and its reasoning once ran past
the output cap. Off is better than the model it replaces on both counts. Its two `bgp_mode`
misses are the more serious kind: the user would get a valid unnumbered fabric they did not
ask for. The run page shows the planned intent, BGP mode included, before any config is
written, which is where a user can catch it.

### Super-VL: three settings on the vendor set

`fabloom bench run --scenarios-dir benchmark/vendors-v0.1 --arm super-repair --arm routed`,
Lightning planner as above, final pass after repair:

| Super-VL setting | Bench | `super-repair` | `routed` | $/scenario (sr / routed) |
|---|---|---|---|---|
| thinking on, 12000 cap | `20261104T202652Z-c2231e` | 18/25 | 20/25 | $0.0098 / $0.0103 |
| thinking off, 12000 cap | `20261104T204950Z-c2231e` | 18/25 (SONiC 0/5) | 23/25 | $0.0079 / $0.0162 |
| **thinking on, 32000 cap** | `20261104T210837Z-c2231e` | **25/25** | **25/25** | $0.0107 / $0.0109 |
| retiring Super (26 Oct) | `vendors-v0.1.md` | 23/25 | 25/25 | $0.0077 / $0.0081 |

- With a 12000 cap every 10-device `*-0005` scenario truncated. On vendor-nxos-0005 Super-VL
  spent 9,267 reasoning tokens and ~4,000 tokens of config (13,269 in all). With thinking off
  the same config took 4,001 tokens and passed pre-lint, but the repair loop then failed all
  five SONiC scenarios, and the routed arm paid for Ultra escalations instead.
- Only the winning setting has a committed report
  ([vendors-v0.1-nemotron35.md](../benchmark/vendors-v0.1-nemotron35.md)); the other two
  bench directories stay local, and their numbers are copied here from their reports.

### Benchmark v0.1 with the chosen settings

Bench `20261104T214121Z-f1348b`, report
[v0.1-nemotron35.md](../benchmark/v0.1-nemotron35.md); all four regression gates against
the frozen baseline passed.

| Arm | First-pass | Final pass | $/scenario | Wall p50 / p95 | Baseline (23 Oct) |
|---|---|---|---|---|---|
| `super-repair` | 44/49 | 49/49 | $0.0087 | 33 s / 180 s | 37 → 46/49, $0.0067 |
| `routed` | 41/49 | 49/49 | $0.0096 | 28 s / 88 s | 33 → 47/49, $0.0082 |

- The reports record commit `c5ffba1` because the config change was not yet committed; the
  routing they list (model IDs per role) is this ADR's. They do not record
  `max_output_tokens` or `extra_body`.
- One Super-VL call timed out four times in a row late in the run (Token Factory was slow
  overall at the time: p90 call latency 86 s against 26–40 s in the other runs). The harness
  stopped on the infrastructure error, as ADR-0007 intends, and the resumed run finished the
  remaining 7 runs.

## Consequences

- Better pass rates on both sets, at about 20–35% more per scenario (still about a cent).
  One run per arm and scenario: the 95% intervals overlap the old results, so this shows the
  migration did not regress, not that Super-VL is better.
- The client timeout is 180 s per attempt (`llm/client.py`). At ~140 tokens/s a response near
  the new 32000 cap would need about 230 s. The longest seen was 17,704 tokens in 128 s, so
  ordinary fabrics fit; a very large fabric on a slow day might not. Not changed here.
- The old benchmark reports stay as they are, dated, with the models they measured.

## Alternatives considered

- **Lightning with thinking on as planner.** More exact intents, but a minute-plus wait for
  planning in the demo and a truncation risk.
- **Super-VL with thinking off.** Cheapest first drafts, but it lost every SONiC repair.
- **Move generation to Ultra.** Ultra alone passed 20/25 vendor scenarios on 26 Oct at
  $0.0186 per scenario; not needed once Super-VL had room to think.
- **Dedicated Endpoints for the old models.** Keeps the old behaviour, but always-on paid
  capacity is out of scale for a demo that costs about a cent per run.
