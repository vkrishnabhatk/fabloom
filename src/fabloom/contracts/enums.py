"""Closed vocabularies shared by every plane."""

from __future__ import annotations

from enum import StrEnum


class Platform(StrEnum):
    FRR = "frr"
    ARISTA_EOS = "arista_eos"
    JUNOS = "junos"
    CISCO_NXOS = "cisco_nxos"
    CISCO_IOSXE = "cisco_iosxe"
    CISCO_IOSXR = "cisco_iosxr"
    SONIC = "sonic"


class VerificationLevel(StrEnum):
    """How strongly a result was checked. Every report carries exactly one."""

    L0 = "L0"  # advisory: no automated check was possible
    L1 = "L1"  # rule checked: project rules, no simulator models the behaviour
    L2 = "L2"  # statically verified: Batfish parses and confirms sessions/routes/filters
    L3 = "L3"  # runtime verified: booted on the real network OS


class Role(StrEnum):
    SPINE = "spine"
    LEAF = "leaf"


class BgpMode(StrEnum):
    NUMBERED = "numbered"
    UNNUMBERED = "unnumbered"


class Severity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class CheckKind(StrEnum):
    PRELINT = "prelint"
    PARSE_CLEAN = "parse_clean"
    NO_UNDEFINED_REFS = "no_undefined_refs"
    BGP_COMPATIBLE = "bgp_compatible"
    BGP_ESTABLISHED = "bgp_established"
    L3_EDGES = "l3_edges"
    ROUTE_PRESENT = "route_present"
    REACHABILITY = "reachability"
    FILTER_DENIES = "filter_denies"
    NO_FORWARDING_LOOPS = "no_forwarding_loops"


class ParseStatus(StrEnum):
    """Mirrors Batfish ``fileParseStatus`` values."""

    PASSED = "PASSED"
    PARTIALLY_UNRECOGNIZED = "PARTIALLY_UNRECOGNIZED"
    FAILED = "FAILED"
    EMPTY = "EMPTY"
    UNKNOWN = "UNKNOWN"


class RunState(StrEnum):
    PLANNING = "PLANNING"
    GENERATING = "GENERATING"
    VERIFYING = "VERIFYING"
    REPAIRING = "REPAIRING"
    PASSED = "PASSED"
    EXHAUSTED = "EXHAUSTED"
    EXPLAINING = "EXPLAINING"
    DONE = "DONE"
    FAILED = "FAILED"


class ModelRole(StrEnum):
    PLANNER = "planner"
    GENERATOR = "generator"
    REPAIRER = "repairer"
    REPAIRER_ESCALATED = "repairer_escalated"
    EXPLAINER = "explainer"
