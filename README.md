# Fabloom

[![CI](https://github.com/vkrishnabhatk/fabloom/actions/workflows/ci.yml/badge.svg)](https://github.com/vkrishnabhatk/fabloom/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/vkrishnabhatk/fabloom)](https://github.com/vkrishnabhatk/fabloom/releases)
[![Licence: Apache-2.0](https://img.shields.io/badge/licence-Apache--2.0-blue)](LICENSE)

Describe a data center fabric in plain language and get switch configs that NVIDIA Nemotron
writes on Nebius Token Factory and **Batfish verifies**. Failed checks go back to the model
until every check passes, or you get an honest report of what could not be fixed.

**Live demo: [fabloom.dev](https://fabloom.dev).** Type a request, watch the run, read the
verified configs. A run costs about one cent in model calls.

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

- **The model cannot influence what it is graded on.** Checks are compiled from the intent
  by plain code; the verifier and the model side cannot import each other, and CI enforces
  it ([ADR-0001](docs/decisions/ADR-0001-verification-independence.md)).
- **Every step is an event** in the run's log. The CLI, the web page's live timeline and the
  benchmark all read that same log.
- **Every report states how it was checked**: L2 means statically verified with Batfish
  (parse, BGP sessions, routes, reachability, filters). Nothing is booted.

## Results

Every result below is graded by Batfish against checks compiled from each scenario's
ground-truth intent, one run per arm and scenario, at Token Factory list prices.

**Benchmark v0.1** (FRR and Arista EOS, 49 scenarios, 23 Oct 2026):

| Arm | Final pass | $/scenario |
|---|---|---|
| Nemotron Nano, one attempt | 10/49 (20%) | $0.0020 |
| Nemotron Super, one attempt | 33/49 (67%) | $0.0039 |
| Nemotron Ultra, one attempt | 47/49 (96%) | $0.0127 |
| Super + Batfish repair loop | 46/49 (94%) | $0.0067 |
| Super + repair + Tavily doc grounding | 42/49 (86%) | $0.0081 |
| **Fabloom routed** (Super, escalating to Ultra) | **47/49 (96%)** | **$0.0082** |

**Vendor benchmark** (5 scenarios per platform, 26 Oct 2026):

| Arm | Junos | NX-OS | IOS-XE | IOS-XR | SONiC | Final pass | $/scenario |
|---|---|---|---|---|---|---|---|
| Nemotron Super, one attempt | 4/5 | 1/5 | 4/5 | 4/5 | 2/5 | 15/25 (60%) | $0.0047 |
| Nemotron Ultra, one attempt | 4/5 | 2/5 | 5/5 | 4/5 | 5/5 | 20/25 (80%) | $0.0186 |
| Super + Batfish repair loop | 5/5 | 4/5 | 4/5 | 5/5 | 5/5 | 23/25 (92%) | $0.0077 |
| **Fabloom routed** | **5/5** | **5/5** | **5/5** | **5/5** | **5/5** | **25/25 (100%)** | **$0.0081** |

The routed product matches Ultra alone on v0.1 at about two-thirds of its cost, and beats it
on the vendor set at under half its cost. The repair loop matters most where a model is
weakest: Super alone got 1 of 5 NX-OS fabrics right. Doc grounding during repair showed no
gain, so it is off. With one run each the intervals are wide; read the reports
([v0.1](docs/benchmark/v0.1.md), [vendors](docs/benchmark/vendors-v0.1.md)) and the
[analysis](docs/benchmark/index.md) before comparing arms closely.

## Platforms

| Platform | eBGP numbered | BGP unnumbered | Traffic filters verified |
|---|---|---|---|
| Juniper Junos | yes | no | yes |
| Cisco NX-OS, IOS-XE, IOS-XR | yes | no | yes |
| Arista EOS | yes | no | yes |
| SONiC | yes | no | not yet (ACL tables) |
| FRR | yes | yes | no (`frr.conf` has no packet filter) |

Leaf-spine underlays only. Requests for systems Batfish cannot parse (Nokia, Huawei, Cumulus
NVUE and others) are refused before any model call, because an unverifiable config would be
a guess ([ADR-0009](docs/decisions/ADR-0009-more-platforms.md)). Unverifiable parts of a
request, such as a filter on FRR, are reported as *not verifiable*, never as passed.

## Built on Nebius

| Piece | Where it runs |
|---|---|
| All model calls: Nemotron 3 Nano, Super and Ultra | [Nebius Token Factory](https://tokenfactory.nebius.com), OpenAI-compatible API; model IDs, prices and routing in `conf/models.yaml` |
| The live demo: Caddy, the app and Batfish | One CPU VM on Nebius AI Cloud with `docker compose` |
| Vendor documentation links | [Tavily](https://tavily.com), searching only the vendors' own documentation sites |

A run makes three model calls when the first draft passes (Nano plans, Super writes, Nano
explains) and one more per repair round, up to three, with Ultra taking over a repair that
Super could not finish. Where Token Factory made the difference:

- **Three Nemotron sizes behind one endpoint**, so each step gets the model it needs and
  routing is configuration, not code. The benchmarks measured that choice.
- **Strict JSON-schema output** for the planner, so its intent is parsed, never scraped.
- **Model IDs and prices from the API**; `fabloom doctor` checks every configured model
  nightly, so a retired ID is caught before a visitor finds it.
- **Cheap enough to measure honestly**: both benchmarks, 394 runs through the real loop,
  cost about $3.

## Quick start

```bash
make install                     # locked environment + pre-commit hooks
cp .env.example .env             # add FABLOOM_TOKEN_FACTORY_API_KEY
make batfish-up                  # local Batfish on :9996 (Docker)

uv run fabloom plan benchmark/v0.1/leaf-spine-ebgp-0004.yaml     # plan + checks, no model
uv run fabloom generate "Cisco Nexus, 2 spines and 4 leaves, eBGP numbered, advertise loopbacks"
uv run fabloom serve             # web demo on :8000
docker compose up -d --build     # Caddy + app + Batfish, as on the demo VM
```

Without a key, everything except model runs still works and the web demo serves its recorded
runs read-only. Configuration, the benchmark commands and deployment are in
[docs/running.md](docs/running.md).

## Engineering

- `make check`: ruff, strict mypy, import contracts and 245 offline tests (96% coverage);
  nothing in CI needs a key or a network. CI also scans dependencies, secrets and images.
- **The checker is checked first.** `scripts/check_references.py` runs a known-good config
  for all 74 scenarios through the pre-lint gate and live Batfish; recorded Batfish answers
  for 10 of them are replayed in CI. Doing this found six bugs in the checks and their
  reference configs that synthetic test data had hidden.
- **The benchmark** runs arms × scenarios through the product's own loop, is resumable,
  discards runs hit by provider outages, derives every number from run logs and tracks
  them in MLflow ([ADR-0007](docs/decisions/ADR-0007-benchmark-harness.md)).
- **Every model call, Batfish run and search is budgeted**; failed and truncated calls are
  billed too. The public demo adds a per-visitor rate limit and a daily spend cap.

## Limitations

- Static verification (L2) only; generated fabrics are not booted on a real network OS.
- Leaf-spine eBGP underlays only; no overlays (EVPN-VXLAN) yet.
- Benchmark samples are small (one run per arm and scenario, five scenarios per new
  platform); differences of a scenario or two are noise.
- Reported spend is tokens × the list prices in `conf/models.yaml`, confirmed against Token
  Factory on 22 Oct 2026, not an invoice.

## Roadmap

- **A Fabloom model**: a small model fine-tuned for this task and benchmarked on the same 74
  scenarios against Nemotron, for a local mode where configs never leave the network.
- **Routed by verification**: the small model writes first; when Batfish rejects its
  configs, a larger model repairs them. The verifier decides when to escalate, as it does
  from Super to Ultra today.
- **Any provider**: per-model endpoints for any OpenAI-compatible server (vLLM, Ollama,
  Hugging Face) and a bring-your-own-key option for hosted models such as Claude or GPT, with
  the key used for one run and never stored.
- **Deeper platform coverage**: SONiC ACL tables, BGP unnumbered beyond FRR, EVPN-VXLAN
  overlays, each added only once Batfish verifies it.
- **Runtime verification (L3)**: boot the fabric in containerlab, check sessions and traffic
  automatically, and offer the topology as a downloadable lab.
- **Batfish in Nebius Sandboxes**, to verify repair candidates in parallel
  ([ADR-0003](docs/decisions/ADR-0003-sandbox-checkpoint-ladder.md)).

## Privacy and safety

Fabloom proposes; you deploy. It never connects to or pushes anything to a device. Input is
scrubbed of credentials before it leaves the process and is never used as training data.
See [SECURITY.md](SECURITY.md).

## Documentation

- [docs/running.md](docs/running.md): install, configuration, benchmark, web demo, deployment
- [docs/benchmark/](docs/benchmark/index.md): reports and their analysis
- [docs/decisions/](docs/decisions/): architecture decision records
- [CONTRIBUTING.md](CONTRIBUTING.md) · [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) · [SECURITY.md](SECURITY.md)

## Licence

Code: [Apache-2.0](LICENSE). Benchmark scenarios: [CC BY 4.0](benchmark/LICENSE).
