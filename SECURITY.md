# Security policy

## Reporting a vulnerability

Report privately through
[GitHub security advisories](https://github.com/vkrishnabhatk/fabloom/security/advisories/new).
Please do not open a public issue. Expect an acknowledgement within 7 days.

## What Fabloom does with your input

- Every message is passed through `fabloom.scrub` **before it leaves the process** for
  Nebius Token Factory: enable secrets, user passwords, BGP/OSPF/IS-IS keys, SNMP
  communities, TACACS+/RADIUS keys, pre-shared keys, PEM blocks and high-entropy tokens are
  redacted. The same scrubber runs on operational logs.
- User input is **never used as training data**. Phase-2 training data comes only from
  benchmark scenarios, which are public and CC BY 4.0.
- Run directories (`runs/<run_id>/`) hold the request and generated configs on the machine
  that ran them. On the public demo at fabloom.dev, anyone with a run's link can open it, and
  runs are kept until the demo is shut down after judging. Do not enter confidential
  addressing or names there; run Fabloom yourself for that.

## Deployment secrets

API keys live only in a git-ignored `.env`: on a developer machine, and on the demo server
readable only by its service user, with a key per deployment so each can be revoked alone.
CI uses its own key in a GitHub environment restricted to `main`. Rotation: immediately on
any suspected exposure, otherwise every 90 days and after the judging window closes.
Encrypting deploy secrets in git (sops/age) is planned, not in place.

## Scope

Fabloom output is a **proposal for human review**, never pushed to devices. Passing Batfish
checks is strong evidence, not a guarantee; each report states its verification level.
