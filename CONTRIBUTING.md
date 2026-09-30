# Contributing to Fabloom

Thanks for helping. Fabloom is Apache-2.0 (code) and CC BY 4.0 (benchmark scenarios).

## Setup

```bash
make install        # uv sync --frozen --extra batfish, plus pre-commit hooks
make check          # what CI runs: ruff, mypy --strict, import-linter, tests + coverage gate
make batfish-up     # optional: a local Batfish for `fabloom generate`
```

Tests run with zero network and zero credits. Tests that need a Token Factory key or Nebius
Sandboxes are marked `llm` / `sandbox` and run nightly, not on pull requests.

## The one rule that is not negotiable

The model must not be able to influence what it is graded on (ADR-0001). Concretely:

- `verify/` never imports `llm/` or `agent/`, and they never import `verify/`;
- `checks/` is deterministic and depends only on `contracts/`.

`.importlinter` enforces this in CI. If your change needs to break a contract, that is an
architecture decision: open an issue and write an ADR first.

## Commits and pull requests

- [Conventional Commits](https://www.conventionalcommits.org/) with a scope, subject stating
  the finding rather than the file: `fix(verify): unnumbered sessions matched by interface`.
- Sign off every commit ([DCO](https://developercertificate.org/)): `git commit -s`.
- Numbers in docs say how they were measured. Negative results are welcome and get published.

## Architecture decisions

`docs/decisions/ADR-NNNN-kebab-slug.md`, with `## Status`, `## Context` (measured tables where
possible), `## Decision`, `## Alternatives considered`. Code cites ADR numbers where it
implements one.
