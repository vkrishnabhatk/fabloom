"""``PrelintGate``: run the deterministic pre-lint, and only call the backend if it passes.

A candidate rejected here costs no verifier execution; its report says so with
``backend_executed=False``, which is what the run budget counts.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from fabloom.contracts import Check, Snapshot, VerificationReport
from fabloom.contracts.verifier import Verifier
from fabloom.verify.prelint import prelint, prelint_report


class PrelintGate:
    def __init__(self, inner: Verifier) -> None:
        self.inner = inner
        self.name = inner.name
        self.level = inner.level
        self.platforms = inner.platforms

    async def verify(
        self, snapshot: Snapshot, configs: Mapping[str, str], checks: Sequence[Check]
    ) -> VerificationReport:
        results = prelint(snapshot.fabric, configs)
        if not all(r.passed for r in results):
            return prelint_report(results, checks)
        return await self.inner.verify(snapshot, configs, checks)
