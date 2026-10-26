"""Synthesize Batfish answers in the column shapes Batfish returns.

These are *synthetic*: shaped after Batfish's documented answer columns, not recorded from a
live server. Real answers, recorded by ``scripts/record_batfish_fixtures.py``, are replayed by
tests/integration/test_recorded_fixtures.py; these fakes cover the cases a recording cannot,
such as a check failing on purpose.
"""

from __future__ import annotations

from typing import Any

from fabloom.contracts import BgpMode, Check, CheckKind, FabricPlan
from fabloom.verify.batfish_questions import Evaluator, Question, Row
from fabloom.verify.snapshot import config_filename


def _session_row(fabric: FabricPlan, node: str, status_col: str, status: str) -> list[Row]:
    rows: list[Row] = []
    unnumbered = fabric.bgp_mode is BgpMode.UNNUMBERED
    for s in fabric.sessions:
        for local, remote in (("a", "b"), ("b", "a")):
            if getattr(s, f"{local}_device") != node:
                continue
            ld, rd = getattr(s, f"{local}_device"), getattr(s, f"{remote}_device")
            li, ri = getattr(s, f"{local}_interface"), getattr(s, f"{remote}_interface")
            lip, rip = getattr(s, f"{local}_ip"), getattr(s, f"{remote}_ip")
            rows.append({
                "Node": ld,
                "VRF": "default",
                "Local_AS": getattr(s, f"{local}_asn"),
                "Local_Interface": f"{ld}[{li}]" if unnumbered else None,
                "Local_IP": None if lip is None else str(lip),
                "Remote_AS": str(getattr(s, f"{remote}_asn")),
                "Remote_Node": rd,
                "Remote_Interface": f"{rd}[{ri}]" if unnumbered else None,
                "Remote_IP": None if rip is None else str(rip),
                "Address_Families": ["IPV4_UNICAST"],
                "Session_Type": "EBGP_UNNUMBERED" if unnumbered else "EBGP_SINGLEHOP",
                status_col: status,
            })  # fmt: skip
    return rows


def healthy_answers(fabric: FabricPlan, checks: list[Check]) -> dict[str, list[Row]]:
    """Every question the evaluator will ask for ``checks``, answered as a healthy fabric."""
    names = fabric.device_names
    answers: dict[str, list[Row]] = {
        Question("fileParseStatus").key: [
            {
                "File_Name": config_filename(n),
                "Status": "PASSED",
                "File_Format": "CUMULUS_CONCATENATED",
                "Nodes": [n],
            }
            for n in names
        ],
        Question("parseWarning").key: [],
        Question("undefinedReferences").key: [],
        Question("detectLoops").key: [],
        Question("bgpSessionCompatibility").key: [
            r for n in names for r in _session_row(fabric, n, "Configured_Status", "UNIQUE_MATCH")
        ],
        Question("bgpSessionStatus").key: [
            r for n in names for r in _session_row(fabric, n, "Established_Status", "ESTABLISHED")
        ],
        Question("layer3Edges").key: [
            row
            for link in fabric.links
            if link.subnet is not None
            for row in (
                {
                    "Interface": f"{link.a_device}[{link.a_interface}]",
                    "IPs": [str(link.subnet.network_address + 1)],
                    "Remote_Interface": f"{link.b_device}[{link.b_interface}]",
                    "Remote_IPs": [str(link.subnet.network_address)],
                },
                {
                    "Interface": f"{link.b_device}[{link.b_interface}]",
                    "IPs": [str(link.subnet.network_address)],
                    "Remote_Interface": f"{link.a_device}[{link.a_interface}]",
                    "Remote_IPs": [str(link.subnet.network_address + 1)],
                },
            )
        ],
        Question("routes").key: [
            {
                "Node": c.params["node"],
                "VRF": "default",
                "Network": c.params["prefix"],
                "Next_Hop_IP": "10.1.0.0",
                "Protocol": "bgp",
            }
            for c in checks
            if c.kind is CheckKind.ROUTE_PRESENT
        ],
    }
    # Per-check reachability questions: ask the evaluator which keys it would use.
    probe = _KeyProbe()
    ev = Evaluator(probe)
    ev._cache.update(answers)
    for c in checks:
        if c.kind in (CheckKind.REACHABILITY, CheckKind.FILTER_DENIES):
            ev.evaluate(c)
    for key in probe.keys:
        answers[key] = []
    return answers


class _KeyProbe:
    def __init__(self) -> None:
        self.keys: list[str] = []

    def ask(self, question: Question) -> list[Row]:
        self.keys.append(question.key)
        return []


def break_session(
    answers: dict[str, list[Row]], node: str, remote: str, status: str = "NOT_ESTABLISHED"
) -> None:
    for row in answers[Question("bgpSessionStatus").key]:
        if {row["Node"], row["Remote_Node"]} == {node, remote}:
            row["Established_Status"] = status


def add_parse_warning(answers: dict[str, list[Row]], device: str, text: str, line: int) -> None:
    answers[Question("parseWarning").key].append(
        {
            "Filename": config_filename(device),
            "Line": line,
            "Text": text,
            "Parser_Context": "[s_router_bgp]",
            "Comment": "This syntax is unrecognized",
        }
    )
    for row in answers[Question("fileParseStatus").key]:
        if row["File_Name"] == config_filename(device):
            row["Status"] = "PARTIALLY_UNRECOGNIZED"


def as_fixture(answers: dict[str, Any]) -> dict[str, Any]:
    return {"_provenance": "synthetic (tests/fakes/batfish.py)", "answers": answers}
