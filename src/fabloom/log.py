"""structlog setup: JSON to stdout, every string value scrubbed before it is rendered.

Operational logs only. The run event log is a separate JSONL file per run
(``fabloom.agent.events``) because it must be replayable and servable.
"""

from __future__ import annotations

import logging
import sys

import structlog

from fabloom.scrub import structlog_processor


def _stderr_logger(*_args: object) -> structlog.PrintLogger:
    return structlog.PrintLogger(sys.stderr)


def configure_logging(level: str = "INFO", json: bool = True) -> None:
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer()
    )
    # JSON needs exceptions rendered into the event, or log.exception() emits only
    # "exc_info": true (the first containerised web run crashed with no traceback, 2026-10-23).
    # Before the scrubber, so a traceback that quotes a config is scrubbed too. The console
    # renderer formats exceptions itself and warns if this runs first.
    exc: list[structlog.types.Processor] = [structlog.processors.format_exc_info] if json else []
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            *exc,
            structlog_processor,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level)),
        # Resolve sys.stderr per logger, not once at configure time: a redirected or closed
        # stream (test runners, daemonised workers) must not break logging later.
        logger_factory=_stderr_logger,
        cache_logger_on_first_use=False,
    )
