"""Scrub plane: secret redaction.

Runs on the ingress path (every message, before it leaves the process for Token Factory) and
on the logging path (a structlog processor). Depends on nothing else in fabloom.
"""

from fabloom.scrub.redact import REDACTED, ScrubResult, scrub, structlog_processor

__all__ = ["REDACTED", "ScrubResult", "scrub", "structlog_processor"]
