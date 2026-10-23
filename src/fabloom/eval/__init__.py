"""Eval plane: the benchmark runner, MLflow tracking, gates and the generated report.

Boundary rule: never imports ``fabloom.verify``. Like the agent, the benchmark is handed a
``Verifier`` by the composition root, so the code that measures the model cannot reach into
the code that grades it. Nothing below this plane imports it.
"""

from fabloom.eval.gates import Gate, all_passed, evaluate
from fabloom.eval.report import render, write_report
from fabloom.eval.results import ArmSummary, BenchMeta, BenchResults, RunRecord, summarize
from fabloom.eval.runner import (
    Arm,
    BenchAbortedError,
    BenchMismatchError,
    BenchRunner,
    collect,
    resolve_arms,
    select_scenarios,
    worst_case_usd,
)
from fabloom.eval.tracking import MlflowTracker, NullTracker, Tracker

__all__ = [
    "Arm",
    "ArmSummary",
    "BenchAbortedError",
    "BenchMeta",
    "BenchMismatchError",
    "BenchResults",
    "BenchRunner",
    "Gate",
    "MlflowTracker",
    "NullTracker",
    "RunRecord",
    "Tracker",
    "all_passed",
    "collect",
    "evaluate",
    "render",
    "resolve_arms",
    "select_scenarios",
    "summarize",
    "worst_case_usd",
    "write_report",
]
