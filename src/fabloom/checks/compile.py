"""Deterministic compiler: (``Intent``, ``FabricPlan``) -> ``CheckSet``.

This is the grading rubric. It is pure, has no model in the loop, and is the only thing that
decides what a run is graded on (ADR-0001). Check ids are stable strings so results can be
diffed across runs, repair rounds and benchmark arms.
"""

from __future__ import annotations

from collections.abc import Sequence
from fnmatch import fnmatchcase
from ipaddress import IPv4Address
from itertools import permutations

from fabloom.contracts import (
    BgpMode,
    Check,
    CheckKind,
    CheckSet,
    FabricPlan,
    FilterRule,
    Intent,
    Role,
    Severity,
)
from fabloom.contracts.platforms import spec

SEVERITY: dict[CheckKind, Severity] = {
    CheckKind.PRELINT: Severity.CRITICAL,
    CheckKind.PARSE_CLEAN: Severity.CRITICAL,
    CheckKind.NO_UNDEFINED_REFS: Severity.HIGH,
    CheckKind.BGP_COMPATIBLE: Severity.HIGH,
    CheckKind.BGP_ESTABLISHED: Severity.HIGH,
    CheckKind.L3_EDGES: Severity.MEDIUM,
    CheckKind.ROUTE_PRESENT: Severity.MEDIUM,
    CheckKind.REACHABILITY: Severity.HIGH,
    CheckKind.FILTER_DENIES: Severity.HIGH,
    CheckKind.NO_FORWARDING_LOOPS: Severity.HIGH,
}

REMEDIATION: dict[CheckKind, str] = {
    CheckKind.PRELINT: "Emit exactly one <device> block per planned device, containing only "
    "configuration text for that device.",
    CheckKind.PARSE_CLEAN: "Remove or correct the unrecognized lines; use only commands that "
    "exist on this platform.",
    CheckKind.NO_UNDEFINED_REFS: "Define every route-map, prefix-list, ACL or peer-group before "
    "referencing it, or remove the reference.",
    CheckKind.BGP_COMPATIBLE: "Make both ends agree: neighbor address or interface, remote-as, "
    "and address family must match the plan.",
    CheckKind.BGP_ESTABLISHED: "Ensure the session is compatible on both ends and the peering "
    "addresses are reachable.",
    CheckKind.L3_EDGES: "Assign the planned address to both ends of the link.",
    CheckKind.ROUTE_PRESENT: "Advertise the loopback into BGP on its owner and make sure no "
    "policy filters it along the way.",
    CheckKind.REACHABILITY: "Check that both loopbacks are advertised and that every hop has a "
    "route to the destination.",
    CheckKind.FILTER_DENIES: "Apply an inbound ACL on every uplink that denies this traffic to "
    "the device's own addresses.",
    CheckKind.NO_FORWARDING_LOOPS: "Remove static routes or redistribution that send traffic "
    "back the way it came.",
}

# All ordered leaf pairs up to this size; above it, a hub-and-ring sample keeps the number of
# reachability queries linear in fabric size.
FULL_MESH_REACHABILITY_MAX_LEAVES = 6


def _check(kind: CheckKind, suffix: str, description: str, **params: object) -> Check:
    check_id = f"{kind}:{suffix}" if suffix else str(kind)
    return Check(
        id=check_id,
        kind=kind,
        severity=SEVERITY[kind],
        description=description,
        params=dict(params),
    )


def _reachability_pairs(leaves: list[str]) -> list[tuple[str, str]]:
    if len(leaves) <= FULL_MESH_REACHABILITY_MAX_LEAVES:
        return list(permutations(leaves, 2))
    hub = leaves[0]
    pairs: set[tuple[str, str]] = set()
    for leaf in leaves[1:]:
        pairs.update({(hub, leaf), (leaf, hub)})
    for a, b in zip(leaves, leaves[1:] + leaves[:1], strict=True):
        pairs.add((a, b))
    return sorted(pairs, key=lambda p: (leaves.index(p[0]), leaves.index(p[1])))


def _src_space(rule: FilterRule) -> str:
    # Batfish IP-space specifier grammar: "A \ B" is A minus B.
    return f"0.0.0.0/0 \\ {rule.src_not_in}" if rule.src_not_in else "0.0.0.0/0"


def _device_checks(fabric: FabricPlan) -> list[Check]:
    out: list[Check] = []
    for d in fabric.devices:
        out.append(
            _check(
                CheckKind.PARSE_CLEAN,
                d.name,
                f"{d.name} parses with no unrecognized lines",
                device=d.name,
            )
        )
        out.append(
            _check(
                CheckKind.NO_UNDEFINED_REFS,
                d.name,
                f"{d.name} references only defined structures",
                device=d.name,
            )
        )
    return out


def _session_checks(fabric: FabricPlan) -> list[Check]:
    l3 = spec(fabric.platform).l3_interface
    out: list[Check] = []
    for s in fabric.sessions:
        pair = f"{s.a_device}--{s.b_device}"
        common = {
            "a_device": s.a_device,
            "b_device": s.b_device,
            "a_interface": l3(s.a_interface),
            "b_interface": l3(s.b_interface),
            "a_ip": str(s.a_ip) if s.a_ip else None,
            "b_ip": str(s.b_ip) if s.b_ip else None,
        }
        out.append(
            _check(
                CheckKind.BGP_COMPATIBLE,
                pair,
                f"BGP between {s.a_device} (AS{s.a_asn}) and {s.b_device} (AS{s.b_asn}) "
                "is configured compatibly on both ends",
                **common,
            )
        )
        out.append(
            _check(
                CheckKind.BGP_ESTABLISHED,
                pair,
                f"BGP between {s.a_device} and {s.b_device} is established",
                **common,
            )
        )
    return out


