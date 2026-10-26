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
    Platform,
    VerificationLevel,
    VerificationReport,
    score_checks,
)

_FENCE = re.compile(r"^\s*```", re.M)
_TAG = re.compile(r"</?device\b", re.I)
_PLACEHOLDER = re.compile(r"<[A-Z_][A-Z0-9_ -]*>|\bTODO\b|^\s*\.\.\.\s*$", re.M)
_HOSTNAME = re.compile(r"^\s*hostname\s+(\S+)\s*$", re.M)
_ROUTER_BGP = re.compile(r"^\s*router bgp\s+(\d+)\b", re.M)
# Junos names the same two facts differently, and they need not start a line: Nemotron wrote
# "system { host-name leaf1; }" on one line, which a line-anchored pattern rejected for four
# rounds (live run, 2026-10-26). Matches hierarchical, one-line and "set" forms.
_JUNOS_HOSTNAME = re.compile(r"(?<![\w-])host-name\s+\"?([^\s;\"}]+)")
_JUNOS_ASN = re.compile(r"(?<![\w-])autonomous-system\s+(\d+)\b")

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
    junos = fabric.platform is Platform.JUNOS
    host_re, host_stmt = (_JUNOS_HOSTNAME, "host-name") if junos else (_HOSTNAME, "hostname")
    asn_re, asn_stmt = (_JUNOS_ASN, "autonomous-system") if junos else (_ROUTER_BGP, "router bgp")
    hostnames = host_re.findall(config)
    if not hostnames:
        issues.append(f"missing '{host_stmt} {device}'")
    elif hostnames[0] != device:
        issues.append(f"{host_stmt} is {hostnames[0]!r}, expected {device!r}")
    asns = asn_re.findall(config)
    expected_asn = fabric.device(device).asn
    if not asns:
        issues.append(f"missing '{asn_stmt} {expected_asn}'")
    elif int(asns[0]) != expected_asn:
        issues.append(f"{asn_stmt} {asns[0]}, but the plan assigns AS{expected_asn}")
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
