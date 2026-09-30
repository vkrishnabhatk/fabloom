"""Replay and record Batfish answers, so real report shapes can be tested with no JVM.

``RecordingAnswerer`` wraps a live answerer and captures every question it is asked;
``RecordedAnswerer`` replays such a capture and fails loudly on any question it has not seen,
so a change in which questions a check asks cannot silently pass against stale fixtures.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fabloom.verify.batfish_questions import Question, Row

if TYPE_CHECKING:
    from fabloom.verify.batfish_local import SnapshotAnswerer


class MissingAnswerError(KeyError):
    pass


class RecordedAnswerer:
    def __init__(self, answers: dict[str, list[Row]], batfish_version: str | None = None) -> None:
        self.answers = answers
        self._version = batfish_version
        self.asked: list[str] = []

    @classmethod
    def from_file(cls, path: Path) -> RecordedAnswerer:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return cls(data["answers"], data.get("batfish_version"))

    def init_snapshot(self, path: Path, name: str) -> None:
        return None

    def batfish_version(self) -> str | None:
        return self._version

    def ask(self, question: Question) -> list[Row]:
        self.asked.append(question.key)
        if question.key not in self.answers:
            raise MissingAnswerError(f"no recorded answer for {question.key}")
        return self.answers[question.key]


class RecordingAnswerer:
    """Wraps a live answerer; snapshot calls pass straight through."""

    def __init__(self, inner: SnapshotAnswerer) -> None:
        self.inner = inner
        self.answers: dict[str, list[Row]] = {}

    def init_snapshot(self, path: Path, name: str) -> None:
        self.inner.init_snapshot(path, name)

    def batfish_version(self) -> str | None:
        return self.inner.batfish_version()

    def ask(self, question: Question) -> list[Row]:
        rows = self.inner.ask(question)
        self.answers[question.key] = rows
        return rows

    def dump(self, path: Path, *, batfish_version: str | None, provenance: str) -> None:
        payload = {
            "_provenance": provenance,
            "batfish_version": batfish_version,
            "answers": self.answers,
        }
        path.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n", encoding="utf-8")
