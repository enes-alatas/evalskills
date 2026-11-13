"""Post the fixed first reply on a report from outside the team (#146; .github/workflows/community-reply.yml).

The reply is `.github/community-reply.md`, word for word. Nothing from the issue goes into it: a report is untrusted
input (docs/CONVENTIONS.md, Issues). The script reads only the issue's labels and whether a comment already carries
the reply's marker, so it posts once per issue, however often it runs.

  GH_TOKEN=... python3 -I scripts/community_reply.py --repo owner/name --issue 12
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPLY = Path(__file__).resolve().parents[1] / ".github" / "community-reply.md"
MARKER = "<!-- evalskills:community-reply -->"
LABEL = "community"


def reply_text() -> str:
    text = REPLY.read_text()
    if not text.startswith(MARKER):
        raise SystemExit(f"{REPLY.name} must start with its marker")
    return text


def should_reply(labels: list[str], comment_bodies: list[str]) -> bool:
    """Only a `community` issue, and only if no comment carries the marker yet."""
    return LABEL in labels and not any(MARKER in body for body in comment_bodies)


def gh_json(*args: str):
    out = subprocess.run(["gh", "api", *args], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--issue", required=True, type=int)
    args = ap.parse_args(argv)
    base = f"repos/{args.repo}/issues/{args.issue}"
    issue = gh_json(base)
    if "pull_request" in issue:
        print("a pull request, not an issue: nothing to do")
        return 0
    labels = [label["name"] for label in issue.get("labels", [])]
    comments = [c.get("body") or "" for page in gh_json("--paginate", "--slurp", f"{base}/comments") for c in page]
    if not should_reply(labels, comments):
        print("no reply: not a community issue, or already replied")
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
