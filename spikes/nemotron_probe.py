"""Week-0 spike: which Nemotron models exist on Token Factory, do they do strict JSON, and how
does Super do on a first real generation?

    uv run python spikes/nemotron_probe.py [--generate leaf-spine-ebgp-0002]

1. Lists every nvidia/* model Token Factory advertises (to catch IDs not yet in conf/models.yaml).
2. For each model in conf/models.yaml: is it listed, does a strict json_schema call work,
   latency and token counts.
3. --generate: one real generator call for a pilot scenario, then deterministic pre-lint on
   the output: the first data point for "how often does Nemotron write working config".

Prices are not in the API: confirm them on the Token Factory pricing page, update
conf/models.yaml and set price_verified: true. Results go to spikes/results/.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fabloom.checks import allocate
from fabloom.config import REPO_ROOT, Settings, models_config
from fabloom.contracts import ModelRole
from fabloom.llm import TokenFactoryClient
from fabloom.llm.parsing import parse_device_blocks
from fabloom.llm.prompts import generator_messages
from fabloom.scenarios import load_scenario
from fabloom.verify import prelint


async def run(generate: str | None) -> dict[str, Any]:
    settings = Settings()
    if settings.token_factory_api_key is None:
        raise SystemExit("set FABLOOM_TOKEN_FACTORY_API_KEY")
    client = TokenFactoryClient(
        settings.token_factory_api_key.get_secret_value(), settings.token_factory_base_url
    )
    cfg = models_config()
    out: dict[str, Any] = {"at": datetime.now(UTC).isoformat()}

    listed = await client.list_models()
    out["nvidia_models"] = sorted(
        m for m in listed if "nemotron" in m.lower() or m.startswith("nvidia/")
    )
    print("Nemotron/NVIDIA models advertised:")
    for m in out["nvidia_models"]:
        print(f"  {m}")

    out["configured"] = {}
    for name, spec in cfg.models.items():
        start = time.perf_counter()
        error = None if spec.id in listed else "not listed"
        if error is None:
            error = await client.probe(spec)
        ms = int((time.perf_counter() - start) * 1000)
        out["configured"][name] = {"id": spec.id, "ok": error is None, "error": error, "ms": ms}
        mark = "ok  " if error is None else "FAIL"
        print(f"{mark} {name:<10} {spec.id:<45} {ms:>6}ms {error or ''}")

    if generate:
        sc = load_scenario(REPO_ROOT / "benchmark/v0.1" / f"{generate}.yaml")
        fabric = allocate(sc.intent)
        spec = cfg.for_role(ModelRole.GENERATOR)
        c = await client.complete_text(spec, generator_messages(sc.intent, fabric))
        configs = parse_device_blocks(c.value)
        results = prelint(fabric, configs)
        out["generation"] = {
            "scenario": generate,
            "model": spec.id,
            "prompt_tokens": c.prompt_tokens,
            "completion_tokens": c.completion_tokens,
            "cost_usd_at_configured_price": round(c.cost_usd, 6),
            "duration_ms": c.duration_ms,
            "devices_returned": sorted(configs),
            "prelint_failures": {r.id: r.evidence for r in results if not r.passed},
        }
        print(json.dumps(out["generation"], indent=2))
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--generate", metavar="SCENARIO_ID")
    args = parser.parse_args()
    out = asyncio.run(run(args.generate))
    results = REPO_ROOT / "spikes/results"
    results.mkdir(exist_ok=True)
    path: Path = results / f"nemotron_probe-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(out, indent=2) + "\n")
    print(f"\nwrote {path.relative_to(REPO_ROOT)}; update conf/models.yaml and ADR-0005")


if __name__ == "__main__":
    main()
