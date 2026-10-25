from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx2
import pytest
from typer.testing import CliRunner

from fabloom import cli
from fabloom.agent.budget import CHARS_PER_TOKEN_ESTIMATE
from fabloom.checks import allocate, compile_checks
from fabloom.config import REPO_ROOT, Settings, models_config
from fabloom.contracts import BgpMode, Intent, ModelRole, Platform, TopologySpec
from fabloom.llm import TokenFactoryClient
from fabloom.scenarios import load_scenarios
from fabloom.verify import PrelintGate
from tests.fakes import FakeChatClient, FakeVerifier
from tests.fakes.render import as_device_blocks, render_good_configs

pytestmark = pytest.mark.integration

runner = CliRunner()
SCENARIO = REPO_ROOT / "benchmark" / "v0.1" / "leaf-spine-ebgp-0002.yaml"


def test_version() -> None:
    result = runner.invoke(cli.app, ["version"])
    assert result.exit_code == 0
    assert result.output.startswith("fabloom ")


def test_plan_is_deterministic() -> None:
    a = runner.invoke(cli.app, ["plan", str(SCENARIO)])
    b = runner.invoke(cli.app, ["plan", str(SCENARIO)])
    assert a.exit_code == 0
    assert a.output == b.output
    assert "33 checks:" in a.output
    assert "leaf1 swp1 10.1.0.1 AS65101 <-> spine1 swp1 10.1.0.0 AS65000" in a.output


def test_plan_reports_unverifiable(tmp_path: Path) -> None:
    sc = tmp_path / "frr-filter-0001.yaml"
    sc.write_text(
        "id: frr-filter-0001\nfamily: frr-filter\nrequest: block ssh on frr please\n"
        "intent:\n  platform: frr\n  topology: {spines: 1, leaves: 1}\n"
        "  filters: [{devices: 'leaf*', dst_port: 22}]\n"
    )
    result = runner.invoke(cli.app, ["plan", str(sc), "--no-checks"])
    assert result.exit_code == 0
    assert "not verifiable: deny tcp/22 to leaf*" in result.output


def test_scenarios_validate() -> None:
    result = runner.invoke(cli.app, ["scenarios", "validate"])
    assert result.exit_code == 0, result.output
    assert "49 scenarios OK" in result.output


def test_benchmark_scenarios_well_formed() -> None:
    scenarios = load_scenarios()
    assert 40 <= len(scenarios) <= 60  # the v0.1 target (ADR-0007)
    families = {s.family for s in scenarios}
    assert {"leaf-spine-ebgp", "leaf-spine-unnumbered", "leaf-spine-filters"} <= families
    assert {s.intent.platform for s in scenarios} == set(Platform)
    eos = [s for s in scenarios if s.intent.platform is Platform.ARISTA_EOS]
    assert len(eos) * 3 >= len(scenarios)  # EOS is a first-class platform, not a token one


def test_filter_scenarios_are_gradeable() -> None:
    # Only the unverifiable family may carry a filter the verifier cannot see (ADR-0006);
    # anywhere else it would quietly shrink the rubric instead of testing the filter.
    for sc in load_scenarios():
        checkset = compile_checks(sc.intent, allocate(sc.intent))
        if sc.family == "leaf-spine-unverifiable":
            assert sc.intent.filters
            assert checkset.unverifiable, sc.id
        else:
            assert not checkset.unverifiable, sc.id


def test_no_eos_unnumbered_scenarios() -> None:
    # The EOS generator rules describe numbered peering only; an unnumbered EOS scenario
    # would grade the prompt's omission, not the model.
    for sc in load_scenarios():
        if sc.intent.platform is Platform.ARISTA_EOS:
            assert sc.intent.bgp_mode is BgpMode.NUMBERED, sc.id


def test_scenarios_fit_the_generator_output_cap() -> None:
    # A model's config is wordier than the minimal reference render. The 2x allowance is an
    # unmeasured guess; recalibrate it from completion_tokens in real events.jsonl runs.
    cap = models_config().for_role(ModelRole.GENERATOR).max_output_tokens
    for sc in load_scenarios():
        blocks = as_device_blocks(
            render_good_configs(sc.intent, allocate(sc.intent)), sc.intent.platform
        )
        assert 2 * len(blocks) // CHARS_PER_TOKEN_ESTIMATE <= cap, sc.id


def test_scenario_id_must_match_filename(tmp_path: Path) -> None:
    (tmp_path / "wrong-name-0001.yaml").write_text(
        SCENARIO.read_text().replace("leaf-spine-ebgp-0002", "leaf-spine-ebgp-0003")
    )
    with pytest.raises(ValueError, match="must match the file name"):
        load_scenarios(tmp_path)


def test_generate_watch_and_events(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    intent = Intent(platform=Platform.FRR, topology=TopologySpec(spines=2, leaves=2))
    blocks = as_device_blocks(render_good_configs(intent, allocate(intent)), Platform.FRR)
    fake = FakeChatClient(script=[intent.model_dump(mode="json"), blocks, "All passed."])
    monkeypatch.setattr(cli, "build_client", lambda s: fake)
    monkeypatch.setattr(cli, "build_verifier", lambda s: FakeVerifier([0]))
    runs = tmp_path / "runs"
    result = runner.invoke(
        cli.app,
        ["generate", "2 spines 2 leaves", "--runs-dir", str(runs), "--max-repair-rounds", "1"],
    )
    assert result.exit_code == 0, result.output
    assert "PLANNING -> GENERATING" in result.output
    assert "33/33 checks" in result.output
    assert "finished: passed" in result.output
    run_dir = next(runs.iterdir())
    replay = runner.invoke(cli.app, ["events", str(run_dir)])
    assert "All passed." in replay.output
    follow = runner.invoke(cli.app, ["events", str(run_dir), "--follow"])
    assert follow.output == replay.output


def test_generate_without_key_explains_the_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FABLOOM_TOKEN_FACTORY_API_KEY", raising=False)
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)
    monkeypatch.chdir(REPO_ROOT / "tests")  # no .env here
    result = runner.invoke(cli.app, ["generate", "anything at all"])
    assert result.exit_code == 2
    assert "FABLOOM_TOKEN_FACTORY_API_KEY" in result.output


def _chat(content: str) -> dict[str, Any]:
    return {
        "id": "c", "object": "chat.completion", "created": 0, "model": "m",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }  # fmt: skip


def test_doctor(monkeypatch: pytest.MonkeyPatch) -> None:
    listed = {"nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B", "nvidia/nemotron-3-super-120b-a12b"}

    def handler(req: httpx2.Request) -> httpx2.Response:
        if req.url.path.endswith("/models"):
            data = [{"id": i, "object": "model", "created": 0, "owned_by": "x"} for i in listed]
            return httpx2.Response(200, json={"object": "list", "data": data})
        return httpx2.Response(200, json=_chat(json.dumps({"ok": True})))

    client = TokenFactoryClient(
        "k", "https://tf.test/v1/",
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )  # fmt: skip
    monkeypatch.setattr(cli, "build_client", lambda s: client)
    result = runner.invoke(cli.app, ["doctor"])
    assert result.exit_code == 1  # ultra and lightning are not in the listing
    assert "ok   nano" in result.output
    assert "FAIL ultra" in result.output
    assert "not listed by /v1/models" in result.output


def test_build_verifier_wires_the_prelint_gate() -> None:
    verifier = cli.build_verifier(Settings())
    assert isinstance(verifier, PrelintGate)
    assert verifier.name == "batfish-local"
