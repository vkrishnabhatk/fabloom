"""FastAPI app factory. JSON first: every HTML page is a thin shell over the JSON API, so the
pages and any future front end read exactly the same data.

The app is handed a ready ``RunMachine`` by the composition root (``fabloom serve``) and
never builds a verifier or a model client itself, so ``web`` cannot reach ``verify``.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, ConfigDict, Field

from fabloom import __version__
from fabloom.agent import RunMachine, tail_events
from fabloom.config import Settings
from fabloom.contracts import ModelRole, Platform, Scenario
from fabloom.provenance import git_sha
from fabloom.web.guards import RateLimiter, SpendBreaker, spent_today
from fabloom.web.runs import BusyError, ReadOnlyError, RunLocation, RunService, summarize

log = structlog.get_logger(__name__)

HERE = Path(__file__).parent
# A run that outlives this has hit max_wall_clock_s; stop holding the connection open.
SSE_TIMEOUT_S = 1200.0
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}


class RunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: str = Field(min_length=10, max_length=2000)
    platform: Platform | None = None


def client_ip(request: Request, trust_proxy: bool) -> str:
    if trust_proxy and (fwd := request.headers.get("x-forwarded-for")):
        # The rightmost entry is the one our own proxy appended; anything left of it is
        # whatever the visitor chose to send.
        return fwd.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


@dataclass(frozen=True)
class _Web:
    settings: Settings
    machine: RunMachine | None
    service: RunService
    limiter: RateLimiter
    breaker: SpendBreaker
    examples: list[Scenario]
    templates: Jinja2Templates
    git_sha: str  # read once: git_sha() may shell out to git

    def info(self) -> dict[str, Any]:
        m = self.machine
        return {
            "version": __version__,
            "git_sha": self.git_sha,
            "mode": "read-only" if self.service.read_only_reason else "live",
            "read_only_reason": self.service.read_only_reason,
            "verifier": m.verifier.name if m else None,
            "models": {r.value: m.router.spec(r).id for r in ModelRole} if m else None,
            "spend_today_usd": round(self.breaker.spent_usd, 4),
            "spend_cap_usd": self.breaker.cap_usd,
            "active_runs": self.service.active,
        }

    def locate(self, run_id: str) -> RunLocation:
        loc = self.service.locate(run_id)
        if loc is None:
            raise HTTPException(404, "no such run")
        return loc


def create_app(
    settings: Settings,
    machine: RunMachine | None,
    *,
    examples: list[Scenario] | None = None,
    limiter: RateLimiter | None = None,
    breaker: SpendBreaker | None = None,
) -> FastAPI:
    runs_dir = machine.runs_dir if machine else settings.runs_dir
    breaker = breaker or SpendBreaker(
        settings.web_spend_cap_usd, spent_today(runs_dir, datetime.now(UTC).date())
    )
    service = RunService(
        machine, runs_dir, settings.gallery_dir, breaker, settings.web_max_active_runs
    )
    w = _Web(
        settings=settings,
        machine=machine,
        service=service,
        limiter=limiter or RateLimiter(settings.web_runs_per_ip, settings.web_refill_s),
        breaker=breaker,
        examples=examples or [],
        templates=Jinja2Templates(directory=HERE / "templates"),
        git_sha=git_sha(),
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await service.drain()

    app = FastAPI(
        title="Fabloom",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.service = service
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    _add_middleware(app)
    _add_ops_routes(app, w)
    _add_api_routes(app, w)
    _add_html_routes(app, w)
    return app


def _add_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        rid = request.headers.get("x-request-id", "")[:64] or uuid.uuid4().hex
        structlog.contextvars.bind_contextvars(request_id=rid)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.unbind_contextvars("request_id")
        response.headers["X-Request-ID"] = rid
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        log.info(
            "http_request",
            request_id=rid,
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=int((time.perf_counter() - start) * 1000),
        )
        return response


def _add_ops_routes(app: FastAPI, w: _Web) -> None:
    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> dict[str, Any]:
        # Read-only is a working state: the gallery is served. Ready means "serving".
        return {"status": "ready", "mode": w.info()["mode"]}

    @app.get("/info")
    async def info() -> dict[str, Any]:
        return w.info()

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def _add_api_routes(app: FastAPI, w: _Web) -> None:
    @app.get("/api/v1/scenarios")
    async def scenarios() -> list[dict[str, Any]]:
        return [
            {"id": s.id, "family": s.family, "platform": s.intent.platform, "request": s.request}
            for s in w.examples
        ]

    @app.get("/api/v1/gallery")
    async def gallery() -> list[dict[str, Any]]:
        return w.service.gallery()

    @app.post("/api/v1/runs", status_code=202)
    async def create_run(body: RunCreate, request: Request) -> JSONResponse:
        ip = client_ip(request, w.settings.web_trust_proxy)
        if (reason := w.service.read_only_reason) is not None:
            raise HTTPException(503, f"read-only: {reason}")
        if not w.limiter.allow(ip):
            wait = w.limiter.retry_after_s(ip)
            raise HTTPException(
                429, f"rate limited: try again in {wait}s", headers={"Retry-After": str(wait)}
            )
        try:
            run_id = w.service.start(body.request, body.platform)
        except ReadOnlyError as exc:
            raise HTTPException(503, f"read-only: {exc}") from exc
        except BusyError as exc:
            raise HTTPException(503, str(exc), headers={"Retry-After": "30"}) from exc
        log.info("web_run_started", run_id=run_id, ip=ip)
        return JSONResponse(
            {
                "run_id": run_id,
                "url": f"/runs/{run_id}",
                "events_url": f"/api/v1/runs/{run_id}/events",
            },
            status_code=202,
        )

    @app.get("/api/v1/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        return summarize(w.locate(run_id))

    @app.get("/api/v1/runs/{run_id}/events")
    async def run_events(run_id: str) -> StreamingResponse:
        loc = w.locate(run_id)

        async def stream() -> AsyncIterator[str]:
            async for e in tail_events(loc.events, timeout_s=SSE_TIMEOUT_S):
                yield f"id: {e.seq}\nevent: {e.type}\ndata: {e.model_dump_json()}\n\n"
            yield "event: end\ndata: {}\n\n"

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )


def _add_html_routes(app: FastAPI, w: _Web) -> None:
    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index(request: Request) -> HTMLResponse:
        return w.templates.TemplateResponse(
            request,
            "index.html",
            {
                "info": w.info(),
                "gallery": w.service.gallery(),
                "examples": [s.request for s in w.examples[:6]],
            },
        )

    @app.get("/runs/{run_id}", response_class=HTMLResponse, include_in_schema=False)
    async def run_page(request: Request, run_id: str) -> HTMLResponse:
        loc = w.locate(run_id)
        return w.templates.TemplateResponse(
            request, "run.html", {"info": w.info(), "run_id": loc.run_id, "gallery": loc.gallery}
        )
