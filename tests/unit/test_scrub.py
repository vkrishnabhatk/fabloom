from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from fabloom.scrub import REDACTED, scrub, structlog_processor

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("line", "kept"),
    [
        ("enable secret 5 $1$abcd$Xy9zQ", "enable secret 5 "),
        ("enable password hunter2", "enable password "),
        ("username admin privilege 15 secret sha512 $6$salt$hash", "secret sha512 "),
        ("username ops password 0 letmein", "password 0 "),
        (" neighbor 10.1.0.0 password 7 0822455D0A16", " neighbor 10.1.0.0 password 7 "),
        (" neighbor FABRIC password s3cret", " neighbor FABRIC password "),
        (" ip ospf authentication-key 7 045802150C2E", "authentication-key 7 "),
        (" ip ospf message-digest-key 1 md5 0 mykey", "md5 0 "),
        ("snmp-server community public RO", "snmp-server community "),
        ("tacacs-server host 10.9.9.9 key 7 1234ABCD", "key 7 "),
        ("radius-server key supersecret", "radius-server key "),
        ("crypto isakmp key pre-shared-key ascii mysharedkey", "pre-shared-key ascii "),
        (" key-string 7 070C285F4D06", " key-string 7 "),
        ("my password=Tr0ub4dor&3 please", "password="),
    ],
)
def test_keyword_rules(line: str, kept: str) -> None:
    result = scrub(line)
    assert result.redactions >= 1
    assert kept in result.text
    assert REDACTED in result.text


def test_pem_block() -> None:
    text = "x\n-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nabc\n-----END RSA PRIVATE KEY-----\ny"
    result = scrub(text)
    assert "MIIEow" not in result.text
    assert result.text.startswith("x\n-----BEGIN RSA PRIVATE KEY-----<redacted>-----END")


def test_high_entropy_token() -> None:
    result = scrub("use key nb-9fQx2LmP7vR4tK8wZ3yH6jD1sA5gC0e please")
    assert "nb-9fQx2LmP7vR4tK8wZ3yH6jD1sA5gC0e" not in result.text
    assert result.redactions == 1


@pytest.mark.parametrize(
    "benign",
    [
        "router bgp 65101\n bgp router-id 10.0.1.1\n neighbor 10.1.0.0 remote-as 65000",
        "interface Ethernet1\n   ip address 10.1.0.1/31",
        "route-map RM_LEAF_TO_SPINE_OUT_V4 permit 10",
        "frr defaults datacenter",
        "ip prefix-list LOOPBACKS seq 5 permit 10.0.0.0/23 le 32",
        "2 spines, 4 leaves, eBGP underlay, block SSH from outside 10.0.0.0/8",
    ],
)
def test_benign_config_untouched(benign: str) -> None:
    assert scrub(benign).text == benign
    assert scrub(benign).redactions == 0


@given(st.text(alphabet=st.characters(exclude_categories=["Cs"]), max_size=200))
def test_idempotent(text: str) -> None:
    once = scrub(text).text
    assert scrub(once).text == once


def test_structlog_processor() -> None:
    out = structlog_processor(None, "info", {"event": "snmp-server community public", "n": 3})
    assert out["event"] == f"snmp-server community {REDACTED}"
    assert out["n"] == 3
