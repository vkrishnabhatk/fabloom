"""Pre-lint, snapshot layout, and the Batfish evaluator against synthetic answers."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from fabloom.checks import allocate, compile_checks
from fabloom.contracts import (
    BgpMode,
    CheckKind,
    CheckSet,
    FabricPlan,
    Intent,
    Platform,
    Snapshot,
    TopologySpec,
    VerificationLevel,
)
from fabloom.verify import PrelintGate, prelint
from fabloom.verify.batfish_local import BatfishVerifier, run_checks, to_cell
from fabloom.verify.batfish_questions import Evaluator, Question
from fabloom.verify.recorded import MissingAnswerError, RecordedAnswerer, RecordingAnswerer
from fabloom.verify.snapshot import layer1_topology, render_device_file, write_snapshot
from tests.fakes import FakeVerifier
from tests.fakes.batfish import add_parse_warning, break_session, healthy_answers
from tests.fakes.render import render_good_configs

pytestmark = pytest.mark.unit


# --- prelint ---------------------------------------------------------------------------


def test_prelint_passes_good_configs(frr_intent: Intent, frr_fabric: FabricPlan) -> None:
    results = prelint(frr_fabric, render_good_configs(frr_intent, frr_fabric))
    assert all(r.passed for r in results)
    assert len(results) == len(frr_fabric.devices)


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda c: "", "config is empty"),
        (lambda c: "```\n" + c, "markdown code fence"),
        (lambda c: c.replace("hostname leaf1", "hostname leafX"), "expected 'leaf1'"),
        (lambda c: c.replace("hostname leaf1\n", ""), "missing 'hostname leaf1'"),
        (lambda c: c.replace("router bgp 65101", "router bgp 65999"), "plan assigns AS65101"),
        (lambda c: c.replace("router bgp 65101", "router ospf"), "missing 'router bgp 65101'"),
        (lambda c: c + "neighbor <PEER_IP> remote-as 1\n", "placeholder"),
    ],
)
def test_prelint_catches(
    frr_intent: Intent, frr_fabric: FabricPlan, mutate: object, expected: str
) -> None:
    configs = render_good_configs(frr_intent, frr_fabric)
    configs["leaf1"] = mutate(configs["leaf1"])  # type: ignore[operator]
    bad = [r for r in prelint(frr_fabric, configs) if not r.passed]
    assert [r.id for r in bad] == ["prelint:leaf1"]
    assert any(expected in issue for issue in bad[0].evidence["issues"])


def test_prelint_missing_and_unexpected_devices(frr_intent: Intent, frr_fabric: FabricPlan) -> None:
    configs = render_good_configs(frr_intent, frr_fabric)
    del configs["spine2"]
    configs["router9"] = "hostname router9\n"
    bad = {r.id: r for r in prelint(frr_fabric, configs) if not r.passed}
    assert set(bad) == {"prelint:spine2", "prelint:unexpected_devices"}
    assert bad["prelint:unexpected_devices"].evidence["unexpected"] == ["router9"]


async def test_gate_skips_backend_on_prelint_failure(
    frr_fabric: FabricPlan, frr_checkset: CheckSet
) -> None:
    inner = FakeVerifier([0])
    gate = PrelintGate(inner)
    report = await gate.verify(Snapshot(name="s", fabric=frr_fabric), {}, frr_checkset.checks)
    assert inner.calls == []
    assert report.backend_executed is False
    assert report.level is VerificationLevel.L0
    assert not report.passed
    assert all(c.evidence.get("not_run") for c in report.checks if c.kind is not CheckKind.PRELINT)


async def test_gate_calls_backend_when_clean(
    frr_intent: Intent, frr_fabric: FabricPlan, frr_checkset: CheckSet
) -> None:
    inner = FakeVerifier([0])
    gate = PrelintGate(inner)
    configs = render_good_configs(frr_intent, frr_fabric)
    report = await gate.verify(Snapshot(name="s", fabric=frr_fabric), configs, frr_checkset.checks)
    assert len(inner.calls) == 1
    assert report.passed
    assert report.backend_executed


# --- snapshot --------------------------------------------------------------------------


def test_frr_snapshot_layout(tmp_path: Path, frr_intent: Intent, frr_fabric: FabricPlan) -> None:
    configs = render_good_configs(frr_intent, frr_fabric)
    write_snapshot(tmp_path, frr_fabric, configs)
    text = (tmp_path / "configs" / "leaf1.cfg").read_text()
    lines = text.splitlines()
    assert lines[0] == "leaf1"  # Cumulus concatenated format: hostname first
    assert "# This file describes the network interfaces" in text
    assert "iface swp1" in text
    # addresses are the model's job: the scaffolded interfaces section must carry none
    iface_section = text.split("frr version")[0]
    assert "address" not in iface_section
    topo = json.loads((tmp_path / "batfish" / "layer1_topology.json").read_text())
    assert len(topo["edges"]) == 2 * len(frr_fabric.links)


def test_frr_version_line_added_once(frr_fabric: FabricPlan) -> None:
    dev = frr_fabric.device("leaf1")
    assert render_device_file(Platform.FRR, dev, "hostname leaf1\n").count("frr version") == 1
    assert (
        render_device_file(Platform.FRR, dev, "frr version 9.1\nhostname leaf1").count(
            "frr version"
        )
        == 1
    )


def test_eos_written_verbatim() -> None:
    fabric = allocate(
        Intent(platform=Platform.ARISTA_EOS, topology=TopologySpec(spines=1, leaves=1))
    )
    assert render_device_file(Platform.ARISTA_EOS, fabric.devices[0], "hostname spine1") == (
        "hostname spine1\n"
    )


def test_layer1_always_written_for_unnumbered(frr_intent: Intent) -> None:
    fabric = allocate(frr_intent.model_copy(update={"bgp_mode": BgpMode.UNNUMBERED}))
    edge = layer1_topology(fabric)["edges"][0]
    assert edge == {
        "node1": {"hostname": "leaf1", "interfaceName": "swp1"},
        "node2": {"hostname": "spine1", "interfaceName": "swp1"},
    }


# --- evaluator -------------------------------------------------------------------------


def _answers(intent: Intent) -> tuple[FabricPlan, CheckSet, dict[str, list[dict[str, object]]]]:
    fabric = allocate(intent)
    cs = compile_checks(intent, fabric)
    return fabric, cs, healthy_answers(fabric, cs.checks)


@pytest.mark.parametrize("mode", list(BgpMode))
def test_healthy_fabric_passes_every_check(frr_intent: Intent, mode: BgpMode) -> None:
    _, cs, answers = _answers(frr_intent.model_copy(update={"bgp_mode": mode}))
    facts = run_checks(RecordedAnswerer(answers), cs.checks)
    failed = [r.id for r in facts["checks"] if not r.passed]
    assert failed == []
    assert facts["score"] == 1.0


def test_eos_filters_pass_when_nothing_accepted() -> None:
    intent = Intent(
        platform=Platform.ARISTA_EOS,
        topology=TopologySpec(spines=2, leaves=2),
        filters=[{"devices": "leaf*", "dst_port": 22, "src_not_in": "10.0.0.0/8"}],
    )
    _, cs, answers = _answers(intent)
    key = next(k for k in answers if '"ACCEPTED"' in k and "leaf1[Ethernet1]" in k)
    assert "@enter(leaf1[Ethernet1]),@enter(leaf1[Ethernet2])" in key
    assert all(r.passed for r in run_checks(RecordedAnswerer(answers), cs.checks)["checks"])
    answers[key] = [{"Flow": "203.0.113.9->10.0.1.1 tcp:22", "Traces": ["ACCEPTED"]}]
    results = {r.id: r for r in run_checks(RecordedAnswerer(answers), cs.checks)["checks"]}
    assert not results["filter_denies:leaf1:tcp/22"].passed
    assert results["filter_denies:leaf1:tcp/22"].evidence["accepted_flows"][0]["flow"]


def test_broken_session_and_parse_warning(frr_intent: Intent) -> None:
    _, cs, answers = _answers(frr_intent)
    break_session(answers, "leaf1", "spine2")
    add_parse_warning(answers, "leaf2", "neighbor fabric peer-group-magic", 12)
    facts = run_checks(RecordedAnswerer(answers), cs.checks)
    failed = {r.id: r for r in facts["checks"] if not r.passed}
    assert set(failed) == {"bgp_established:leaf1--spine2", "parse_clean:leaf2"}
    assert failed["bgp_established:leaf1--spine2"].evidence["leaf1"] == ["NOT_ESTABLISHED"]
    assert failed["parse_clean:leaf2"].evidence["status"] == "PARTIALLY_UNRECOGNIZED"
    assert failed["parse_clean:leaf2"].remediation_hint
    assert facts["unrecognized_lines"][0].device == "leaf2"
    assert 0 < facts["score"] < 1


def test_missing_session_side_fails(frr_intent: Intent) -> None:
    _, cs, answers = _answers(frr_intent)
    key = Question("bgpSessionCompatibility").key
    answers[key] = [r for r in answers[key] if r["Node"] != "spine1"]
    failed = [r for r in run_checks(RecordedAnswerer(answers), cs.checks)["checks"] if not r.passed]
    assert {r.id for r in failed} == {
        "bgp_compatible:leaf1--spine1",
        "bgp_compatible:leaf2--spine1",
    }
    assert failed[0].evidence["spine1"] == ["NO_SESSION_CONFIGURED"]


def test_unparsed_device_fails_dependent_checks(frr_intent: Intent) -> None:
    _, cs, answers = _answers(frr_intent)
    key = Question("fileParseStatus").key
    answers[key] = [r for r in answers[key] if r["File_Name"] != "configs/leaf1.cfg"]
    results = {r.id: r for r in run_checks(RecordedAnswerer(answers), cs.checks)["checks"]}
    assert results["parse_clean:leaf1"].evidence["status"] == "MISSING"
    assert not results["no_undefined_refs:leaf1"].passed
    assert not results["reachability:leaf1->leaf2"].passed
    assert results["reachability:leaf2->leaf1"].passed


def test_missing_routes_edges_and_loops(frr_intent: Intent) -> None:
    _, cs, answers = _answers(frr_intent)
    answers[Question("routes").key] = []
    answers[Question("layer3Edges").key] = []
    answers[Question("detectLoops").key] = [{"Flow": "f", "Traces": "loop"}]
    answers[Question("undefinedReferences").key] = [
        {"File_Name": "configs/spine1.cfg", "Struct_Type": "route-map", "Ref_Name": "RM",
         "Context": "bgp neighbor", "Lines": [3]}
    ]  # fmt: skip
    results = run_checks(RecordedAnswerer(answers), cs.checks)["checks"]
    failing_kinds = {r.kind for r in results if not r.passed}
    assert failing_kinds == {
        CheckKind.ROUTE_PRESENT,
        CheckKind.L3_EDGES,
        CheckKind.NO_FORWARDING_LOOPS,
        CheckKind.NO_UNDEFINED_REFS,
    }


def test_answerer_error_becomes_failed_check(frr_intent: Intent) -> None:
    _, cs, answers = _answers(frr_intent)
    del answers[Question("detectLoops").key]
    results = {r.id: r for r in run_checks(RecordedAnswerer(answers), cs.checks)["checks"]}
    assert "MissingAnswerError" in results["no_forwarding_loops"].evidence["error"]


def test_each_question_asked_once(frr_intent: Intent) -> None:
    _, cs, answers = _answers(frr_intent)
    answerer = RecordedAnswerer(answers)
    Evaluator(answerer)  # construction asks nothing
    run_checks(answerer, cs.checks)
    assert len(answerer.asked) == len(set(answerer.asked))


def test_recording_round_trip(tmp_path: Path, frr_intent: Intent) -> None:
    _, cs, answers = _answers(frr_intent)
    rec = RecordingAnswerer(RecordedAnswerer(answers))
    run_checks(rec, cs.checks)
    path = tmp_path / "fixture.json"
    rec.dump(path, batfish_version="2026.1", provenance="test")
    replay = RecordedAnswerer.from_file(path)
    assert replay.batfish_version() == "2026.1"
    assert all(r.passed for r in run_checks(replay, cs.checks)["checks"])
    with pytest.raises(MissingAnswerError):
        replay.ask(Question("nope"))


async def test_batfish_verifier_builds_report(
    frr_intent: Intent, frr_fabric: FabricPlan, frr_checkset: CheckSet
) -> None:
    answers = healthy_answers(frr_fabric, frr_checkset.checks)
    seen: list[Path] = []

    class Spy(RecordedAnswerer):
        def init_snapshot(self, path: Path, name: str) -> None:
            seen.append(path)
            assert (path / "configs" / "leaf1.cfg").exists()

    verifier = BatfishVerifier(lambda: Spy(answers, "2026.9"), name="batfish-test")
    report = await verifier.verify(
        Snapshot(name="s1", fabric=frr_fabric),
        render_good_configs(frr_intent, frr_fabric),
        frr_checkset.checks,
    )
    assert report.passed
    assert report.level is VerificationLevel.L2
    assert report.batfish_version == "2026.9"
    assert report.verifier_name == "batfish-test"
    assert not seen[0].exists()  # the snapshot tempdir is cleaned up


def test_to_cell_renders_pybatfish_values() -> None:
    iface = SimpleNamespace(hostname="leaf1", interface="swp1")
    assert to_cell(iface) == "leaf1[swp1]"
    assert to_cell(float("nan")) is None
    assert to_cell(1.5) == 1.5
    assert to_cell([iface, None, "x"]) == ["leaf1[swp1]", None, "x"]
    assert to_cell({"a": (1, 2)}) == {"a": [1, 2]}
    assert to_cell(object()).startswith("<object")
