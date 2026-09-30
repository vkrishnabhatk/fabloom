"""The four model-backed steps. Each call is budget-checked, costed and logged as an event."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from pydantic import BaseModel

from fabloom import metrics
from fabloom.agent.budget import BudgetTracker
from fabloom.agent.events import EventLog
from fabloom.config import ModelSpec
from fabloom.contracts import (
    CheckSet,
    FabricPlan,
    Intent,
    ModelRole,
    Platform,
    VerificationReport,
)
from fabloom.contracts.run import LlmCall
from fabloom.llm import ChatClient, Completion, LlmOutputError, ModelRouter
from fabloom.llm.parsing import parse_device_blocks
from fabloom.llm.prompts import (
    explainer_messages,
    generator_messages,
    planner_messages,
    repair_messages,
)

M = TypeVar("M", bound=BaseModel)


class LlmSteps:
    def __init__(
        self, client: ChatClient, router: ModelRouter, log: EventLog, budget: BudgetTracker
    ) -> None:
        self._client = client
        self._router = router
        self._log = log
        self._budget = budget

    def _account(self, role: ModelRole, c: Completion[Any]) -> None:
        self._budget.record_llm(c.prompt_tokens, c.completion_tokens, c.cost_usd)
        metrics.LLM_TOKENS.labels(c.model, "input").inc(c.prompt_tokens)
        metrics.LLM_TOKENS.labels(c.model, "output").inc(c.completion_tokens)
        metrics.LLM_COST.labels(c.model).inc(c.cost_usd)
        self._log.emit(
            LlmCall,
            role=role,
            model=c.model,
            prompt_tokens=c.prompt_tokens,
            completion_tokens=c.completion_tokens,
            cost_usd=c.cost_usd,
            duration_ms=c.duration_ms,
            redactions=c.redactions,
        )

    async def _json(self, role: ModelRole, messages: list[dict[str, str]], output: type[M]) -> M:
        """Structured call with one corrective retry if the output fails validation."""
        spec = self._router.spec(role)
        self._budget.check_llm(spec, messages)
        try:
            c = await self._client.complete_json(spec, messages, output)
        except LlmOutputError as exc:
            retry = [
                *messages,
                {"role": "user", "content": f"Your previous answer was invalid: {exc}. "
                 "Answer again with only a valid JSON object."},
            ]  # fmt: skip
            self._budget.check_llm(spec, retry)
            c = await self._client.complete_json(spec, retry, output)
        self._account(role, c)
        return c.value

    async def _text(self, role: ModelRole, spec: ModelSpec, messages: list[dict[str, str]]) -> str:
        self._budget.check_llm(spec, messages)
        c = await self._client.complete_text(spec, messages)
        self._account(role, c)
        return c.value

    async def plan(self, request: str, platform: Platform | None) -> Intent:
        intent = await self._json(
            ModelRole.PLANNER, planner_messages(request, platform or Platform.FRR), Intent
        )
        if platform is not None and intent.platform is not platform:
            intent = Intent.model_validate({**intent.model_dump(), "platform": platform})
        return intent

    async def generate(self, intent: Intent, fabric: FabricPlan) -> dict[str, str]:
        role = ModelRole.GENERATOR
        text = await self._text(role, self._router.spec(role), generator_messages(intent, fabric))
        return parse_device_blocks(text)

    async def repair(
        self,
        *,
        round_: int,
        intent: Intent,
        fabric: FabricPlan,
        configs: Mapping[str, str],
        report: VerificationReport,
        checkset: CheckSet,
    ) -> dict[str, str]:
        role = self._router.repairer(round_, report.failed_checks)
        messages = repair_messages(intent, fabric, configs, report, checkset)
        changed = parse_device_blocks(await self._text(role, self._router.spec(role), messages))
        return {**configs, **changed}

    async def explain(
        self,
        outcome: str,
        report: VerificationReport | None,
        checkset: CheckSet | None,
        repair_rounds: int,
    ) -> str:
        role = ModelRole.EXPLAINER
        messages = explainer_messages(outcome, report, checkset, repair_rounds)
        return await self._text(role, self._router.spec(role), messages)
