"""The one interface every verification backend implements.

It lives in ``contracts`` so the agent can hold a verifier without importing the ``verify``
package: concrete backends are wired in at the composition root (CLI, web app). The agent
and the model it drives have no import path to the code that grades them (ADR-0001).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol, runtime_checkable

from fabloom.contracts.checks import Check, VerificationReport
from fabloom.contracts.enums import Platform, VerificationLevel
from fabloom.contracts.fabric import Snapshot


@runtime_checkable
class Verifier(Protocol):
    name: str
    level: VerificationLevel
    platforms: frozenset[Platform]

    async def verify(
        self,
        snapshot: Snapshot,
        configs: Mapping[str, str],
        checks: Sequence[Check],
    ) -> VerificationReport:
        """Grade ``configs`` against ``checks``.

        ``checks`` is compiled from the intent by ``fabloom.checks.compile_checks`` and passed
        in explicitly. It is never produced by a model.
        """
        ...
