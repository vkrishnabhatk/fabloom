"""config, provenance, logging, the verifier entrypoint and the pybatfish adapter."""

from __future__ import annotations

import json
import socket
import sys
import types
from pathlib import Path
from typing import Any, ClassVar

import pytest
import structlog

from fabloom.checks import allocate, compile_checks
from fabloom.config import (
    Settings,
    ensure_parent,
    load_config,
    models_config,
    resolve,
    run_config,
)
from fabloom.contracts import Intent, Platform, Snapshot, TopologySpec
from fabloom.log import configure_logging
from fabloom.provenance import content_key, file_digest, git_sha, set_seeds
from fabloom.verify import entrypoint
from fabloom.verify.batfish_local import PybatfishAnswerer
from fabloom.verify.batfish_questions import Question
from fabloom.verify.protocol import VerifierUnavailableError
from tests.conftest import CONF_DIR
from tests.fakes import FakeVerifier

pytestmark = pytest.mark.unit


def test_config_loads_and_caches() -> None:
    assert load_config("models", CONF_DIR) is load_config("models", CONF_DIR)
    assert models_config(CONF_DIR).roles
    assert run_config(CONF_DIR).budget.max_repair_rounds == 3


def test_config_must_be_a_mapping(tmp_path: Path) -> None:
    (tmp_path / "bad.yaml").write_text("- a list\n")
    with pytest.raises(TypeError, match="mapping"):
        load_config("bad", tmp_path)


def test_every_configured_price_is_verified() -> None:
    # Benchmark costs are only as real as these prices. A model added with a guessed price
    # fails here until it is checked against Token Factory (ADR-0005).
    unverified = [n for n, m in models_config(CONF_DIR).models.items() if not m.price_verified]
    assert unverified == []


@pytest.mark.parametrize("value", ["", "  "])
def test_blank_key_counts_as_missing(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # Actions passes "" for a secret that was never set; that must read as "no key".
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)
    monkeypatch.setenv("FABLOOM_TOKEN_FACTORY_API_KEY", value)
    assert Settings(_env_file=None).token_factory_api_key is None


def test_paths(tmp_path: Path) -> None:
    assert resolve("/abs/x") == Path("/abs/x")
    assert resolve("conf").name == "conf"
    target = ensure_parent(tmp_path / "a" / "b" / "c.txt")
    assert target.parent.is_dir()


def test_provenance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert content_key({"b": 1, "a": [1, 2]}) == content_key({"a": [1, 2], "b": 1})
    assert content_key({"a": 1}) != content_key({"a": 2})
    f = tmp_path / "f"
    f.write_bytes(b"abc")
    assert file_digest(f).startswith("ba7816bf")
    git_sha.cache_clear()
    monkeypatch.setenv("IMAGE_TAG", "git-abc1234")
    assert git_sha() == "git-abc1234"
    git_sha.cache_clear()
    monkeypatch.delenv("IMAGE_TAG")
    assert git_sha()
    git_sha.cache_clear()
    set_seeds(7)


def test_json_logs_carry_the_traceback(capsys: pytest.CaptureFixture[str]) -> None:
    def crash() -> None:
        raise PermissionError("Permission denied: 'runs'")

    configure_logging("INFO", json=True)
    try:
        crash()
    except PermissionError:
        structlog.get_logger("t").exception("web_run_crashed")
    line = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert "PermissionError: Permission denied: 'runs'" in line["exception"]
    assert "Traceback" in line["exception"]


def test_logging_scrubs(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", json=True)
    structlog.get_logger("t").info("config", line="snmp-server community public")
    err = capsys.readouterr().err
    assert "public" not in err
    assert "<redacted>" in err
    configure_logging("INFO", json=False)
    structlog.reset_defaults()


# --- fabloom-verify entrypoint ----------------------------------------------------------


def _job(tmp_path: Path) -> Path:
    intent = Intent(platform=Platform.FRR, topology=TopologySpec(spines=1, leaves=1))
    fabric = allocate(intent)
    job = {
        "snapshot": Snapshot(name="s", fabric=fabric).model_dump(mode="json"),
        "configs": {"spine1": "hostname spine1"},
        "checks": [c.model_dump(mode="json") for c in compile_checks(intent, fabric).checks],
    }
    path = tmp_path / "job.json"
    path.write_text(json.dumps(job))
    return path


def test_entrypoint_prints_one_report_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(entrypoint, "wait_for_port", lambda *a: None)
    monkeypatch.setattr(entrypoint, "local_verifier", lambda host: FakeVerifier([0]))
    assert entrypoint.main(["--job", str(_job(tmp_path))]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["verifier_name"] == "fake"


def test_entrypoint_reports_errors_as_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FABLOOM_BATFISH_START_CMD", f"{sys.executable} -c pass")

    def boom(*a: Any) -> None:
        raise TimeoutError("no batfish")

    monkeypatch.setattr(entrypoint, "wait_for_port", boom)
    assert entrypoint.main(["--job", str(_job(tmp_path))]) == 1
    assert json.loads(capsys.readouterr().out) == {"error": "TimeoutError: no batfish"}


def test_wait_for_port() -> None:
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        entrypoint.wait_for_port("127.0.0.1", srv.getsockname()[1], 1)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    with pytest.raises(TimeoutError):
        entrypoint.wait_for_port("127.0.0.1", free, 0.1)


# --- pybatfish adapter (fake Session; the real one is exercised by the week-0 spike) ----


class _Frame:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self.records = records

    def to_dict(self, orient: str) -> list[dict[str, Any]]:
        assert orient == "records"
        return self.records


class _FakeSession:
    last_params: ClassVar[dict[str, Any]] = {}

    def __init__(self, host: str) -> None:
        if host == "down":
            raise ConnectionError("refused")
        self.q = self

    def set_network(self, name: str) -> None: ...

    def init_snapshot(self, path: str, name: str, overwrite: bool) -> None: ...

    def get_component_versions(self) -> dict[str, str]:
        return {"Batfish": "2026.9.17"}

    def __getattr__(self, question: str) -> Any:
        def build(**params: Any) -> Any:
            type(self).last_params = params
            frame = _Frame([{"Interface": types.SimpleNamespace(hostname="a", interface="e1")}])
            answer = types.SimpleNamespace(frame=lambda: frame)
            return types.SimpleNamespace(answer=lambda: answer)

        return build


def test_pybatfish_answerer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    session_mod = pytest.importorskip("pybatfish.client.session")
    monkeypatch.setattr(session_mod, "Session", _FakeSession)
    a = PybatfishAnswerer("localhost")
    a.init_snapshot(tmp_path, "s")
    assert a.batfish_version() == "2026.9.17"
    rows = a.ask(
        Question(
            "reachability",
            {"headers": {"dstIps": "10.0.0.1"}, "pathConstraints": {"startLocation": "leaf1"}},
        )
    )
    assert rows == [{"Interface": "a[e1]"}]
    assert type(_FakeSession.last_params["headers"]).__name__ == "HeaderConstraints"
    with pytest.raises(VerifierUnavailableError, match="make batfish-up"):
        PybatfishAnswerer("down")
