"""Deterministic pre-parse linter.

Rejects obviously malformed candidates before a verifier execution is spent on them. Every
rule here is cheap, has no false positives on a well-formed config, and produces a
remediation the repairer can act on.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from fabloom.checks import REMEDIATION, SEVERITY
from fabloom.contracts import (
    Check,
    CheckKind,
    CheckResult,
    FabricPlan,
    VerificationLevel,
    VerificationReport,
    score_checks,
)

_FENCE = re.compile(r"^\s*```", re.M)
_TAG = re.compile(r"</?device\b", re.I)
_PLACEHOLDER = re.compile(r"<[A-Z_][A-Z0-9_ -]*>|\bTODO\b|^\s*\.\.\.\s*$", re.M)
_HOSTNAME = re.compile(r"^\s*hostname\s+(\S+)\s*$", re.M)
_ROUTER_BGP = re.compile(r"^\s*router bgp\s+(\d+)\b", re.M)

MAX_ISSUES_PER_DEVICE = 10


def _device_issues(fabric: FabricPlan, device: str, config: str) -> list[str]:
    if not config.strip():
        return ["config is empty"]
    issues: list[str] = []
    if _FENCE.search(config):
        issues.append("contains a markdown code fence; emit raw configuration only")
    if _TAG.search(config):
        issues.append("contains a <device> tag inside the device body")
    for m in _PLACEHOLDER.finditer(config):
        issues.append(f"contains a placeholder: {m.group(0).strip()!r}")
    hostnames = _HOSTNAME.findall(config)
    if not hostnames:
        issues.append(f"missing 'hostname {device}'")
    elif hostnames[0] != device:
        issues.append(f"hostname is {hostnames[0]!r}, expected {device!r}")
    asns = _ROUTER_BGP.findall(config)
    expected_asn = fabric.device(device).asn
    if not asns:
        issues.append(f"missing 'router bgp {expected_asn}'")
    elif int(asns[0]) != expected_asn:
        issues.append(f"router bgp {asns[0]}, but the plan assigns AS{expected_asn}")
    return issues[:MAX_ISSUES_PER_DEVICE]


def prelint(fabric: FabricPlan, configs: Mapping[str, str]) -> list[CheckResult]:
    """One result per planned device, plus one for unexpected device blocks if there are any."""
    results: list[CheckResult] = []
    for device in fabric.device_names:
        issues = (
            ["no <device> block for this device"]
            if device not in configs
            else _device_issues(fabric, device, configs[device])
        )
        results.append(
            CheckResult(
                id=f"{CheckKind.PRELINT}:{device}",
                kind=CheckKind.PRELINT,
                passed=not issues,
                severity=SEVERITY[CheckKind.PRELINT],
                evidence={"issues": issues} if issues else {},
                remediation_hint=REMEDIATION[CheckKind.PRELINT] if issues else None,
            )
        )
    unexpected = sorted(set(configs) - set(fabric.device_names))
    if unexpected:
        results.append(
            CheckResult(
                id=f"{CheckKind.PRELINT}:unexpected_devices",
                kind=CheckKind.PRELINT,
                passed=False,
                severity=SEVERITY[CheckKind.PRELINT],
                evidence={"unexpected": unexpected, "planned": fabric.device_names},
                remediation_hint="Emit blocks only for the planned devices.",
            )
        )
    return results


def prelint_report(
    prelint_results: Sequence[CheckResult], checks: Sequence[Check]
) -> VerificationReport:
    """The report for a candidate that never reached the verifier.

    Every compiled check is reported as not run (failed), so the score can only go up once
    the candidate is well-formed enough to be verified for real.
    """
    not_run = [
        CheckResult(
            id=c.id,
            kind=c.kind,
            passed=False,
            severity=c.severity,
            evidence={"not_run": "candidate failed pre-lint"},
        )
        for c in checks
    ]
    results = [*prelint_results, *not_run]
    return VerificationReport(
        level=VerificationLevel.L0,
        checks=results,
        score=score_checks(results),
        duration_ms=0,
        verifier_name="prelint",
        verifier_version="1",
        backend_executed=False,
    )
