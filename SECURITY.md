# Security policy

evalskills fetches repositories that anyone submits and runs a static scanner over them. An internal live pilot,
run only with the operator's approval, will run skills inside a gVisor sandbox. A weakness here can reach the host
that does that work, so reports are welcome and taken seriously. `docs/ARCHITECTURE.md` describes the trust zones a report can test.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting: the repository's **Security** tab, then **Report a vulnerability**.
The report is visible only to the maintainers, and the fix and the advisory can be drafted in place.

Please don't open a public issue, a discussion or a pull request for something exploitable. Don't test against the
live site beyond what a normal visitor does: submit only repositories you own, and don't try to exhaust its
resources.

For a problem that is not a vulnerability, use the **Report a problem** issue form ([CONTRIBUTING.md](CONTRIBUTING.md)).

**Never put a real credential in a report.** If a token or key is part of the finding, quote at most eight characters
of it after any structural prefix, then `…`, and say where it lives instead of what it is.

evalskills has one maintainer and no on-call, so this policy promises no response time beyond reading reports as
soon as possible. If a report means a credential of yours needs rotating, rotate it at once rather than waiting for
a reply.

Coordinated disclosure, please: give a fix a reasonable window before going public. There is no bug bounty.
Reporters are credited in the advisory unless they ask not to be.
