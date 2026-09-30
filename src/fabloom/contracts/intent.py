"""The vendor-neutral intent: what the user asked for, before any addressing is decided.

The planner model fills this from plain language. Everything downstream that matters for
grading (the address plan, the check set) is derived from it deterministically, so a
benchmark scenario can pin a ground-truth ``Intent`` and grade against that instead of the
planner's reading of the request (ADR-0006).
"""

from __future__ import annotations

from ipaddress import IPv4Network
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fabloom.contracts.enums import BgpMode, Platform

PRIVATE_ASN_MIN = 64512
PRIVATE_ASN_MAX = 65534
MAX_SPINES = 8
MAX_LEAVES = 32


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TopologySpec(_Frozen):
    spines: int = Field(ge=1, le=MAX_SPINES, description="Number of spine switches.")
    leaves: int = Field(ge=1, le=MAX_LEAVES, description="Number of leaf switches.")


class AsnPlan(_Frozen):
    spine_asn: int = Field(
        default=65000,
        ge=PRIVATE_ASN_MIN,
        le=PRIVATE_ASN_MAX,
        description="One private ASN shared by every spine.",
    )
    leaf_asn_base: int = Field(
        default=65101,
        ge=PRIVATE_ASN_MIN,
        le=PRIVATE_ASN_MAX,
        description="ASN of leaf1; leaf N gets leaf_asn_base + N - 1.",
    )


class AddressingSpec(_Frozen):
    loopback_pool: IPv4Network = Field(
        default=IPv4Network("10.0.0.0/23"),
        description="Loopbacks: spines from the first half of the pool, leaves from the second.",
    )
    p2p_pool: IPv4Network = Field(
        default=IPv4Network("10.1.0.0/16"),
        description="Point-to-point /31 links for numbered BGP. Unused when unnumbered.",
    )

    @model_validator(mode="after")
    def _pools_disjoint(self) -> AddressingSpec:
        if self.loopback_pool.overlaps(self.p2p_pool):
            raise ValueError("loopback_pool and p2p_pool must not overlap")
        return self


class FilterRule(_Frozen):
    """Deny traffic to the matching devices themselves (management-plane protection)."""

    devices: str = Field(description='Glob over device names, e.g. "leaf*".')
    action: Literal["deny"] = "deny"
    protocol: Literal["tcp", "udp"] = "tcp"
    dst_port: int = Field(ge=1, le=65535)
    src_not_in: IPv4Network | None = Field(
        default=None,
        description="Deny only sources outside this prefix. null = deny from everywhere.",
    )


class Intent(_Frozen):
    platform: Platform
    topology: TopologySpec
    bgp_mode: BgpMode = BgpMode.NUMBERED
    asn: AsnPlan = AsnPlan()
    addressing: AddressingSpec = AddressingSpec()
    advertise_loopbacks: bool = True
    filters: list[FilterRule] = Field(default_factory=list)

    @model_validator(mode="after")
    def _asns_fit(self) -> Intent:
        last_leaf_asn = self.asn.leaf_asn_base + self.topology.leaves - 1
        if last_leaf_asn > PRIVATE_ASN_MAX:
            raise ValueError(f"leaf ASNs run past the private range (last = {last_leaf_asn})")
        if self.asn.leaf_asn_base <= self.asn.spine_asn <= last_leaf_asn:
            raise ValueError("spine_asn collides with a leaf ASN")
        return self
