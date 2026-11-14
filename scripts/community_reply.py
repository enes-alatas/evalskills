"""Label and answer a report from outside the team (#146, #151; .github/workflows/community-reply.yml).

An issue is a report when its author isn't on the team (GitHub's `author_association` is not OWNER, MEMBER or
COLLABORATOR), whichever form they used, or when it carries `community` (the report form presets it). A report from
outside gets `community` and `triage` if either is missing. Every report gets the fixed first reply,
`.github/community-reply.md` word for word, once: the script skips it when a comment already carries the marker.

Nothing from the issue goes into what it writes: a report is untrusted input (docs/CONVENTIONS.md, Issues). It reads
only the association, the labels and whether its marker is in a comment.

  GH_TOKEN=... python3 -I scripts/community_reply.py --repo owner/name --issue 12 [--author-association NONE]
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPLY = Path(__file__).resolve().parents[1] / ".github" / "community-reply.md"
MARKER = "<!-- evalskills:community-reply -->"
LABEL = "community"
REPORT_LABELS = ("community", "triage")
TEAM = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})


def reply_text() -> str:
    text = REPLY.read_text()
    if not text.startswith(MARKER):
        raise SystemExit(f"{REPLY.name} must start with its marker")
    return text


def from_outside(association: str) -> bool:
    return association.upper() not in TEAM


def labels_to_add(association: str, labels: list[str]) -> list[str]:
    """The report labels an outside author's issue is missing; none for the team's own issues."""
    return [name for name in REPORT_LABELS if name not in labels] if from_outside(association) else []


def should_reply(association: str, labels: list[str], comment_bodies: list[str]) -> bool:
    """A report (from outside, or labelled `community`), and only if no comment carries the marker yet."""
    is_report = from_outside(association) or LABEL in labels
    return is_report and not any(MARKER in body for body in comment_bodies)


def gh_json(*args: str):
    out = subprocess.run(["gh", "api", *args], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--issue", required=True, type=int)
    # A drill (workflow_dispatch) treats a maintainer's test issue as if its author were outside the team.
    # ISSUE (the dispatch default) and "" (an issue event, with no inputs) both mean the issue's own association.
    ap.add_argument(
        "--author-association", default="", choices=["", "ISSUE", "NONE", "CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR"]
    )
    args = ap.parse_args(argv)
    base = f"repos/{args.repo}/issues/{args.issue}"
    issue = gh_json(base)
    if "pull_request" in issue:
        print("a pull request, not an issue: nothing to do")
        return 0
    override = "" if args.author_association == "ISSUE" else args.author_association
    association = override or issue.get("author_association") or "NONE"
    labels = [label["name"] for label in issue.get("labels", [])]
    missing = labels_to_add(association, labels)
    if missing:
        cmd = ["gh", "api", "-X", "POST", f"{base}/labels"]
        for name in missing:
            cmd += ["-f", f"labels[]={name}"]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL)
        print("labelled: " + ", ".join(missing))
        labels += missing
    comments = [c.get("body") or "" for page in gh_json("--paginate", "--slurp", f"{base}/comments") for c in page]
    if not should_reply(association, labels, comments):
        print("no reply: a team issue, or already replied")
        return 0
    subprocess.run(
        ["gh", "api", "-X", "POST", f"{base}/comments", "-f", f"body={reply_text()}"],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    print("replied")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
