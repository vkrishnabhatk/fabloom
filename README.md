# Fabloom

Describe a data center fabric in plain language; get FRR or Arista EOS configs that NVIDIA
Nemotron generates on Nebius Token Factory, **Batfish verifies**, and the model repairs until
every check passes, or an honest report of what could not be fixed.

**Live demo: [fabloom.dev](https://fabloom.dev)**. Type a request, watch the run, read the
verified configs. The recorded runs cost between $0.002 and $0.013 each in model calls.

## Results

v0.1 benchmark, 23 October 2026: 49 scenarios × 6 arms against live Nemotron models on
Token Factory, graded by Batfish against checks compiled from each scenario's intent.
The generated report is [docs/benchmark/v0.1.md](docs/benchmark/v0.1.md) and the reading of it is in
[docs/benchmark/index.md](docs/benchmark/index.md).

| Arm | Final pass | $/scenario |
|---|---|---|
| Nemotron Nano, one attempt | 10/49 (20%) | $0.0020 |
| Nemotron Super, one attempt | 33/49 (67%) | $0.0039 |
| Nemotron Ultra, one attempt | 47/49 (96%) | $0.0127 |
| Super + Batfish repair loop | 46/49 (94%) | $0.0067 |
| Super + repair + Tavily doc grounding | 42/49 (86%) | $0.0081 |
| **Fabloom routed** (Super, escalating to Ultra) | **47/49 (96%)** | **$0.0082** |

The repair loop lifts Super from 76% to 94% on the same runs. The routed product matches
Ultra at about two-thirds of its cost. Doc grounding showed no gain and stays off. One run
per arm and scenario, so the intervals are wide: see the report.

## The one rule

**The model cannot influence what it is graded on.** Checks are compiled deterministically
from the intent (`src/fabloom/checks/`); the verifier (`src/fabloom/verify/`) and the model
side (`llm/`, `agent/`) cannot import each other. import-linter enforces this in CI
([ADR-0001](docs/decisions/ADR-0001-verification-independence.md)).

## How it works

```
request ──► planner (Nemotron Nano) ──► intent JSON
            deterministic allocator  ──► fabric plan (addresses, ASNs, sessions)
            deterministic compiler   ──► checks
            generator (Nemotron Super) ──► per-device configs
            pre-lint gate ──► Batfish ──► report ──┐
            repairer (Super, escalating to Ultra) ◄┘ until pass or budget/rounds exhausted
            explainer (Nano) ──► plain-language summary
```

Every step is an event in `runs/<run_id>/events.jsonl`. The CLI's `--watch`, the web page's
live timeline and the benchmark all read that same log.

Each report states its verification level: **L2**, statically verified with Batfish, is
what this release targets. L0 means nothing could be checked.

## Built on Nebius

| Piece | Where it runs |
|---|---|
| All model calls: Nemotron 3 Nano, Super and Ultra | [Nebius Token Factory](https://tokenfactory.nebius.com), OpenAI-compatible API. Model IDs, prices and routing in `conf/models.yaml` |
| The live demo: Caddy, the app and Batfish | One CPU VM on Nebius AI Cloud (2 vCPU, 8 GiB) with `docker compose` |

A run makes three model calls when the first draft passes (Nano plans, Super writes, Nano
explains) and one more per repair round, up to three: Super repairs, and Ultra takes over a
repair that Super could not finish. Each call's tokens and cost are in
the run log; reported spend is tokens × the list prices in `conf/models.yaml`.

## Quick start

```bash
make install                     # uv sync --frozen with the batfish, eval and web extras + pre-commit
cp .env.example .env             # add FABLOOM_TOKEN_FACTORY_API_KEY
make batfish-up                  # local Batfish on :9996

uv run fabloom plan benchmark/v0.1/leaf-spine-ebgp-0004.yaml    # no model, no network
uv run fabloom generate "2 spines, 4 leaves, eBGP underlay, one private ASN per leaf" --platform frr
uv run fabloom doctor            # every configured Nemotron model exists and answers JSON

uv run fabloom bench run --dry-run                 # the arm x scenario matrix and its worst-case spend
uv run fabloom bench run --arm super-repair        # runs, reports, gates, logs to MLflow
uv run fabloom bench report runs/bench/<id> --report docs/benchmark/v0.1.md   # no model

uv run fabloom serve             # web demo on :8000; read-only gallery without a key
docker compose up -d --build     # Caddy + app + Batfish, as on the demo VM
```

Everything except `generate`, `doctor` and `bench run` works without a key. The web demo
then serves the recorded gallery read-only. More in [docs/running.md](docs/running.md).

## What is built and tested

`make check` runs lint, strict types, the import contracts and 213 offline tests (97%
coverage). Nothing in CI needs a key or a network.

| Piece | State |
|---|---|
| Deterministic fabric plan + check compiler | 49 v0.1 scenarios in 7 families (23 on Arista EOS) compile; allocator property-tested (no overlaps, order-independent, never renumbers) |
| Run state machine, budget, event log | Tested with scripted models and verifiers; golden path end to end |
| Token Factory client | Strict JSON output, retries, refusal and truncation detection, cost accounting (failed calls are billed too), secret scrubbing |
| Batfish verifier | Against live Batfish 2026.08.27, the known-good reference config of every one of the 49 scenarios passes every check. Recordings of 5 scenarios are replayed in CI |
| Benchmark harness | Arms × scenarios through the real loop, resumable, aborts cleanly on provider outages; records derived from event logs only; MLflow tracking; regression and grounding gates; generated report |
| Doc-grounded repair | Tavily search of vendor docs for unrecognised lines, cached, rate-limited and capped per run. Off: its benchmark arm failed the grounding gate ([ADR-0008](docs/decisions/ADR-0008-doc-grounded-repair.md)) |
| Web demo | Live at [fabloom.dev](https://fabloom.dev): JSON API ([/api/docs](https://fabloom.dev/api/docs)), live event stream, per-visitor rate limit, $0.40 daily spend cap, read-only gallery when there is no key or credit |
| Nebius Sandboxes backend | Not built. Spike script ready (`spikes/batfish_in_sandbox.py`) |

## Fabloom proposes; you deploy

Output is a proposal for a network engineer to review. Fabloom never connects to or pushes
anything to a device. Batfish checks what it can model; anything it cannot is reported as
*not verifiable*, never as passed.

## Known limitations

- **Leaf-spine eBGP underlays only**: FRR and Arista EOS numbered, FRR unnumbered. The EOS
  generator rules have no unnumbered peering yet, so no EOS scenario uses it.
- **FRR traffic filters cannot be verified.** `frr.conf` has no packet filter and Batfish
  does not model iptables, so "block SSH" on FRR is reported as *not verifiable* rather than
  passed. Filter scenarios use Arista EOS ([ADR-0006](docs/decisions/ADR-0006-deterministic-fabric-plan.md)).
- **Doc grounding is off.** Measured on v0.1, it showed no gain (42/49 against 46/49
  without it, within noise). It searches for the line Batfish flags, which is often not the
  line that is wrong ([ADR-0008](docs/decisions/ADR-0008-doc-grounded-repair.md)).
- **Costs use list prices.** Model IDs and per-token prices in `conf/models.yaml` were
  confirmed against Token Factory on 22 Oct 2026 ([ADR-0005](docs/decisions/ADR-0005-model-routing-in-config.md)).
  Reported spend is tokens × those prices, not an invoice.
- **One run per arm and scenario** in v0.1, so small differences between arms are noise.
- **Checking the checker found four bugs.** Running every reference config against live
  Batfish showed that FRR configs were not parsed at all (a missing `# ports.conf --`
  section marker), that reachability counted traffic the intent blocks as a failure, that
  FRR's own top-level `exit` lines tripped Batfish's grammar, and that a reference ACL
  over-blocked transit traffic. All four are fixed and covered by tests; synthetic answers
  had hidden every one.

## Roadmap

- **Downloadable containerlab lab**: a ready-to-run topology with the generated configs, so
  a run can be booted on the real network OS. Today the run page offers the configs as a
  `.zip`; the lab comes with the runtime verifier below.
- **L3 runtime verification**: boot the fabric in containerlab and check sessions and
  traffic automatically, on top of Batfish's static checks.
- **Batfish in Nebius Sandboxes**, to verify repair candidates in parallel ([ADR-0003](docs/decisions/ADR-0003-sandbox-checkpoint-ladder.md)).
- **A Fabloom model**: a small model fine-tuned for this task, benchmarked on the same 49
  scenarios against Nemotron, for a local mode where configs never leave the network.

## Privacy

Input is scrubbed of credentials before it leaves the process and is never used as training
data. See [SECURITY.md](SECURITY.md).

## Project

- [docs/running.md](docs/running.md): install, configuration, web demo, deployment
- [docs/decisions/](docs/decisions/): architecture decision records
- [CONTRIBUTING.md](CONTRIBUTING.md) · [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) · [SECURITY.md](SECURITY.md)

Fabloom started as an entry to the Nebius × NVIDIA Global AI Hackathon (October 2026) and
continues as an open-source project.

## Licence

Code: [Apache-2.0](LICENSE). Benchmark scenarios: [CC BY 4.0](benchmark/LICENSE).
