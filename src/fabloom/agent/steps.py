"""The four model-backed steps. Each call is budget-checked, costed and logged as an event."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from pydantic import BaseModel

from fabloom import metrics
from fabloom.agent.budget import BudgetTracker
from fabloom.agent.events import EventLog
from fabloom.config import DocsConfig, ModelSpec
from fabloom.contracts import (
    CheckSet,
    FabricPlan,
    Intent,
    ModelRole,
    Platform,
    VerificationReport,
)
from fabloom.contracts.run import DocsFound, GroundingLookup, LlmCall
from fabloom.llm import ChatClient, Completion, LlmError, LlmOutputError, ModelRouter
from fabloom.llm.grounding import Excerpt, Grounder, best_links
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
        self,
        client: ChatClient,
        router: ModelRouter,
        log: EventLog,
        budget: BudgetTracker,
        grounder: Grounder | None = None,
        *,
        docs: tuple[Grounder, DocsConfig] | None = None,
    ) -> None:
        self._client = client
        self._router = router
        self._log = log
        self._budget = budget
        self._grounder = grounder
        self._docs = docs

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

    def _account_failure(self, role: ModelRole, exc: LlmError) -> None:
        if exc.spent is not None:
            self._account(role, exc.spent)

    async def _json(self, role: ModelRole, messages: list[dict[str, str]], output: type[M]) -> M:
        """Structured call with one corrective retry if the output fails validation."""
        spec = self._router.spec(role)
        self._budget.check_llm(spec, messages)
        try:
            c = await self._client.complete_json(spec, messages, output)
        except LlmOutputError as exc:
            self._account_failure(role, exc)
            retry = [
                *messages,
                {"role": "user", "content": f"Your previous answer was invalid: {exc}. "
                 "Answer again with only a valid JSON object."},
            ]  # fmt: skip
            self._budget.check_llm(spec, retry)
            try:
                c = await self._client.complete_json(spec, retry, output)
            except LlmError as retry_exc:
                self._account_failure(role, retry_exc)
                raise
        self._account(role, c)
        return c.value

    async def _text(self, role: ModelRole, spec: ModelSpec, messages: list[dict[str, str]]) -> str:
        self._budget.check_llm(spec, messages)
        try:
            c = await self._client.complete_text(spec, messages)
        except LlmError as exc:
            self._account_failure(role, exc)
            raise
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
        references = await self._ground(round_, fabric.platform, report)
        messages = repair_messages(intent, fabric, configs, report, checkset, references=references)
        changed = parse_device_blocks(await self._text(role, self._router.spec(role), messages))
        return {**configs, **changed}

    async def _ground(
        self, round_: int, platform: Platform, report: VerificationReport
    ) -> list[Excerpt]:
        if self._grounder is None or not report.unrecognized_lines:
            return []
        g = await self._grounder.lookup(
            platform, report.unrecognized_lines, self._budget.grounding_allowance()
        )
        self._budget.record_grounding(g.searched)
        self._log.emit(
            GroundingLookup,
            round=round_,
            queries=g.queries,
            searched=g.searched,
            excerpts=len(g.excerpts),
            urls=sorted({x.url for x in g.excerpts}),
            error=g.error,
        )
        return g.excerpts

    async def find_docs(self, intent: Intent) -> None:
        """Vendor doc links for the reader of the result; searches count against the run's
        grounding budget like the repairer's."""
        if self._docs is None:
            return
        grounder, config = self._docs
        allowance = min(config.max_queries, self._budget.grounding_allowance())
        g = await grounder.docs(intent, allowance, config.cache_ttl_days * 86400)
        self._budget.record_grounding(g.searched)
        # https only: these become hrefs on the result page.
        links = best_links(g.excerpts, config.max_links)
        self._log.emit(
            DocsFound,
            queries=g.queries,
            searched=g.searched,
            docs=[{"title": x.title or x.url, "url": x.url, "query": x.query} for x in links],
            error=g.error,
        )

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
