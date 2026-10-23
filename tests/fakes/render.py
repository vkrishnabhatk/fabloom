"""A tiny known-good renderer, for tests only.

Produces the configuration a correct model would write for a fabric plan. It is not the
phase-2 reference renderer; it exists so tests can start from a passing candidate and break
it on purpose.
"""

from __future__ import annotations

from fnmatch import fnmatchcase

from fabloom.contracts import BgpMode, Device, FabricPlan, Intent, Platform


def _frr(fabric: FabricPlan, d: Device) -> str:
    lines = ["frr version 8.4", "frr defaults datacenter", f"hostname {d.name}", "!"]
    lines += ["interface lo", f" ip address {d.loopback}", "!"]
    for i in d.interfaces:
        lines.append(f"interface {i.name}")
        if i.address:
            lines.append(f" ip address {i.address}")
        lines.append("!")
    lines += [f"router bgp {d.asn}", f" bgp router-id {d.router_id}"]
    for s in fabric.sessions:
        if d.name not in (s.a_device, s.b_device):
            continue
        local_a = d.name == s.a_device
        if fabric.bgp_mode is BgpMode.UNNUMBERED:
            iface = s.a_interface if local_a else s.b_interface
            lines.append(f" neighbor {iface} interface remote-as external")
        else:
            peer_ip, peer_asn = (s.b_ip, s.b_asn) if local_a else (s.a_ip, s.a_asn)
            lines.append(f" neighbor {peer_ip} remote-as {peer_asn}")
    lines += [
        " address-family ipv4 unicast",
        f"  network {d.loopback.network}",
        " exit-address-family",
        "!",
    ]
    return "\n".join(lines) + "\n"


def _eos(intent: Intent, fabric: FabricPlan, d: Device) -> str:
    lines = [
        f"hostname {d.name}",
        "service routing protocols model multi-agent",
        "ip routing",
        "!",
    ]
    rules = [r for r in intent.filters if fnmatchcase(d.name, r.devices)]
    if rules:
        lines.append("ip access-list FABLOOM-PROTECT")
        seq = 10
        # Only traffic to this device's own addresses: "deny any any" on a spine would also
        # drop transit traffic between leaves the filter does not cover (filters-0006, live
        # Batfish 2026-10-23).
        own = [d.loopback.ip, *(i.address.ip for i in d.interfaces if i.address)]
        for r in rules:
            for ip in own:
                # "deny from outside X" = permit from X first, then deny from anywhere.
                if r.src_not_in is not None:
                    lines.append(
                        f"   {seq} permit {r.protocol} {r.src_not_in} host {ip} eq {r.dst_port}"
                    )
                    seq += 10
                lines.append(f"   {seq} deny {r.protocol} any host {ip} eq {r.dst_port}")
                seq += 10
        lines += [f"   {seq} permit ip any any", "!"]
    lines += ["interface Loopback0", f"   ip address {d.loopback}", "!"]
    for i in d.interfaces:
        lines += [f"interface {i.name}", "   no switchport", f"   ip address {i.address}"]
        if rules:
            lines.append("   ip access-group FABLOOM-PROTECT in")
        lines.append("!")
    lines += [f"router bgp {d.asn}", f"   router-id {d.router_id}"]
    for s in fabric.sessions:
        if d.name == s.a_device:
            lines.append(f"   neighbor {s.b_ip} remote-as {s.b_asn}")
        elif d.name == s.b_device:
            lines.append(f"   neighbor {s.a_ip} remote-as {s.a_asn}")
    lines += [f"   network {d.loopback.network}", "!"]
    return "\n".join(lines) + "\n"


def render_good_configs(intent: Intent, fabric: FabricPlan) -> dict[str, str]:
    if fabric.platform is Platform.FRR:
        return {d.name: _frr(fabric, d) for d in fabric.devices}
    return {d.name: _eos(intent, fabric, d) for d in fabric.devices}


def as_device_blocks(configs: dict[str, str], platform: Platform) -> str:
    """The generator output contract, as a model would emit it."""
    return "\n".join(
        f'<device name="{name}" platform="{platform}">\n{text.rstrip()}\n</device>'
        for name, text in configs.items()
    )
