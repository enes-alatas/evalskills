import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

import evaluate as ev  # noqa: E402


class Ran(Exception):
    """Raised by the stubbed evaluator: proves a fresh run started instead of a reuse."""


@pytest.fixture
def skill(tmp_path, monkeypatch):
    d = tmp_path / "demo"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: demo\ndescription: fixture\n---\n")
    monkeypatch.setattr(ev, "RESULTS", tmp_path / "results.jsonl")
    monkeypatch.setattr(ev, "REPORTS", tmp_path / "reports")
    monkeypatch.setattr(ev, "evaluator_version", lambda: {"commit": "e" * 40})

    def run_tier1(*_a, **_k):
        raise Ran

    monkeypatch.setattr(ev, "run_tier1", run_tier1)
    return d


def previous(skill, status):
    rec = {
        "status": status,
        "mode": "tier1-static",
        "source": {"repo": "example/repo", "skill_path": "demo", "tree_digest": ev.tree_digest(skill)},
        "evaluator": {"commit": "e" * 40},
    }
    ev.RESULTS.write_text(json.dumps(rec) + "\n")


def test_a_completed_result_is_reused(skill):
    previous(skill, "completed")
    rec = ev._evaluate("example/repo", "demo", "", False, False, skill, "c" * 40)
    assert rec["_cached"] is True


def test_a_failed_result_is_retried_not_reused(skill):
    previous(skill, "failed")
    with pytest.raises(Ran):
        ev._evaluate("example/repo", "demo", "", False, False, skill, "c" * 40)
