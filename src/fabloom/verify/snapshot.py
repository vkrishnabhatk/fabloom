"""Lay out a Batfish snapshot directory from a fabric plan and candidate configs.

    <root>/configs/<device>.cfg
    <root>/batfish/layer1_topology.json

Two platform packagings:

- Arista EOS: the candidate is the whole running-config, written as-is.
- FRR: Batfish reads FRR through its Cumulus "concatenated" format: hostname line, then
  ``/etc/network/interfaces``, then a ``# ports.conf --`` section, then ``frr.conf``. The
  ports.conf marker is what ends the interfaces section: without it Batfish parsed every
  ``frr.conf`` line as an interfaces statement (live Batfish 2026.08.27, 2026-10-23: all
  four devices PARTIALLY_UNRECOGNIZED, no BGP sessions; with it, PASSED and 8/8 sessions
  established). A blank line is not enough. The interfaces section only declares which
  ports exist (that is cabling, decided by the plan). It deliberately carries no addresses:
  addressing is the model's job, in ``frr.conf``, and must not be silently supplied here.

Top-level ``exit`` lines are dropped from the FRR copy Batfish parses, and only there: the
configs returned to the user are untouched. FRR 8.x writes ``exit`` after every block in its
own running-config, but Batfish's FRR grammar reports each one as unrecognised syntax, so
``parse_clean`` failed correct configs (live Batfish 2026.08.27, 2026-10-23: 4 of 10 pilot
runs, every one an ``exit``; BGP came up 8/8 with or without them). A column-0 ``exit``
only returns to the root config node, and the next block starts with a top-level command
anyway, so dropping it does not change what Batfish evaluates. Indented lines and
``exit-address-family`` are kept.

``layer1_topology.json`` is always written. Without it, FRR BGP-unnumbered sessions silently
fail to come up in Batfish.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fabloom.contracts import Device, FabricPlan, Platform

FRR_VERSION_LINE = "frr version 8.4"
PORTS_CONF_MARKER = "# ports.conf --"
FRR_NOOP_LINES = frozenset({"exit"})


def config_filename(device: str) -> str:
    return f"configs/{device}.cfg"


def _frr_interfaces(device: Device) -> str:
    lines = ["# This file describes the network interfaces", "auto lo", "iface lo inet loopback"]
    for iface in device.interfaces:
        lines += ["", f"auto {iface.name}", f"iface {iface.name}"]
    return "\n".join(lines)


def render_device_file(platform: Platform, device: Device, config: str) -> str:
    body = config.strip("\n")
    if platform is Platform.ARISTA_EOS:
        return body + "\n"
    body = "\n".join(line for line in body.splitlines() if line.rstrip() not in FRR_NOOP_LINES)
    if not body.lstrip().startswith("frr version"):
        body = f"{FRR_VERSION_LINE}\n{body}"
    return f"{device.name}\n{_frr_interfaces(device)}\n{PORTS_CONF_MARKER}\n{body}\n"


def layer1_topology(fabric: FabricPlan) -> dict[str, Any]:
    edges: list[dict[str, Any]] = []
    for link in fabric.links:
        a = {"hostname": link.a_device, "interfaceName": link.a_interface}
        b = {"hostname": link.b_device, "interfaceName": link.b_interface}
        edges += [{"node1": a, "node2": b}, {"node1": b, "node2": a}]
    return {"edges": edges}


def write_snapshot(root: Path, fabric: FabricPlan, configs: Mapping[str, str]) -> Path:
    """Write the snapshot under ``root``. Only planned devices are written."""
    (root / "configs").mkdir(parents=True, exist_ok=True)
    (root / "batfish").mkdir(parents=True, exist_ok=True)
    for device in fabric.devices:
        if device.name not in configs:
            continue
        text = render_device_file(fabric.platform, device, configs[device.name])
        (root / config_filename(device.name)).write_text(text, encoding="utf-8")
    (root / "batfish" / "layer1_topology.json").write_text(
        json.dumps(layer1_topology(fabric), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return root
