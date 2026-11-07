"""PreToolUse hook: refuses a gh issue/PR body that @-mentions anyone or lacks a `— Name · Role` signature.

We share one GitHub identity, and an @name on GitHub notifies whoever owns that login. Reads the hook JSON on stdin;
exit 2 blocks the call and shows stderr to the agent. Anything it cannot parse is let through: the PR review is the
gate, this is the early warning.
"""

import json
import re
import shlex
import sys
from pathlib import Path

SUBCOMMANDS = {("issue", "create"), ("issue", "comment"), ("issue", "edit"), ("pr", "create"), ("pr", "comment"),
               ("pr", "edit"), ("pr", "review")}  # fmt: skip
MENTION = re.compile(r"(?<![\w`/.])@[A-Za-z0-9][A-Za-z0-9-]*")
SIGNATURE = re.compile(r"— [^\n·]+ · [^\n]+\s*$")


def bodies(command):
    try:
        words = shlex.split(command)
    except ValueError:
        return
    for index, word in enumerate(words):
        if word != "gh" or tuple(words[index + 1 : index + 3]) not in SUBCOMMANDS:
            continue
        rest = words[index + 3 :]
        for position, arg in enumerate(rest):
            value = None
            if arg in ("--body", "-b") and position + 1 < len(rest):
                value = rest[position + 1]
            elif arg.startswith("--body="):
                value = arg.split("=", 1)[1]
            elif arg in ("--body-file", "-F") and position + 1 < len(rest):
                path = Path(rest[position + 1])
                value = path.read_text() if path.is_file() else None
            if value is not None:
                yield value


def problems(body):
    without_code = re.sub(r"```.*?```|`[^`\n]*`", "", body, flags=re.S)
    found = []
    mentions = sorted(set(MENTION.findall(without_code)))
    if mentions:
        found.append(f"names with @ notify GitHub users: {', '.join(mentions)}. Write the name without @.")
    if not SIGNATURE.search(body):
        found.append("the body must end with a signature line: `— Name · Role` (e.g. `— skillator · Author`).")
    return found


def main():
    try:
        command = json.load(sys.stdin).get("tool_input", {}).get("command", "")
    except (json.JSONDecodeError, AttributeError):
        return 0
    issues = [problem for body in bodies(command) for problem in problems(body)]
    if issues:
        print("gh body refused:\n- " + "\n- ".join(issues), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
