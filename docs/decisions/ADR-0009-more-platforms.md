# ADR-0009: More platforms, only where Batfish can verify them

## Status

Accepted — 2026-10-26.

## Context

v0.1 wrote FRR and Arista EOS. Most data center fabrics run on Juniper, Cisco or SONiC, and
a live request for "Juniper QFX running Junos" was silently planned as FRR, generated, and
reported as passed (run `20261026T000530Z-a279de`). That is worse than refusing: the user
asked for one thing and got another thing marked verified.

Fabloom's claim is that a config is checked before anyone trusts it, so a platform is only
worth adding if Batfish parses it and models its BGP, routes and filters.

## Decision

- **Add Junos, Cisco NX-OS, IOS-XE, IOS-XR and SONiC.** Before writing code, a hand-written
  2-spine/2-leaf numbered eBGP fabric for each was run through live Batfish 2026.08.27: every
  one parsed cleanly, established 8/8 sessions and carried the loopback routes.
- **Refuse what Batfish cannot read.** Nokia (SR Linux, SR OS), Huawei VRP, Cumulus NVUE
  and a few others are not parsed by Batfish. A request naming one is refused before the
  rate limiter and any model call, with the list of supported platforms
  (`contracts/platforms.py: unsupported_vendor`). Cumulus Linux's FRR files are what the
  FRR platform already writes; NVUE is not.
- **One place for what differs.** `contracts/platforms.py` holds, per platform: port names,
  how Batfish names interfaces, snapshot packaging, whether filters and BGP unnumbered are
  verifiable. Allocation, check compilation, the snapshot writer, the prompts and the web
  pages read it. Adding a platform is an entry there, generator rules and a reference
  renderer.
- **Packaging per platform.** Junos and the Cisco dialects are one file per device with a
  RANCID content-type first line, so Batfish's detection cannot confuse IOS, NX-OS and
  IOS-XR. SONiC is two files per device (`config_db.json`, `frr.conf`) in
  `sonic_configs/<device>/`; the model writes them as two `<file>` sections in the device
  block, and the web download keeps them as two files.
- **What is not offered.** BGP unnumbered stays FRR-only (the intent validator refuses it
  elsewhere). SONiC ACLs live in `config_db.json` tables that are not verified here, so a
  SONiC filter is reported as not verifiable, like an FRR one.
- **A separate scenario set.** `benchmark/vendors-v0.1/` holds 5 scenarios per new platform.
  The frozen v0.1 benchmark stays FRR and EOS, so its baseline stays comparable.

## Checking the checker

`scripts/check_references.py` runs every scenario's known-good reference config through
live Batfish; nothing is generated. All 25 vendor scenarios and all 49 v0.1 scenarios pass.
Getting there found two things, both in the checker rather than the configs:

- **Junos needs `local-address`.** Without it Batfish reports the sessions as `NO_LOCAL_IP`
  in its compatibility answer although they establish; real Junos takes the outgoing
  interface's address. The reference config and the generator rules always set it.
- **Filter checks now ignore non-initial fragments.** Batfish answered NX-OS's "is SSH
  from outside dropped?" with a TCP SYN at fragment offset 1. Such a packet carries no
  ports, so no port-based deny can match it on any vendor, and it cannot open a
  connection. The check now asks about `fragmentOffsets: 0` only. EOS and FRR results are
  unchanged.

## Consequences

- Prompts changed (planner platform list, new rules), so `prompt_hash` changes: benchmark
  runs after this commit are a new prompt version, not a resume of v0.1.
- Each new platform's model quality is measured on `benchmark/vendors-v0.1/`, reported per
  platform, including weak results.
