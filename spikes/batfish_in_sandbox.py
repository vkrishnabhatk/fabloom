"""Week-0 spike: does Batfish fit in a Nebius Sandbox, how fast does it start, what does it cost?

Answers the architecture's open questions before any sandbox code is written:
  1. RAM and CPUs a sandbox actually gets (undocumented).
  2. Time to import the image (L0), make the warm checkpoint (L1), and run one candidate (L3).
  3. Whether identical commands on the same checkpoint yield the same UUID (dedup claim).
  4. Whether fabloom-verify's report parses and passes on a known-good training job.

    uv run --with contree-sdk==0.3.6 python spikes/batfish_in_sandbox.py \
        --image ghcr.io/vkrishnabhatk/fabloom-verifier:git-<sha>

Needs FABLOOM_TOKEN_FACTORY_API_KEY (or NEBIUS_API_KEY). Cost per execution is not exposed by
the SDK: read it from the Token Factory console after the run and record it in ADR-0003.
Results go to spikes/results/batfish_in_sandbox-<timestamp>.json.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from contree_sdk import ContreeSync

ENDPOINT = "https://api.tokenfactory.nebius.com/sandboxes/"
REPO = Path(__file__).resolve().parents[1]
DEFAULT_JOB = REPO / "docker/verifier/training/leaf-spine-ebgp-0002.json"


def timed(label: str, results: dict[str, Any], fn: Any) -> Any:
    start = time.perf_counter()
    out = fn()
    results[f"{label}_s"] = round(time.perf_counter() - start, 2)
    print(f"{label:<28} {results[f'{label}_s']:>8.2f}s")
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True, help="fabloom-verifier image reference")
    parser.add_argument("--job", type=Path, default=DEFAULT_JOB)
    parser.add_argument("--timeout", type=float, default=900)
    args = parser.parse_args()

    token = os.environ.get("FABLOOM_TOKEN_FACTORY_API_KEY") or os.environ["NEBIUS_API_KEY"]
    client = ContreeSync(base_url=ENDPOINT, token=token)
    r: dict[str, Any] = {
        "image": args.image,
        "job": args.job.name,
        "at": datetime.now(UTC).isoformat(),
    }

    image = timed("L0 import image", r, lambda: client.images.pull(args.image))
    probe = timed(
        "L1 warm checkpoint",
        r,
        lambda: image.run(
            shell="nproc; grep -E 'MemTotal|MemAvailable' /proc/meminfo; df -h / | tail -1; "
            "java -version 2>&1 | head -1; ls -la /opt/fabloom/",
            disposable=False,
            timeout=args.timeout,
        ).wait(),
    )
    r["sandbox_facts"] = str(probe.stdout)
    r["warm_uuid"] = str(probe.uuid)
    print(r["sandbox_facts"])

    job = args.job.read_bytes()
    cmd = "fabloom-verify --job /tmp/job.json"
    runs = []
    for i in range(3):  # 1st: cold-ish; 2nd/3rd: same command on the same checkpoint
        out = timed(
            f"L3 candidate #{i + 1}",
            r,
            lambda: probe.run(
                shell=cmd, files={"/tmp/job.json": job}, disposable=False, timeout=args.timeout
            ).wait(),
        )
        runs.append({"uuid": str(out.uuid), "exit_code": out.exit_code, "stdout": str(out.stdout)})
    r["candidates"] = runs
    r["dedup_same_uuid"] = len({c["uuid"] for c in runs}) == 1

    last = runs[-1]["stdout"].strip().splitlines()[-1] if runs[-1]["stdout"].strip() else "{}"
    report = json.loads(last)
    r["report_parsed"] = "checks" in report
    r["report_passed"] = bool(report.get("checks")) and all(c["passed"] for c in report["checks"])
    r["report_error"] = report.get("error")
    print(f"dedup (identical UUIDs)       {r['dedup_same_uuid']}")
    print(f"known-good job passed         {r['report_passed']}  {r['report_error'] or ''}")

    out_dir = REPO / "spikes/results"
    out_dir.mkdir(exist_ok=True)
    path = out_dir / f"batfish_in_sandbox-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(r, indent=2) + "\n")
    print(f"\nwrote {path.relative_to(REPO)}; record RAM, timings and console cost in ADR-0003")


if __name__ == "__main__":
    main()
