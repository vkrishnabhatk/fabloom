"""Contracts plane: Pydantic models only.

Boundary rule: zero I/O, and imports nothing else in fabloom. Every other plane speaks in
these types, so this package must stay the dependency-free bottom of the graph.
"""

from fabloom.contracts.checks import (
    Check,
    CheckResult,
    CheckSet,
    UndefinedRef,
    UnrecognizedLine,
    VerificationReport,
    score_checks,
)
from fabloom.contracts.enums import (
    BgpMode,
    CheckKind,
    ModelRole,
    ParseStatus,
    Platform,
    Role,
    RunState,
    Severity,
    VerificationLevel,
)
from fabloom.contracts.fabric import BgpSession, Device, FabricPlan, Interface, Link, Snapshot
from fabloom.contracts.intent import AddressingSpec, AsnPlan, FilterRule, Intent, TopologySpec
from fabloom.contracts.run import RUN_EVENT_ADAPTER, RunBudget, RunEvent, Usage
from fabloom.contracts.scenario import Scenario
from fabloom.contracts.verifier import Verifier

__all__ = [
    "RUN_EVENT_ADAPTER",
    "AddressingSpec",
    "AsnPlan",
    "BgpMode",
    "BgpSession",
    "Check",
    "CheckKind",
    "CheckResult",
    "CheckSet",
    "Device",
    "FabricPlan",
    "FilterRule",
    "Intent",
    "Interface",
    "Link",
    "ModelRole",
    "ParseStatus",
    "Platform",
    "Role",
    "RunBudget",
    "RunEvent",
    "RunState",
    "Scenario",
    "Severity",
    "Snapshot",
    "TopologySpec",
    "UndefinedRef",
    "UnrecognizedLine",
    "Usage",
    "VerificationLevel",
    "VerificationReport",
    "Verifier",
    "score_checks",
]
