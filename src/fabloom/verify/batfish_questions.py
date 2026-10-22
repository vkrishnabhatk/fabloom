"""Check -> Batfish question mapping, and evaluation of answers into ``CheckResult``s.

The evaluator only ever sees answer *rows*: plain dicts keyed by Batfish column names, with
interfaces rendered as ``"host[iface]"``. That keeps it independent of pybatfish, so it runs
the same against a live Batfish, a sandbox, or recorded JSON fixtures.

| CheckKind            | Question(s)                                         |
|----------------------|-----------------------------------------------------|
| parse_clean          | fileParseStatus, parseWarning                        |
| no_undefined_refs    | undefinedReferences                                  |
| bgp_compatible       | bgpSessionCompatibility                              |
| bgp_established      | bgpSessionStatus                                     |
| l3_edges             | layer3Edges                                          |
| route_present        | routes                                               |
| reachability         | reachability(actions=FAILURE), must be empty; flows  |
|                      | the intent itself denies are left out of the query   |
| filter_denies        | reachability(@enter uplinks, actions=ACCEPTED), empty|
| no_forwarding_loops  | detectLoops                                          |
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Protocol

from fabloom.checks import REMEDIATION
from fabloom.contracts import (
    Check,
    CheckKind,
    CheckResult,
    ParseStatus,
    UndefinedRef,
    UnrecognizedLine,
)

Row = dict[str, Any]
MAX_EVIDENCE_ROWS = 5


@dataclass(frozen=True)
class Question:
    name: str
    params: Mapping[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return json.dumps({"name": self.name, "params": dict(self.params)}, sort_keys=True)


class Answerer(Protocol):
    def ask(self, question: Question) -> list[Row]: ...


def _device_of_file(filename: str) -> str:
    return PurePosixPath(str(filename)).stem


MAX_PORT = 65535


def _port_complement(ports: list[int]) -> str:
    """Batfish dstPorts for every port except ``ports``: ``[23]`` -> ``"1-22,24-65535"``."""
    ranges, lo = [], 1
    for port in sorted(set(ports)):
        if port > lo:
            ranges.append(f"{lo}-{port - 1}")
        lo = port + 1
    if lo <= MAX_PORT:
        ranges.append(f"{lo}-{MAX_PORT}")
    return ",".join(ranges)


def _iface(device: str, interface: str) -> str:
    return f"{device}[{interface}]"


class Evaluator:
    """Evaluates checks against one initialised snapshot, asking each question at most once."""

    def __init__(self, answerer: Answerer) -> None:
        self._answerer = answerer
        self._cache: dict[str, list[Row]] = {}
        self._dispatch: dict[CheckKind, Callable[[Check], tuple[bool, dict[str, Any]]]] = {
            CheckKind.PARSE_CLEAN: self._parse_clean,
            CheckKind.NO_UNDEFINED_REFS: self._no_undefined_refs,
            CheckKind.BGP_COMPATIBLE: self._bgp_compatible,
            CheckKind.BGP_ESTABLISHED: self._bgp_established,
            CheckKind.L3_EDGES: self._l3_edges,
            CheckKind.ROUTE_PRESENT: self._route_present,
            CheckKind.REACHABILITY: self._reachability,
            CheckKind.FILTER_DENIES: self._filter_denies,
            CheckKind.NO_FORWARDING_LOOPS: self._no_forwarding_loops,
        }

    def rows(self, name: str, **params: Any) -> list[Row]:
        q = Question(name, params)
        if q.key not in self._cache:
            self._cache[q.key] = self._answerer.ask(q)
        return self._cache[q.key]

    # --- report-level facts -------------------------------------------------------------

    def parse_status(self) -> dict[str, ParseStatus]:
        out: dict[str, ParseStatus] = {}
        for r in self.rows("fileParseStatus"):
            status = str(r.get("Status", "UNKNOWN"))
            out[_device_of_file(r["File_Name"])] = (
                ParseStatus(status) if status in ParseStatus.__members__ else ParseStatus.UNKNOWN
            )
        return out

    def unrecognized_lines(self) -> list[UnrecognizedLine]:
        return [
            UnrecognizedLine(
                device=_device_of_file(r["Filename"]),
                line_number=r.get("Line"),
                text=str(r.get("Text", "")),
            )
            for r in self.rows("parseWarning")
        ]

    def undefined_references(self) -> list[UndefinedRef]:
        return [
            UndefinedRef(
                device=_device_of_file(r["File_Name"]),
                struct_type=str(r.get("Struct_Type", "")),
                name=str(r.get("Ref_Name", "")),
                context=str(r.get("Context", "")),
            )
            for r in self.rows("undefinedReferences")
        ]

    # --- per-check evaluation -----------------------------------------------------------

    def evaluate(self, check: Check) -> CheckResult:
        handler = self._dispatch.get(check.kind)
        if handler is None:
            passed, evidence = False, {"error": f"no evaluator for check kind {check.kind}"}
        else:
            try:
                passed, evidence = handler(check)
            except Exception as exc:  # a verifier error on one check must not sink the report
                passed, evidence = False, {"error": f"{type(exc).__name__}: {exc}"}
        return CheckResult(
            id=check.id,
            kind=check.kind,
            passed=passed,
            severity=check.severity,
            evidence=evidence,
            remediation_hint=None if passed else REMEDIATION.get(check.kind),
        )

    def _parsed(self, device: str) -> bool:
        return device in self.parse_status()

    def _parse_clean(self, check: Check) -> tuple[bool, dict[str, Any]]:
        device = check.params["device"]
        status = self.parse_status().get(device)
        if status is None:
            return False, {"status": "MISSING", "detail": "Batfish did not load this file"}
        warnings = [
            {"line": r.get("Line"), "text": r.get("Text"), "comment": r.get("Comment")}
            for r in self.rows("parseWarning")
            if _device_of_file(r["Filename"]) == device
        ]
        passed = status is ParseStatus.PASSED and not warnings
        evidence: dict[str, Any] = {"status": status.value}
        if warnings:
            evidence["warnings"] = warnings[:MAX_EVIDENCE_ROWS]
            evidence["warning_count"] = len(warnings)
        return passed, evidence

    def _no_undefined_refs(self, check: Check) -> tuple[bool, dict[str, Any]]:
        device = check.params["device"]
        if not self._parsed(device):
            return False, {"detail": "device not parsed"}
        refs = [r.model_dump() for r in self.undefined_references() if r.device == device]
        return not refs, ({"undefined": refs[:MAX_EVIDENCE_ROWS]} if refs else {})

    def _session_rows(self, question: str, check: Check) -> tuple[list[Row], list[Row]]:
        p = check.params

        def side(node: str, peer: str, local_if: str, peer_ip: str | None) -> list[Row]:
            return [
                r
                for r in self.rows(question)
                if r.get("Node") == node
                and (
                    r.get("Remote_Node") == peer
                    or (peer_ip is not None and r.get("Remote_IP") == peer_ip)
                    or (peer_ip is None and r.get("Local_Interface") == _iface(node, local_if))
                )
            ]

        return (
            side(p["a_device"], p["b_device"], p["a_interface"], p["b_ip"]),
            side(p["b_device"], p["a_device"], p["b_interface"], p["a_ip"]),
        )

    def _session_check(
        self, question: str, column: str, ok: str, check: Check
    ) -> tuple[bool, dict[str, Any]]:
        a_rows, b_rows = self._session_rows(question, check)
        evidence = {
            check.params["a_device"]: [r.get(column) for r in a_rows] or ["NO_SESSION_CONFIGURED"],
            check.params["b_device"]: [r.get(column) for r in b_rows] or ["NO_SESSION_CONFIGURED"],
        }
        rows = a_rows + b_rows
        passed = bool(a_rows) and bool(b_rows) and all(r.get(column) == ok for r in rows)
        return passed, evidence

    def _bgp_compatible(self, check: Check) -> tuple[bool, dict[str, Any]]:
        return self._session_check(
            "bgpSessionCompatibility", "Configured_Status", "UNIQUE_MATCH", check
        )

    def _bgp_established(self, check: Check) -> tuple[bool, dict[str, Any]]:
        return self._session_check("bgpSessionStatus", "Established_Status", "ESTABLISHED", check)

    def _l3_edges(self, check: Check) -> tuple[bool, dict[str, Any]]:
        p = check.params
        a, b = _iface(p["a_device"], p["a_interface"]), _iface(p["b_device"], p["b_interface"])
        for r in self.rows("layer3Edges"):
            if {r.get("Interface"), r.get("Remote_Interface")} == {a, b}:
                return True, {"ips": r.get("IPs"), "remote_ips": r.get("Remote_IPs")}
        return False, {"detail": f"no layer-3 edge between {a} and {b}"}

    def _route_present(self, check: Check) -> tuple[bool, dict[str, Any]]:
        node, prefix = check.params["node"], check.params["prefix"]
        matches = [
            r for r in self.rows("routes") if r.get("Node") == node and r.get("Network") == prefix
        ]
        if not matches:
            return False, {"detail": f"{prefix} not in {node}'s routing table"}
        return True, {
            "protocols": sorted({str(r.get("Protocol")) for r in matches}),
            "next_hops": [r.get("Next_Hop_IP") or r.get("Next_Hop") for r in matches][
                :MAX_EVIDENCE_ROWS
            ],
        }

    def _flows(self, rows: list[Row]) -> list[dict[str, Any]]:
        return [
            {"flow": r.get("Flow"), "traces": str(r.get("Traces"))[:500]}
            for r in rows[:MAX_EVIDENCE_ROWS]
        ]

    def _reachability(self, check: Check) -> tuple[bool, dict[str, Any]]:
        p = check.params
        if not self._parsed(p["src_device"]):
            return False, {"detail": f"{p['src_device']} not parsed"}
        base = {"srcIps": p["src_ip"], "dstIps": p["dst_ip"]}
        denied = sorted({int(d["dst_port"]) for d in p.get("intended_denials", [])})
        if not denied:
            header_sets = [base]
        else:
            # Everything except what the intent itself drops: ICMP, then TCP/UDP on every
            # other port. Excluding a port for both protocols when only one is denied loses
            # a little coverage but never reports an intended drop as a failure.
            header_sets = [
                {**base, "ipProtocols": ["icmp"]},
                {**base, "ipProtocols": ["tcp", "udp"], "dstPorts": _port_complement(denied)},
            ]
        failing = [
            row
            for headers in header_sets
            for row in self.rows(
                "reachability",
                pathConstraints={"startLocation": p["src_device"]},
                headers=headers,
                actions="FAILURE",
            )
        ]
        return not failing, ({"failing_flows": self._flows(failing)} if failing else {})

    def _filter_denies(self, check: Check) -> tuple[bool, dict[str, Any]]:
        p = check.params
        if not self._parsed(p["device"]):
            return False, {"detail": f"{p['device']} not parsed"}
        start = ",".join(f"@enter({_iface(p['device'], i)})" for i in p["ingress_interfaces"])
        accepted = self.rows(
            "reachability",
            pathConstraints={"startLocation": start},
            headers={
                "srcIps": p["src_ips"],
                "dstIps": p["dst_ip"],
                "ipProtocols": [p["protocol"]],
                "dstPorts": str(p["dst_port"]),
            },
            actions="ACCEPTED",
        )
        return not accepted, ({"accepted_flows": self._flows(accepted)} if accepted else {})

    def _no_forwarding_loops(self, check: Check) -> tuple[bool, dict[str, Any]]:
        loops = self.rows("detectLoops")
        return not loops, ({"loops": self._flows(loops)} if loops else {})
