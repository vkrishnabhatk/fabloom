"""Role -> model routing, declared in ``conf/models.yaml`` and never hardcoded (ADR-0005)."""

from __future__ import annotations

from collections.abc import Sequence

from fabloom.config import ModelsConfig, ModelSpec
from fabloom.contracts import CheckResult, ModelRole, Severity


class ModelRouter:
    def __init__(self, config: ModelsConfig) -> None:
        missing = [r for r in ModelRole if r not in config.roles]
        if missing:
            raise ValueError(f"conf/models.yaml has no model for roles: {missing}")
        unknown = {m for m in config.roles.values() if m not in config.models}
        if unknown:
            raise ValueError(f"conf/models.yaml roles reference undefined models: {unknown}")
        self.config = config

    def spec(self, role: ModelRole) -> ModelSpec:
        return self.config.for_role(role)

    def repairer(self, round_: int, failed: Sequence[CheckResult]) -> ModelRole:
        """Escalate only on repeated failure: from a fixed round, or earlier if a critical
        check (a parse failure) is still failing after a repair has already been tried."""
        rule = self.config.escalation
        critical = any(c.severity is Severity.CRITICAL for c in failed)
        if round_ >= rule.from_round or (critical and round_ >= rule.critical_from_round):
            return ModelRole.REPAIRER_ESCALATED
        return ModelRole.REPAIRER
