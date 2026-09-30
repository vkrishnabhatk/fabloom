# ADR-0004: The run is an explicit state machine over one append-only event log

## Status

Accepted — 2026-09-30

## Context

A run must be watchable live (web UI, CLI), replayable (benchmark into MLflow, the frozen
example gallery), diffable (nightly regression check) and testable offline. moodrift's
prediction log showed that one append-only log can be the observability backbone.

## Decision

- States and allowed transitions are a table (`agent/state_machine.py: TRANSITIONS`). Every
  transition is validated and appended as a typed `StateChanged` event. An invalid transition
  raises; it is never silently ignored.
- `runs/<run_id>/events.jsonl` holds one typed event per line: a Pydantic discriminated union
  in `contracts/run.py`. The run is the only writer. Readers tail the file with no
  cooperation from the writer (`agent/events.py: tail_events`).
- The CLI's `generate --watch` is just another tail reader, which exercises the web SSE path
  before the web app exists.
- The event log carries report *summaries*, never raw Batfish answers or secrets.
  `configs/`, `report.json` and `result.json` sit beside it, so a run directory is
  self-contained.
- `RunBudget` is enforced in exactly one place (`agent/budget.py`), checked before every model
  call (projected at the model's max output length) and every verifier execution (reserved
  up front, so parallel candidates cannot overshoot together).
- A budget stop ends in `EXHAUSTED`, not `FAILED`, and is explained deterministically, with no
  further spend. The repair loop keeps the best-scoring candidate ever seen, so a worse repair
  never replaces a better one.

## Alternatives considered

- **A message queue (Celery/RQ).** Nothing here is background-job shaped; the loop is async
  and single-writer.
- **A database table of events.** Solves a multi-writer problem that doesn't exist, and makes
  the frozen gallery harder to ship as plain files.
