---
name: review-fix
description: >-
  Use before acting on PR review comments, or when asked to address review feedback or clean up a PR's commit
  story.
metadata:
  author: evalskills maintainers
---

# Folding review feedback into the original commits

The rule (`docs/CONVENTIONS.md`): review fixes fold into the commit that introduced the problem. There are no
"address review" commits; only genuinely new scope gets a commit of its own. This is the procedure.

1. **Read the whole review:** the summary (`gh pr view <n> --comments`), the inline comments
   (`gh api repos/{owner}/{repo}/pulls/<n>/comments`) and the PR-level comments.
2. **Check that each comment is right** before acting. Answer a wrong one on the PR with your reasoning, and give a
   question a real answer. If a comment's intent is ambiguous, ask.
3. **Map each fix to its origin commit:** `base=$(git merge-base origin/main HEAD)`, then
   `git log --oneline $base..HEAD -- <path>` or `git log -S '<symbol>' $base..HEAD`. Sweep the branch for other
   instances of the same mistake.
4. **Make each fix as a fixup:** edit, `git add <paths>`, `git commit --fixup=<origin-sha>`. New-scope commits go
   on top as ordinary commits. Run the definition-of-done checks once, then take a snapshot:
   `git branch backup/<branch>`.
5. **Fold:** `git rebase --autosquash $base` on git ≥ 2.44; on older git, `GIT_SEQUENCE_EDITOR=true git rebase -i
   --autosquash $base` (the todo list is accepted unedited, so nothing is interactive). On a conflict, resolve towards what
   *that* commit should contain, then `GIT_EDITOR=true git rebase --continue`.
6. **Prove it:** `git diff backup/<branch> --stat` must be empty, and `git log --oneline $base..HEAD` must show no
   `fixup!` subjects.
7. **Push to the same PR:** `git push --force-with-lease`. Then re-ping the reviewer (never Enes) with the new head
   SHA and a list of what was folded where.

Never rename a PR's branch, because GitHub closes the PR. Never use a bare `--force`.
