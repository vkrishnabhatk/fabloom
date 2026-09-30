"""Run budget, run results, and the typed events that make up ``runs/<run_id>/events.jsonl``.

One event log, five consumers: web SSE, ``fabloom generate --watch``, the benchmark runner,
the nightly regression check, and the frozen example gallery (ADR-0004).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from fabloom.contracts.enums import ModelRole, Platform, RunState


class RunBudget(BaseModel):
    """Hard limits for one run. Enforced in exactly one place: ``fabloom.agent.budget``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_repair_rounds: int = Field(default=3, ge=0)
    max_tokens: int = Field(default=200_000, ge=1)
    max_usd: float = Field(default=0.10, gt=0)
    max_wall_clock_s: float = Field(default=600.0, gt=0)
    max_verifications: int = Field(default=12, ge=1)  # sandbox executions, or local verifies


class Usage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    llm_calls: int = 0
    verifications: int = 0
    verification_cache_hits: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class RunEventBase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int
    ts: datetime
    run_id: str
    state: RunState


class RunStarted(RunEventBase):
    type: Literal["run_started"] = "run_started"
    request: str
    platform: Platform | None
    budget: RunBudget
    verifier: str


class StateChanged(RunEventBase):
    type: Literal["state_changed"] = "state_changed"
    from_state: RunState
    to_state: RunState
    reason: str = ""


class IntentPlanned(RunEventBase):
    type: Literal["intent_planned"] = "intent_planned"
    source: Literal["planner", "provided"]
    intent: dict[str, Any]


class ChecksCompiled(RunEventBase):
    type: Literal["checks_compiled"] = "checks_compiled"
    count: int
    unverifiable: list[str]


class LlmCall(RunEventBase):
    type: Literal["llm_call"] = "llm_call"
    role: ModelRole
    model: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    duration_ms: int
    redactions: int


class CandidateGenerated(RunEventBase):
    type: Literal["candidate_generated"] = "candidate_generated"
    round: int
    candidate: int
    content_key: str
    devices: list[str]


class VerificationCompleted(RunEventBase):
    type: Literal["verification_completed"] = "verification_completed"
    round: int
    candidate: int
    content_key: str
    cached: bool
    summary: dict[str, Any]


class BudgetExhausted(RunEventBase):
    type: Literal["budget_exhausted"] = "budget_exhausted"
    limit: str
    detail: str


class ExplanationReady(RunEventBase):
    type: Literal["explanation_ready"] = "explanation_ready"
    text: str
    source: Literal["model", "deterministic"]


class RunFinished(RunEventBase):
    type: Literal["run_finished"] = "run_finished"
    outcome: Literal["passed", "exhausted", "failed"]
    score: float
    repair_rounds: int
    usage: Usage


class RunError(RunEventBase):
    type: Literal["run_error"] = "run_error"
    error_type: str
    message: str


RunEvent = Annotated[
    RunStarted
    | StateChanged
    | IntentPlanned
    | ChecksCompiled
    | LlmCall
    | CandidateGenerated
    | VerificationCompleted
    | BudgetExhausted
    | ExplanationReady
    | RunFinished
    | RunError,
    Field(discriminator="type"),
]

RUN_EVENT_ADAPTER: TypeAdapter[RunEvent] = TypeAdapter(RunEvent)
