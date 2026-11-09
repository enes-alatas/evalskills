"""Self-test of the shared code-review grader: the scenarios in #57."""

import importlib.util
import json
from pathlib import Path

import pytest

SET = Path(__file__).resolve().parents[1] / "shared-tasks" / "code-review"


def load_grader():
    spec = importlib.util.spec_from_file_location("cr_grader", SET / "grader.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def grader(tmp_path, monkeypatch):
    g = load_grader()
    monkeypatch.setattr(g, "OUT", tmp_path)
    return g


def review(tmp_path, items):
    (tmp_path / "review.json").write_text(json.dumps(items))


def f(line, severity="high"):
    return {"file": "orders.py", "line": line, "severity": severity, "issue": "x"}


def test_planted_spans_match_the_fixture():
    lines = (SET / "files" / "review" / "orders.py").read_text().splitlines()
    starts = {
        "sql-injection": 'query = f"SELECT',
        "skips-first-item": "for i in range(1, len(items))",
        "swallowed-exception": "try:",
    }
    ends = {
        "sql-injection": "return conn.execute(query)",
        "skips-first-item": "total += items[i]",
        "swallowed-exception": "return {}",
    }
    for name, (a, b) in load_grader().PLANTED.items():
        assert lines[a - 1].strip().startswith(starts[name]), name
        assert lines[b - 1].strip().startswith(ends[name]), name


def changed_lines(name):
    """Line numbers in the post-change file that the diff adds or changes."""
    out, line = [], 0
    for x in (SET / "files" / name / "change.diff").read_text().splitlines():
        if x.startswith("@@"):
            line = int(x.split("+")[1].split(",")[0]) - 1
        elif x.startswith("+") and not x.startswith("+++"):
            line += 1
            out.append(line)
        elif x.startswith(" "):
            line += 1
    return out


def test_perfect_review_scores_one(grader, tmp_path):
    review(tmp_path, [f(11), f(20), f(39, "medium"), f(26, "low"), f(6, "low")])
    assert grader.grade("review-shared-planted-defects")[0] == 1.0


@pytest.mark.parametrize("content", [None, "not json", '{"a": 1}', "[]"])
def test_missing_or_empty_review_scores_zero(grader, tmp_path, content):
    if content is not None:
        (tmp_path / "review.json").write_text(content)
    assert grader.grade("review-shared-planted-defects")[0] == 0.0


@pytest.mark.parametrize("severity", ["high", "medium"])
@pytest.mark.parametrize("scope", ["changed", "all"])
def test_flagging_every_line_scores_below_a_precise_review(grader, tmp_path, severity, scope):
    n = len((SET / "files" / "review" / "orders.py").read_text().splitlines())
    lines = changed_lines("review") if scope == "changed" else range(1, n + 1)
    review(tmp_path, [f(i, severity) for i in lines])
    shotgun = grader.grade("review-shared-planted-defects")[0]
    review(tmp_path, [f(11), f(20)])
    precise = grader.grade("review-shared-planted-defects")[0]
    assert precise == pytest.approx(2 / 3, abs=0.001)
    assert shotgun <= 0.1 < precise


def test_low_findings_neither_hit_nor_cost(grader, tmp_path):
    review(tmp_path, [f(i, "low") for i in range(1, 41)])
    assert grader.grade("review-shared-planted-defects")[0] == 0.0
    review(tmp_path, [f(11), f(20), f(37)] + [f(i, "low") for i in range(1, 41)])
    assert grader.grade("review-shared-planted-defects")[0] == 1.0


def test_partial_review_scores_by_recall(grader, tmp_path):
    review(tmp_path, [f(13, "medium")])
    assert grader.grade("review-shared-planted-defects")[0] == pytest.approx(1 / 3, abs=0.001)


def test_clean_refactor(grader, tmp_path):
    review(tmp_path, [f(3, "high")])
    assert grader.grade("review-shared-clean-refactor")[0] == 0.0
    review(tmp_path, [f(3, "low"), f(7, "medium")])
    assert grader.grade("review-shared-clean-refactor")[0] == 1.0
    review(tmp_path, [])
    assert grader.grade("review-shared-clean-refactor")[0] == 1.0


def test_negative_case_has_no_deterministic_check(grader):
    assert grader.grade("review-shared-negative")[0] is None


def test_refactor_fixture_preserves_behaviour():
    """The 'clean' change really is clean: before and after agree on sample inputs."""
    diff = (SET / "files" / "refactor" / "change.diff").read_text()
    before = "\n".join(x[1:] for x in diff.splitlines()[3:] if x[:1] in (" ", "-"))
    ns_before, ns_after = {}, {}
    exec(compile(before, "before", "exec"), ns_before)
    exec(compile((SET / "files" / "refactor" / "inventory.py").read_text(), "after", "exec"), ns_after)
    items = [
        {"sku": "a", "count": 1, "missing": 2},
        {"sku": "b", "count": 5, "missing": 0},
        {"sku": "c", "count": 0, "missing": 4},
    ]
    costs = {"a": 1.5, "b": 9.0, "c": 0.25}
    for ns in (ns_before, ns_after):
        assert ns["low_stock"](items, 3) == ["a", "c"]
        assert ns["restock_cost"](items, costs) == 4.0
