"""Prompt templates for the four model roles.

Templates are plain module constants so ``prompt_hash()`` can fingerprint them: a benchmark
run records the hash, and a prompt change is visible as a new MLflow run, not a silent drift.
Structured roles carry the JSON schema in the system prompt *and* in ``response_format``,
per Nebius guidance.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from fabloom.contracts import (
    BgpMode,
    CheckResult,
    CheckSet,
    FabricPlan,
    Intent,
    Platform,
    VerificationReport,
)
from fabloom.llm.schema import strict_json_schema

MAX_FAILED_CHECKS_IN_PROMPT = 40
MAX_UNRECOGNIZED_IN_PROMPT = 30

PLANNER_SYSTEM = """\
You turn a network engineer's request for a data center leaf-spine underlay into a JSON
intent. Output only a JSON object matching this schema:

{schema}

Rules:
- Use the defaults below for anything the request does not state. Never invent requirements.
  Defaults: {defaults}
- platform: "frr" or "arista_eos". If the request does not say, use "{platform_hint}".
- bgp_mode: "unnumbered" only if the request asks for unnumbered/interface BGP.
- filters: one entry per traffic-blocking requirement, e.g. "block SSH to the leaves from
  outside 10.0.0.0/8" -> {{"devices": "leaf*", "action": "deny", "protocol": "tcp",
  "dst_port": 22, "src_not_in": "10.0.0.0/8"}}.
"""

GENERATOR_SYSTEM = """\
You are a senior network engineer writing production {platform_name} configuration for a
leaf-spine eBGP underlay. The addressing, ASNs and cabling are already decided in the plan
you are given; follow it exactly. Do not add features that were not asked for.

{platform_rules}

Output contract, strictly: one block per planned device and nothing else, no prose, no
markdown:
<device name="DEVICE" platform="{platform}">
...complete configuration for DEVICE...
</device>
"""

FRR_RULES = """\
FRR rules:
- Each block is a complete frr.conf. Start with "frr version 8.4", "frr defaults datacenter",
  then "hostname DEVICE".
- Put every address in frr.conf: "interface lo" / " ip address LOOPBACK/32", and for each
  numbered link "interface swpN" / " ip address A.B.C.D/31".
- "router bgp ASN", " bgp router-id LOOPBACK-IP".
- Numbered neighbors: " neighbor PEER-IP remote-as PEER-ASN".
- Unnumbered neighbors: " neighbor swpN interface remote-as external".
- " address-family ipv4 unicast" / "  network LOOPBACK/32" / " exit-address-family".
- Only FRR syntax. No Linux shell, ifupdown, iptables or nftables commands."""

EOS_RULES = """\
Arista EOS rules:
- Each block is a complete running-config: "hostname DEVICE", "service routing protocols
  model multi-agent", "ip routing".
- Loopback: "interface Loopback0" / " ip address LOOPBACK/32".
- Each fabric link: "interface EthernetN" / " no switchport" / " ip address A.B.C.D/31".
- "router bgp ASN" / " router-id LOOPBACK-IP" / " neighbor PEER-IP remote-as PEER-ASN" /
  " network LOOPBACK/32".
- Traffic filters: an "ip access-list NAME" that denies exactly what the intent says and
  ends with "permit ip any any", applied with "ip access-group NAME in" on every fabric link
  of the matching devices. It must not block BGP (tcp/179) between fabric peers."""

REPAIR_SYSTEM = (
    GENERATOR_SYSTEM
    + """
