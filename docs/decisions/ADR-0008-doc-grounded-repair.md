# ADR-0008: Doc-grounded repair is a benchmark arm first, a product feature only if it wins

## Status

Accepted — 2026-10-22. Built and tested offline only (scripted search, `httpx2.MockTransport`
for Tavily). No search has run against Tavily and the `super-repair-grounding` arm has no
result yet, so nothing below is measured.

## Context

When Batfish reports unrecognised lines, the model has usually invented a command or used
another vendor's syntax. The repair prompt already lists those lines, but the repairer then
has to recall the right syntax from memory, which is how the line was invented in the first
place. Architecture §8 proposes retrieving the vendor's own documentation instead, and ships
it only if the benchmark shows it helps.

## Decision

- **Where it sits.** `llm/grounding.py` turns a report's unrecognised lines into search
  queries and returns excerpts. `agent/steps.py` calls it before a repair and passes the
  excerpts to `repair_messages`. Grounding only adds prompt context. It never sees a check,
  so it stays on the model's side of the verification boundary (ADR-0001), and no import
  contract changes.
- **What is searched.** One query per distinct command shape: keywords are kept, while
  addresses, ASNs and interface numbers are dropped and the query is scrubbed. That
  deduplicates `neighbor 10.0.0.1 bfd` and `neighbor 10.0.0.3 bfd`, and keeps a user's
  addressing plan out of a third-party API. Search is restricted to the vendor's domains in
  `conf/fabloom.yaml` (`docs.frrouting.org`, `arista.com`). A platform without configured
  domains is not grounded, because forum answers would reach the prompt looking like vendor
  syntax.
- **Excerpts are data.** Web text goes into the prompt under a header that calls it reference
  material, not instructions, and each excerpt is capped (`max_excerpt_chars`).
- **Budget.** `RunBudget.max_grounding_queries` (default 6) caps the searches a run makes,
  enforced in `agent/budget.py` like every other limit. Running out skips grounding rather
  than stopping the run, because grounding helps the repairer but is not part of the loop.
  Results are cached on disk by (platform, query, domains), so the benchmark's repeated
  invented commands and resumed benches cost nothing more. Uncached searches are spaced by
  `min_interval_s` across the process. Failures are not cached.
- **Best-effort.** A search error gives no excerpts and an `error` on the `grounding_lookup`
  event. The run continues.
- **Measured before shipped.** `grounding.enabled` is false in `conf/fabloom.yaml`. Only the
  `super-repair-grounding` arm turns it on, and it differs from `super-repair` in nothing
  else, so the existing grounding gate (`min_gain` 0.05 final pass rate, `max_cost_ratio`
  1.5) measures grounding alone. The result is published either way. Tavily bills in search
  credits, not dollars, so the report lists searches per arm separately from `$/scenario`.

## Consequences

- The grounding arm needs `FABLOOM_TAVILY_API_KEY`. `fabloom bench run` refuses to start a
  matrix that includes it without a key; `fabloom generate` warns and repairs without it.
- Adding the arm raises the full matrix's worst case (sum of run caps) to $73.50 at the
  current $0.25 cap. The real spend is expected to be far lower, but the cap is the number
  that matters before the first run.
- The cost-ratio gate compares model spend only. If Tavily credits ever become the binding
  cost, the gate needs a search-cost input.
- The gate reads final pass rate, but grounding acts only on runs with unrecognised lines. On
  a benchmark where few first candidates have invented commands, a real effect can be too
  small to clear `min_gain`. That is a negative result to report, not a reason to lower the
  bar.
