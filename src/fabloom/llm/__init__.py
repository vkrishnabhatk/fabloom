"""LLM plane: the Token Factory client, model routing, cost accounting and prompts.

Boundary rule: may use ``contracts``, ``checks``, ``config`` and ``scrub``; never ``verify``.
The model proposes; it has no path to the code that grades the proposal.
"""

from fabloom.llm.client import (
    ChatClient,
    Completion,
    LlmError,
    LlmOutputError,
    LlmRefusalError,
    TokenFactoryClient,
)
from fabloom.llm.router import ModelRouter

__all__ = [
    "ChatClient",
    "Completion",
    "LlmError",
    "LlmOutputError",
    "LlmRefusalError",
    "ModelRouter",
    "TokenFactoryClient",
]
