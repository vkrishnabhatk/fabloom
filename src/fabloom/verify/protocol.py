"""Verifier protocol (defined in ``contracts``, re-exported here) and backend errors."""

from __future__ import annotations

from fabloom.contracts.verifier import Verifier


class VerifierUnavailableError(RuntimeError):
    """The backend cannot run at all (server down, image missing). Not a config failure."""


__all__ = ["Verifier", "VerifierUnavailableError"]
