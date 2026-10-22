"""Doc-grounded repair, offline: a scripted search, httpx2.MockTransport for Tavily."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import httpx2
import pytest

from fabloom.agent import BudgetTracker, RunMachine, RunRequest, read_events
from fabloom.agent.events import EVENTS_FILE
from fabloom.config import ArmSpec, GroundingConfig, RunConfig
from fabloom.contracts import (
    Check,
    CheckSet,
    FabricPlan,
    Intent,
    Platform,
    RunBudget,
    Snapshot,
    UnrecognizedLine,
    VerificationReport,
)
from fabloom.contracts.run import GroundingLookup
from fabloom.llm import ModelRouter
from fabloom.llm.grounding import (
    TAVILY_URL,
    DocGrounder,
    Excerpt,
    GroundingError,
    TavilySearch,
    _Spacer,
    build_queries,
    command_query,
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
