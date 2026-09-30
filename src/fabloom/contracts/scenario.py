"""Benchmark scenario: a natural-language request plus the ground-truth intent it means."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fabloom.contracts.intent import Intent

_ID = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*-\d{4}$")


class Scenario(BaseModel):
    """``benchmark/v0.1/<id>.yaml``.

    Checks are compiled from ``intent`` (the ground truth), not from what the planner model
    makes of ``request``. Planner accuracy is measured separately against ``intent``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    family: str
    request: str = Field(min_length=10)
    intent: Intent
    notes: str = ""

    @model_validator(mode="after")
    def _id_shape(self) -> Scenario:
        if not _ID.match(self.id):
            raise ValueError(f"scenario id {self.id!r} must look like 'family-name-0001'")
        return self
