"""Token Factory client: OpenAI-compatible API, strict JSON output, retries, cost accounting.

Every outbound message is scrubbed here, the single choke point before anything leaves the
process. Refusals and truncations are detected before parsing, so they surface as typed
errors rather than as confusing validation failures.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Generic, Protocol, TypeVar

import httpx2
import openai
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion, ChatCompletionMessageParam
from pydantic import BaseModel, ValidationError
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

from fabloom.config import ModelSpec
from fabloom.llm.parsing import strip_reasoning
from fabloom.llm.schema import strict_json_schema
from fabloom.scrub import scrub

T = TypeVar("T")
M = TypeVar("M", bound=BaseModel)

RETRYABLE = (
    openai.APIConnectionError,
    openai.APITimeoutError,
    openai.RateLimitError,
    openai.InternalServerError,
)


class LlmError(RuntimeError):
    pass


class LlmRefusalError(LlmError):
    """The model declined to answer. Nebius docs warn this can happen; never parse it."""


class LlmOutputError(LlmError):
    """The model answered, but not in the shape asked for."""


@dataclass(frozen=True)
class Completion(Generic[T]):
    value: T
    model: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    duration_ms: int
    redactions: int


class ChatClient(Protocol):
    async def complete_json(
        self, spec: ModelSpec, messages: list[dict[str, str]], output: type[M]
    ) -> Completion[M]: ...

    async def complete_text(
        self, spec: ModelSpec, messages: list[dict[str, str]]
    ) -> Completion[str]: ...


def scrub_messages(messages: list[dict[str, str]]) -> tuple[list[dict[str, str]], int]:
    total = 0
    out: list[dict[str, str]] = []
    for msg in messages:
        result = scrub(msg["content"])
        total += result.redactions
        out.append({**msg, "content": result.text})
    return out, total


class TokenFactoryClient:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        *,
        http_client: httpx2.AsyncClient | None = None,
        max_attempts: int = 4,
        max_backoff_s: float = 20.0,
        timeout_s: float = 180.0,
    ) -> None:
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            max_retries=0,  # tenacity owns retries; two retry layers multiply latency
            timeout=timeout_s,
            http_client=http_client,
        )
        self._max_attempts = max_attempts
        self._max_backoff_s = max_backoff_s

    async def _create(
        self, spec: ModelSpec, messages: list[dict[str, str]], **kwargs: Any
    ) -> tuple[ChatCompletion, int, int]:
        clean, redactions = scrub_messages(messages)
        params: list[ChatCompletionMessageParam] = clean  # type: ignore[assignment]
        start = time.perf_counter()
        async for attempt in AsyncRetrying(
            retry=retry_if_exception_type(RETRYABLE),
            wait=wait_random_exponential(multiplier=0.5, max=self._max_backoff_s),
            stop=stop_after_attempt(self._max_attempts),
            reraise=True,
        ):
            with attempt:
                resp = await self._client.chat.completions.create(
                    model=spec.id,
                    messages=params,
                    temperature=spec.temperature,
                    max_tokens=spec.max_output_tokens,
                    extra_body=spec.extra_body or None,
                    **kwargs,
                )
        return resp, redactions, int((time.perf_counter() - start) * 1000)

    @staticmethod
    def _content(resp: ChatCompletion) -> str:
        if not resp.choices:
            raise LlmOutputError("response has no choices")
        choice = resp.choices[0]
        refusal = getattr(choice.message, "refusal", None)
        if refusal or choice.finish_reason == "content_filter":
            raise LlmRefusalError(refusal or "content filtered")
        if choice.finish_reason == "length":
            raise LlmOutputError("output truncated at max_output_tokens")
        return strip_reasoning(choice.message.content or "")

    @staticmethod
    def _completion(
        spec: ModelSpec, resp: ChatCompletion, value: T, redactions: int, duration_ms: int
    ) -> Completion[T]:
        pt = resp.usage.prompt_tokens if resp.usage else 0
        ct = resp.usage.completion_tokens if resp.usage else 0
        return Completion(
            value=value,
            model=spec.id,
            prompt_tokens=pt,
            completion_tokens=ct,
            cost_usd=spec.cost_usd(pt, ct),
            duration_ms=duration_ms,
            redactions=redactions,
        )

    async def complete_json(
        self, spec: ModelSpec, messages: list[dict[str, str]], output: type[M]
    ) -> Completion[M]:
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": output.__name__,
                "schema": strict_json_schema(output),
                "strict": True,
            },
        }
        resp, redactions, ms = await self._create(spec, messages, response_format=response_format)
        content = self._content(resp)
        try:
            value = output.model_validate_json(content)
        except ValidationError as exc:
            raise LlmOutputError(f"{output.__name__} did not validate: {exc}") from exc
        return self._completion(spec, resp, value, redactions, ms)

    async def complete_text(
        self, spec: ModelSpec, messages: list[dict[str, str]]
    ) -> Completion[str]:
        resp, redactions, ms = await self._create(spec, messages)
        return self._completion(spec, resp, self._content(resp), redactions, ms)

    # --- doctor ---------------------------------------------------------------------------

    async def list_models(self) -> set[str]:
        page = await self._client.models.list()
        return {m.id for m in page.data}

    async def probe(self, spec: ModelSpec) -> str | None:
        """One small structured completion. Returns an error string, or None if healthy."""

        class Probe(BaseModel):
            ok: bool

        probe_spec = spec.model_copy(update={"max_output_tokens": 256, "temperature": 0.0})
        messages = [
            {"role": "system", "content": 'Reply with the JSON object {"ok": true} only.'},
            {"role": "user", "content": json.dumps({"ok": True})},
        ]
        try:
            await self.complete_json(probe_spec, messages, Probe)
        except (LlmError, openai.OpenAIError) as exc:
            return f"{type(exc).__name__}: {exc}"
        return None
