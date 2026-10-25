"""Vendor documentation search: for the repairer, and for the reader of a result.

Doc-grounded repair: vendor documentation for the lines a parser did not recognise.

An unrecognised line usually means the model invented a command (architecture §8). Before a
repair, the agent hands those lines to a ``Grounder``, which searches the platform vendor's
own docs (Tavily, restricted to the domains in ``conf/fabloom.yaml``) and returns short
excerpts for the repair prompt.

Grounding only adds context to a prompt. It never touches a check or a report, so it sits
on the model's side of the verification boundary (ADR-0001). It is best-effort: a search
error yields no excerpts and an ``error`` note, never a failed run.

Doc links: after verification, one search per feature the intent uses (``doc_queries``),
linked beside the result for the engineer reviewing it. Same domains, same cache, but a
cached answer expires, because the page a reader is sent to should still exist.

Results are cached on disk by (platform, query, domains): the benchmark repeats the same
invented commands across scenarios, and a re-run should not spend search credits twice.
Uncached searches are spaced by ``min_interval_s`` so a burst of runs cannot hammer the API.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx2

from fabloom.config import GroundingConfig
from fabloom.contracts import BgpMode, Intent, Platform, UnrecognizedLine
from fabloom.scrub import scrub

TAVILY_URL = "https://api.tavily.com/search"
MAX_QUERY_TOKENS = 6

_PLATFORM_NAME = {Platform.FRR: "FRRouting", Platform.ARISTA_EOS: "Arista EOS"}
# Addresses, prefixes, ASNs and other numbers say nothing about a command's syntax, and
# keeping them out of the query keeps a user's addressing plan out of a third-party API.
_VALUE = re.compile(r"^[\d.:/]+$|^[0-9a-f:]+::?[0-9a-f:/]*$", re.IGNORECASE)
_TRAILING_DIGITS = re.compile(r"(?<=[A-Za-z])[\d/.:]+$")
# Versioned doc trees (docs.frrouting.org/en/stable-8.2/bgp.html) hold the same page per release.
_DOC_VERSION = re.compile(r"/en/[^/]+/")


@dataclass(frozen=True)
class Excerpt:
    query: str
    url: str
    title: str
    text: str


@dataclass(frozen=True)
class Grounding:
    queries: list[str] = field(default_factory=list)
    excerpts: list[Excerpt] = field(default_factory=list)
    searched: int = 0  # queries that reached the search API; the rest were cache hits
    error: str | None = None


class Grounder(Protocol):
    async def lookup(
        self, platform: Platform, lines: Sequence[UnrecognizedLine], max_searches: int
    ) -> Grounding: ...

    async def docs(self, intent: Intent, max_searches: int, max_age_s: float) -> Grounding: ...


class DocSearch(Protocol):
    async def search(
        self, query: str, domains: Sequence[str], max_results: int
    ) -> list[dict[str, str]]: ...


class GroundingError(RuntimeError):
    pass


def command_query(text: str) -> str:
    """The command's shape: keywords kept, values dropped, interface numbers stripped.

    ``" neighbor 10.0.0.1 bfd fast-detect"`` becomes ``"neighbor bfd fast-detect"``.
    """
    words: list[str] = []
    for token in text.strip().split():
        if _VALUE.match(token):
            continue
        words.append(_TRAILING_DIGITS.sub("", token))
        if len(words) == MAX_QUERY_TOKENS:
            break
    return " ".join(words)


def build_queries(platform: Platform, lines: Sequence[UnrecognizedLine]) -> list[str]:
    """One query per distinct command shape, in the order the parser reported them."""
    seen: dict[str, None] = {}
    for line in lines:
        shape = command_query(line.text)
        if shape:
            query = scrub(f"{_PLATFORM_NAME[platform]} {shape} command syntax").text
            seen.setdefault(query, None)
    return list(seen)


def doc_queries(intent: Intent) -> list[str]:
    """One query per feature the fabric uses, in a fixed order.

    Built from the intent's features alone, never from its addresses, ASNs or names, so
    nothing of the user's plan reaches the search API and repeat runs share the cache.
    """
    name = _PLATFORM_NAME[intent.platform]
    queries = [
        f"{name} BGP unnumbered interface neighbor configuration"
        if intent.bgp_mode == BgpMode.UNNUMBERED
        else f"{name} BGP neighbor remote-as configuration"
    ]
    if intent.advertise_loopbacks:
        queries.append(f"{name} BGP network statement advertise prefix")
    # FRR filters are reported as not verifiable (ADR-0006); no doc link implies otherwise.
    if intent.filters and intent.platform == Platform.ARISTA_EOS:
        queries.append(f"{name} IP access list deny {intent.filters[0].protocol} apply interface")
    return queries


def best_links(excerpts: Sequence[Excerpt], limit: int) -> list[Excerpt]:
    """The top hit per query, each page once whatever its release, https only.

    Measured on 2026-10-25: a query's later hits were the same FRR page in older releases,
    or unrelated EOS chapters, so only the first distinct page per topic is worth a link.
    """
    pages: set[str] = set()
    done: set[str] = set()
    out: list[Excerpt] = []
    for x in excerpts:
        page = _DOC_VERSION.sub("/en/", x.url)
        if x.query in done or page in pages or not x.url.startswith("https://"):
            continue
        pages.add(page)
        done.add(x.query)
        out.append(x)
    return out[:limit]


class TavilySearch:
    def __init__(
        self, api_key: str, timeout_s: float = 15.0, http_client: httpx2.AsyncClient | None = None
    ) -> None:
        self._key = api_key
        self._http = http_client or httpx2.AsyncClient(timeout=timeout_s)

    async def search(
        self, query: str, domains: Sequence[str], max_results: int
    ) -> list[dict[str, str]]:
        body: dict[str, Any] = {
            "query": query,
            "search_depth": "basic",
            "max_results": max_results,
            "include_domains": list(domains),
        }
        try:
            resp = await self._http.post(
                TAVILY_URL, json=body, headers={"Authorization": f"Bearer {self._key}"}
            )
            resp.raise_for_status()
            results = resp.json().get("results", [])
        except (httpx2.HTTPError, ValueError) as exc:
            raise GroundingError(f"tavily: {type(exc).__name__}: {exc}") from exc
        return [
            {"url": str(r.get("url", "")), "title": str(r.get("title", "")),
             "content": str(r.get("content", ""))}
            for r in results if isinstance(r, dict)
        ]  # fmt: skip


class _Spacer:
    """At most one uncached search per ``interval_s`` across every run in the process."""

    def __init__(
        self,
        interval_s: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._interval = interval_s
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._last: float | None = None

    async def wait(self) -> None:
        async with self._lock:
            if self._last is not None:
                delay = self._interval - (self._clock() - self._last)
                if delay > 0:
                    await self._sleep(delay)
            self._last = self._clock()


class DocGrounder:
    def __init__(
        self,
        search: DocSearch,
        config: GroundingConfig,
        cache_dir: Path,
        spacer: _Spacer | None = None,
    ) -> None:
        self._search = search
        self._config = config
        self._cache_dir = cache_dir
        self._spacer = spacer or _Spacer(config.min_interval_s)

    def _cache_path(self, platform: Platform, query: str, domains: Sequence[str]) -> Path:
        key = json.dumps([platform.value, query, sorted(domains)], separators=(",", ":"))
        return self._cache_dir / f"{hashlib.sha256(key.encode()).hexdigest()[:24]}.json"

    def _excerpts(self, query: str, hits: list[dict[str, str]]) -> list[Excerpt]:
        cap = self._config.max_excerpt_chars
        return [
            Excerpt(query=query, url=h["url"], title=h["title"], text=h["content"][:cap])
            for h in hits[: self._config.max_results]
            if h.get("content")
        ]

    async def lookup(
        self, platform: Platform, lines: Sequence[UnrecognizedLine], max_searches: int
    ) -> Grounding:
        return await self._run(platform, build_queries(platform, lines), max_searches, None)

    async def docs(self, intent: Intent, max_searches: int, max_age_s: float) -> Grounding:
        return await self._run(intent.platform, doc_queries(intent), max_searches, max_age_s)

    async def _run(
        self,
        platform: Platform,
        queries: list[str],
        max_searches: int,
        max_age_s: float | None,
    ) -> Grounding:
        domains = self._config.domains.get(platform, [])
        if not domains:
            # Unrestricted web search would put forum guesses in the prompt as if they were
            # the vendor's syntax; no configured domains means no grounding for the platform.
            return Grounding(error=f"no documentation domains configured for {platform}")
        excerpts: list[Excerpt] = []
        searched = 0
        errors: list[str] = []
        for query in queries:
            path = self._cache_path(platform, query, domains)
            cached = path.exists()
            fresh = cached and (max_age_s is None or time.time() - path.stat().st_mtime < max_age_s)
            if fresh or (cached and searched >= max_searches):
                hits = json.loads(path.read_text(encoding="utf-8"))["hits"]
            elif searched < max_searches:
                await self._spacer.wait()
                searched += 1
                try:
                    hits = await self._search.search(query, domains, self._config.max_results)
                except GroundingError as exc:
                    errors.append(str(exc))
                    if not cached:
                        continue
                    # A stale answer beats none: the search failed, the old pages likely exist.
                    hits = json.loads(path.read_text(encoding="utf-8"))["hits"]
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    tmp = path.with_suffix(".tmp")
                    tmp.write_text(json.dumps({"query": query, "hits": hits}), encoding="utf-8")
                    tmp.replace(path)
            else:
                continue
            excerpts.extend(self._excerpts(query, hits))
        return Grounding(
            queries=queries,
            excerpts=excerpts,
            searched=searched,
            error="; ".join(errors) or None,
        )
