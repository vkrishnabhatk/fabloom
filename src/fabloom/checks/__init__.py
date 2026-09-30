"""Checks plane: the deterministic intent -> check-set compiler and address allocator.

Boundary rule: depends only on ``fabloom.contracts``. No I/O, no randomness, no model.
Whatever this package emits is the rubric a run is graded against, so nothing the model says
can reach it.
"""

from fabloom.checks.allocate import AllocationError, allocate, allocate_fabric
from fabloom.checks.compile import REMEDIATION, SEVERITY, compile_checks

__all__ = [
    "REMEDIATION",
    "SEVERITY",
    "AllocationError",
    "allocate",
    "allocate_fabric",
    "compile_checks",
]
