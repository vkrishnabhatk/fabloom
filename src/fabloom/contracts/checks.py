"""Checks, their results, and the verification report."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from fabloom.contracts.enums import CheckKind, ParseStatus, Severity, VerificationLevel

# Weighted pass fraction: a parse failure costs more than a missing route. The score is the
# phase-4 RL reward, so the weights are part of the contract, not a tuning knob.
SEVERITY_WEIGHT: dict[Severity, int] = {
    Severity.CRITICAL: 4,
    Severity.HIGH: 3,
    Severity.MEDIUM: 2,
    Severity.LOW: 1,
}


class Check(BaseModel):
    """One machine-checkable assertion. Compiled from the intent, never produced by a model."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    kind: CheckKind
    severity: Severity
    description: str
    params: dict[str, Any] = Field(default_factory=dict)


class CheckSet(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    checks: list[Check]
    # Parts of the intent no check on this platform can verify; reported, never silently dropped.
    unverifiable: list[str] = Field(default_factory=list)


class CheckResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    kind: CheckKind
    passed: bool
    severity: Severity
    evidence: dict[str, Any] = Field(default_factory=dict)  # structured, never a prose blob
    remediation_hint: str | None = None


class UnrecognizedLine(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    device: str
    line_number: int | None
    text: str


class UndefinedRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    device: str
    struct_type: str
    name: str
    context: str


class VerificationReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    level: VerificationLevel
    checks: list[CheckResult]
    parse_status: dict[str, ParseStatus] = Field(default_factory=dict)  # per device
    unrecognized_lines: list[UnrecognizedLine] = Field(default_factory=list)
    undefined_references: list[UndefinedRef] = Field(default_factory=list)
    score: float
    duration_ms: int
    verifier_name: str
    verifier_version: str
    batfish_version: str | None = None
    # False when a deterministic gate rejected the candidate before any backend ran; the run
    # budget counts only executions that actually happened.
    backend_executed: bool = True

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(c.passed for c in self.checks)

    @property
    def failed_checks(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.passed]

    def summary(self) -> dict[str, Any]:
        """Compact form for the event log and prompts."""
        return {
            "level": self.level,
            "passed": self.passed,
            "score": round(self.score, 4),
            "total": len(self.checks),
            "failed": [c.id for c in self.failed_checks],
            "unrecognized_lines": len(self.unrecognized_lines),
            "verifier": self.verifier_name,
            "backend_executed": self.backend_executed,
            "duration_ms": self.duration_ms,
        }


def score_checks(results: Sequence[CheckResult]) -> float:
    """Severity-weighted pass fraction in [0, 1]. An empty result set scores 0, never 1."""
    total = sum(SEVERITY_WEIGHT[r.severity] for r in results)
    if total == 0:
        return 0.0
    return sum(SEVERITY_WEIGHT[r.severity] for r in results if r.passed) / total
