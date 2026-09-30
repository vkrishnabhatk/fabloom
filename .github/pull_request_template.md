## What and why

<!-- The finding, not the file list. Link the issue or ADR. -->

## Evidence

<!-- Tests added, benchmark numbers (with how they were measured), or "none: docs only". -->

## Checklist

- [ ] `make check` passes (ruff, mypy --strict, import-linter, tests, coverage)
- [ ] No change lets `llm/` or `agent/` reach `verify/`, or lets a model influence `checks/` (ADR-0001)
- [ ] An architecture change comes with an ADR in `docs/decisions/`
- [ ] Commits are signed off (`git commit -s`, DCO)
