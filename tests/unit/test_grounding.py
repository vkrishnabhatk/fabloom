"""Doc-grounded repair, offline: a scripted search, httpx2.MockTransport for Tavily."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import httpx2
import pytest

from fabloom.agent import BudgetTracker, RunMachine, RunRequest, read_events
from fabloom.agent.events import EVENTS_FILE
from fabloom.config import ArmSpec, DocsConfig, GroundingConfig, RunConfig
from fabloom.contracts import (
    BgpMode,
    Check,
    CheckSet,
    FabricPlan,
    FilterRule,
    Intent,
    Platform,
    RunBudget,
    Snapshot,
    TopologySpec,
    UnrecognizedLine,
    VerificationReport,
)
from fabloom.contracts.run import (
    DocsFound,
    ExplanationReady,
    GroundingLookup,
    VerificationCompleted,
)
from fabloom.llm import ModelRouter
from fabloom.llm.grounding import (
    TAVILY_URL,
    DocGrounder,
    Excerpt,
    GroundingError,
    TavilySearch,
    _Spacer,
    best_links,
    build_queries,
    command_query,
    doc_queries,
)
from fabloom.llm.prompts import GROUNDING_HEADER, repair_messages
from tests.fakes import FakeChatClient, FakeVerifier
from tests.fakes.render import as_device_blocks, render_good_configs

pytestmark = pytest.mark.unit

CONFIG = GroundingConfig(
    enabled=True,
    domains={Platform.FRR: ["docs.frrouting.org"]},
    max_results=2,
    max_excerpt_chars=100,
    min_interval_s=0,
)
INVENTED = [
    UnrecognizedLine(device="leaf1", line_number=12, text=" neighbor 10.0.0.1 bfd fast-detect"),
    UnrecognizedLine(device="leaf2", line_number=12, text=" neighbor 10.0.0.3 bfd fast-detect"),
    UnrecognizedLine(device="leaf1", line_number=20, text="interface swp1 mtu-jumbo 9216"),
]


class ScriptedSearch:
    def __init__(self, fail: bool = False) -> None:
        self.queries: list[str] = []
        self.fail = fail

    async def search(
        self, query: str, domains: Sequence[str], max_results: int
    ) -> list[dict[str, str]]:
        self.queries.append(query)
        if self.fail:
            raise GroundingError("tavily: down")
        return [
            {"url": f"https://docs.frrouting.org/{len(self.queries)}", "title": "BGP",
             "content": f"excerpt for {query} " + "x" * 100},
            {"url": "https://docs.frrouting.org/empty", "title": "empty", "content": ""},
        ]  # fmt: skip


def test_command_query_keeps_keywords_and_drops_values() -> None:
    assert command_query(" neighbor 10.0.0.1 bfd fast-detect") == "neighbor bfd fast-detect"
    assert command_query("interface Ethernet3/1 mtu 9216") == "interface Ethernet mtu"
    assert command_query(" ip address 10.0.0.0/31") == "ip address"
    assert command_query("   ") == ""


def test_queries_are_one_per_command_shape() -> None:
    queries = build_queries(Platform.FRR, INVENTED)
    assert queries == [
        "FRRouting neighbor bfd fast-detect command syntax",
        "FRRouting interface swp mtu-jumbo command syntax",
    ]
    assert not any("10.0.0" in q for q in queries)


async def test_lookup_caches_and_respects_the_search_allowance(tmp_path: Path) -> None:
    search = ScriptedSearch()
    grounder = DocGrounder(search, CONFIG, tmp_path)

    first = await grounder.lookup(Platform.FRR, INVENTED, max_searches=1)
    assert first.searched == 1
    assert len(first.queries) == 2
    # empty pages are dropped and text is capped at max_excerpt_chars
    assert [len(x.text) for x in first.excerpts] == [100]
    assert first.excerpts[0].text.startswith("excerpt for FRRouting neighbor bfd fast-detect")

    again = await grounder.lookup(Platform.FRR, INVENTED, max_searches=5)
    assert again.searched == 1  # the first query came from the cache
    assert len(again.excerpts) == 2
    assert len(search.queries) == 2


async def test_lookup_without_domains_or_with_a_failing_search(tmp_path: Path) -> None:
    none = await DocGrounder(ScriptedSearch(), CONFIG, tmp_path).lookup(
        Platform.ARISTA_EOS, INVENTED, max_searches=5
    )
    assert none.excerpts == []
    assert none.error is not None
    assert "no documentation domains" in none.error

    failing = await DocGrounder(ScriptedSearch(fail=True), CONFIG, tmp_path).lookup(
        Platform.FRR, INVENTED, max_searches=5
    )
    assert failing.excerpts == []
    assert failing.searched == 2
    assert failing.error is not None
    assert "down" in failing.error
    # A failure is never cached: the next lookup searches both queries again.
    retry = await DocGrounder(ScriptedSearch(), CONFIG, tmp_path).lookup(
        Platform.FRR, INVENTED, max_searches=5
    )
    assert retry.searched == 2


def _tavily(handler: Callable[[httpx2.Request], httpx2.Response]) -> TavilySearch:
    return TavilySearch(
        "tvly-k", http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    )


async def test_tavily_sends_the_domain_filter_and_parses_results() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(
            200, json={"results": [{"url": "u", "title": "t", "content": "c"}, "junk"]}
        )

    hits = await _tavily(handler).search("q", ["docs.frrouting.org"], 3)
    assert hits == [{"url": "u", "title": "t", "content": "c"}]
    assert str(seen[0].url) == TAVILY_URL
    assert seen[0].headers["Authorization"] == "Bearer tvly-k"
    body = json.loads(seen[0].content)
    assert body["include_domains"] == ["docs.frrouting.org"]
    assert body["max_results"] == 3


async def test_tavily_errors_become_grounding_errors() -> None:
    search = _tavily(lambda r: httpx2.Response(429, json={"detail": "slow down"}))
    with pytest.raises(GroundingError, match="429"):
        await search.search("q", ["d"], 1)


async def test_spacer_waits_between_searches() -> None:
    now = [100.0]
    slept: list[float] = []

    async def sleep(s: float) -> None:
        slept.append(s)
        now[0] += s

    spacer = _Spacer(2.0, clock=lambda: now[0], sleep=sleep)
    await spacer.wait()
    now[0] += 0.5
    await spacer.wait()
    assert slept == [1.5]


def test_repair_prompt_frames_excerpts_as_reference(
    frr_intent: Intent, frr_fabric: FabricPlan, frr_checkset: CheckSet
) -> None:
    report = VerificationReport(
        level="L2", checks=[], score=0.0, duration_ms=1, verifier_name="v", verifier_version="1",
        unrecognized_lines=INVENTED,
    )  # fmt: skip
    ref = Excerpt(query="q", url="https://docs.frrouting.org/bgp", title="t", text="neighbor X bfd")
    plain = repair_messages(frr_intent, frr_fabric, {}, report, frr_checkset)[1]["content"]
    grounded = repair_messages(frr_intent, frr_fabric, {}, report, frr_checkset, references=[ref])
    assert GROUNDING_HEADER not in plain
    assert GROUNDING_HEADER in grounded[1]["content"]
    assert '<excerpt source="https://docs.frrouting.org/bgp" query="q">' in grounded[1]["content"]


def test_budget_allowance_counts_only_searches() -> None:
    tracker = BudgetTracker(RunBudget(max_grounding_queries=3))
    tracker.record_grounding(2)
    assert tracker.grounding_allowance() == 1
    tracker.record_grounding(5)
    assert tracker.grounding_allowance() == 0


def test_only_the_grounding_arm_turns_it_on() -> None:
    base = RunConfig(grounding=CONFIG.model_copy(update={"enabled": False}))
    on = ArmSpec(description="g", grounding=True).run_config(base)
    assert on.grounding.enabled
    assert on.grounding.domains == CONFIG.domains
    assert not ArmSpec(description="plain").run_config(base).grounding.enabled


def test_no_benchmark_arm_searches_for_doc_links() -> None:
    base = RunConfig(docs=DocsConfig(enabled=True))
    assert not ArmSpec(description="g", grounding=True).run_config(base).docs.enabled


def test_doc_queries_follow_the_fabric_features_never_its_values(frr_intent: Intent) -> None:
    assert doc_queries(frr_intent) == [
        "FRRouting BGP neighbor remote-as configuration",
        "FRRouting BGP network statement advertise prefix",
    ]
    unnumbered = frr_intent.model_copy(update={"bgp_mode": BgpMode.UNNUMBERED})
    assert doc_queries(unnumbered)[0] == "FRRouting BGP unnumbered interface neighbor configuration"
    ssh = FilterRule(devices="leaf*", dst_port=22, src_not_in="10.0.0.0/8")
    eos = Intent(platform=Platform.ARISTA_EOS, topology=TopologySpec(spines=2, leaves=2),
                 advertise_loopbacks=False, filters=[ssh])  # fmt: skip
    assert doc_queries(eos) == [
        "Arista EOS BGP neighbor remote-as configuration",
        "Arista EOS IP access list deny tcp apply interface",
    ]
    # FRR filters are not verifiable, so they get no doc link that would suggest otherwise.
    assert len(doc_queries(frr_intent.model_copy(update={"filters": [ssh]}))) == 2
    assert not any("10.0.0" in q or "22" in q for q in doc_queries(eos))


def test_best_links_keep_one_distinct_page_per_topic() -> None:
    def x(query: str, url: str) -> Excerpt:
        return Excerpt(query=query, url=url, title="t", text="")

    hits = [
        x("bgp", "https://docs.frrouting.org/en/latest/bgp.html"),
        x("bgp", "https://docs.frrouting.org/en/stable-8.2/bgp.html"),
        x("loopbacks", "https://docs.frrouting.org/en/stable-10.2/bgp.html"),  # same page
        x("loopbacks", "https://docs.frrouting.org/en/latest/zebra.html"),
        x("acl", "http://www.arista.com/en/um-eos/eos-acls"),  # not https
        x("acl", "https://www.arista.com/en/um-eos/eos-acls"),
        x("acl", "https://www.arista.com/en/um-eos/eos-data-transfer"),
    ]
    assert [h.url for h in best_links(hits, 6)] == [
        "https://docs.frrouting.org/en/latest/bgp.html",
        "https://docs.frrouting.org/en/latest/zebra.html",
        "https://www.arista.com/en/um-eos/eos-acls",
    ]
    assert len(best_links(hits, 2)) == 2


def _age_cache(cache_dir: Path, days: float) -> None:
    then = time.time() - days * 86400
    for path in cache_dir.glob("*.json"):
        os.utime(path, (then, then))


async def test_doc_links_expire_and_a_failed_refresh_keeps_the_old_ones(
    tmp_path: Path, frr_intent: Intent
) -> None:
    search = ScriptedSearch()
    grounder = DocGrounder(search, CONFIG, tmp_path)
    first = await grounder.docs(frr_intent, max_searches=3, max_age_s=3600)
    assert first.searched == 2
    assert {x.url for x in first.excerpts} == {
        "https://docs.frrouting.org/1",
        "https://docs.frrouting.org/2",
    }

    again = await grounder.docs(frr_intent, max_searches=3, max_age_s=3600)
    assert again.searched == 0  # fresh in the cache

    _age_cache(tmp_path, days=8)
    failing = DocGrounder(ScriptedSearch(fail=True), CONFIG, tmp_path)
    stale = await failing.docs(frr_intent, max_searches=3, max_age_s=7 * 86400)
    assert stale.searched == 2
    assert stale.error is not None
    assert {x.url for x in stale.excerpts} == {x.url for x in first.excerpts}

    refreshed = await grounder.docs(frr_intent, max_searches=1, max_age_s=7 * 86400)
    assert refreshed.searched == 1  # one stale answer refreshed, the other served stale
    assert len(refreshed.excerpts) == 2


@pytest.mark.parametrize("enabled", [True, False])
async def test_a_run_links_vendor_docs_after_verification(
    enabled: bool, runs_dir: Path, router: ModelRouter, frr_intent: Intent, frr_fabric: FabricPlan
) -> None:
    good = render_good_configs(frr_intent, frr_fabric)
    client = FakeChatClient(script=[as_device_blocks(good, Platform.FRR), "All checks pass."])
    rc = RunConfig(budget=RunBudget(max_usd=1.0, max_grounding_queries=6),
                   docs=DocsConfig(enabled=enabled, max_queries=1))  # fmt: skip
    search = ScriptedSearch()
    grounder = DocGrounder(search, CONFIG, runs_dir.parent / "cache")
    machine = RunMachine(client, router, FakeVerifier([0]), rc, runs_dir, grounder=grounder)
    result = await machine.run(RunRequest("x" * 10, intent=frr_intent))

    assert result.outcome == "passed"
    events = list(read_events(result.run_dir / EVENTS_FILE))
    found = [e for e in events if isinstance(e, DocsFound)]
    if not enabled:
        assert found == []
        assert search.queries == []
        return
    [docs] = found
    assert docs.searched == 1  # capped by max_queries; the second feature was not searched
    assert docs.docs == [
        {"title": "BGP", "url": "https://docs.frrouting.org/1",
         "query": "FRRouting BGP neighbor remote-as configuration"},
    ]  # fmt: skip
    assert result.usage.grounding_queries == 1
    order = [type(e) for e in events]
    assert (
        order.index(VerificationCompleted) < order.index(DocsFound) < order.index(ExplanationReady)
    )
    # The explainer's prompt is the same with or without links: they never reach a model.
    assert "docs.frrouting.org" not in json.dumps(client.calls)


class InventingVerifier(FakeVerifier):
    """The first candidate has invented lines; later ones parse cleanly."""

    async def verify(
        self, snapshot: Snapshot, configs: Mapping[str, str], checks: Sequence[Check]
    ) -> VerificationReport:
        first = not self.calls
        report = await super().verify(snapshot, configs, checks)
        return report.model_copy(update={"unrecognized_lines": INVENTED}) if first else report


@pytest.mark.parametrize("enabled", [True, False])
async def test_repair_is_grounded_only_when_enabled(
    enabled: bool,
    frr_intent: Intent,
    frr_fabric: FabricPlan,
    router: ModelRouter,
    runs_dir: Path,
) -> None:
    good = render_good_configs(frr_intent, frr_fabric)
    broken = {**good, "leaf1": good["leaf1"] + " neighbor 10.0.0.1 bfd fast-detect\n"}
    client = FakeChatClient(
        script=[as_device_blocks(broken, Platform.FRR), as_device_blocks(good, Platform.FRR)]
    )
    rc = RunConfig(
        budget=RunBudget(max_usd=1.0, max_grounding_queries=1),
        explain_with_model=False,
        grounding=CONFIG.model_copy(update={"enabled": enabled}),
    )
    search = ScriptedSearch()
    machine = RunMachine(
        client,
        router,
        InventingVerifier([3, 0]),
        rc,
        runs_dir,
        grounder=DocGrounder(search, CONFIG, runs_dir.parent / "cache"),
    )
    result = await machine.run(RunRequest("x" * 10, intent=frr_intent))

    assert result.outcome == "passed"
    lookups = [
        e for e in read_events(result.run_dir / EVENTS_FILE) if isinstance(e, GroundingLookup)
    ]
    repair_prompt = client.calls[1][1][1]["content"]
    if enabled:
        assert len(lookups) == 1
        assert lookups[0].searched == 1  # capped by max_grounding_queries
        assert lookups[0].urls == ["https://docs.frrouting.org/1"]
        assert result.usage.grounding_queries == 1
        assert GROUNDING_HEADER in repair_prompt
    else:
        assert lookups == []
        assert search.queries == []
        assert GROUNDING_HEADER not in repair_prompt


def test_vendor_doc_queries_use_each_vendors_terms() -> None:
    ssh = FilterRule(devices="leaf*", dst_port=22, src_not_in="10.0.0.0/8")
    junos = Intent(
        platform=Platform.JUNOS, topology=TopologySpec(spines=2, leaves=2), filters=[ssh]
    )
    assert doc_queries(junos) == [
        "Junos configure external BGP group neighbor peer-as",
        "Junos routing policy export direct routes into BGP",
        "Junos firewall filter discard term apply family inet input",
    ]
    # SONiC filters are not verified, so no filter doc link either.
    sonic = Intent(
        platform=Platform.SONIC, topology=TopologySpec(spines=2, leaves=2), filters=[ssh]
    )
    assert len(doc_queries(sonic)) == 2


def test_wiki_revisions_are_the_same_page() -> None:
    page = "https://github.com/sonic-net/SONiC/wiki/Configuration"
    hits = [
        Excerpt(
            query="a", url=page + "/8ca83fdf38c2680d4e38ce7584e39362eb617b16", title="t", text=""
        ),
        Excerpt(query="b", url=page, title="t", text=""),
    ]
    assert len(best_links(hits, 6)) == 1
