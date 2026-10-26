"""The web app over a real RunMachine with fakes: FakeChatClient + FakeVerifier, no network."""

from __future__ import annotations

import base64
import hashlib
import io
import re
import shutil
import zipfile
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from fabloom.agent import RunMachine
from fabloom.config import RunConfig, Settings
from fabloom.contracts import FabricPlan, Intent, Platform, RunBudget
from fabloom.llm import ModelRouter
from fabloom.scenarios import DEFAULT_BENCHMARK_DIR, load_scenarios
from fabloom.web import create_app
from fabloom.web.app import client_ip
from fabloom.web.guards import RateLimiter, SpendBreaker, spent_today
from fabloom.web.runs import RUN_ID
from tests.fakes import FakeChatClient, FakeVerifier
from tests.fakes.render import as_device_blocks, render_good_configs

pytestmark = pytest.mark.integration

REQUEST = "2 spines and 2 leaves on FRR, eBGP numbered"


def _settings(tmp_path: Path, **kw: object) -> Settings:
    fields: dict[str, object] = {
        "runs_dir": tmp_path / "runs",
        "gallery_dir": tmp_path / "gallery",
        "log_json": False,
        **kw,
    }
    return Settings(_env_file=None, **fields)  # type: ignore[arg-type]


def _machine(
    tmp_path: Path, router: ModelRouter, intent: Intent, fabric: FabricPlan, runs: int = 1
) -> RunMachine:
    blocks = as_device_blocks(render_good_configs(intent, fabric), Platform.FRR)
    script: list[object] = [intent.model_dump(mode="json"), blocks, "All checks pass."] * runs
    rc = RunConfig(budget=RunBudget(max_usd=0.25))
    return RunMachine(
        FakeChatClient(script=script), router, FakeVerifier([0]), rc, tmp_path / "runs"
    )


@pytest.fixture
def live(
    tmp_path: Path, router: ModelRouter, frr_intent: Intent, frr_fabric: FabricPlan
) -> Iterator[TestClient]:
    machine = _machine(tmp_path, router, frr_intent, frr_fabric, runs=2)
    examples = load_scenarios(DEFAULT_BENCHMARK_DIR)[:3]
    app = create_app(_settings(tmp_path), machine, examples=examples)
    with TestClient(app) as client:
        yield client


def _run_to_completion(client: TestClient, body: dict[str, object]) -> str:
    resp = client.post("/api/v1/runs", json=body)
    assert resp.status_code == 202, resp.text
    run_id = str(resp.json()["run_id"])
    # The SSE stream ends after run_finished, so reading it waits for the run.
    with client.stream("GET", f"/api/v1/runs/{run_id}/events") as stream:
        text = "".join(stream.iter_text())
    assert "event: run_started" in text
    assert "event: run_finished" in text
    assert text.endswith("event: end\ndata: {}\n\n")
    return run_id


def test_a_run_end_to_end(live: TestClient) -> None:
    run_id = _run_to_completion(live, {"request": REQUEST, "platform": "frr"})
    run = live.get(f"/api/v1/runs/{run_id}").json()
    assert run["outcome"] == "passed"
    assert run["request"] == REQUEST
    assert run["explanation"] == "All checks pass."
    assert run["level"] == "L2"
    assert {"spine1", "leaf1"} <= set(run["configs"])
    assert run["fabric"] == {"platform": "frr", "platform_name": "FRR", "spines": 2, "leaves": 2}
    assert run["docs"] == []  # no search configured
    assert all(c["passed"] for c in run["checks"])

    resp = live.get(f"/api/v1/runs/{run_id}/configs.zip")
    assert resp.status_code == 200
    assert resp.headers["content-disposition"] == f'attachment; filename="fabloom-{run_id}.zip"'
    with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
        names = set(z.namelist())
        assert {f"{run_id}/spine1.cfg", f"{run_id}/leaf1.cfg", f"{run_id}/README.txt"} <= names
        readme = z.read(f"{run_id}/README.txt").decode()
    assert REQUEST in readme
    assert "a proposal for review" in readme

    page = live.get(f"/runs/{run_id}")
    assert page.status_code == 200
    assert f'data-run-id="{run_id}"' in page.text
    info = live.get("/info").json()
    assert info["mode"] == "live"
    assert info["verifier"] == "fake"
    assert info["spend_today_usd"] > 0