def _link_checks(fabric: FabricPlan) -> list[Check]:
    if fabric.bgp_mode is not BgpMode.NUMBERED:
        return []  # unnumbered links carry no IPv4 addresses, so there is no L3 edge to check
    l3 = spec(fabric.platform).l3_interface
    return [
        _check(
            CheckKind.L3_EDGES,
            f"{link.a_device}[{link.a_interface}]--{link.b_device}[{link.b_interface}]",
            f"{link.a_device} {link.a_interface} and {link.b_device} {link.b_interface} "
            f"form a layer-3 edge on {link.subnet}",
            a_device=link.a_device,
            a_interface=l3(link.a_interface),
            b_device=link.b_device,
            b_interface=l3(link.b_interface),
        )
        for link in fabric.links
    ]


def _intended_denials(
    filters: Sequence[FilterRule], src_ip: IPv4Address, dst_device: str
) -> list[dict[str, object]]:
    """Traffic from ``src_ip`` to ``dst_device`` that the intent itself says to drop.

    Reachability must not count those flows as failures: Batfish answers "is any flow
    dropped?" with one example flow, and for a "block telnet to every switch" fabric that
    example is telnet to the destination leaf (found against live Batfish, 2026-10-23).
    """
    denied = {
        (rule.protocol, rule.dst_port)
        for rule in filters
        if fnmatchcase(dst_device, rule.devices)
        and (rule.src_not_in is None or src_ip not in rule.src_not_in)
    }
    return [{"protocol": proto, "dst_port": port} for proto, port in sorted(denied)]


def _loopback_checks(fabric: FabricPlan, filters: Sequence[FilterRule] = ()) -> list[Check]:
    out: list[Check] = []
    for node in fabric.devices:
        for owner in fabric.devices:
            if owner.name == node.name:
                continue
            if node.role is Role.SPINE and owner.role is Role.SPINE:
                continue  # eBGP loop prevention: spines share an ASN by design
            prefix = str(owner.loopback.network)
            out.append(
                _check(
                    CheckKind.ROUTE_PRESENT,
                    f"{node.name}:{prefix}",
                    f"{node.name} has a route to {owner.name}'s loopback {prefix}",
                    node=node.name,
                    prefix=prefix,
                    owner=owner.name,
                )
            )
    leaves = [d.name for d in fabric.devices if d.role is Role.LEAF]
    for src, dst in _reachability_pairs(leaves):
        src_ip, dst_ip = fabric.device(src).loopback.ip, fabric.device(dst).loopback.ip
        out.append(
            _check(
                CheckKind.REACHABILITY,
                f"{src}->{dst}",
                f"traffic from {src} loopback {src_ip} reaches {dst} loopback {dst_ip}",
                src_device=src,
                src_ip=str(src_ip),
                dst_device=dst,
                dst_ip=str(dst_ip),
                intended_denials=_intended_denials(filters, src_ip, dst),
            )
        )
    return out


def _filter_checks(intent: Intent, fabric: FabricPlan) -> tuple[list[Check], list[str]]:
    platform = spec(fabric.platform)
    out: list[Check] = []
    unverifiable: list[str] = []
    for rule in intent.filters:
        src = f" from outside {rule.src_not_in}" if rule.src_not_in else ""
        label = f"deny {rule.protocol}/{rule.dst_port} to {rule.devices}{src}"
        targets = [d for d in fabric.devices if fnmatchcase(d.name, rule.devices)]
        if not platform.filters_verifiable:
            unverifiable.append(f"{label}: {fabric.platform} filters are not modelled by Batfish")
        elif not targets:
            unverifiable.append(f"{label}: no device matches {rule.devices!r}")
        for d in targets if platform.filters_verifiable else []:
            out.append(
                _check(
                    CheckKind.FILTER_DENIES,
                    f"{d.name}:{rule.protocol}/{rule.dst_port}",
                    f"{d.name} drops {rule.protocol}/{rule.dst_port} to itself{src}",
                    device=d.name,
                    ingress_interfaces=[platform.l3_interface(i.name) for i in d.interfaces],
                    dst_ip=str(d.loopback.ip),
                    protocol=rule.protocol,
                    dst_port=rule.dst_port,
                    src_ips=_src_space(rule),
                )
            )
    return out, unverifiable


def compile_checks(intent: Intent, fabric: FabricPlan) -> CheckSet:
    filters, unverifiable = _filter_checks(intent, fabric)
    checks = [
        *_device_checks(fabric),
        *_session_checks(fabric),
        *_link_checks(fabric),
        *(_loopback_checks(fabric, intent.filters) if intent.advertise_loopbacks else []),
        *filters,
        _check(CheckKind.NO_FORWARDING_LOOPS, "", "no forwarding loops anywhere"),
    ]
    return CheckSet(checks=checks, unverifiable=unverifiable)
