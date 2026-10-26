# ADR-0003: Sandboxes ladder built on a filesystem artifact (JVM AOT cache)

## Status

Accepted — 2026-09-30. **Deferred** — 2026-10-26: v0.1 and v0.2 verify with the local
Batfish backend only; the Sandboxes backend is not built and its measurements were never
taken (`spikes/batfish_in_sandbox.py` is ready). The verifier image and its AOT training jobs
still cover FRR and EOS only. Revisit after judging (README roadmap).

## Context

The original design proposed starting Batfish once in a Nebius Sandbox and forking the *running*
server per repair candidate. ConTree checkpoints capture the filesystem, not process state,
so a warm JVM cannot be checkpointed. Batfish's JVM start-up cost has to be paid per
execution, or moved into a file.

Facts read from `batfish/allinone:2026.08.27.3685` (the registry image config and
`wrapper.sh`, 2026-09-30):

| Fact | Value |
|---|---|
| JVM | OpenJDK **25** (`/usr/lib/jvm/java-25-openjdk-amd64`) |
| Entry point | `./wrapper.sh`: Batfish (`org.batfish.allinone.Main`) plus Jupyter |
| Batfish flags | `-XX:-UseCompressedOops -XX:+UseContainerSupport -XX:MaxRAMPercentage=80` |
| Ports | 9996 (Batfish), 8888 (Jupyter) |
| pybatfish | bundled in the image, version-matched to the server |

Still unmeasured (spike): sandbox RAM for a 6–12 device snapshot, start time with and
without the cache, and cost per execution.

## Decision

Four layers:

| Layer | What | Paid |
|---|---|---|
| L0 image | `fabloom-verifier` = Batfish image pinned by digest + `fabloom-verify` + baked AOT cache | per release |
| L1 warm checkpoint | `images.use(...)`, one retained warm-up run; UUID cached by image tag | per deploy |
| L2 scenario checkpoint | snapshot skeleton (topology, `layer1_topology.json`) in one retained run | per scenario |
| L3 candidate | `fabloom-verify --job ...`: start Batfish from the cache, run checks, print one JSON line | per candidate |

On JDK 25 the architecture's "AppCDS archive" becomes a **JEP 514 AOT cache**
(`-XX:AOTCacheOutput` at build, `-XX:AOTCache` at run). It is the successor to AppCDS
dynamic archives and also caches linked classes. It is still a single file, so the reason
for choosing it is unchanged. `docker/verifier/bake-aot.sh` trains it by replaying the
committed training jobs, the exact questions Fabloom asks. `BAKE_AOT=0` builds without it
if the spike finds a problem.

One candidate per execution keeps executions content-addressable. Identical repair
candidates collide on checkpoint UUID and reuse the result; the in-run cache keyed by
`content_key(configs)` does the same locally.

`batfish-local` (same `Verifier` interface, local container) ships from day one. The
sandbox is an accelerator and a track-fit story, never a single point of failure.

## Alternatives considered

- **Keep a long-lived Batfish VM and send candidates to it.** Works, but it is not
  Sandboxes, and it loses per-candidate isolation and checkpoint dedup.
- **CRIU checkpoint/restore of the JVM.** Needs kernel support inside the microVM that is not
  documented; far riskier than a file the JVM already knows how to load.
- **AppCDS (`-XX:ArchiveClassesAtExit`).** Also works on JDK 25; the AOT cache is strictly
  more capable on the same JVM. Fall back to it if the spike shows AOT-cache problems.
