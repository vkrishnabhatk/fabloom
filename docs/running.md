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
- **Daily spend cap** (`FABLOOM_WEB_SPEND_CAP_USD`, default $0.40, UTC day). Each run
  reserves its worst-case budget (`FABLOOM_WEB_RUN_MAX_USD`, default $0.05) before it
  starts, so concurrent runs can't overshoot. On restart, the
  day's spend is read back from the run logs. At the cap, the app switches to read-only and
  serves the gallery (`gallery/`) instead of erroring.

`docker compose up -d --build` runs the deployment stack: Caddy (TLS, set `FABLOOM_DOMAIN`),
then the app, then a pinned Batfish.

## Deploying

The demo at [fabloom.dev](https://fabloom.dev) runs on one CPU VM on Nebius AI Cloud
(2 vCPU, 8 GiB, Ubuntu 24.04, 30 GiB disk), about $1.60 a day. Any Linux host with Docker
works the same way:

1. Open ports 22, 80 and 443 only. Batfish and the app are not published; Caddy is the only
   door.
2. Point an `A` record at the host. Leave any CDN proxy off: the rate limit keys on the
   visitor's address, and a proxy that cuts idle connections breaks the live event stream.
3. Copy the repository and create `.env` next to `docker-compose.yml`, readable only by the
   deploying user:

   ```bash
   FABLOOM_DOMAIN="example.org, www.example.org"
   FABLOOM_TOKEN_FACTORY_API_KEY=...   # a key for this deployment only, so it can be revoked alone
   ```

4. `docker compose build --build-arg GIT_SHA=$(git rev-parse --short HEAD) app && docker compose up -d`.
   Caddy obtains the certificates on first start; `/info` shows the deployed commit.

Without the key the same stack serves the gallery read-only, which is how to keep a demo up
at no model cost.

## Configuration

| File | Holds |
|---|---|
| `conf/models.yaml` | model IDs, prices, role routing, escalation ([ADR-0005](decisions/ADR-0005-model-routing-in-config.md)) |
| `conf/fabloom.yaml` | run budget, repair candidates, doc grounding ([ADR-0008](decisions/ADR-0008-doc-grounded-repair.md)) |
| `conf/bench.yaml` | benchmark arms, gates, nightly subset ([ADR-0007](decisions/ADR-0007-benchmark-harness.md)) |
