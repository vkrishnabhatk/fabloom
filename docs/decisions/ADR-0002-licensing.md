# ADR-0002: Apache-2.0 for code, CC BY 4.0 for benchmark and dataset

## Status

Accepted — 2026-09-30

## Context

The repository was created with an MIT licence. The hackathon requires an open-source
licence visible on the repo page. Fabloom is also meant as a long-term project that
corporate network teams can adopt and contribute to, and a benchmark others can reuse and
cite.

## Decision

- Code: Apache-2.0 (`LICENSE`, `NOTICE`). Its explicit patent grant matters to the corporate
  contributors Fabloom wants.
- Benchmark scenarios and, in phase 2, the dataset: CC BY 4.0 (`benchmark/LICENSE`).
  Attribution-only keeps them usable for research and commercial evaluation alike.
- Vendor images (Arista cEOS and similar) are never redistributed.
- CI fails if a runtime dependency carries a GPL-family licence (`pip-licenses`,
  `security` job). Dev-only tools are exempt (codespell is GPL-2.0 and is never shipped).
  Checked on 2026-09-30: no runtime dependency matched.
- Contributions are accepted under the DCO (`git commit -s`), not a CLA.

## Alternatives considered

- **Keep MIT.** Permissive, but no patent grant.
- **CC BY-SA for data.** Share-alike would stop the benchmark being mixed into proprietary
  evaluation suites, which is exactly where it would do the most good.
