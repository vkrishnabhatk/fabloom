"""Redact credentials from network configs and free text.

Keyword rules cover the places network configs keep secrets; a high-entropy detector
catches API keys and tokens pasted into free text. The redaction marker keeps the keyword,
so a scrubbed config stays readable and the model can still see that a secret belonged there.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import MutableMapping
from dataclasses import dataclass
from typing import Any

REDACTED = "<redacted>"

# (pattern, replacement). Group 1 is always the part that is kept.
_RULES: list[tuple[re.Pattern[str], str]] = [
    # PEM blocks: private keys, certificates.
    (
        re.compile(r"(-----BEGIN [A-Z0-9 ]+-----)[\s\S]*?(-----END [A-Z0-9 ]+-----)"),
        rf"\1{REDACTED}\2",
    ),
    # enable secret 5 $1$..., enable password foo
    (re.compile(r"(?im)^(\s*enable\s+(?:secret|password)(?:\s+(?:sha512|\d))?\s+)\S+"), ""),
    # username admin secret 5 xyz / username admin privilege 15 password 0 xyz
    (
        re.compile(r"(?im)^(\s*username\s+\S+.*?\b(?:secret|password)(?:\s+(?:sha512|\d))?\s+)\S+"),
        "",
    ),
    # neighbor 10.0.0.1 password 7 xyz (BGP), including peer-groups
    (re.compile(r"(?im)^(\s*neighbor\s+\S+\s+password(?:\s+\d)?\s+)\S+"), ""),
    # OSPF / IS-IS authentication keys
    (re.compile(r"(?im)^(\s*ip\s+ospf\s+authentication-key(?:\s+\d)?\s+)\S+"), ""),
    (re.compile(r"(?im)^(\s*ip\s+ospf\s+message-digest-key\s+\d+\s+md5(?:\s+\d)?\s+)\S+"), ""),
    (re.compile(r"(?im)^(\s*(?:isis\s+)?authentication\s+key(?:\s+\d)?\s+)\S+"), ""),
    # SNMP communities
    (re.compile(r"(?im)^(\s*snmp-server\s+community\s+)\S+"), ""),
    # TACACS+/RADIUS shared keys, on their own or on a host line
    (re.compile(r"(?im)^(\s*(?:tacacs|radius)-server\b.*?\bkey(?:\s+\d)?\s+)\S+"), ""),
    # IPsec/IKE pre-shared keys
    (re.compile(r"(?im)(\b(?:pre-shared-key|psk)(?:\s+(?:ascii|hex|\d))?\s+)\S+"), ""),
    # key chains: "key-string 7 xyz"
    (re.compile(r"(?im)(\bkey-string(?:\s+\d)?\s+)\S+"), ""),
    # generic assignment in free text: password=..., api_key: ...
    (
        re.compile(r"(?i)(\b(?:password|passwd|secret|api[_-]?key|token)\s*[:=]\s*)[^\s,;]+"),
        "",
    ),
]

_TOKEN = re.compile(r"[A-Za-z0-9+/=_\-]{24,}")
ENTROPY_THRESHOLD = 4.0  # bits per character


def shannon_entropy(s: str) -> float:
    counts = Counter(s)
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in counts.values())


def _looks_like_secret(token: str) -> bool:
    has_digit = any(ch.isdigit() for ch in token)
    has_alpha = any(ch.isalpha() for ch in token)
    return has_digit and has_alpha and shannon_entropy(token) >= ENTROPY_THRESHOLD


@dataclass(frozen=True)
class ScrubResult:
    text: str
    redactions: int


def scrub(text: str) -> ScrubResult:
    count = 0
    for pattern, replacement in _RULES:
        repl = replacement or rf"\1{REDACTED}"
        text, n = pattern.subn(repl, text)
        count += n

    def _entropy(m: re.Match[str]) -> str:
        nonlocal count
        token = m.group(0)
        if REDACTED in token or not _looks_like_secret(token):
            return token
        count += 1
        return REDACTED

    text = _TOKEN.sub(_entropy, text)
    return ScrubResult(text=text, redactions=count)


def structlog_processor(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Scrub every string value in a log event before it is rendered."""
    for key, value in list(event_dict.items()):
        if isinstance(value, str):
            event_dict[key] = scrub(value).text
    return event_dict
