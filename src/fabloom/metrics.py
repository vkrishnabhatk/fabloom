"""Prometheus metrics. Module-level, ``fabloom_`` prefix, tiny label cardinality.

Bucket bounds checked against benchmarks v0.1 and vendors-v0.1: Batfish took 2 to 12 s per
candidate and runs used 0 to 3 repair rounds.
"""

from __future__ import annotations

from prometheus_client import Counter, Histogram

RUNS = Counter("fabloom_runs_total", "Finished runs", ["outcome"])
CHECKS = Counter("fabloom_checks_total", "Evaluated checks", ["kind", "passed"])
REPAIR_ROUNDS = Histogram(
    "fabloom_repair_rounds", "Repair rounds per finished run", buckets=(0, 1, 2, 3, 4, 5, 8)
)
LLM_TOKENS = Counter("fabloom_llm_tokens_total", "Tokens", ["model", "direction"])
LLM_COST = Counter("fabloom_llm_cost_usd_total", "Token Factory spend in USD", ["model"])
VERIFY_DURATION = Histogram(
    "fabloom_verify_duration_seconds",
    "Verifier wall time per candidate",
    ["backend"],
    buckets=(0.5, 1, 2, 5, 10, 20, 40, 80),
)
SANDBOX_EXECUTIONS = Counter("fabloom_sandbox_executions_total", "Verifier executions", ["result"])
BUDGET_EXHAUSTED = Counter("fabloom_budget_exhausted_total", "Runs stopped by a budget limit")
