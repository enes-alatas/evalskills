---
name: security-review
description: >-
  Use when reviewing an evalskills pull request for security: one labelled needs-security-review, touching a path
  in .github/security-review-paths, changing a workflow, the sandbox, the gateway, limits or what is published.
metadata:
  author: evalskills maintainers
---

# Security pass on a pull request

Quality review belongs to `review-pr`. This pass answers only: what does this change open, and does it hold? The
system's zones are in `docs/adr/0002-three-trust-zones.md`, the sandbox in ADR-0003 and secrets in ADR-0004.

## 1. Name the boundary, then answer its question on the PR
- **Public → zone A (app):** does untrusted input (a URL, a form field, a repo's files, an evaluator report) reach
  a parser, a shell, a path, HTML or SQL in a new way? Are rate, size, queue and time limits still enforced
  server-side?
- **Zone A → anything:** the app holds no secrets and never executes skill code. Does it now?
- **Zone B (orchestrator) → zone C (trials):** trials get only a per-run gateway token. Do they get anything else:
  a real key, a host mount, the Docker socket, a new egress destination?
- **Zone C → out:** egress goes only to the gateway. Does a new image, network or DNS rule widen it?
- **Budget:** are the per-trial, per-day and total spend caps still enforced before spending, failing closed?
- **Publication:** does anything raw (matched lines, file contents, env, paths) reach the public catalog or API
  without going through the sanitizer?

## 2. Secrets
- No secret in code, CI, images, logs, API responses or fixtures. Fixtures are visibly synthetic.
- A new secret is a host-side file mounted into one container. It is never inherited by a child process that runs
  untrusted code.
- A live credential anywhere in the PR, its commits, its CI logs or its evidence is an incident:
  `.claude/skills/credential-incident/`.

## 3. CI and dependencies
- Actions pinned by full SHA; minimal `permissions:` per job; no `pull_request_target`; no secrets.
- A workflow with a write token runs scripts from the base branch, not the PR.
- New dependency: why it's needed, its maintenance status, pinned version and checksum. Runtime dependencies need
  an ADR.

## 4. The record
- A change to the security posture is Enes's decision, recorded in an ADR. Until then the PR carries an
  `Operator decision pending:` line.
- The author's Notes / Risks says what the change opens and why that holds. "None" alone is a finding.

## 5. Verdict
Comment at the current head, signed `— <name> · Security reviewer`: the boundaries checked, findings by severity,
and "nothing from me" or "changes needed". A new push needs a re-check of the interdiff.
