"""Every plane wired together, offline.

Real TokenFactoryClient (over httpx2.MockTransport with recorded-shape completions), real
prompts and parser, real PrelintGate, real BatfishVerifier replaying synthetic answers. The
generator's first answer invents a command and gets an ASN wrong; the repair fixes it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx2
import pytest

from fabloom.agent import RunMachine, RunRequest, read_events
from fabloom.agent.events import EVENTS_FILE
from fabloom.checks import allocate, compile_checks
from fabloom.config import RunConfig
from fabloom.contracts import Intent, Platform, RunBudget, TopologySpec
from fabloom.llm import ModelRouter, TokenFactoryClient
from fabloom.verify import PrelintGate
from fabloom.verify.batfish_local import BatfishVerifier
from fabloom.verify.recorded import RecordedAnswerer
from tests.fakes.batfish import add_parse_warning, healthy_answers
from tests.fakes.render import as_device_blocks, render_good_configs

pytestmark = pytest.mark.integration

INVENTED = " bgp bestpath as-path multipath-relax-magic"


def _chat(content: str, prompt_tokens: int, completion_tokens: int) -> dict[str, Any]:
    return {
        "id": "c",
        "object": "chat.completion",
        "created": 0,
        "model": "m",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


class ReplayingAnswerer(RecordedAnswerer):
    """First snapshot: leaf2 has an unrecognised line. Later snapshots: healthy."""

    snapshots = 0

    def init_snapshot(self, path: Path, name: str) -> None:
        type(self).snapshots += 1
        text = (path / "configs" / "leaf2.cfg").read_text()
        if INVENTED in text:
            add_parse_warning(self.answers, "leaf2", INVENTED.strip(), 20)


async def test_generate_verify_repair_explain(tmp_path: Path, router: ModelRouter) -> None:
    intent = Intent(platform=Platform.FRR, topology=TopologySpec(spines=2, leaves=2))
    fabric = allocate(intent)
    checkset = compile_checks(intent, fabric)
    good = render_good_configs(intent, fabric)
    first = {
        **good,
        "leaf1": good["leaf1"].replace("router bgp 65101", "router bgp 65199"),  # prelint catches
        "leaf2": good["leaf2"].replace(" bgp router-id", f"{INVENTED}\n bgp router-id"),
    }
    completions = iter([
        _chat(json.dumps(intent.model_dump(mode="json")), 2000, 150),  # planner
        _chat(as_device_blocks(first, Platform.FRR), 3000, 2500),  # generator
        _chat(f'<device name="leaf1">{good["leaf1"]}</device>', 5000, 600),  # repair 1
        _chat(f'<device name="leaf2">{good["leaf2"]}</device>', 5000, 600),  # repair 2
        _chat("Level L2: all checks passed after two repairs.", 800, 40),  # explainer
    ])  # fmt: skip
    requests: list[dict[str, Any]] = []

    def handler(req: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(req.content))
        return httpx2.Response(200, json=next(completions))

    client = TokenFactoryClient(
        "k",
        "https://tf.test/v1/",
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )

    def answerer() -> ReplayingAnswerer:
        return ReplayingAnswerer(healthy_answers(fabric, checkset.checks), "synthetic")

    verifier = PrelintGate(BatfishVerifier(answerer, name="batfish-replay"))
    machine = RunMachine(
        client,
        router,
        verifier,
        RunConfig(budget=RunBudget(max_repair_rounds=3, max_usd=1.0)),
        tmp_path,
    )
    result = await machine.run(RunRequest("2 spines, 2 leaves, eBGP underlay on FRR"))

    assert result.outcome == "passed", result.explanation
    assert result.repair_rounds == 2
    assert result.best is not None
    assert result.best.configs == good
    # round 0 was stopped by prelint (no Batfish), round 1 reached Batfish and failed parsing
    assert ReplayingAnswerer.snapshots == 2
    assert result.usage.verifications == 2
    assert result.history[0] < result.history[1] < result.history[2] == 1.0

    events = list(read_events(tmp_path / result.run_id / EVENTS_FILE))
    verifs = [e for e in events if e.type == "verification_completed"]
    assert [v.summary["verifier"] for v in verifs] == [
        "prelint",
        "batfish-replay",
        "batfish-replay",
    ]
    repair2_prompt = requests[3]["messages"][1]["content"]
    assert "multipath-relax-magic" in repair2_prompt  # the unrecognised line reached the repairer
    assert "parse_clean:leaf2" in repair2_prompt
    assert requests[0]["response_format"]["type"] == "json_schema"
    llm_costs = sum(e.cost_usd for e in events if e.type == "llm_call")
    assert result.usage.cost_usd == pytest.approx(llm_costs)
    # the event log carries report summaries, never raw Batfish answers
    assert "File_Name" not in (tmp_path / result.run_id / EVENTS_FILE).read_text()
