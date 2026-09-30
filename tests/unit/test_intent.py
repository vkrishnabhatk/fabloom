from __future__ import annotations

from ipaddress import IPv4Network

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from fabloom.contracts import (
    AddressingSpec,
    AsnPlan,
    BgpMode,
    FilterRule,
    Intent,
    Platform,
    Scenario,
    TopologySpec,
)

pytestmark = pytest.mark.unit

filters = st.builds(
    FilterRule,
    devices=st.sampled_from(["leaf*", "spine*", "*"]),
    protocol=st.sampled_from(["tcp", "udp"]),
    dst_port=st.integers(1, 65535),
    src_not_in=st.one_of(st.none(), st.just(IPv4Network("10.0.0.0/8"))),
)
intents = st.builds(
    Intent,
    platform=st.sampled_from(list(Platform)),
    topology=st.builds(TopologySpec, spines=st.integers(1, 8), leaves=st.integers(1, 32)),
    bgp_mode=st.sampled_from(list(BgpMode)),
    advertise_loopbacks=st.booleans(),
    filters=st.lists(filters, max_size=3),
)


@given(intents)
@settings(max_examples=100)
def test_json_round_trip(intent: Intent) -> None:
    assert Intent.model_validate_json(intent.model_dump_json()) == intent


def test_rejects_overlapping_pools() -> None:
    with pytest.raises(ValidationError, match="overlap"):
        AddressingSpec(
            loopback_pool=IPv4Network("10.0.0.0/16"), p2p_pool=IPv4Network("10.0.1.0/24")
        )


def test_rejects_spine_asn_inside_leaf_range() -> None:
    with pytest.raises(ValidationError, match="collides"):
        Intent(
            platform=Platform.FRR,
            topology=TopologySpec(spines=2, leaves=4),
            asn=AsnPlan(spine_asn=65102, leaf_asn_base=65101),
        )


def test_rejects_leaf_asns_past_private_range() -> None:
    with pytest.raises(ValidationError, match="private range"):
        Intent(
            platform=Platform.FRR,
            topology=TopologySpec(spines=2, leaves=4),
            asn=AsnPlan(spine_asn=65000, leaf_asn_base=65533),
        )


def test_extra_fields_forbidden() -> None:
    with pytest.raises(ValidationError):
        Intent.model_validate({"platform": "frr", "topology": {"spines": 1, "leaves": 1}, "x": 1})


def test_scenario_id_shape(frr_intent: Intent) -> None:
    with pytest.raises(ValidationError, match="must look like"):
        Scenario(id="Bad_ID", family="f", request="a long enough request", intent=frr_intent)
