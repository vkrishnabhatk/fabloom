# ADR-0001: Verification independence, enforced as plane boundaries

## Status

Accepted — 2026-09-30

## Context

Fabloom's claim is that configs are *verified*, not that a model says they are fine. That
claim is only as strong as the separation between the code that proposes configs (models,
the repair loop) and the code that grades them (the check compiler, the verifiers). A
convention in a README rots; an import that quietly lets the repair loop tweak a check, or
lets a verifier call a model to "interpret" a result, would hollow the product out while
every test still passed.

The architecture document states the rule twice: "the agent and llm packages cannot import
the verify package" and "verify/ never imports llm/ or agent/". Read together, the boundary
runs both ways.

## Decision

Six planes under `src/fabloom/`, with boundaries enforced by import-linter in the CI lint job:

| Contract | Rule |
|---|---|
| verification-independence | `verify` and `llm`/`agent` are mutually independent (neither imports the other) |
| contracts-are-the-bottom | `contracts` imports nothing else in fabloom |
| checks-are-deterministic | `checks` depends only on `contracts` |
| scrub-is-standalone | `scrub` depends on nothing else in fabloom |
| only-the-cli-composes | no plane imports `cli` |

Consequences for the code:

- The `Verifier` protocol lives in `contracts/verifier.py`, so the agent can hold a verifier
  without importing `verify`. Concrete backends are wired at the composition root (`cli.py`,
  later `web/`).
- Pre-lint lives in `verify/` and reaches the agent only as `PrelintGate`, a `Verifier`
  wrapper. A gated candidate's report says `backend_executed=False`, which is what the run
  budget counts.
- Checks are compiled by `checks/compile.py` from the intent, before any model output
  exists, and passed to `Verifier.verify()` explicitly.

The contracts were tested by planting violations in both directions
(`agent → verify.prelint`, `verify → llm.prompts`); import-linter reported both.

## Alternatives considered

- **One-way rule only (`verify` must not import `llm`/`agent`).** Simpler, and it covers the
  worst case. Rejected because the agent could then call `verify` internals directly (for
  example re-scoring with a relaxed check), which the architecture's first statement forbids.
- **Separate distributions per plane.** Stronger isolation, much more packaging overhead for a
  single-maintainer project. Revisit if the verifier image ever needs to ship without the
  rest of the code.