def test_pages_ops_and_headers(live: TestClient) -> None:
    home = live.get("/")
    assert home.status_code == 200
    assert "Generate and verify" in home.text
    assert 'class="chip example"' in home.text
    assert "3 runs, plus one more every 10 minutes" in home.text
    # Path-only asset links: behind the TLS proxy an absolute http:// link is blocked.
    assert 'href="/static/app.css"' in home.text
    assert 'src="/static/app.js"' in home.text
    assert "default-src 'self'" in home.headers["content-security-policy"]
    assert live.get("/", headers={"X-Request-ID": "abc"}).headers["x-request-id"] == "abc"
    assert live.get("/healthz").json() == {"status": "ok"}
    assert live.get("/readyz").json() == {"status": "ready", "mode": "live"}
    assert "fabloom_runs_total" in live.get("/metrics").text
    assert len(live.get("/api/v1/scenarios").json()) == 3
    assert live.get("/static/app.js").status_code == 200
    assert live.get("/static/favicon.svg").status_code == 200
    # Link previews: absolute URLs, an image that exists, and every platform's example.
    assert '<meta property="og:image" content="https://testserver/static/og.png">' in home.text
    assert live.get("/static/og.png").status_code == 200
    assert "Juniper, Cisco, Arista, SONiC or FRR" in home.text


def test_api_docs_get_their_own_csp(live: TestClient) -> None:
    # Swagger UI only ships from a CDN; that is allowed on this one page, and its inline
    # bootstrap script by hash, never by 'unsafe-inline'.
    docs = live.get("/api/docs")
    assert docs.status_code == 200
    csp = docs.headers["content-security-policy"]
    assert "script-src 'self' https://cdn.jsdelivr.net 'sha256-" in csp
    assert "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]
    [inline] = re.findall(rb"<script>(.*?)</script>", docs.content, re.DOTALL)
    digest = base64.b64encode(hashlib.sha256(inline).digest()).decode()
    assert f"'sha256-{digest}'" in csp
    assert "swagger-ui-dist@5.17.14" in docs.text
    # Every other page keeps the strict policy.
    assert "cdn.jsdelivr.net" not in live.get("/").headers["content-security-policy"]


def test_bad_input_and_unknown_runs(live: TestClient) -> None:
    assert live.post("/api/v1/runs", json={"request": "short"}).status_code == 422
    assert live.post("/api/v1/runs", json={"request": REQUEST, "x": 1}).status_code == 422
    assert live.get("/api/v1/runs/20261022T000000Z-abcdef").status_code == 404
    # Anything that is not a run ID never reaches the filesystem.
    assert live.get("/api/v1/runs/..%2F..%2Fetc").status_code == 404
    assert live.get("/runs/nope").status_code == 404
    assert live.get("/api/v1/runs/20261022T000000Z-abcdef/configs.zip").status_code == 404


def test_unsupported_vendors_are_refused_before_any_model_call(live: TestClient) -> None:
    resp = live.post(
        "/api/v1/runs", json={"request": "Nokia SR Linux fabric, 2 spines and 2 leaves"}
    )
    assert resp.status_code == 422
    assert "can't verify Nokia" in resp.json()["detail"]
    assert "Juniper Junos" in resp.json()["detail"]
    # Refused before the rate limiter: it cost the visitor nothing.
    assert live.get("/info").json()["spend_today_usd"] == 0
    home = live.get("/").text
    assert '<option value="cisco_nxos">Cisco NX-OS</option>' in home


def test_rate_limit_per_visitor(
    tmp_path: Path, router: ModelRouter, frr_intent: Intent, frr_fabric: FabricPlan
) -> None:
    machine = _machine(tmp_path, router, frr_intent, frr_fabric)
    app = create_app(_settings(tmp_path), machine, limiter=RateLimiter(1, 600.0))
    with TestClient(app) as client:
        _run_to_completion(client, {"request": REQUEST})
        resp = client.post("/api/v1/runs", json={"request": REQUEST})
        assert resp.status_code == 429
        assert int(resp.headers["retry-after"]) > 0


def test_read_only_without_a_machine_serves_the_gallery(
    tmp_path: Path, router: ModelRouter, frr_intent: Intent, frr_fabric: FabricPlan
) -> None:
    # Record one run, then serve it as the gallery from a server with no model.
    machine = _machine(tmp_path, router, frr_intent, frr_fabric)
    with TestClient(create_app(_settings(tmp_path), machine)) as client:
        run_id = _run_to_completion(client, {"request": REQUEST})
    shutil.copytree(tmp_path / "runs" / run_id, tmp_path / "gallery" / run_id)

    settings = _settings(tmp_path, runs_dir=tmp_path / "empty")
    with TestClient(create_app(settings, None)) as client:
        assert client.post("/api/v1/runs", json={"request": REQUEST}).status_code == 503
        info = client.get("/info").json()
        assert info["mode"] == "read-only"
        assert info["models"] is None
        [item] = client.get("/api/v1/gallery").json()
        assert item["run_id"] == run_id
        assert item["gallery"] is True
        home = client.get("/").text
        assert "Read-only: no model is configured" in home
        assert f"/runs/{run_id}" in home
        assert client.get(f"/api/v1/runs/{run_id}").json()["outcome"] == "passed"


