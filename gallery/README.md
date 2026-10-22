# Gallery

Recorded runs that the web demo serves, including in read-only mode (no model key, or the
daily spend cap reached). Each entry is a complete run directory copied from `runs/`:
`events.jsonl`, `result.json`, `report.json` and `configs/`. The demo reads them exactly as
it reads a live run (ADR-0004), so a gallery run is never re-rendered or edited by hand.

To add one: run `fabloom generate "..."` against real Token Factory and Batfish, then copy
`runs/<run_id>/` here.
