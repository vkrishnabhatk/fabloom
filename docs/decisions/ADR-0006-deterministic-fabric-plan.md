# ADR-0006: Addressing is decided deterministically, and benchmarks grade against ground truth

## Status

Accepted — 2026-09-30

## Context

A check such as "leaf3 has a route to leaf1's loopback" needs to know leaf1's loopback. If
the model chose the addresses, the checks would have to be derived from model output, and
the model would influence what it is graded on (ADR-0001).

## Decision

- The intent is vendor-neutral and address-free (topology, BGP mode, ASN plan, pools,
  filters). `checks/allocate.py` turns it into a `FabricPlan`: every device, interface,
  address and session. The allocator is deterministic, independent of device order, never
  renumbers existing devices when the fabric grows, and never produces overlaps (all
  property-tested with hypothesis).
- The generator is *told* the plan and must follow it. The checks are compiled from the same
  plan.
- Benchmark scenarios pin a ground-truth `intent`. Arms that isolate generation and repair
  pass it directly (`RunRequest.intent`), skipping the planner. Planner accuracy is measured
  separately against the same ground truth.
- For FRR the snapshot builder declares which ports exist (cabling), but deliberately
  writes **no addresses** outside `frr.conf`. Addressing stays the model's job, so a wrong
  address still fails verification.
- **FRR traffic filters are reported as unverifiable.** `frr.conf` has no packet filter, and
  SSH filtering on an FRR box lives in iptables/nftables, which Batfish's Cumulus/FRR model
  does not read. Filter scenarios therefore use Arista EOS. The report lists unverifiable
  intent explicitly instead of dropping it. This refines the plan's example scenario (FRR +
  SSH filter) and is to be confirmed against a live Batfish in the week-0 spike.

## Alternatives considered

- **Let the model pick addresses, derive checks from its configs.** Circular grading.
- **Render FRR addresses into `/etc/network/interfaces` from the plan.** It would make the
  snapshot "correct" regardless of what the model wrote, which silently does the model's
  homework for it.
