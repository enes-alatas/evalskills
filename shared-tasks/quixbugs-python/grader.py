#!/usr/bin/env python3
"""Grader for the QuixBugs pilot set (default_plus_custom). Stdlib only.

Runs QuixBugs' own JSON test cases (json_testcases/<program>.json at the commit in SOURCE.md, embedded below
unchanged) against the agent's fixed program, the way upstream's python_testcases do: call the function with the
case's arguments and compare with ==. The score is the fraction of upstream cases that pass. Each program runs in a
fresh interpreter with a timeout, so a fix that loops forever scores what it passed before the timeout.

The program under test shares the child process, so the child never decides a verdict. It emits one tagged record
per case, carrying a per-run nonce and json.dumps(result); the parent parses the records and compares them with the
expected values itself. That defeats stray PASS output and objects whose __eq__ always returns True. A case scores
only with exactly one well-formed record for its index, and the score is clamped to [0, 1]. Residual: code that
walks the interpreter's frames could still read the nonce; trial transcripts remain the backstop for that.
The child gets the arguments only, never the expected values, and runs in its own session that is killed as a
group when it finishes or times out."""

import json
import os
import secrets
import signal
import subprocess
import sys
from pathlib import Path

ENTRY = Path(os.environ.get("HARBOR_ENTRY_JSON", "/tests/entry.json"))
REWARD_JSON = Path(os.environ.get("HARBOR_REWARD_JSON", "/logs/verifier/reward.json"))
REWARD_TXT = Path(os.environ.get("HARBOR_REWARD_TXT", "/logs/verifier/reward.txt"))
OUT = Path(os.environ.get("GRADER_OUT", "/workspace/output"))
TIMEOUT = float(os.environ.get("GRADER_TIMEOUT", "30"))
PREFIX = "quixbugs-"

# Upstream test cases, one JSON line per case exactly as in json_testcases/<program>.json. A test checks them
# against upstream/.
CASES = {
    "find_first_in_sorted": [
        json.loads(x)
        for x in [
            "[[[3, 4, 5, 5, 5, 5, 6], 5], 2]",
            "[[[3, 4, 5, 5, 5, 5, 6], 7], -1]",
            "[[[3, 4, 5, 5, 5, 5, 6], 2], -1]",
            "[[[3, 6, 7, 9, 9, 10, 14, 27], 14], 6]",
            "[[[0, 1, 6, 8, 13, 14, 67, 128], 80], -1]",
            "[[[0, 1, 6, 8, 13, 14, 67, 128], 67], 6]",
            "[[[0, 1, 6, 8, 13, 14, 67, 128], 128], 7]",
        ]
    ],
    "lis": [
        json.loads(x)
        for x in [
            "[[[]], 0]",
            "[[[3]], 1]",
            "[[[10, 20, 11, 32, 22, 48, 43]], 4]",
            "[[[4, 2, 1]], 1]",
            "[[[5, 1, 3, 4, 7]], 4]",
            "[[[4, 1]], 1]",
            "[[[-1, 0, 2]], 3]",
            "[[[0, 2]], 2]",
            "[[[4, 1, 5, 3, 7, 6, 2]], 3]",
            "[[[10, 22, 9, 33, 21, 50, 41, 60, 80]], 6]",
            "[[[7, 10, 9, 2, 3, 8, 1]], 3]",
            "[[[9, 11, 2, 13, 7, 15]], 4]",
        ]
    ],
    "next_permutation": [
        json.loads(x)
        for x in [
            "[[[3, 2, 4, 1]], [3, 4, 1, 2]]",
            "[[[3, 5, 6, 2, 1]], [3, 6, 1, 2, 5]]",
            "[[[3, 5, 6, 2]], [3, 6, 2, 5]]",
            "[[[4, 5, 1, 7, 9]], [4, 5, 1, 9, 7]]",
            "[[[4, 5, 8, 7, 1]], [4, 7, 1, 5, 8]]",
            "[[[9, 5, 2, 6, 1]], [9, 5, 6, 1, 2]]",
            "[[[44, 5, 1, 7, 9]], [44, 5, 1, 9, 7]]",
            "[[[3, 4, 5]], [3, 5, 4]]",
        ]
    ],
    "quicksort": [
        json.loads(x)
        for x in [
            "[[[1, 2, 6, 72, 7, 33, 4]], [1, 2, 4, 6, 7, 33, 72]]",
            "[[[3, 1, 4, 1, 5, 9, 2, 6, 5, 3, 5, 8, 9, 7, 9, 3]], [1, 1, 2, 3, 3, 3, 4, 5, 5, 5, 6, 7, 8, 9, 9, 9]]",
            "[[[5, 4, 3, 2, 1]], [1, 2, 3, 4, 5]]",
            "[[[5, 4, 3, 1, 2]], [1, 2, 3, 4, 5]]",
            "[[[8, 1, 14, 9, 15, 5, 4, 3, 7, 17, 11, 18, 2, 12, 16, 13, 6, 10]], [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18]]",  # noqa: E501
            "[[[9, 4, 5, 2, 17, 14, 10, 6, 15, 8, 12, 13, 16, 3, 1, 7, 11]], [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17]]",  # noqa: E501
            "[[[13, 14, 7, 16, 9, 5, 24, 21, 19, 17, 12, 10, 1, 15, 23, 25, 11, 3, 2, 6, 22, 8, 20, 4, 18]], [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25]]",  # noqa: E501
            "[[[8, 5, 15, 7, 9, 14, 11, 12, 10, 6, 2, 4, 13, 1, 3]], [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]]",  # noqa: E501
            "[[[4, 3, 7, 6, 5, 2, 1]], [1, 2, 3, 4, 5, 6, 7]]",
            "[[[4, 3, 1, 5, 2]], [1, 2, 3, 4, 5]]",
            "[[[5, 4, 2, 3, 6, 7, 1]], [1, 2, 3, 4, 5, 6, 7]]",
            "[[[10, 16, 6, 1, 14, 19, 15, 2, 9, 4, 18, 17, 12, 3, 11, 8, 13, 5, 7]], [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19]]",  # noqa: E501
            "[[[10, 16, 6, 1, 14, 19, 15, 2, 9, 4, 18]], [1, 2, 4, 6, 9, 10, 14, 15, 16, 18, 19]]",
        ]
    ],
    "sieve": [
        json.loads(x)
        for x in [
            "[[1], []]",
            "[[2], [2]]",
            "[[4], [2, 3]]",
            "[[7], [2, 3, 5, 7]]",
            "[[20], [2, 3, 5, 7, 11, 13, 17, 19]]",
            "[[50], [2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47]]",
        ]
    ],
    "to_base": [
        json.loads(x)
        for x in [
            '[[8227, 18], "1771"]',
            '[[73, 8], "111"]',
            '[[16, 19], "G"]',
            '[[31, 16], "1F"]',
            '[[41, 2], "101001"]',
            '[[44, 5], "134"]',
            '[[27, 23], "14"]',
            '[[56, 23], "2A"]',
            '[[8237, 24], "E75"]',
            '[[8237, 34], "749"]',
        ]
    ],
}

