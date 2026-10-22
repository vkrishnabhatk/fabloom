# Fabloom

Describe a data center network in plain language. Fabloom has NVIDIA Nemotron on Nebius
Token Factory write the configs, **verifies them with Batfish**, and repairs them until every
check passes. If something can't be fixed, it says so plainly.

!!! note "Status"
    Pre-alpha. No benchmark numbers yet: everything below is built and tested offline, with
    scripted models and verifiers. Output is always a proposal for a human to review, never
    pushed to devices.

## The one rule

**The model cannot influence what it is graded on.** Checks are compiled deterministically
from the intent. The verifier and the model side cannot import each other, and import-linter
enforces this in CI ([ADR-0001](decisions/ADR-0001-verification-independence.md)).

## How a run works

```
request ──► planner (Nemotron Nano) ──► intent JSON
            deterministic allocator  ──► fabric plan (addresses, ASNs, sessions)
            deterministic compiler   ──► checks
            generator (Nemotron Super) ──► per-device configs
            pre-lint gate ──► Batfish ──► report ──┐
            [doc grounding: vendor docs for unrecognised lines]
            repairer (Super, escalating to Ultra) ◄┘ until pass or budget/rounds exhausted
            explainer (Nano) ──► plain-language summary
```

Every step is an event in `runs/<run_id>/events.jsonl` ([ADR-0004](decisions/ADR-0004-run-event-log.md)).
The CLI's `--watch`, the web demo's live log and the benchmark all read that same file.

Each report states its verification level. **L2** (statically verified with Batfish) is
what this release targets. **L0** means nothing could be checked.

## Where to go next

- [Running Fabloom](running.md): CLI, web demo, benchmark, configuration
- [Benchmark](benchmark/index.md): what is measured and how
- [Decisions](decisions/ADR-0001-verification-independence.md): the architecture decision records
