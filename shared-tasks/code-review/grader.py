#!/usr/bin/env python3
"""Shared deterministic grader for code-review (default_plus_custom). Stdlib only.

A finding counts by location, not wording: it hits a planted defect when its line falls within one line of the
defect's span in the changed file. Wording is left to the default LLM-judged assertions."""

import json
import os
from pathlib import Path

ENTRY = Path(os.environ.get("HARBOR_ENTRY_JSON", "/tests/entry.json"))
REWARD_JSON = Path(os.environ.get("HARBOR_REWARD_JSON", "/logs/verifier/reward.json"))
REWARD_TXT = Path(os.environ.get("HARBOR_REWARD_TXT", "/logs/verifier/reward.txt"))
OUT = Path(os.environ.get("GRADER_OUT", "/workspace/output"))

# Line spans of the planted defects in files/review/orders.py, embedded so grading never depends on where the
# fixture lands or on an agent that edited it. A test checks them against the fixture.
PLANTED = {
    "sql-injection": (10, 13),  # customer and status formatted into the SQL string
    "skips-first-item": (18, 19),  # range(1, len(items))
    "swallowed-exception": (29, 33),  # except Exception: return {}
}
FALSE_HIGH_PENALTY = 0.1


def findings():
    p = OUT / "review.json"
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text())
    except ValueError:
        return None
    if not isinstance(data, list):
        return None
    out = []
    for f in data:
        if not isinstance(f, dict):
            continue
        try:
            line = int(f.get("line"))
        except (TypeError, ValueError):
            line = None
        out.append((line, str(f.get("severity", "")).strip().lower()))
    return out


def grade(eid):
    found = findings()
    if eid == "review-shared-planted-defects":
        if found is None:
            return 0.0, "output/review.json missing or not a JSON list"
        sp = PLANTED
        hit = {n for n, (a, b) in sp.items() for line, _ in found if line is not None and a - 1 <= line <= b + 1}
        false_high = sum(
            1
            for line, sev in found
            if sev == "high" and not any(line is not None and a - 1 <= line <= b + 1 for a, b in sp.values())
        )
        score = max(0.0, len(hit) / len(sp) - FALSE_HIGH_PENALTY * false_high)
        return round(score, 3), f"planted found {sorted(hit)} of {len(sp)}; false high findings {false_high}"
    if eid == "review-shared-clean-refactor":
        if found is None:
            return 0.0, "output/review.json missing or not a JSON list"
        highs = sum(1 for _, sev in found if sev == "high")
        return (0.0 if highs else 1.0), f"high findings on a behaviour-preserving refactor: {highs}"
    return None, "no deterministic check"


def main():
    entry = json.loads(ENTRY.read_text()) if ENTRY.exists() else {}
    score, reason = grade(str(entry.get("id") or entry.get("entry_id") or ""))
    reward = {"custom_metrics": {}, "details": {}}
    if score is not None:
        reward["custom_metrics"]["review_task_success"] = score
        reward["details"]["review_task_success"] = {"score": score, "reason": reason}
    REWARD_JSON.parent.mkdir(parents=True, exist_ok=True)
    REWARD_JSON.write_text(json.dumps(reward, indent=2))
    REWARD_TXT.write_text(str(score if score is not None else 0.0))


if __name__ == "__main__":
    main()
