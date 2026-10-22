# ADR-0005: Model routing and prices live in conf/models.yaml

## Status

Accepted — 2026-09-30. IDs and prices confirmed against Token Factory on 2026-10-22 (see
Findings).

## Context

Week-0 findings (which Nemotron IDs exist on Token Factory, what they cost, whether Ultra is
available) must not require code changes. A model retired during the 1–15 December judging
window must be detected before a judge detects it.

## Decision

- `conf/models.yaml` maps roles (planner, generator, repairer, repairer_escalated, explainer)
  to models, each with ID, prices, max output tokens, temperature and optional `extra_body`
  (for example, reasoning toggles). The router refuses to start if a role is missing or
  points to an undefined model.
- Escalation happens only on repeated failure: from round 3 for anything, or from round 2
  if a critical (parse) failure survived a repair.
- Every price carries `price_verified`. Unconfirmed prices are set pessimistically, so the
  budget guard over-estimates spend rather than under-estimates it. A test asserts that
  every configured price is verified, so a newly added model fails CI until it is checked.
- `fabloom doctor` lists `/v1/models`, checks every configured ID exists, and makes one small
  structured-output call per model. It runs nightly in CI.

## Findings (2026-10-22)

Measured with `fabloom doctor`, `spikes/nemotron_probe.py --generate leaf-spine-ebgp-0002`
(result in `spikes/results/nemotron_probe-20261022T214707Z.json`) and
`GET /v1/models?verbose=true`, which returns per-token pricing for each model.

| Role model | Token Factory ID | $/M input | $/M output | Context |
|---|---|---|---|---|
| nano | `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` | 0.06 | 0.24 | 262k |
| super | `nvidia/nemotron-3-super-120b-a12b` | 0.30 | 0.90 | 262k |
| ultra | `nvidia/Nemotron-3-Ultra-550b-a55b` | 1.00 | 3.00 | 1M |
| lightning | `nvidia/Nemotron-3_5-Lightning` | 0.06 | 0.24 | 1M |

- **The configured Nano ID was wrong.** `nvidia/nvidia-nemotron-3-nano-30b-a3b` is not
  listed; IDs are case-sensitive. Every planner and `nano-raw` call would have failed.
  `fabloom doctor` caught it on the first run against the live API.
- Super's, Ultra's and Nano's planned prices were exact. Lightning's placeholder was high
  ($0.10/$0.40), which is the direction the policy above intends.
- Every model answered a strict `json_schema` call. All four advertise `tools` and
  `reasoning`.
- Not routed, noted only: `nvidia/Nemotron-3.5-Super-VL` is also offered.
- The first real Super generation (4 devices, FRR, numbered eBGP) took 852 prompt + 2,753
  completion tokens, 11.3 s, $0.0027, and passed the deterministic pre-lint. That is one
  sample, not a pass rate; Batfish did not run.
- Nano's `max_output_tokens` raised from 4096 to 12000, matching Super. Nano advertises no
  separate output cap, and 4096 would have truncated about 9 of 49 v0.1 scenarios in
  `nano-raw`, measuring the cap instead of the model.

## Alternatives considered

- **Hard-coded IDs with env overrides.** Scatters routing across code and deployment.
- **Router chooses by live latency/cost.** Premature; the benchmark first has to show which
  model does what well.
