"""#177: no machine path of the team's in the pipeline; a missing SkillEvaluator says which variable to set."""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))
import evaluate as ev  # noqa: E402


def test_no_loop_machine_path_is_left_in_the_tree():
    hits = subprocess.run(
        ["git", "grep", "-n", "/home/" + "loop", "--", ".", ":!docs"], cwd=ROOT, capture_output=True, text=True
    ).stdout
    assert hits == ""


def test_a_missing_evaluator_names_the_variable_before_anything_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "EVALUATOR_DIR", tmp_path / "nowhere")
    monkeypatch.setattr(ev, "SCANNER_IN", "")
    ev._local_evaluator_version.cache_clear()
    started = []
    with pytest.raises(ev.EvaluatorMissing, match="SKILLEVALUATOR_DIR"):
        ev.run_tier1(tmp_path, tmp_path / "out", llm=False, on_start=lambda: started.append(1))
    with pytest.raises(ev.EvaluatorMissing, match="SKILLEVALUATOR_DIR"):
        ev._local_evaluator_version()
    assert started == []  # never marked running
    ev._local_evaluator_version.cache_clear()


def test_a_checkout_is_accepted(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'skillevaluator'\n")
    monkeypatch.setattr(ev, "EVALUATOR_DIR", tmp_path)
    assert ev.require_evaluator() == tmp_path
