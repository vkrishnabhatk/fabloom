"""What differs between the platforms Fabloom writes configs for, in one place.

The checks, the snapshot writer, the prompts and the web pages read a platform's port
names, how Batfish names its interfaces, how its files are packaged and whether Batfish
can verify its filters from here, so adding a platform is one entry here plus its
generator rules (ADR-0009). Probed against live Batfish 2026.08.27 on 2026-10-26: a
hand-written 2-spine/2-leaf numbered eBGP fabric parsed cleanly and established 8/8
sessions on every platform below.

Vendors Batfish cannot parse are refused before any model call (``unsupported_vendor``):
Fabloom's point is that a config is verified, and an unverifiable one is a guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from fabloom.contracts.enums import Platform

Layout = Literal["single", "cumulus", "sonic"]


@dataclass(frozen=True)
class PlatformSpec:
    platform: Platform
    name: str
    # Front-panel port n (1-based), as the operator and the generated config name it.
    port_template: str
    # How the snapshot is packaged for Batfish: one file per device, FRR in Cumulus's
    # concatenated format, or SONiC's config_db.json + frr.conf per device directory.
    layout: Layout
    # First line that pins Batfish's vendor detection, for dialects it could confuse.
    batfish_marker: str | None = None
    # Batfish names Junos' unit-0 logical interface "et-0/0/1.0"; edges and flows use it.
    l3_suffix: str = ""
    # Batfish models packet filters written in this platform's config.
    filters_verifiable: bool = True
    # BGP unnumbered (interface peering) is generated and verified only where tested.
    unnumbered: bool = False

    def port(self, n: int) -> str:
        return self.port_template.format(n=n, lane=4 * (n - 1))

    def l3_interface(self, port: str) -> str:
        return port + self.l3_suffix


PLATFORMS: dict[Platform, PlatformSpec] = {
    Platform.FRR: PlatformSpec(
        Platform.FRR,
        "FRR",
        "swp{n}",
        "cumulus",
        # frr.conf has no packet filter; SSH filtering there lives in iptables/nftables.
        filters_verifiable=False,
        unnumbered=True,
    ),
    Platform.ARISTA_EOS: PlatformSpec(Platform.ARISTA_EOS, "Arista EOS", "Ethernet{n}", "single"),
    Platform.JUNOS: PlatformSpec(
        Platform.JUNOS,
        "Juniper Junos",
        "et-0/0/{n}",
        "single",
        batfish_marker="#RANCID-CONTENT-TYPE: juniper",
        l3_suffix=".0",
    ),
    Platform.CISCO_NXOS: PlatformSpec(
        Platform.CISCO_NXOS,
        "Cisco NX-OS",
        "Ethernet1/{n}",
        "single",
        batfish_marker="!RANCID-CONTENT-TYPE: cisco-nx",
    ),
    Platform.CISCO_IOSXE: PlatformSpec(
        Platform.CISCO_IOSXE,
        "Cisco IOS-XE",
        "GigabitEthernet1/0/{n}",
        "single",
        batfish_marker="!RANCID-CONTENT-TYPE: cisco",
    ),
    Platform.CISCO_IOSXR: PlatformSpec(
        Platform.CISCO_IOSXR,
        "Cisco IOS-XR",
        "HundredGigE0/0/0/{n}",
        "single",
        batfish_marker="!RANCID-CONTENT-TYPE: cisco-xr",
    ),
    Platform.SONIC: PlatformSpec(
        Platform.SONIC,
        "SONiC",
        # SONiC numbers ports by their first lane: Ethernet0, Ethernet4, ...
        "Ethernet{lane}",
        "sonic",
        # SONiC ACLs live in config_db.json ACL tables; not verified here yet.
        filters_verifiable=False,
    ),
}


def spec(platform: Platform) -> PlatformSpec:
    return PLATFORMS[platform]


SONIC_FILES = ("config_db.json", "frr.conf")
_FILE_BLOCK = re.compile(r'<file\s+name="(?P<name>[^"]+)"\s*>\n?(?P<body>.*?)</file>', re.S | re.I)


def device_files(platform: Platform, config: str) -> dict[str, str]:
    """A device's config as the files it is deployed as.

    SONiC is two files, which the generator writes as ``<file name="...">`` sections inside
    the device block; every other platform is one file, returned under ``""``.
    """
    if spec(platform).layout != "sonic":
        return {"": config}
    return {
        m.group("name").strip(): m.group("body").strip("\n") + "\n"
        for m in _FILE_BLOCK.finditer(config)
    }


# Network operating systems Batfish cannot parse, so Fabloom cannot verify configs for them.
_UNSUPPORTED = [
    ("Nokia", r"nokia|\bsr[ -]?(linux|os)\b|\bsros\b|\btimos\b"),
    ("Huawei", r"huawei|\bvrp\b"),
    ("Cumulus Linux", r"cumulus|\bnvue\b|\bnclu\b"),
    ("Dell OS10", r"\bdell\b|\bos10\b|\bos9\b"),
    ("Extreme Networks", r"extreme networks|\bexos\b|\bvoss\b"),
    ("HPE Aruba", r"\baruba\b|aos-cx|\bcomware\b"),
    ("MikroTik", r"mikrotik|routeros"),
    ("NVIDIA Onyx", r"\bonyx\b|mellanox"),
]
_UNSUPPORTED_RE = [(name, re.compile(pattern, re.IGNORECASE)) for name, pattern in _UNSUPPORTED]


def unsupported_vendor(request: str) -> str | None:
    """The unsupported vendor a request names, if any."""
    for name, pattern in _UNSUPPORTED_RE:
        if pattern.search(request):
            return name
    return None


def unsupported_message(vendor: str) -> str:
    supported = ", ".join(s.name for s in PLATFORMS.values())
    return (
        f"Fabloom can't verify {vendor} configs yet, because Batfish does not read them, so it "
        f"won't generate them either. Supported: {supported}."
    )