You are repairing configuration that failed verification. Fix the causes of the failed
checks without changing anything that already works. Return complete blocks only for the
devices you change; devices you do not return keep their current configuration.
"""
)

EXPLAINER_SYSTEM = """\
You summarise a network configuration verification result for a network engineer in at
most 120 words of plain text. State the verification level and what it means, what passed,
what failed and why, and anything that could not be verified. Claim nothing the report does
not show. No markdown headings, no lists longer than five items."""

_LEVEL_MEANING = {
    "L0": "advisory: not checked by a verifier",
    "L1": "rule checked",
    "L2": "statically verified with Batfish",
    "L3": "runtime verified",
}


def _platform_name(platform: Platform) -> str:
    return {Platform.FRR: "FRR", Platform.ARISTA_EOS: "Arista EOS"}[platform]


def _platform_rules(platform: Platform) -> str:
    return {Platform.FRR: FRR_RULES, Platform.ARISTA_EOS: EOS_RULES}[platform]


def render_plan(intent: Intent, fabric: FabricPlan) -> str:
    """The decided fabric as compact text: the generator must follow it exactly."""
    lines = [
        f"platform: {fabric.platform}",
        f"bgp: eBGP, {fabric.bgp_mode}",
        f"advertise loopbacks into BGP: {'yes' if intent.advertise_loopbacks else 'no'}",
        "",
        "devices:",
    ]
    for d in fabric.devices:
        lines.append(f"- {d.name}: role={d.role} asn={d.asn} loopback={d.loopback}")
        for i in d.interfaces:
            addr = f" address={i.address}" if i.address else " (unnumbered)"
            lines.append(f"    {i.name} -> {i.peer_device} {i.peer_interface}{addr}")
    lines += ["", "bgp sessions (leaf <-> spine):"]
    for s in fabric.sessions:
        a = f"{s.a_device} {s.a_interface}" + (f" {s.a_ip}" if s.a_ip else "")
        b = f"{s.b_device} {s.b_interface}" + (f" {s.b_ip}" if s.b_ip else "")
        lines.append(f"- {a} AS{s.a_asn} <-> {b} AS{s.b_asn}")
    if intent.filters:
        lines += ["", "filters:"]
        for f in intent.filters:
            src = f" from outside {f.src_not_in}" if f.src_not_in else " from anywhere"
            lines.append(f"- on {f.devices}: deny {f.protocol}/{f.dst_port} to the device{src}")
    if fabric.bgp_mode is BgpMode.UNNUMBERED:
        lines += ["", "links carry no IPv4 addresses; peer over the interfaces."]
    return "\n".join(lines)


def planner_messages(request: str, platform_hint: Platform) -> list[dict[str, str]]:
    schema = json.dumps(strict_json_schema(Intent), indent=None)
    defaults = Intent(platform=platform_hint, topology={"spines": 2, "leaves": 4}).model_dump(
        mode="json", exclude={"platform", "topology"}
    )
    system = PLANNER_SYSTEM.format(
        schema=schema, defaults=json.dumps(defaults), platform_hint=platform_hint.value
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": request}]


def generator_messages(intent: Intent, fabric: FabricPlan) -> list[dict[str, str]]:
    system = GENERATOR_SYSTEM.format(
        platform_name=_platform_name(fabric.platform),
        platform_rules=_platform_rules(fabric.platform),
        platform=fabric.platform.value,
    )
    user = (
        f"Write the configuration for every device in this plan.\n\n{render_plan(intent, fabric)}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _failed_check_lines(
    failed: Sequence[CheckResult], descriptions: Mapping[str, str]
) -> list[str]:
    lines: list[str] = []
    for c in failed[:MAX_FAILED_CHECKS_IN_PROMPT]:
        lines.append(f"- {c.id}: {descriptions.get(c.id, c.kind)}")
        if c.evidence:
            lines.append(f"    evidence: {json.dumps(c.evidence, default=str)[:600]}")
        if c.remediation_hint:
            lines.append(f"    hint: {c.remediation_hint}")
    if len(failed) > MAX_FAILED_CHECKS_IN_PROMPT:
        lines.append(f"- ... and {len(failed) - MAX_FAILED_CHECKS_IN_PROMPT} more")
    return lines


def repair_messages(
    intent: Intent,
    fabric: FabricPlan,
    configs: Mapping[str, str],
    report: VerificationReport,
    checkset: CheckSet,
) -> list[dict[str, str]]:
    system = REPAIR_SYSTEM.format(
        platform_name=_platform_name(fabric.platform),
        platform_rules=_platform_rules(fabric.platform),
        platform=fabric.platform.value,
    )
    descriptions = {c.id: c.description for c in checkset.checks}
    parts = [
        "Plan:",
        render_plan(intent, fabric),
        "",
        f"Failed checks ({len(report.failed_checks)} of {len(report.checks)}):",
        *_failed_check_lines(report.failed_checks, descriptions),
    ]
    if report.unrecognized_lines:
        parts += ["", "Lines the platform parser does not recognise (invented or wrong syntax):"]
        parts += [
            f"- {u.device} line {u.line_number}: {u.text}"
            for u in report.unrecognized_lines[:MAX_UNRECOGNIZED_IN_PROMPT]
        ]
    parts += ["", "Current configuration:"]
    for name in fabric.device_names:
        if name in configs:
            parts.append(
                f'<device name="{name}" platform="{fabric.platform}">\n'
                f"{configs[name].rstrip()}\n</device>"
            )
    return [{"role": "system", "content": system}, {"role": "user", "content": "\n".join(parts)}]


def explainer_messages(
    outcome: str,
    report: VerificationReport | None,
    checkset: CheckSet | None,
    repair_rounds: int,
) -> list[dict[str, str]]:
    payload: dict[str, Any] = {"outcome": outcome, "repair_rounds": repair_rounds}
    if report is not None:
        payload["level"] = f"{report.level}: {_LEVEL_MEANING[report.level]}"
        payload["summary"] = report.summary()
        payload["failed"] = [
            {"id": c.id, "evidence": c.evidence} for c in report.failed_checks[:15]
        ]
    if checkset is not None and checkset.unverifiable:
        payload["not_verifiable"] = checkset.unverifiable
    return [
        {"role": "system", "content": EXPLAINER_SYSTEM},
        {"role": "user", "content": json.dumps(payload, default=str, indent=1)},
    ]


def prompt_hash() -> str:
    from fabloom.provenance import content_key  # noqa: PLC0415 - avoid import cycle at load

    return content_key(
        [PLANNER_SYSTEM, GENERATOR_SYSTEM, FRR_RULES, EOS_RULES, REPAIR_SYSTEM, EXPLAINER_SYSTEM]
    )
