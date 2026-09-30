"""Deterministic run summary: the explainer's fallback, and the ground truth it must not
contradict. Used whenever the budget does not allow a model call."""

from __future__ import annotations

from fabloom.contracts import CheckSet, VerificationReport

MAX_LISTED = 5

_LEVEL = {
    "L0": "not verified (advisory only)",
    "L1": "rule checked",
    "L2": "statically verified with Batfish",
    "L3": "runtime verified",
}


def deterministic_explanation(
    outcome: str,
    report: VerificationReport | None,
    checkset: CheckSet | None,
    repair_rounds: int,
    stop_reason: str | None = None,
) -> str:
    if report is None:
        base = "No configuration was verified"
        return f"{base}: {stop_reason}." if stop_reason else f"{base}."
    total, failed = len(report.checks), len(report.failed_checks)
    rounds = f"after {repair_rounds} repair round{'s' if repair_rounds != 1 else ''}"
    parts = [f"Level {report.level}, {_LEVEL[report.level]}."]
    if outcome == "passed":
        parts.append(f"All {total} checks passed {rounds}.")
    else:
        parts.append(f"{total - failed} of {total} checks passed {rounds}; {failed} still fail.")
        parts.append(
            "Failing: "
            + ", ".join(c.id for c in report.failed_checks[:MAX_LISTED])
            + (f" and {failed - MAX_LISTED} more." if failed > MAX_LISTED else ".")
        )
    if report.unrecognized_lines:
        parts.append(f"{len(report.unrecognized_lines)} lines were not recognised by the parser.")
    if stop_reason:
        parts.append(f"Stopped: {stop_reason}.")
    if checkset and checkset.unverifiable:
        parts.append("Not verifiable: " + "; ".join(checkset.unverifiable) + ".")
    return " ".join(parts)
