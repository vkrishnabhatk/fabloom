# Running Fabloom

## Install

```bash
make install        # uv sync --frozen with the batfish, eval and web extras, plus pre-commit
make batfish-up     # local Batfish on :9996 (Docker)
```

Secrets are read from the environment or a `.env` file, never from YAML:

| Variable | Needed for |
|---|---|
| `FABLOOM_TOKEN_FACTORY_API_KEY` (or `NEBIUS_API_KEY`) | any model call: `generate`, `doctor`, `bench run`, a live web demo |
| `FABLOOM_TAVILY_API_KEY` (or `TAVILY_API_KEY`) | doc-grounded repair only |

A blank value counts as unset.

## Without a model (no key, no credits)

```bash
uv run fabloom plan benchmark/v0.1/leaf-spine-ebgp-0004.yaml   # fabric plan and compiled checks
uv run fabloom scenarios validate                               # every scenario compiles
uv run fabloom bench run --dry-run                              # the matrix and its worst-case spend
uv run fabloom serve                                            # web demo, read-only gallery
make check                                                      # lint, types, contracts, tests
```

## With a model

```bash
uv run fabloom doctor     # every configured Nemotron model exists and answers JSON
uv run fabloom generate "2 spines, 4 leaves, eBGP underlay, one private ASN per leaf" --platform frr
```

The run directory holds `events.jsonl`, `result.json`, `report.json` and the best
candidate's `configs/`.

## Web demo

`fabloom serve` serves a JSON API (`/api/v1/runs`, `/api/v1/runs/{id}/events` as
server-sent events, `/api/v1/gallery`), operations endpoints (`/healthz`, `/readyz`,
`/info`, `/metrics`) and two pages built on the same JSON. Interactive API docs are at
`/api/docs`.

Two guards protect the credits:

- **Per-visitor rate limit.** A token bucket per IP (`FABLOOM_WEB_RUNS_PER_IP`, refilling
  one run every `FABLOOM_WEB_REFILL_S`). `X-Forwarded-For` is trusted only with
  `FABLOOM_WEB_TRUST_PROXY=true`, behind your own proxy.
- **Daily spend cap** (`FABLOOM_WEB_SPEND_CAP_USD`, UTC day). Each run reserves its
  worst-case budget before it starts, so concurrent runs can't overshoot. On restart, the
  day's spend is read back from the run logs. At the cap, the app switches to read-only and
  serves the gallery (`gallery/`) instead of erroring.

`docker compose up -d --build` runs the deployment stack: Caddy (TLS, set `FABLOOM_DOMAIN`),
then the app, then a pinned Batfish.

## Configuration

| File | Holds |
|---|---|
| `conf/models.yaml` | model IDs, prices, role routing, escalation ([ADR-0005](decisions/ADR-0005-model-routing-in-config.md)) |
| `conf/fabloom.yaml` | run budget, repair candidates, doc grounding ([ADR-0008](decisions/ADR-0008-doc-grounded-repair.md)) |
| `conf/bench.yaml` | benchmark arms, gates, nightly subset ([ADR-0007](decisions/ADR-0007-benchmark-harness.md)) |
