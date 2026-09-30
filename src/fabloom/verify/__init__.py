"""Verify plane: the ``Verifier`` protocol and its backends.

Boundary rule, both directions: ``verify`` never imports ``fabloom.llm`` or ``fabloom.agent``,
and neither of them imports ``verify``. Only the deterministic ``fabloom.checks`` package
feeds it; the agent receives a ``Verifier`` (a ``contracts`` protocol) from the composition
root. This is what keeps a model from grading its own homework, and it is enforced by
import-linter in CI, not left as a convention (ADR-0001).
"""

from fabloom.verify.gate import PrelintGate
from fabloom.verify.prelint import prelint, prelint_report
from fabloom.verify.protocol import Verifier, VerifierUnavailableError

__all__ = ["PrelintGate", "Verifier", "VerifierUnavailableError", "prelint", "prelint_report"]
