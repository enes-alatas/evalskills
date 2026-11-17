# ADR-0005: One shared GitHub identity, PR-only, Enes merges

Date: 2026-11-08 · Status: accepted (Enes, 2026-11-07, by direct message)

## Context
Agents (an author and a peer reviewer) do most of the work, and they use Enes's GitHub account. The process is
adapted from enes-alatas/spool and trimmed to this repository.

## Decision
- Every change lands by PR from a typed issue, with green CI. A peer agent reviews, and Enes reviews and merges last.
  The merge request comes from the reviewer, never the author.
- Rebase-merge only, with atomic Conventional Commits. Review fixes fold into the commit they fix.
- Because everyone shares one identity, GitHub cannot count an approval. Reviews are `--comment` reviews ending in
  "approved at `<sha>`", and Enes's merge is the approval. gh bodies are signed `— Name · Role` and contain no `@`.
- Dev-only tools gitleaks (pinned), ruff and pytest are approved. UI text uses no em dashes. Process skills are
  rewritten for this repo, not vendored.
- The rules themselves live in `docs/CONVENTIONS.md`.

## Consequences
- GitHub can't enforce branch protection on a private repo on the Free plan, so until the repo goes public or the
  plan changes, the conventions and CI are the only guard on `main`. The protection settings to apply then:
  require a PR and a green `checks` job, linear history, no force-push or deletion, admins included, 0 required
  reviews.

**Amendment (2026-11-20, #223): the protection is applied, now that the repository is public.** The repository went
public on 2026-11-20, and a ruleset named `main` was applied to the default branch, active, with no bypass actors:
- A PR is required, with 0 approvals, and rebase is the only merge method allowed.
- The `checks` and `security-paths` status checks are required. A branch needn't be up to date with `main` first.
- Deletion and force-pushes are blocked.

How it differs from the list above:
- **"Linear history":** there is no linear-history rule. Rebase-only merging keeps PR merges linear instead.
- **"A green `checks` job":** `security-paths` is required as well.
- **"Admins included":** the ruleset has no bypass actors, so it applies to everyone, admins included.
