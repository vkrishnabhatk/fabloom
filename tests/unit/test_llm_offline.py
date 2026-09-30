"""Parsing, strict schema, routing, prompts, and the Token Factory client over a mock transport."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx2
import pytest
from pydantic import BaseModel

from fabloom.config import ModelsConfig, ModelSpec
from fabloom.contracts import (
    CheckKind,
    CheckResult,
    CheckSet,
    FabricPlan,
    Intent,
    ModelRole,
    Severity,
    VerificationLevel,
    VerificationReport,
)
from fabloom.llm import LlmOutputError, LlmRefusalError, ModelRouter, TokenFactoryClient
from fabloom.llm.parsing import parse_device_blocks, strip_reasoning
from fabloom.llm.prompts import (
    explainer_messages,
    generator_messages,
    planner_messages,
    prompt_hash,
    repair_messages,
)
from fabloom.llm.schema import strict_json_schema

pytestmark = pytest.mark.unit

BASE_URL = "https://tf.test/v1/"


# --- parsing ---------------------------------------------------------------------------


def test_parse_device_blocks() -> None:
    text = """<think>let me plan</think>Here you go:
<device name="leaf1" platform="frr">
```
hostname leaf1
```
</device>
<device name="leaf2">hostname leaf2</device>
<device name="leaf1" platform="frr">duplicate ignored</device>"""
    blocks = parse_device_blocks(text)
    assert blocks == {"leaf1": "hostname leaf1\n", "leaf2": "hostname leaf2\n"}


def test_parse_empty_block_and_nothing() -> None:
    assert parse_device_blocks('<device name="x"></device>') == {"x": ""}
    assert parse_device_blocks("no blocks at all") == {}


def test_strip_reasoning() -> None:
    assert strip_reasoning("<THINK>a\nb</THINK> answer ") == "answer"


# --- strict schema ---------------------------------------------------------------------


def test_intent_schema_is_strict() -> None:
    schema = strict_json_schema(Intent)
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    blob = json.dumps(schema)
    assert '"default"' not in blob
    assert '"format"' not in blob
    for sub in schema["$defs"].values():
        if "properties" in sub:
            assert sub["additionalProperties"] is False
            assert set(sub["required"]) == set(sub["properties"])


def test_property_named_title_survives() -> None:
    class Doc(BaseModel):
        title: str
        format: int

    schema = strict_json_schema(Doc)
    assert set(schema["properties"]) == {"title", "format"}


# --- router ----------------------------------------------------------------------------


def _failed(severity: Severity) -> list[CheckResult]:
    return [CheckResult(id="x", kind=CheckKind.PARSE_CLEAN, passed=False, severity=severity)]


def test_router_escalates_only_on_repeated_failure(router: ModelRouter) -> None:
    assert router.repairer(1, _failed(Severity.CRITICAL)) is ModelRole.REPAIRER
    assert router.repairer(2, _failed(Severity.HIGH)) is ModelRole.REPAIRER
    assert router.repairer(2, _failed(Severity.CRITICAL)) is ModelRole.REPAIRER_ESCALATED
    assert router.repairer(3, _failed(Severity.LOW)) is ModelRole.REPAIRER_ESCALATED
    assert "Ultra" in router.spec(ModelRole.REPAIRER_ESCALATED).id


def test_router_rejects_incomplete_config(models: ModelsConfig) -> None:
    roles: dict[ModelRole, str] = {
        k: v for k, v in models.roles.items() if k is not ModelRole.EXPLAINER
    }
    with pytest.raises(ValueError, match="no model for roles"):
        ModelRouter(models.model_copy(update={"roles": roles}))
    roles = {**models.roles, ModelRole.EXPLAINER: "ghost"}
    with pytest.raises(ValueError, match="undefined models"):
        ModelRouter(models.model_copy(update={"roles": roles}))


def test_cost_accounting(models: ModelsConfig) -> None:
    spec = models.models["super"]
    assert spec.cost_usd(1_000_000, 1_000_000) == pytest.approx(1.20)


# --- prompts ---------------------------------------------------------------------------


def _report(checkset: CheckSet) -> VerificationReport:
    results = [
        CheckResult(
            id=c.id,
            kind=c.kind,
            passed=i > 0,
            severity=c.severity,
            evidence={} if i else {"status": "PARTIALLY_UNRECOGNIZED"},
            remediation_hint=None if i else "fix it",
        )
        for i, c in enumerate(checkset.checks)
    ]
    return VerificationReport(
        level=VerificationLevel.L2,
        checks=results,
        score=0.5,
        duration_ms=1,
        verifier_name="t",
        verifier_version="t",
    )


def test_prompts_carry_plan_and_failures(
    frr_intent: Intent, frr_fabric: FabricPlan, frr_checkset: CheckSet
) -> None:
    plan = planner_messages("2 spines 2 leaves", frr_intent.platform)
    assert '"additionalProperties": false' in plan[0]["content"]
    gen = generator_messages(frr_intent, frr_fabric)
    assert "leaf1: role=leaf asn=65101 loopback=10.0.1.1/32" in gen[1]["content"]
    assert '<device name="DEVICE" platform="frr">' in gen[0]["content"]
    rep = repair_messages(
        frr_intent, frr_fabric, {"leaf1": "hostname leaf1"}, _report(frr_checkset), frr_checkset
    )
    assert "parse_clean:spine1" in rep[1]["content"]
    assert "hint: fix it" in rep[1]["content"]
    assert '<device name="leaf1" platform="frr">' in rep[1]["content"]
    exp = explainer_messages("exhausted", _report(frr_checkset), frr_checkset, 2)
    assert "statically verified with Batfish" in exp[1]["content"]


def test_prompt_hash_is_stable() -> None:
    assert prompt_hash() == prompt_hash()
    assert len(prompt_hash()) == 16


# --- client ----------------------------------------------------------------------------


def _chat(content: str | None, finish: str = "stop", refusal: str | None = None) -> dict[str, Any]:
    return {
        "id": "c1",
        "object": "chat.completion",
        "created": 0,
        "model": "m",
        "choices": [
            {
                "index": 0,
                "finish_reason": finish,
                "message": {"role": "assistant", "content": content, "refusal": refusal},
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
    }


Handler = Callable[[httpx2.Request], httpx2.Response]


def _client(handler: Handler) -> TokenFactoryClient:
    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return TokenFactoryClient("k", BASE_URL, http_client=http, max_backoff_s=0.0)


class Answer(BaseModel):
    ok: bool


SPEC = ModelSpec(id="nvidia/test", usd_per_m_input=1.0, usd_per_m_output=2.0)


async def test_json_completion_sends_strict_schema_and_scrubs() -> None:
    seen: list[dict[str, Any]] = []

    def handler(req: httpx2.Request) -> httpx2.Response:
        seen.append(json.loads(req.content))
        return httpx2.Response(200, json=_chat('<think>x</think>{"ok": true}'))

    msgs = [{"role": "user", "content": "snmp-server community public"}]
    c = await _client(handler).complete_json(SPEC, msgs, Answer)
    assert c.value == Answer(ok=True)
    assert (c.prompt_tokens, c.completion_tokens) == (100, 50)
    assert c.cost_usd == pytest.approx(200 / 1_000_000)
    assert c.redactions == 1
    body = seen[0]
    assert body["response_format"]["json_schema"]["strict"] is True
    assert "public" not in body["messages"][0]["content"]


async def test_retries_server_errors() -> None:
    calls = 0

    def handler(req: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx2.Response(503, json={"error": {"message": "busy"}})
        return httpx2.Response(200, json=_chat("hello"))

    c = await _client(handler).complete_text(SPEC, [{"role": "user", "content": "hi"}])
    assert c.value == "hello"
    assert calls == 3


async def test_does_not_retry_client_errors() -> None:
    calls = 0

    def handler(req: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return httpx2.Response(400, json={"error": {"message": "bad"}})

    with pytest.raises(Exception, match="bad"):
        await _client(handler).complete_text(SPEC, [{"role": "user", "content": "hi"}])
    assert calls == 1


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (_chat(None, refusal="I can't help with that"), LlmRefusalError),
        (_chat("x", finish="content_filter"), LlmRefusalError),
        (_chat('{"ok": tr', finish="length"), LlmOutputError),
        (_chat('{"nope": 1}'), LlmOutputError),
        ({**_chat("x"), "choices": []}, LlmOutputError),
    ],
)
async def test_refusal_truncation_and_invalid_output(
    response: dict[str, Any], error: type[Exception]
) -> None:
    client = _client(lambda req: httpx2.Response(200, json=response))
    with pytest.raises(error):
        await client.complete_json(SPEC, [{"role": "user", "content": "hi"}], Answer)


async def test_models_and_probe() -> None:
    def handler(req: httpx2.Request) -> httpx2.Response:
        if req.url.path.endswith("/models"):
            return httpx2.Response(
                200, json={"object": "list", "data": [{"id": "nvidia/test", "object": "model",
                                                        "created": 0, "owned_by": "nvidia"}]}
            )  # fmt: skip
        return httpx2.Response(200, json=_chat('{"ok": true}'))

    client = _client(handler)
    assert await client.list_models() == {"nvidia/test"}
    assert await client.probe(SPEC) is None
    bad = _client(lambda req: httpx2.Response(200, json=_chat("not json")))
    assert "LlmOutputError" in (await bad.probe(SPEC) or "")
