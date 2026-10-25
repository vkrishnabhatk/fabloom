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
  that ran them. On the hosted demo they are deleted on a retention schedule (set in week 3).

## Deployment secrets

Planned, not yet in place: the Token Factory key, Tavily key and GHCR
token stored sops/age-encrypted in git, decrypted only at deploy time. Until then they live
only in a local, git-ignored `.env` and in GitHub environment secrets. Rotation: on any suspected exposure
immediately, otherwise every 90 days and after the judging window closes.

## Scope

Fabloom output is a **proposal for human review**, never pushed to devices. Passing Batfish
checks is strong evidence, not a guarantee; each report states its verification level.
