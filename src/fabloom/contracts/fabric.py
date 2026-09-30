"""The concrete fabric: every device, interface, address and BGP session, fully decided.

Produced deterministically from an ``Intent`` by ``fabloom.checks.allocate``. The generator is
told this plan; the checks are compiled from it; the snapshot builder lays it out for Batfish.
"""

from __future__ import annotations

from ipaddress import IPv4Address, IPv4Interface, IPv4Network

from pydantic import BaseModel, ConfigDict

from fabloom.contracts.enums import BgpMode, Platform, Role


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Interface(_Frozen):
    name: str
    peer_device: str
    peer_interface: str
    address: IPv4Interface | None  # None for BGP unnumbered


class Device(_Frozen):
    name: str
    role: Role
    asn: int
    loopback: IPv4Interface  # always a /32
    interfaces: list[Interface]

    @property
    def router_id(self) -> IPv4Address:
        return self.loopback.ip


class Link(_Frozen):
    a_device: str
    a_interface: str
    b_device: str
    b_interface: str
    subnet: IPv4Network | None  # None for BGP unnumbered


class BgpSession(_Frozen):
    """One expected eBGP session. ``a`` is always the leaf, ``b`` the spine."""

    a_device: str
    a_interface: str
    a_asn: int
    a_ip: IPv4Address | None
    b_device: str
    b_interface: str
    b_asn: int
    b_ip: IPv4Address | None


class FabricPlan(_Frozen):
    platform: Platform
    bgp_mode: BgpMode
    devices: list[Device]
    links: list[Link]
    sessions: list[BgpSession]

    def device(self, name: str) -> Device:
        for d in self.devices:
            if d.name == name:
                return d
        raise KeyError(name)

    @property
    def device_names(self) -> list[str]:
        return [d.name for d in self.devices]


class Snapshot(_Frozen):
    """Everything a verifier needs besides the candidate configs themselves."""

    name: str
    fabric: FabricPlan
