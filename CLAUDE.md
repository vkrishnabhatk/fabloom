# Fabloom: notes for Claude Code

Read `fabloom-architecture.html` (engineering design) and `fabloom-plan.html` (product plan)
before structural changes. Decisions live in `docs/decisions/`.

## Commands

- `make check`: ruff, mypy --strict (src + tests), import-linter, tests with the 80% coverage gate
- `make test`: offline tests only (`-m "unit or integration"`); never needs network or credits
- `uv run fabloom plan benchmark/v0.1/<id>.yaml`: deterministic plan + checks, no model
- `uv run fabloom bench run --dry-run`: benchmark arms x scenarios and worst-case spend, no model

## Rules

- **Verification independence (ADR-0001).** `verify/` never imports `llm/` or `agent/`, and
  they never import `verify/`. The agent receives a `Verifier` (`contracts/verifier.py`)
  from the composition root (`cli.py`). `checks/` depends only on `contracts/`. Never add an
  `ignore_imports` to `.importlinter` to make a change pass.
- Checks are compiled from the intent (`checks/compile.py`); nothing a model outputs may
  alter a check or its evaluation.
- Every model call and every verifier execution goes through `agent/budget.py`.
- `eval/` never imports `verify/`; benchmark numbers are derived from run event logs only
  (ADR-0007).
- Model IDs and prices live in `conf/models.yaml`, never in code (ADR-0005).
- Tests must pass offline: use `tests/fakes` (FakeVerifier, FakeChatClient, synthetic Batfish
  answers) and `httpx2.MockTransport` for the Token Factory client. openai 3.x uses httpx2,
  so respx cannot intercept it.
- mypy is strict on tests too: annotate every test function.

## Style

Conventional Commits with scopes; subjects state the finding. Comments explain *why*.
Docs: numbers say how they were measured; negative results are reported, not hidden.
