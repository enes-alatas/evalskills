---
name: credential-incident
description: >-
  Use the moment a live credential is in front of you (in a log, a report, an issue or PR body, a transcript, a
  screenshot, a submitted skill's files) or one has been pushed or posted to GitHub.
metadata:
  author: evalskills maintainers
---

# Credential incident

A live credential in front of you is an incident. The rule in `docs/CONVENTIONS.md` binds you whether or not this
skill is loaded. This file is the procedure.

1. **Stop.** Don't copy the value anywhere: not into a message, a note, a commit, an issue or a test.
2. **Contain what you can reach.**
   - In your working tree: delete it, then check that `scripts/secret-scan.sh --staged` passes.
   - In an unpushed commit: rewrite the commit, then rerun `scripts/secret-scan.sh`.
   - In a pushed commit, issue, PR or comment: edit the value out. Edit history and old commits still hold it, so
     removal is not enough, and Enes must rotate it.
3. **Tell Enes privately** (owner DM): what kind of credential, where it appeared (file, commit, URL), and when.
   Never include the value or steps to reproduce it. The shared chat may hear *that* an exposure happened and
   where, nothing more.
4. **Enes rotates.** Only the owner of the credential can, and rotation is not optional once the value was
   published.
5. **File the fix** (for example, a missing redaction or a log line that printed a header) as a normal issue,
   with the value redacted to at most 8 characters + `…`. The fix is ordinary public work; the exposure stays
   private.

## Third-party skills
Submitted repositories often contain credential *shapes*: examples, test fixtures, or the subject of a
security-scanner skill. A third-party value is not our incident unless it looks live (a real-looking token in
non-example code, a recent commit, a matching account). Even then, never republish it: public reports are
sanitized (`report.public.json`), and raw reports stay in the gitignored `reports/`. If you think a third-party
secret is live, tell Enes privately; don't contact the repo owner yourself.
