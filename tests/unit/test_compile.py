from __future__ import annotations

from collections import Counter
from ipaddress import IPv4Network

import pytest

from fabloom.checks import allocate, compile_checks
from fabloom.checks.compile import FULL_MESH_REACHABILITY_MAX_LEAVES, _reachability_pairs
from fabloom.contracts import (
    BgpMode,
    CheckKind,
    CheckSet,
    FilterRule,
    Intent,
    Platform,
    TopologySpec,
)

pytestmark = pytest.mark.unit


def _kinds(cs: CheckSet) -> Counter[CheckKind]:
    return Counter(c.kind for c in cs.checks)


def test_counts_for_2x2(frr_checkset: CheckSet) -> None:
    k = _kinds(frr_checkset)
    assert k[CheckKind.PARSE_CLEAN] == 4
    assert k[CheckKind.BGP_ESTABLISHED] == 4
    assert k[CheckKind.L3_EDGES] == 4
    # leaves see 3 other loopbacks each, spines see only the 2 leaves (not each other)
    assert k[CheckKind.ROUTE_PRESENT] == 2 * 3 + 2 * 2
    assert k[CheckKind.REACHABILITY] == 2
    assert k[CheckKind.NO_FORWARDING_LOOPS] == 1


def test_ids_unique_and_stable(frr_intent: Intent) -> None:
    a = compile_checks(frr_intent, allocate(frr_intent))
    b = compile_checks(frr_intent, allocate(frr_intent))
    ids = [c.id for c in a.checks]
    assert len(ids) == len(set(ids))
    assert a == b


def test_unnumbered_has_no_l3_edge_checks(frr_intent: Intent) -> None:
    intent = frr_intent.model_copy(update={"bgp_mode": BgpMode.UNNUMBERED})
    cs = compile_checks(intent, allocate(intent))
    assert _kinds(cs)[CheckKind.L3_EDGES] == 0
    session = next(c for c in cs.checks if c.kind is CheckKind.BGP_ESTABLISHED)
    assert session.params["a_ip"] is None


def test_no_loopback_checks_when_not_advertised(frr_intent: Intent) -> None:
    intent = frr_intent.model_copy(update={"advertise_loopbacks": False})
    k = _kinds(compile_checks(intent, allocate(intent)))
    assert k[CheckKind.ROUTE_PRESENT] == k[CheckKind.REACHABILITY] == 0


def _ssh_rule(devices: str = "leaf*") -> FilterRule:
    return FilterRule(devices=devices, dst_port=22, src_not_in=IPv4Network("10.0.0.0/8"))


def test_frr_filters_are_reported_unverifiable(frr_intent: Intent) -> None:
    intent = frr_intent.model_copy(update={"filters": [_ssh_rule()]})
    cs = compile_checks(intent, allocate(intent))
    assert _kinds(cs)[CheckKind.FILTER_DENIES] == 0
    assert len(cs.unverifiable) == 1
    assert "not modelled by Batfish" in cs.unverifiable[0]


def test_eos_filters_compile_per_matching_device() -> None:
    intent = Intent(
        platform=Platform.ARISTA_EOS,
        topology=TopologySpec(spines=2, leaves=3),
        filters=[_ssh_rule(), _ssh_rule("nomatch*")],
    )
    cs = compile_checks(intent, allocate(intent))
    filters = [c for c in cs.checks if c.kind is CheckKind.FILTER_DENIES]
    assert [c.params["device"] for c in filters] == ["leaf1", "leaf2", "leaf3"]
    assert filters[0].params["src_ips"] == "0.0.0.0/0 \\ 10.0.0.0/8"
    assert filters[0].params["ingress_interfaces"] == ["Ethernet1", "Ethernet2"]
    assert cs.unverifiable == ["deny tcp/22 to nomatch* from outside 10.0.0.0/8: no device "
                               "matches 'nomatch*'"]  # fmt: skip


def test_reachability_sampling_is_linear_for_big_fabrics() -> None:
    small = [f"leaf{i}" for i in range(1, FULL_MESH_REACHABILITY_MAX_LEAVES + 1)]
    assert len(_reachability_pairs(small)) == len(small) * (len(small) - 1)
    big = [f"leaf{i}" for i in range(1, 17)]
    pairs = _reachability_pairs(big)
    assert len(pairs) < 3 * len(big)
    assert len(set(pairs)) == len(pairs)
    assert all(a != b for a, b in pairs)
    assert {a for a, _ in pairs} == set(big)  # every leaf is a source


def test_reachability_leaves_out_only_what_the_intent_denies() -> None:
    telnet_everywhere = FilterRule(devices="*", protocol="tcp", dst_port=23)
    intent = Intent(
        platform=Platform.ARISTA_EOS,
        topology=TopologySpec(spines=2, leaves=2),
        # Sources inside 10.0.0.0/8 (every loopback) are allowed SSH, so it is not excluded.
        filters=[telnet_everywhere, _ssh_rule(), FilterRule(devices="spine*", dst_port=80)],
    )
    cs = compile_checks(intent, allocate(intent))
    reach = {c.id: c.params for c in cs.checks if c.kind is CheckKind.REACHABILITY}
    # HTTP is blocked only to the spines; traffic to a leaf must still get through.
    assert reach["reachability:leaf1->leaf2"]["intended_denials"] == [
        {"protocol": "tcp", "dst_port": 23}
    ]
    plain = compile_checks(intent.model_copy(update={"filters": []}), allocate(intent))
    assert all(
        c.params["intended_denials"] == [] for c in plain.checks if c.kind is CheckKind.REACHABILITY
    )
