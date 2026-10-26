"""A tiny known-good renderer, for tests only.

Produces the configuration a correct model would write for a fabric plan. It is not the
phase-2 reference renderer; it exists so tests can start from a passing candidate and break
it on purpose.
"""

from __future__ import annotations

import json
from fnmatch import fnmatchcase
from ipaddress import IPv4Address, IPv4Network

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


def _peers(fabric: FabricPlan, d: Device) -> list[tuple[str, int]]:
    """(peer IP, peer ASN) for every numbered session of ``d``."""
    out = []
    for s in fabric.sessions:
        if d.name == s.a_device:
            out.append((str(s.b_ip), s.b_asn))
        elif d.name == s.b_device:
            out.append((str(s.a_ip), s.a_asn))
    return out


def _filter_rules(intent: Intent, d: Device) -> list[tuple[str, int, str | None, list[str]]]:
    """(protocol, port, permitted source prefix or None, own addresses) per rule on ``d``.

    Only traffic to the device's own addresses is dropped: blocking "any" on a spine would
    also drop transit between leaves (filters-0006, live Batfish 2026-10-23).
    """
    own = [str(d.loopback.ip), *(str(i.address.ip) for i in d.interfaces if i.address)]
    return [
        (r.protocol, r.dst_port, str(r.src_not_in) if r.src_not_in else None, own)
        for r in intent.filters
        if fnmatchcase(d.name, r.devices)
    ]


def _junos(intent: Intent, fabric: FabricPlan, d: Device) -> str:
    rules = _filter_rules(intent, d)
    lines = ["#RANCID-CONTENT-TYPE: juniper", "system {", f"    host-name {d.name};", "}"]
    lines.append("interfaces {")
    for i in d.interfaces:
        lines += [f"    {i.name} {{", "        unit 0 {", "            family inet {"]
        if rules:
            lines.append("                filter input FABLOOM-PROTECT;")
        lines += [f"                address {i.address};", "            }", "        }", "    }"]
    lines += [
        "    lo0 {",
        "        unit 0 {",
        f"            family inet {{ address {d.loopback}; }}",
    ]
    lines += ["        }", "    }", "}"]
    lines += [
        "routing-options {",
        f"    router-id {d.router_id};",
        f"    autonomous-system {d.asn};",
        "}",
        "policy-options {",
        "    policy-statement ADVERTISE-LOOPBACK {",
        "        term loopback {",
        f"            from {{ protocol direct; route-filter {d.loopback.network} exact; }}",
        "            then accept;",
        "        }",
        "    }",
        "}",
    ]
    if rules:
        lines += ["firewall {", "    family inet {", "        filter FABLOOM-PROTECT {"]
        for n, (proto, port, allowed, own) in enumerate(rules):
            dst = " ".join(f"{ip}/32;" for ip in own)
            match = f"destination-address {{ {dst} }} protocol {proto}; destination-port {port};"
            if allowed:
                lines.append(
                    f"            term allow-{n} {{ from {{ source-address {{ {allowed}; }} "
                    f"{match} }} then accept; }}"
                )
            lines.append(f"            term deny-{n} {{ from {{ {match} }} then discard; }}")
        lines += ["            term rest { then accept; }", "        }", "    }", "}"]
    lines += ["protocols {", "    bgp {", "        group FABRIC {", "            type external;"]
    if intent.advertise_loopbacks:
        lines.append("            export ADVERTISE-LOOPBACK;")
    for ip, asn in _peers(fabric, d):
        # Without local-address Batfish cannot match the session (NO_LOCAL_IP), although
        # it comes up: Junos takes the outgoing interface's address (live Batfish 2026-10-26).
        local = next(
            i.address.ip for i in d.interfaces if i.address and IPv4Address(ip) in i.address.network
        )
        lines.append(f"            neighbor {ip} {{ peer-as {asn}; local-address {local}; }}")
    lines += ["        }", "    }", "}"]
    return "\n".join(lines) + "\n"


def _nxos(intent: Intent, fabric: FabricPlan, d: Device) -> str:
    rules = _filter_rules(intent, d)
    lines = ["!RANCID-CONTENT-TYPE: cisco-nx", f"hostname {d.name}", "feature bgp", "!"]
    if rules:
        lines.append("ip access-list FABLOOM-PROTECT")
        seq = 10
        for proto, port, allowed, own in rules:
            for ip in own:
                if allowed:
                    lines.append(f"  {seq} permit {proto} {allowed} {ip}/32 eq {port}")
                    seq += 10
                lines.append(f"  {seq} deny {proto} any {ip}/32 eq {port}")
                seq += 10
        lines += [f"  {seq} permit ip any any", "!"]
    lines += ["interface loopback0", f"  ip address {d.loopback}", "!"]
    for i in d.interfaces:
        lines += [f"interface {i.name}", "  no switchport", f"  ip address {i.address}"]
        if rules:
            lines.append("  ip access-group FABLOOM-PROTECT in")
        lines += ["  no shutdown", "!"]
    lines += [f"router bgp {d.asn}", f"  router-id {d.router_id}", "  address-family ipv4 unicast"]
    if intent.advertise_loopbacks:
        lines.append(f"    network {d.loopback.network}")
    for ip, asn in _peers(fabric, d):
        lines += [f"  neighbor {ip}", f"    remote-as {asn}", "    address-family ipv4 unicast"]
    return "\n".join(lines) + "\n"


