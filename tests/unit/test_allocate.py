from __future__ import annotations

import random
from ipaddress import IPv4Network

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from fabloom.checks import AllocationError, allocate, allocate_fabric
from fabloom.checks.allocate import default_names, natural_key
from fabloom.contracts import AddressingSpec, BgpMode, Intent, Platform, Role, TopologySpec
from fabloom.contracts.platforms import spec

pytestmark = pytest.mark.unit

# Every platform with every BGP mode it supports (unnumbered is FRR-only).
PLATFORM_MODES = [
    (p, m) for p in Platform for m in BgpMode if m is BgpMode.NUMBERED or spec(p).unnumbered
]
intents = st.sampled_from(PLATFORM_MODES).flatmap(
    lambda pm: st.builds(
        Intent,
        platform=st.just(pm[0]),
        topology=st.builds(TopologySpec, spines=st.integers(1, 8), leaves=st.integers(1, 32)),
        bgp_mode=st.just(pm[1]),
    )
)


def _all_networks(intent: Intent) -> list[IPv4Network]:
    plan = allocate(intent)
    nets = [d.loopback.network for d in plan.devices]
    nets += [link.subnet for link in plan.links if link.subnet is not None]
    return nets


@given(intents)
@settings(max_examples=60, deadline=None)
def test_no_address_overlaps(intent: Intent) -> None:
    nets = _all_networks(intent)
    for i, a in enumerate(nets):
        for b in nets[i + 1 :]:
            assert not a.overlaps(b), (a, b)


@given(intents)
@settings(max_examples=40, deadline=None)
def test_deterministic(intent: Intent) -> None:
    assert allocate(intent) == allocate(intent)


@given(intents, st.randoms(use_true_random=False))
@settings(max_examples=40, deadline=None)
def test_stable_under_device_reordering(intent: Intent, rnd: random.Random) -> None:
    spines, leaves = default_names(intent)
    shuffled_s, shuffled_l = spines[:], leaves[:]
    rnd.shuffle(shuffled_s)
    rnd.shuffle(shuffled_l)
    assert allocate_fabric(intent, spines=shuffled_s, leaves=shuffled_l) == allocate(intent)


@given(intents)
@settings(max_examples=30, deadline=None)
def test_adding_a_leaf_never_renumbers(intent: Intent) -> None:
    if intent.topology.leaves == 32:
        return
    bigger = intent.model_copy(
        update={
            "topology": TopologySpec(
                spines=intent.topology.spines, leaves=intent.topology.leaves + 1
            )
        }
    )
    small, big = allocate(intent), allocate(bigger)
    for d in small.devices:
        bd = big.device(d.name)
        assert bd.loopback == d.loopback
        assert bd.asn == d.asn
        assert {i.name: i.address for i in d.interfaces}.items() <= {
            i.name: i.address for i in bd.interfaces
        }.items()


def test_shape_of_2x4(frr_intent: Intent) -> None:
    intent = frr_intent.model_copy(update={"topology": TopologySpec(spines=2, leaves=4)})
    plan = allocate(intent)
    assert plan.device_names == ["spine1", "spine2", "leaf1", "leaf2", "leaf3", "leaf4"]
    assert len(plan.links) == len(plan.sessions) == 8
    leaf3 = plan.device("leaf3")
    assert leaf3.role is Role.LEAF
    assert leaf3.asn == 65103
    assert str(leaf3.loopback) == "10.0.1.3/32"
    assert [i.name for i in leaf3.interfaces] == ["swp1", "swp2"]
    assert all(s.b_asn == 65000 for s in plan.sessions)


def test_unnumbered_has_no_link_addresses(frr_intent: Intent) -> None:
    plan = allocate(frr_intent.model_copy(update={"bgp_mode": BgpMode.UNNUMBERED}))
    assert all(link.subnet is None for link in plan.links)
    assert all(s.a_ip is None and s.b_ip is None for s in plan.sessions)


def test_eos_interface_names() -> None:
    plan = allocate(Intent(platform=Platform.ARISTA_EOS, topology=TopologySpec(spines=1, leaves=2)))
    assert [i.name for i in plan.device("spine1").interfaces] == ["Ethernet1", "Ethernet2"]


def test_pool_too_small() -> None:
    intent = Intent(
        platform=Platform.FRR,
        topology=TopologySpec(spines=2, leaves=8),
        addressing=AddressingSpec(loopback_pool=IPv4Network("10.0.0.0/28")),
    )
    with pytest.raises(AllocationError, match="loopback pool"):
        allocate(intent)


def test_p2p_pool_too_small() -> None:
    intent = Intent(
        platform=Platform.FRR,
        topology=TopologySpec(spines=2, leaves=8),
        addressing=AddressingSpec(p2p_pool=IPv4Network("10.1.0.0/26")),
    )
    with pytest.raises(AllocationError, match="p2p pool"):
        allocate(intent)


def test_duplicate_names_rejected(frr_intent: Intent) -> None:
    with pytest.raises(AllocationError, match="unique"):
        allocate_fabric(frr_intent, spines=["x1"], leaves=["x1"])


def test_natural_key_orders_numbers() -> None:
    assert sorted(["leaf10", "leaf9", "leaf1"], key=natural_key) == ["leaf1", "leaf9", "leaf10"]


@pytest.mark.parametrize("platform", list(Platform))
def test_ports_follow_each_platforms_naming(platform: Platform) -> None:
    plan = allocate(Intent(platform=platform, topology=TopologySpec(spines=2, leaves=2)))
    leaf1 = plan.device("leaf1")
    assert [i.name for i in leaf1.interfaces] == [spec(platform).port(1), spec(platform).port(2)]


def test_platform_port_names() -> None:
    assert spec(Platform.JUNOS).port(3) == "et-0/0/3"
    assert spec(Platform.JUNOS).l3_interface("et-0/0/3") == "et-0/0/3.0"
    assert spec(Platform.CISCO_NXOS).port(3) == "Ethernet1/3"
    assert spec(Platform.SONIC).port(1) == "Ethernet0"
    assert spec(Platform.SONIC).port(3) == "Ethernet8"
    assert spec(Platform.FRR).port(3) == "swp3"  # unchanged since v0.1
