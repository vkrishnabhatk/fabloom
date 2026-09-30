"""Deterministic address/ASN/interface allocator: ``Intent`` -> ``FabricPlan``.

Properties (tested with hypothesis):
- no two addresses overlap, loopbacks and link subnets included;
- the same intent always yields the same plan;
- the plan does not depend on the order devices are listed in;
- adding a leaf or a spine never renumbers an existing device or link.

The last property is why link subnets are strided by ``MAX_SPINES`` rather than packed:
a /16 point-to-point pool has room for it, and stable addresses keep diffs reviewable.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from ipaddress import IPv4Interface, IPv4Network

from fabloom.contracts import (
    BgpMode,
    BgpSession,
    Device,
    FabricPlan,
    Intent,
    Interface,
    Link,
    Platform,
    Role,
)
from fabloom.contracts.intent import MAX_SPINES

_INTERFACE_PREFIX: dict[Platform, str] = {
    Platform.FRR: "swp",
    Platform.ARISTA_EOS: "Ethernet",
}


class AllocationError(ValueError):
    """The intent cannot be laid out in the pools it names."""


def natural_key(name: str) -> tuple[str | int, ...]:
    """``leaf10`` sorts after ``leaf9``."""
    return tuple(int(p) if p.isdigit() else p for p in re.split(r"(\d+)", name) if p)


def default_names(intent: Intent) -> tuple[list[str], list[str]]:
    spines = [f"spine{i}" for i in range(1, intent.topology.spines + 1)]
    leaves = [f"leaf{i}" for i in range(1, intent.topology.leaves + 1)]
    return spines, leaves


def allocate(intent: Intent) -> FabricPlan:
    spines, leaves = default_names(intent)
    return allocate_fabric(intent, spines=spines, leaves=leaves)


def allocate_fabric(intent: Intent, *, spines: Sequence[str], leaves: Sequence[str]) -> FabricPlan:
    spines = sorted(spines, key=natural_key)
    leaves = sorted(leaves, key=natural_key)
    if len(set(spines) | set(leaves)) != len(spines) + len(leaves):
        raise AllocationError("device names must be unique")
    if len(spines) > MAX_SPINES:
        raise AllocationError(f"at most {MAX_SPINES} spines are supported")

    lo_pool = intent.addressing.loopback_pool
    half = lo_pool.num_addresses // 2
    # Each half keeps its network address unused, and the second half its broadcast too.
    if len(spines) > half - 1 or len(leaves) > half - 2:
        raise AllocationError(f"loopback pool {lo_pool} is too small for this fabric")

    numbered = intent.bgp_mode is BgpMode.NUMBERED
    p2p_pool = intent.addressing.p2p_pool
    if numbered and len(leaves) * MAX_SPINES * 2 > p2p_pool.num_addresses:
        raise AllocationError(f"p2p pool {p2p_pool} is too small for {len(leaves)} leaves")

    prefix = _INTERFACE_PREFIX[intent.platform]

    def loopback(role: Role, index: int) -> IPv4Interface:
        offset = index if role is Role.SPINE else half + index
        return IPv4Interface(f"{lo_pool.network_address + offset}/32")

    spine_ifaces: dict[str, list[Interface]] = {s: [] for s in spines}
    leaf_ifaces: dict[str, list[Interface]] = {leaf: [] for leaf in leaves}
    links: list[Link] = []
    sessions: list[BgpSession] = []

    for li, leaf in enumerate(leaves, start=1):
        leaf_asn = intent.asn.leaf_asn_base + li - 1
        for si, spine in enumerate(spines, start=1):
            leaf_if, spine_if = f"{prefix}{si}", f"{prefix}{li}"
            subnet: IPv4Network | None = None
            spine_addr: IPv4Interface | None = None
            leaf_addr: IPv4Interface | None = None
            if numbered:
                base = p2p_pool.network_address + 2 * ((li - 1) * MAX_SPINES + (si - 1))
                subnet = IPv4Network(f"{base}/31")
                spine_addr = IPv4Interface(f"{base}/31")
                leaf_addr = IPv4Interface(f"{base + 1}/31")
            leaf_ifaces[leaf].append(
                Interface(
                    name=leaf_if,
                    peer_device=spine,
                    peer_interface=spine_if,
                    address=leaf_addr,
                )
            )
            spine_ifaces[spine].append(
                Interface(
                    name=spine_if,
                    peer_device=leaf,
                    peer_interface=leaf_if,
                    address=spine_addr,
                )
            )
            links.append(
                Link(
                    a_device=leaf,
                    a_interface=leaf_if,
                    b_device=spine,
                    b_interface=spine_if,
                    subnet=subnet,
                )
            )
            sessions.append(
                BgpSession(
                    a_device=leaf,
                    a_interface=leaf_if,
                    a_asn=leaf_asn,
                    a_ip=leaf_addr.ip if leaf_addr else None,
                    b_device=spine,
                    b_interface=spine_if,
                    b_asn=intent.asn.spine_asn,
                    b_ip=spine_addr.ip if spine_addr else None,
                )
            )

    devices = [
        Device(
            name=s,
            role=Role.SPINE,
            asn=intent.asn.spine_asn,
            loopback=loopback(Role.SPINE, i),
            interfaces=sorted(spine_ifaces[s], key=lambda x: natural_key(x.name)),
        )
        for i, s in enumerate(spines, start=1)
    ] + [
        Device(
            name=leaf,
            role=Role.LEAF,
            asn=intent.asn.leaf_asn_base + i - 1,
            loopback=loopback(Role.LEAF, i),
            interfaces=leaf_ifaces[leaf],
        )
        for i, leaf in enumerate(leaves, start=1)
    ]
    return FabricPlan(
        platform=intent.platform,
        bgp_mode=intent.bgp_mode,
        devices=devices,
        links=links,
        sessions=sessions,
    )
