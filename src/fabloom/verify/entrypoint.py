"""``fabloom-verify``: the entry point baked into the verifier image (ADR-0003).

Inside a Nebius Sandbox the image's start command is ignored, so this process starts Batfish
itself (from the AppCDS archive, via ``FABLOOM_BATFISH_START_CMD``), waits for it, runs the
compiled checks against one candidate, prints exactly one JSON line (the
``VerificationReport``) and exits. One candidate per execution keeps executions
content-addressable: identical candidates collide on the checkpoint id and reuse the result.

    fabloom-verify --job job.json
    job.json = {"snapshot": Snapshot, "configs": {device: text}, "checks": [Check, ...]}
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import socket
import subprocess
import sys
import time
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from fabloom.contracts import Check, Snapshot
from fabloom.verify.batfish_local import local_verifier

BATFISH_PORT = 9996


class VerifyJob(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot: Snapshot
    configs: dict[str, str]
    checks: list[Check]


def wait_for_port(host: str, port: int, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            with socket.create_connection((host, port), timeout=1):
                return
        except OSError:
            if time.monotonic() > deadline:
                raise TimeoutError(f"Batfish did not open {host}:{port} in {timeout_s}s") from None
            time.sleep(0.5)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fabloom-verify")
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--startup-timeout", type=float, default=180.0)
    args = parser.parse_args(argv)

    job = VerifyJob.model_validate_json(args.job.read_text(encoding="utf-8"))
    start_cmd = os.environ.get("FABLOOM_BATFISH_START_CMD")
    server = None
    if start_cmd:
        server = subprocess.Popen(  # noqa: S603 - command comes from the image's own ENV
            shlex.split(start_cmd), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    try:
        wait_for_port(args.host, BATFISH_PORT, args.startup_timeout)
        report = asyncio.run(
            local_verifier(args.host).verify(job.snapshot, job.configs, job.checks)
        )
    except Exception as exc:
        sys.stdout.write(json.dumps({"error": f"{type(exc).__name__}: {exc}"}) + "\n")
        return 1
    else:
        sys.stdout.write(report.model_dump_json() + "\n")
        return 0
    finally:
        if server is not None:
            server.terminate()
            server.wait(timeout=30)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
