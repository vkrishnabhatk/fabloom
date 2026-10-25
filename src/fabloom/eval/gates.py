"""Benchmark gates: one module, one rule, consumed by both the report and CI so they can
never disagree (ADR-0007).

- **Regression**: did a prompt, routing or hosted-model change make an arm worse than the
  frozen baseline? Compared only on the scenarios both runs share, so a 10-scenario nightly
  is measured against the same 10 scenarios in the baseline, not against the full set.
- **Grounding**: does Tavily doc-grounding ship? Only if its arm beats its control by a
  margin, at a bounded cost.

A gate whose inputs are missing is *skipped* and says why; it is never silently passed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from fabloom.config import GatesSpec, GroundingGateSpec
from fabloom.eval.results import BenchResults, metric_value

# Rates are fractions of small integers; a drop of exactly max_drop must not fail on rounding.
EPS = 1e-9
# "Drop" only means something for a metric where higher is better.
REGRESSION_METRICS = frozenset({"final_pass_rate", "first_pass_rate", "mean_final_score"})


@dataclass(frozen=True)
class Gate:
    label: str
    passed: bool | None  # None: skipped, see detail
    value: float | None
    threshold: float
    comparison: Literal[">=", "<="]
    detail: str

    @property
    def status(self) -> Literal["passed", "failed", "skipped"]:
        if self.passed is None:
            return "skipped"
        return "passed" if self.passed else "failed"


def _check(value: float, threshold: float, comparison: Literal[">=", "<="]) -> bool:
    return value + EPS >= threshold if comparison == ">=" else value - EPS <= threshold


def _regression(
    runs: BenchResults, champion: BenchResults | None, arm: str, metric: str, max_drop: float
) -> Gate:
    def gate(passed: bool | None, value: float | None, detail: str) -> Gate:
        return Gate(f"no regression: {arm} {metric}", passed, value, -max_drop, ">=", detail)

    if champion is None:
        return gate(None, None, "no frozen baseline")
    if arm not in champion.arm_names:
        return gate(None, None, f"arm not in baseline {champion.meta.bench_id}")
    shared = runs.scenario_ids(arm) & champion.scenario_ids(arm)
    if not shared:
        return gate(None, None, "no scenarios shared with the baseline")
    now = metric_value(runs.summary(arm, shared), metric)
    then = metric_value(champion.summary(arm, shared), metric)
    if now is None or then is None:
        return gate(None, None, f"{metric} missing")
    delta = now - then
    detail = f"{now:.3f} vs baseline {then:.3f} on {len(shared)} shared scenarios"
    return gate(_check(delta, -max_drop, ">="), delta, detail)


def _grounding(runs: BenchResults, spec: GroundingGateSpec) -> list[Gate]:
    lift = f"grounding lifts final pass rate ({spec.arm} vs {spec.control})"
    cost = f"grounding cost ratio ({spec.arm} / {spec.control})"
    missing = [a for a in (spec.arm, spec.control) if a not in runs.arm_names]
    if missing:
        why = f"arm not run: {', '.join(missing)}"
        return [
            Gate(lift, None, None, spec.min_gain, ">=", why),
            Gate(cost, None, None, spec.max_cost_ratio, "<=", why),
        ]
    shared = runs.scenario_ids(spec.arm) & runs.scenario_ids(spec.control)
    treated, control = runs.summary(spec.arm, shared), runs.summary(spec.control, shared)
    gates: list[Gate] = []
    if treated.final_pass_rate is None or control.final_pass_rate is None:
        gates.append(Gate(lift, None, None, spec.min_gain, ">=", "no shared scenarios"))
    else:
        gain = treated.final_pass_rate - control.final_pass_rate
        detail = f"{treated.final_pass_rate:.3f} vs {control.final_pass_rate:.3f}"
        ok = _check(gain, spec.min_gain, ">=")
        gates.append(Gate(lift, ok, gain, spec.min_gain, ">=", detail))
    if not treated.usd_per_scenario or not control.usd_per_scenario:
        gates.append(Gate(cost, None, None, spec.max_cost_ratio, "<=", "no cost recorded"))
    else:
        ratio = treated.usd_per_scenario / control.usd_per_scenario
        detail = f"${treated.usd_per_scenario:.4f} vs ${control.usd_per_scenario:.4f} per scenario"
        ok = _check(ratio, spec.max_cost_ratio, "<=")
        gates.append(Gate(cost, ok, ratio, spec.max_cost_ratio, "<=", detail))
    return gates


def evaluate(runs: BenchResults, champion: BenchResults | None, spec: GatesSpec) -> list[Gate]:
    """``runs`` is the bench under test, ``champion`` the frozen baseline (if any)."""
    # Checked up front: a typo in conf/bench.yaml must fail, not skip a gate forever.
    bad = sorted(set(spec.regression.metrics) - REGRESSION_METRICS)
    if bad:
        raise ValueError(f"regression gate metrics {bad} not in {sorted(REGRESSION_METRICS)}")
    gates = [
        _regression(runs, champion, arm, metric, spec.regression.max_drop)
        for arm in runs.arm_names
        for metric in spec.regression.metrics
    ]
    return gates + _grounding(runs, spec.grounding)


def all_passed(gates: Sequence[Gate]) -> bool:
    """True only if at least one gate was evaluated and none failed. A run where every gate
    was skipped proves nothing, so it does not pass."""
    evaluated = [g for g in gates if g.passed is not None]
    return bool(evaluated) and all(g.passed for g in evaluated)
