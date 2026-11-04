"""Planner accuracy: does the planner's intent match each benchmark scenario's ground truth?

    uv run python spikes/planner_sweep.py [nano] [lightning-nothink] [lightning-think]

The benchmark grades configs against the ground-truth intent, never against the planner's
reading of the request, so it cannot see a planner that misreads a request. This spike
measures that separately: one planner call per scenario (benchmark/v0.1 + vendors-v0.1),
through the product's client, compared field by field with the scenario's intent. One
attempt, no retry, so a miss here is a miss of the first call (the agent retries an invalid
answer once). Results go to spikes/results/. Used for ADR-0010.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fabloom.config import REPO_ROOT, ModelSpec, Settings
from fabloom.contracts import Intent
from fabloom.llm import TokenFactoryClient
from fabloom.llm.prompts import planner_messages
from fabloom.scenarios import load_scenario

_PRICES = {"usd_per_m_input": 0.06, "usd_per_m_output": 0.24, "price_verified": True}
_COMMON: dict[str, Any] = {**_PRICES, "max_output_tokens": 12000, "temperature": 0.0}
SPECS = {
    "nano": ModelSpec(id="nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B", **_COMMON),
    "lightning-nothink": ModelSpec(
        id="nvidia/Nemotron-3_5-Lightning",
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        **_COMMON,
    ),
    "lightning-think": ModelSpec(id="nvidia/Nemotron-3_5-Lightning", **_COMMON),
}


async def run(arms: list[str]) -> dict[str, Any]:
    settings = Settings()
    if settings.token_factory_api_key is None:
        raise SystemExit("set FABLOOM_TOKEN_FACTORY_API_KEY")
    client = TokenFactoryClient(
        settings.token_factory_api_key.get_secret_value(), settings.token_factory_base_url
    )
    paths = sorted((REPO_ROOT / "benchmark/v0.1").glob("*.yaml")) + sorted(
        (REPO_ROOT / "benchmark/vendors-v0.1").glob("*.yaml")
    )
    in_flight = asyncio.Semaphore(8)

    async def one(arm: str, path: Path) -> dict[str, Any]:
        sc = load_scenario(path)
        row: dict[str, Any] = {"arm": arm, "scenario": sc.id}
        async with in_flight:
            try:
                c = await client.complete_json(
                    SPECS[arm], planner_messages(sc.request, sc.intent.platform), Intent
                )
            except Exception as exc:  # every failure is a data point, not a crash
                row.update(ok=False, error=f"{type(exc).__name__}: {exc}"[:300])
                return row
        got, want = c.value.model_dump(mode="json"), sc.intent.model_dump(mode="json")
        diff = {k: {"got": got.get(k), "want": v} for k, v in want.items() if got.get(k) != v}
        row.update(
            ok=not diff,
            diff=diff,
            completion_tokens=c.completion_tokens,
            cost_usd=c.cost_usd,
            ms=c.duration_ms,
        )
        return row

    rows = await asyncio.gather(*(one(a, p) for a in arms for p in paths))
    summary: dict[str, Any] = {}
    for arm in arms:
        rs = [r for r in rows if r["arm"] == arm]
        ms = sorted(r["ms"] for r in rs if "ms" in r)
        summary[arm] = {
            "model": SPECS[arm].id,
            "exact": f"{sum(r['ok'] for r in rs)}/{len(rs)}",
            "errors": sum("error" in r for r in rs),
            "median_ms": ms[len(ms) // 2] if ms else None,
            "max_ms": ms[-1] if ms else None,
            "cost_usd": round(sum(r.get("cost_usd", 0.0) for r in rs), 4),
            "wrong_fields": dict(Counter(k for r in rs for k in r.get("diff", {}))),
        }
    print(json.dumps(summary, indent=2))
    for r in rows:
        if not r["ok"]:
            print(json.dumps(r)[:600])
    return {"at": datetime.now(UTC).isoformat(), "summary": summary, "rows": rows}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("arms", nargs="*", metavar="ARM", help=f"any of {', '.join(SPECS)}")
    args = parser.parse_args()
    if unknown := set(args.arms) - set(SPECS):
        parser.error(f"unknown arm(s): {', '.join(sorted(unknown))}")
    out = asyncio.run(run(args.arms or list(SPECS)))
    results = REPO_ROOT / "spikes/results"
    results.mkdir(exist_ok=True)
    path = results / f"planner_sweep-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(out, indent=2) + "\n")
    print(f"\nwrote {path.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