def test_spend_cap_turns_the_app_read_only(
    tmp_path: Path, router: ModelRouter, frr_intent: Intent, frr_fabric: FabricPlan
) -> None:
    machine = _machine(tmp_path, router, frr_intent, frr_fabric)
    # A run reserves its worst case ($0.25). After one real run has spent anything at all,
    # the next reservation no longer fits under a cap of just over $0.25.
    settings = _settings(tmp_path, web_spend_cap_usd=0.25 + 1e-9)
    with TestClient(create_app(settings, machine)) as client:
        _run_to_completion(client, {"request": REQUEST})
        resp = client.post("/api/v1/runs", json={"request": REQUEST})
        assert resp.status_code == 503
        assert "budget is used up" in resp.json()["detail"]
    # A restart reads today's spend back from the run logs.
    assert spent_today(tmp_path / "runs", datetime.now(UTC).date()) > 0


def test_spend_breaker_reserves_and_rolls_over() -> None:
    day = [date(2026, 10, 22)]
    b = SpendBreaker(1.0, spent_usd=0.5, today=lambda: day[0])
    assert b.reserve(0.4)
    assert not b.reserve(0.4)  # 0.5 spent + 0.4 reserved + 0.4 > 1.0
    b.settle(0.4, 0.6)
    assert b.tripped
    day[0] = date(2026, 10, 23)
    assert not b.tripped
    assert b.spent_usd == 0.0


def test_rate_limiter_refills() -> None:
    now = [0.0]
    r = RateLimiter(2, 10.0, clock=lambda: now[0])
    assert r.allow("a")
    assert r.allow("a")
    assert not r.allow("a")
    assert r.allow("b")
    assert r.retry_after_s("a") == 11
    now[0] = 10.0
    assert r.allow("a")
    assert r.retry_after_s("never-seen") == 0


def test_client_ip_trusts_only_our_proxy() -> None:
    req = Request({
        "type": "http",
        "headers": [(b"x-forwarded-for", b"6.6.6.6, 203.0.113.9")],
        "client": ("10.0.0.2", 51000),
    })  # fmt: skip
    assert client_ip(req, trust_proxy=False) == "10.0.0.2"
    assert client_ip(req, trust_proxy=True) == "203.0.113.9"


def test_run_id_pattern_matches_new_run_ids() -> None:
    from fabloom.agent.events import new_run_id  # noqa: PLC0415

    assert RUN_ID.match(new_run_id())
    assert not RUN_ID.match("../x")


def test_web_runs_fit_the_daily_cap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from fabloom import cli  # noqa: PLC0415

    monkeypatch.setattr(cli, "build_client", lambda s: FakeChatClient(script=[]))
    monkeypatch.setattr(cli, "build_verifier", lambda s: FakeVerifier([0]))
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)
    monkeypatch.setenv("FABLOOM_TOKEN_FACTORY_API_KEY", "k")
    settings = _settings(tmp_path)
    assert settings.web_spend_cap_usd == pytest.approx(0.40)
    machine = cli.build_web_machine(settings)
    assert machine is not None
    # A run reserves its budget against the daily cap before it starts: at the CLI's $0.25,
    # nothing could start after $0.15 of a $0.40 day.
    assert machine.run_config.budget.max_usd == pytest.approx(0.05)
    monkeypatch.setenv("FABLOOM_TOKEN_FACTORY_API_KEY", "")
    assert cli.build_web_machine(_settings(tmp_path)) is None


def test_home_shows_one_example_per_platform() -> None:
    from fabloom.scenarios import VENDOR_BENCHMARK_DIR  # noqa: PLC0415
    from fabloom.web.app import _one_per_platform  # noqa: PLC0415

    examples = load_scenarios(DEFAULT_BENCHMARK_DIR) + load_scenarios(VENDOR_BENCHMARK_DIR)
    picks = _one_per_platform(examples)
    assert len(picks) == len(Platform)
    assert any("Junos" in p for p in picks)
    assert any("SONiC" in p for p in picks)
