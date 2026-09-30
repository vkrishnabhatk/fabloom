"""Provenance helpers, carried over from moodrift.

``content_key`` hashes the scenario set, prompt templates and candidate configs. The
candidate hash doubles as the verification dedup key (and the Sandboxes checkpoint key).
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

from fabloom.config import REPO_ROOT


@lru_cache(maxsize=1)
def git_sha(short: bool = True) -> str:
    """The commit this process runs from: ``IMAGE_TAG`` in a container, else ``git``."""
    if tag := os.environ.get("IMAGE_TAG"):
        return tag
    args = ["git", "rev-parse", "--short" if short else "--verify", "HEAD"]
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, no shell
            args, cwd=REPO_ROOT, capture_output=True, text=True, check=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return out.stdout.strip()


def file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def content_key(obj: Any, length: int = 16) -> str:
    """Stable hash of any JSON-able object: dict key order and whitespace do not matter."""
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:length]


def set_seeds(seed: int) -> None:
    random.seed(seed)
    os.environ["PYTHONHASHSEED"] = str(seed)
