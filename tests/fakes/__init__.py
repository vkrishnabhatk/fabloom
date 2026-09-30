"""Test doubles: everything runs with zero network and zero credits."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel

from fabloom.config import ModelSpec
from fabloom.contracts import (
    Check,
    CheckResult,
    Platform,
    Snapshot,
    VerificationLevel,
    VerificationReport,
    score_checks,
)
from fabloom.llm import Completion

M = TypeVar("M", bound=BaseModel)


class FakeVerifier:
    """Scriptable pass/fail sequence. ``script[i]`` is the number of checks that fail on the
    i-th call (0 = pass). The last entry repeats."""

    level = VerificationLevel.L2
    platforms = frozenset({Platform.FRR, Platform.ARISTA_EOS})

    def __init__(self, script: Sequence[int], name: str = "fake") -> None:
        self.script = list(script)
        self.name = name
        self.calls: list[dict[str, str]] = []

    async def verify(
        self, snapshot: Snapshot, configs: Mapping[str, str], checks: Sequence[Check]
    ) -> VerificationReport:
        failing = self.script[min(len(self.calls), len(self.script) - 1)]
        self.calls.append(dict(configs))
        results = [
            CheckResult(
                id=c.id,
                kind=c.kind,
                passed=i >= failing,
                severity=c.severity,
                evidence={} if i >= failing else {"fake": "scripted failure"},
            )
            for i, c in enumerate(checks)
        ]
        return VerificationReport(
            level=self.level,
            checks=results,
            score=score_checks(results),
            duration_ms=1,
            verifier_name=self.name,
            verifier_version="test",
        )


@dataclass
class FakeChatClient:
    """Answers calls in order from ``script``. An ``Exception`` entry is raised instead."""

    script: list[Any]
    prompt_tokens: int = 1000
    completion_tokens: int = 500
    calls: list[tuple[str, list[dict[str, str]]]] = field(default_factory=list)

    def _next(self, spec: ModelSpec, messages: list[dict[str, str]]) -> Any:
        self.calls.append((spec.id, messages))
        if not self.script:
            raise AssertionError(f"unexpected extra model call to {spec.id}")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def _completion(self, spec: ModelSpec, value: Any) -> Completion[Any]:
        return Completion(
            value=value,
            model=spec.id,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            cost_usd=spec.cost_usd(self.prompt_tokens, self.completion_tokens),
            duration_ms=5,
            redactions=0,
        )

    async def complete_json(
        self, spec: ModelSpec, messages: list[dict[str, str]], output: type[M]
    ) -> Completion[M]:
        value = self._next(spec, messages)
        return self._completion(spec, output.model_validate(value))

    async def complete_text(
        self, spec: ModelSpec, messages: list[dict[str, str]]
    ) -> Completion[str]:
        value = self._next(spec, messages)
        assert isinstance(value, str)
        return self._completion(spec, value)
