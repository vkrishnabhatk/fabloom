"""Agent plane: the run state machine, its budget and its event log.

Boundary rule: never imports ``fabloom.verify``. The agent is handed a ``Verifier`` (a
``contracts`` protocol) by the composition root and cannot reach the grading code itself.
"""

from fabloom.agent.budget import BudgetExceededError, BudgetTracker
from fabloom.agent.events import EventLog, read_events, tail_events
from fabloom.agent.state_machine import (
    TRANSITIONS,
    InvalidTransitionError,
    RunMachine,
    RunRequest,
    RunResult,
)

__all__ = [
    "TRANSITIONS",
    "BudgetExceededError",
    "BudgetTracker",
    "EventLog",
    "InvalidTransitionError",
    "RunMachine",
    "RunRequest",
    "RunResult",
    "read_events",
    "tail_events",
]
