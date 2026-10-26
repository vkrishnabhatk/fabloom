# Fabloom

Describe a data center fabric in plain language. Fabloom has NVIDIA Nemotron on Nebius Token
Factory write the configs for Juniper Junos, Cisco NX-OS, IOS-XE or IOS-XR, Arista EOS, SONiC
or FRR, **verifies them with Batfish**, and repairs them until every check passes. If
something can't be fixed, it says so plainly.

Try it at [fabloom.dev](https://fabloom.dev). Output is always a proposal for a person to
review; Fabloom never connects to a device.

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
            repairer (Super, escalating to Ultra) ◄┘ until pass or budget/rounds exhausted
            explainer (Nano) ──► plain-language summary
```

Every step is an event in `runs/<run_id>/events.jsonl` ([ADR-0004](decisions/ADR-0004-run-event-log.md)).
The CLI's `--watch`, the web demo's live timeline and the benchmark all read that same file.

Each report states its verification level. **L2** (statically verified with Batfish) is
what this release targets. **L0** means nothing could be checked.

## Results in one line

Routed Nemotron passed 47 of 49 FRR and EOS fabrics, matching Ultra alone at about
two-thirds of its cost, and 25 of 25 on Junos, NX-OS, IOS-XE, IOS-XR and SONiC, where Ultra
alone passed 20. How that was measured, and how far to trust it, is in
[Benchmark](benchmark/index.md).

## Where to go next

- [Running Fabloom](running.md): install, CLI, benchmark, web demo, deployment
- [Benchmark](benchmark/index.md): what is measured, the results and their limits
- [Decisions](decisions/ADR-0001-verification-independence.md): the architecture decision records,
  including [more platforms](decisions/ADR-0009-more-platforms.md)
