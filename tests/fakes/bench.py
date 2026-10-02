"""Fakes for benchmark tests: they answer by content, not by call order.

A bench interleaves many runs, so a script of answers in call order (``FakeChatClient``)
cannot say which run a call belongs to. These fakes find the scenario from the plan text in
the prompt and decide from the model ID, so the outcome of every (arm, scenario) is fixed no
matter how the runs are scheduled.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TypeVar

from pydantic import BaseModel

from fabloom.checks import allocate
from fabloom.config import ModelSpec
from fabloom.contracts import (
    Check,
    CheckResult,
    Intent,
    Platform,
    Snapshot,
    UnrecognizedLine,
    VerificationLevel,
    VerificationReport,
    score_checks,
)
from fabloom.llm import Completion
from fabloom.llm.prompts import render_plan
from tests.fakes.render import as_device_blocks, render_good_configs

M = TypeVar("M", bound=BaseModel)

BROKEN = "bgp invented-knob on"  # stands in for a command the model made up
# (model ID, "generate" | "repair", intent) -> write a broken candidate?
BreakRule = Callable[[str, str, Intent], bool]


@dataclass
class PlanAwareChatClient:
    """Answers generator and repair calls with known-good configs for the plan in the prompt,
    plus ``BROKEN`` in the first device when ``breaks`` says so. Never plans or explains."""

    intents: Sequence[Intent]
    breaks: BreakRule
    prompt_tokens: int = 1000
    completion_tokens: int = 500
    calls: list[tuple[str, str]] = field(default_factory=list)  # (model ID, kind)

    def __post_init__(self) -> None:
        self._plans = {
            render_plan(i, f): (i, render_good_configs(i, f))
            for i in self.intents
            for f in [allocate(i)]
        }

    async def complete_json(
        self, spec: ModelSpec, messages: list[dict[str, str]], output: type[M]
    ) -> Completion[M]:
        raise AssertionError(f"benchmark runs never call {spec.id} for structured output")

    async def complete_text(
        self, spec: ModelSpec, messages: list[dict[str, str]]
    ) -> Completion[str]:
        content = messages[-1]["content"]
        kind = "repair" if "Failed checks" in content else "generate"
        matches = [p for p in self._plans if p in content]
        assert matches, "prompt carries no known plan"
        intent, good = self._plans[max(matches, key=len)]
        configs = dict(good)
        if self.breaks(spec.id, kind, intent):
            first = next(iter(configs))
            configs[first] = configs[first].rstrip() + f"\n{BROKEN}\n"
        self.calls.append((spec.id, kind))
        return Completion(
            value=as_device_blocks(configs, intent.platform),
            model=spec.id,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            cost_usd=spec.cost_usd(self.prompt_tokens, self.completion_tokens),
            duration_ms=5,
            redactions=0,
        )


class MarkerVerifier:
    """Fails the first check, and reports an unrecognised line, for every device whose config
    contains ``BROKEN``; passes everything else."""

    name = "marker"
    level = VerificationLevel.L2
    platforms = frozenset(Platform)

    def __init__(self) -> None:
        self.calls = 0

    async def verify(
        self, snapshot: Snapshot, configs: Mapping[str, str], checks: Sequence[Check]
    ) -> VerificationReport:
        self.calls += 1
        broken = sorted(d for d, text in configs.items() if BROKEN in text)
        results = [
            CheckResult(id=c.id, kind=c.kind, passed=not (broken and i == 0), severity=c.severity)
            for i, c in enumerate(checks)
        ]
        return VerificationReport(
            level=self.level,
            checks=results,
            unrecognized_lines=[
                UnrecognizedLine(device=d, line_number=None, text=BROKEN) for d in broken
            ],
            score=score_checks(results),
            duration_ms=1,
            verifier_name=self.name,
            verifier_version="test",
        )
