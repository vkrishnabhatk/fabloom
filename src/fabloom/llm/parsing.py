"""Parse model output. Deterministic, and forgiving only where forgiveness is unambiguous."""

from __future__ import annotations

import re

_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)
_DEVICE = re.compile(
    r'<device\s+name="(?P<name>[^"]+)"(?:\s+platform="(?P<platform>[^"]*)")?\s*>'
    r"(?P<body>.*?)</device>",
    re.S | re.I,
)
_FENCE_LINE = re.compile(r"^\s*```[\w-]*\s*$", re.M)


def strip_reasoning(text: str) -> str:
    """Drop ``<think>`` blocks some reasoning models emit inline with the answer."""
    return _THINK.sub("", text).strip()


def parse_device_blocks(text: str) -> dict[str, str]:
    """``<device name="leaf1" platform="frr">...</device>`` blocks -> {name: config}.

    The first block for a name wins. Markdown fence lines just inside a block are removed,
    because they are formatting, not configuration; everything else is kept verbatim so the
    verifier sees exactly what the model wrote.
    """
    out: dict[str, str] = {}
    for m in _DEVICE.finditer(strip_reasoning(text)):
        name = m.group("name").strip()
        if name in out:
            continue
        body = _FENCE_LINE.sub("", m.group("body")).strip("\n")
        out[name] = body.strip() + "\n" if body.strip() else ""
    return out
