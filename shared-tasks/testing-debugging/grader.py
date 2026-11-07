#!/usr/bin/env python3
"""Shared deterministic grader for testing-debugging (default_plus_custom). Stdlib only.
Runs a hidden copy of the ORIGINAL tests plus extra hidden tests against the agent's stats.py."""
import json, os, shutil, subprocess, tempfile
from pathlib import Path

ENTRY = Path(os.environ.get("HARBOR_ENTRY_JSON", "/tests/entry.json"))
REWARD_JSON = Path(os.environ.get("HARBOR_REWARD_JSON", "/logs/verifier/reward.json"))
REWARD_TXT = Path(os.environ.get("HARBOR_REWARD_TXT", "/logs/verifier/reward.txt"))
OUT = Path(os.environ.get("GRADER_OUT", "/workspace/output"))

HIDDEN = '''
import unittest
from stats import mean, median, moving_average
class H(unittest.TestCase):
    def test_mean(self): self.assertEqual(mean([1, 2, 3, 4]), 2.5)
    def test_median_odd(self): self.assertEqual(median([3, 1, 2]), 2)
    def test_median_even(self): self.assertEqual(median([4, 1, 3, 2]), 2.5)
    def test_median_even_floats(self): self.assertAlmostEqual(median([0.5, 2.5, 1.0, 9.0]), 1.75)
    def test_moving_average(self): self.assertEqual(moving_average([1, 2, 3, 4], 2), [1.5, 2.5, 3.5])
    def test_moving_average_full(self): self.assertEqual(moving_average([2, 4], 2), [3.0])
'''


def run_tests(stats: Path, test_src: str):
    d = Path(tempfile.mkdtemp())
    shutil.copy(stats, d / "stats.py")
    (d / "test_h.py").write_text(test_src)
    p = subprocess.run(["python3", "-m", "unittest", "-q", "test_h"], cwd=d, capture_output=True, text=True, timeout=60)
    import re
    m = re.search(r"Ran (\d+) test", p.stderr)
    ran = int(m.group(1)) if m else 0
    fails = sum(int(x) for x in re.findall(r"(?:failures|errors)=(\d+)", p.stderr))
    return ran, (ran - fails if ran else 0)


def grade(eid):
    stats = OUT / "stats.py"
    if eid == "debug-shared-fix-failing-tests":
        if not stats.is_file():
            return 0.0, "output/stats.py missing"
        ran, ok = run_tests(stats, HIDDEN)
        return (ok / ran if ran else 0.0), f"hidden tests passed {ok}/{ran}"
    if eid == "debug-shared-add-regression-test":
        tests = OUT / "test_stats.py"
        if not stats.is_file() or not tests.is_file():
            return 0.0, "stats.py or test_stats.py missing in output"
        ran, ok = run_tests(stats, HIDDEN.replace("moving_average([1, 2, 3, 4], 2), [1.5, 2.5, 3.5]", "1, 1").replace("moving_average([2, 4], 2), [3.0]", "1, 1"))
        median_fixed = ok == ran and ran > 0
        src = tests.read_text()
        added = src.count("def test_") > 4
        uran, uok = run_tests(stats, src)
        score = 0.5 * median_fixed + 0.25 * added + 0.25 * (uran > 0 and uok == uran)
        return score, f"median_fixed={median_fixed} new_test={added} own_tests={uok}/{uran}"
    return None, "no deterministic check"


def main():
    entry = json.loads(ENTRY.read_text()) if ENTRY.exists() else {}
    score, reason = grade(str(entry.get("id") or entry.get("entry_id") or ""))
    reward = {"custom_metrics": {}, "details": {}}
    if score is not None:
        reward["custom_metrics"]["debug_task_success"] = score
        reward["details"]["debug_task_success"] = {"score": score, "reason": reason}
    REWARD_JSON.parent.mkdir(parents=True, exist_ok=True)
    REWARD_JSON.write_text(json.dumps(reward, indent=2))
    REWARD_TXT.write_text(str(score if score is not None else 0.0))


if __name__ == "__main__":
    main()
