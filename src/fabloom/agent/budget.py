"""``RunBudget`` enforcement, in exactly one place.

Checked before every model call and every verifier execution. This, not HTTP rate limiting,
is what protects the credits: a run can never start a call it cannot afford at the model's
maximum output length.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from fabloom.config import ModelSpec
from fabloom.contracts import RunBudget, Usage

CHARS_PER_TOKEN_ESTIMATE = 3  # deliberately pessimistic for config text


class BudgetExceededError(Exception):
    def __init__(self, limit: str, detail: str) -> None:
        super().__init__(f"{limit}: {detail}")
        self.limit = limit
        self.detail = detail


def estimate_tokens(messages: list[dict[str, str]]) -> int:
    return sum(len(m["content"]) for m in messages) // CHARS_PER_TOKEN_ESTIMATE + 16


class BudgetTracker:
    def __init__(self, budget: RunBudget, clock: Callable[[], float] = time.monotonic) -> None:
        self.budget = budget
        self.usage = Usage()
        self._clock = clock
        self._start = clock()
        self._reserved_verifications = 0

    @property
    def elapsed_s(self) -> float:
        return self._clock() - self._start

    def _check_wall_clock(self) -> None:
        if self.elapsed_s > self.budget.max_wall_clock_s:
            raise BudgetExceededError(
                "max_wall_clock_s",
                f"{self.elapsed_s:.0f}s elapsed of {self.budget.max_wall_clock_s:.0f}s",
            )

    def check_llm(self, spec: ModelSpec, messages: list[dict[str, str]]) -> None:
        self._check_wall_clock()
        prompt = estimate_tokens(messages)
        projected_tokens = self.usage.total_tokens + prompt + spec.max_output_tokens
        if projected_tokens > self.budget.max_tokens:
            raise BudgetExceededError(
                "max_tokens",
                f"next call could reach {projected_tokens} tokens of {self.budget.max_tokens}",
            )
        projected_usd = self.usage.cost_usd + spec.cost_usd(prompt, spec.max_output_tokens)
        if projected_usd > self.budget.max_usd:
            raise BudgetExceededError(
                "max_usd",
                f"next call could reach ${projected_usd:.4f} of ${self.budget.max_usd:.4f}",
            )

    def record_llm(self, prompt_tokens: int, completion_tokens: int, cost_usd: float) -> None:
        self.usage.prompt_tokens += prompt_tokens
        self.usage.completion_tokens += completion_tokens
        self.usage.cost_usd += cost_usd
        self.usage.llm_calls += 1

    def reserve_verification(self) -> None:
        """Claim a verifier execution before starting it, so parallel candidates can't
        collectively overshoot the limit."""
        self._check_wall_clock()
        if self.usage.verifications + self._reserved_verifications >= self.budget.max_verifications:
            raise BudgetExceededError(
                "max_verifications", f"all {self.budget.max_verifications} executions used"
            )
        self._reserved_verifications += 1

    def settle_verification(self, *, executed: bool) -> None:
        self._reserved_verifications -= 1
        if executed:
            self.usage.verifications += 1

    def record_cache_hit(self) -> None:
        self.usage.verification_cache_hits += 1
