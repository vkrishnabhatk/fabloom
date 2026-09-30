# ADR-0005: Model routing and prices live in conf/models.yaml

## Status

Accepted — 2026-09-30. Model IDs and prices unconfirmed until `spikes/nemotron_probe.py` runs.

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
  every price stays flagged until the probe records it.
- `fabloom doctor` lists `/v1/models`, checks every configured ID exists, and makes one small
  structured-output call per model. It runs nightly in CI.

## Alternatives considered

- **Hard-coded IDs with env overrides.** Scatters routing across code and deployment.
- **Router chooses by live latency/cost.** Premature; the benchmark first has to show which
  model does what well.