RUNNER = r"""
import importlib.util, json, sys
_tag = sys.stdin.readline().strip()
_cases = json.loads(sys.stdin.readline())
_dumps, _write, _flush = json.dumps, sys.stdout.write, sys.stdout.flush
_path, _name = sys.argv[1], sys.argv[2]
_spec = importlib.util.spec_from_file_location(_name, _path)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
_fn = getattr(_mod, _name)
for _i, _args in enumerate(_cases):
    try:
        _rec = _dumps(_fn(*_args))
    except Exception:
        _rec = None
    if _rec is not None:
        _write(f"{_tag} {_i} {_rec}\n")
        _flush()
"""


def run_cases(program: Path, name: str):
    """(passed, total) for the upstream cases of `name` against `program`. The parent judges every case."""
    cases = CASES[name]
    tag = "QBREC-" + secrets.token_hex(16)
    # Arguments only: the expected values never enter the child, so the program cannot look them up.
    payload = tag + "\n" + json.dumps([args for args, _ in cases]) + "\n"
    # Own session, killed as a group afterwards: no forked grandchild outlives the run to rewrite reward.*.
    p = subprocess.Popen(
        [sys.executable, "-I", "-c", RUNNER, str(program), name],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )
    try:
        out, _ = p.communicate(payload, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        out = None
    finally:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if out is None:
        try:
            out, _ = p.communicate(timeout=5)
        except subprocess.TimeoutExpired:  # a process outside the group still holds stdout: score nothing
            p.kill()
            out = ""
    records: dict[int, list[str]] = {}
    for line in out.splitlines():
        parts = line.split(" ", 2)
        if len(parts) == 3 and parts[0] == tag and parts[1].isdigit():
            records.setdefault(int(parts[1]), []).append(parts[2])
    passed = 0
    for i, (_, expected) in enumerate(cases):
        recs = records.get(i, [])
        if len(recs) != 1:
            continue  # missing or duplicated record: the case fails
        try:
            got = json.loads(recs[0])
        except ValueError:
            continue
        if got == expected:  # plain JSON values on both sides, compared in the parent
            passed += 1
    return passed, len(cases)


def grade(eid: str):
    name = eid[len(PREFIX) :] if eid.startswith(PREFIX) else ""
    if name not in CASES:
        return None, "no deterministic check"
    program = OUT / f"{name}.py"
    if not program.is_file():
        return 0.0, f"output/{name}.py missing"
    passed, total = run_cases(program, name)
    return round(min(max(passed / total, 0.0), 1.0), 3), f"upstream QuixBugs cases passed {passed}/{total}"


def main():
    entry = json.loads(ENTRY.read_text()) if ENTRY.exists() else {}
    score, reason = grade(str(entry.get("id") or entry.get("entry_id") or ""))
    reward = {"custom_metrics": {}, "details": {}}
    if score is not None:
        reward["custom_metrics"]["quixbugs_cases_passed"] = score
        reward["details"]["quixbugs_cases_passed"] = {"score": score, "reason": reason}
    REWARD_JSON.parent.mkdir(parents=True, exist_ok=True)
    REWARD_JSON.write_text(json.dumps(reward, indent=2))
    REWARD_TXT.write_text(str(score if score is not None else 0.0))


if __name__ == "__main__":
    main()