def _iosxe(intent: Intent, fabric: FabricPlan, d: Device) -> str:
    rules = _filter_rules(intent, d)
    lines = ["!RANCID-CONTENT-TYPE: cisco", f"hostname {d.name}", "ip routing", "!"]
    if rules:
        lines.append("ip access-list extended FABLOOM-PROTECT")
        for proto, port, allowed, own in rules:
            for ip in own:
                if allowed:
                    net = IPv4Network(allowed)
                    src = f"{net.network_address} {net.hostmask}"
                    lines.append(f" permit {proto} {src} host {ip} eq {port}")
                lines.append(f" deny {proto} any host {ip} eq {port}")
        lines += [" permit ip any any", "!"]
    lines += ["interface Loopback0", f" ip address {d.loopback.ip} {d.loopback.netmask}", "!"]
    for i in d.interfaces:
        assert i.address is not None
        lines += [f"interface {i.name}", " no switchport"]
        lines.append(f" ip address {i.address.ip} {i.address.netmask}")
        if rules:
            lines.append(" ip access-group FABLOOM-PROTECT in")
        lines.append("!")
    lines += [f"router bgp {d.asn}", f" bgp router-id {d.router_id}"]
    peers = _peers(fabric, d)
    lines += [f" neighbor {ip} remote-as {asn}" for ip, asn in peers]
    lines.append(" address-family ipv4")
    if intent.advertise_loopbacks:
        lines.append(f"  network {d.loopback.ip} mask {d.loopback.netmask}")
    lines += [f"  neighbor {ip} activate" for ip, _ in peers]
    lines += [" exit-address-family", "!"]
    return "\n".join(lines) + "\n"


def _iosxr(intent: Intent, fabric: FabricPlan, d: Device) -> str:
    rules = _filter_rules(intent, d)
    lines = ["!RANCID-CONTENT-TYPE: cisco-xr", f"hostname {d.name}", "!"]
    if rules:
        lines.append("ipv4 access-list FABLOOM-PROTECT")
        seq = 10
        for proto, port, allowed, own in rules:
            for ip in own:
                if allowed:
                    lines.append(f" {seq} permit {proto} {allowed} host {ip} eq {port}")
                    seq += 10
                lines.append(f" {seq} deny {proto} any host {ip} eq {port}")
                seq += 10
        lines += [f" {seq} permit ipv4 any any", "!"]
    lines += ["interface Loopback0", f" ipv4 address {d.loopback.ip} {d.loopback.netmask}", "!"]
    for i in d.interfaces:
        assert i.address is not None
        lines += [f"interface {i.name}", f" ipv4 address {i.address.ip} {i.address.netmask}"]
        if rules:
            lines.append(" ipv4 access-group FABLOOM-PROTECT ingress")
        lines.append("!")
    # IOS-XR drops eBGP routes without a policy in both directions.
    lines += ["route-policy PASS", "  pass", "end-policy", "!"]
    lines += [
        f"router bgp {d.asn}",
        f" bgp router-id {d.router_id}",
        " address-family ipv4 unicast",
    ]
    if intent.advertise_loopbacks:
        lines.append(f"  network {d.loopback.network}")
    lines.append(" !")
    for ip, asn in _peers(fabric, d):
        lines += [f" neighbor {ip}", f"  remote-as {asn}", "  address-family ipv4 unicast"]
        lines += ["   route-policy PASS in", "   route-policy PASS out", "  !", " !"]
    lines.append("!")
    return "\n".join(lines) + "\n"


def _sonic(intent: Intent, fabric: FabricPlan, d: Device) -> str:
    port: dict[str, dict[str, str]] = {}
    intf: dict[str, dict[str, str]] = {}
    for n, i in enumerate(d.interfaces, start=1):
        port[i.name] = {"admin_status": "up", "alias": f"etp{n}", "lanes": str(4 * (n - 1)),
                        "mtu": "9100", "speed": "100000"}  # fmt: skip
        intf[i.name] = {}
        intf[f"{i.name}|{i.address}"] = {}
    config_db = {
        "DEVICE_METADATA": {"localhost": {"hostname": d.name, "bgp_asn": str(d.asn)}},
        "PORT": port,
        "INTERFACE": intf,
        "LOOPBACK_INTERFACE": {"Loopback0": {}, f"Loopback0|{d.loopback}": {}},
    }
    frr = ["frr version 8.4", "frr defaults datacenter", f"hostname {d.name}", "!"]
    frr += [f"router bgp {d.asn}", f" bgp router-id {d.router_id}"]
    frr += [f" neighbor {ip} remote-as {asn}" for ip, asn in _peers(fabric, d)]
    frr.append(" address-family ipv4 unicast")
    if intent.advertise_loopbacks:
        frr.append(f"  network {d.loopback.network}")
    frr += [" exit-address-family", "!"]
    return (
        f'<file name="config_db.json">\n{json.dumps(config_db, indent=2)}\n</file>\n'
        f'<file name="frr.conf">\n' + "\n".join(frr) + "\n</file>\n"
    )


_RENDERERS = {
    Platform.JUNOS: _junos,
    Platform.CISCO_NXOS: _nxos,
    Platform.CISCO_IOSXE: _iosxe,
    Platform.CISCO_IOSXR: _iosxr,
    Platform.SONIC: _sonic,
}


def render_good_configs(intent: Intent, fabric: FabricPlan) -> dict[str, str]:
    if fabric.platform is Platform.FRR:
        return {d.name: _frr(fabric, d) for d in fabric.devices}
    if fabric.platform is Platform.ARISTA_EOS:
        return {d.name: _eos(intent, fabric, d) for d in fabric.devices}
    render = _RENDERERS[fabric.platform]
    return {d.name: render(intent, fabric, d) for d in fabric.devices}


def as_device_blocks(configs: dict[str, str], platform: Platform) -> str:
    """The generator output contract, as a model would emit it."""
    return "\n".join(
        f'<device name="{name}" platform="{platform}">\n{text.rstrip()}\n</device>'
        for name, text in configs.items()
    )
