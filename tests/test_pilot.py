"""#121 PR B: the internal live pilot's runner."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))
import evaluate as ev  # noqa: E402
import live_pilot as pilot  # noqa: E402
import tier3  # noqa: E402


def test_pilot_records_live_under_the_ignored_reports_dir():
    rel = pilot.RECORDS.relative_to(ROOT)
    assert rel.parts[0] == "reports"
    ignored = subprocess.run(["git", "check-ignore", "-q", str(rel)], cwd=ROOT, timeout=30)
    assert ignored.returncode == 0  # never committed


# --- the skill list ---


def test_the_pilot_list_loads():
    rows = pilot.load_skills()
    assert len(rows) == 10
    assert len({r["category"] for r in rows}) >= 3
    assert all(sum(r["category"] == c for r in rows) >= 2 for c in {r["category"] for r in rows})
    for r in rows:
        if r["task_set"]:
            assert (tier3.SHARED / r["task_set"] / "SOURCE.md").is_file()  # vendored sets only (ADR-0006)
    assert sum(r["repeats"] for r in rows) == 16


@pytest.mark.parametrize(
    "line",
    [
        "a/b\ts\tc\t-",  # four columns
        "a/../b\ts\tc\t-\t1",
        "a/b\t/etc\tc\t-\t1",
        "a/b\tx/../../y\tc\t-\t1",
    ],
)
def test_bad_rows_are_refused(tmp_path, line):
    p = tmp_path / "skills.tsv"
    p.write_text(line + "\n")
    with pytest.raises(ValueError):
        pilot.load_skills(p)


# --- statuses: never a fabricated score ---


def lift_dir(tmp_path, data, agent="opencode"):
    d = tmp_path / "out" / "20261112_000000" / agent
    d.mkdir(parents=True)
    (d / "lift.json").write_text(json.dumps(data))
    return tmp_path / "out"


def test_lift_is_read_as_written(tmp_path):
    out = lift_dir(
        tmp_path,
        {"execution_status": "succeeded", "lift": {"overall": {"with_skill": 0.8, "without_skill": 0.5, "delta": 0.3}}},
    )
    assert pilot.read_lift(out) == {
        "opencode": {"execution_status": "succeeded", "with_skill": 0.8, "without_skill": 0.5, "lift": 0.3}
    }


@pytest.mark.parametrize(
    "exit_code, data, status",
    [
        (None, None, "failed"),  # timed out
        (1, None, "failed"),  # no lift.json
        (0, {"execution_status": "failed", "lift": {}}, "failed"),
        (0, {"execution_status": "succeeded", "lift": {"overall": {"with_skill": 0.7}}}, "inconclusive"),
        (0, {"execution_status": "succeeded", "lift": {"overall": {"with_skill": 0.7, "delta": 0.0}}}, "completed"),
    ],
)
def test_status_never_invents_a_score(tmp_path, exit_code, data, status):
    lift = pilot.read_lift(lift_dir(tmp_path, data)) if data else {}
    got, reason = pilot.status_of(exit_code, lift)
    assert got == status
    assert (reason is None) == (status == "completed")


def test_a_failed_run_records_no_scores(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "_local_evaluator_version", lambda: {"version": "x", "commit": "y"})
    out = ev.ROOT / "reports" / "pilot-test-out"
    res = {"exit": 1, "cases": "starter", "out": out, "commit": "c" * 40, "tree_digest": "sha256:0", "duration_s": 1.0}
    row = {"repo": "a/b", "skill_path": "s", "category": "c", "task_set": None, "repeats": 1}
    rec = pilot.record(row, 1, "pilot-r", res, {}, None, "2026-11-12T00:00:00+00:00")
    assert rec["status"] == "failed" and rec["scores"] is None
    assert rec["mode"] == "tier3" and rec["pilot"] is True and ev.is_pilot(rec)
    assert rec["cost_usd"] is None  # unbilled trial key: not a zero


# --- cases labels ---


def test_cases_label(tmp_path):
    skill = tmp_path / "s"
    skill.mkdir()
    assert tier3.cases_label(skill, "quixbugs-python") == tier3.CASES_SHARED
    assert tier3.cases_label(skill, None) == tier3.CASES_STARTER
    (skill / "evals").mkdir()
    assert tier3.cases_label(skill, None) == tier3.CASES_STARTER  # an empty evals/ is no cases
    (skill / "evals" / "evals.json").write_text("{}")
    assert tier3.cases_label(skill, None) == tier3.CASES_OWN


def test_agent_model_is_passed_to_opencode(monkeypatch, tmp_path):
    monkeypatch.setattr(tier3, "AGENT_MODEL", "vendor/model")
    cmd = tier3.command(tmp_path, tmp_path / "out", "a/b", "c" * 40)
    assert cmd[cmd.index("--agent-model") + 1] == "opencode=openai/vendor/model"


def test_a_crashed_run_is_recorded_failed_without_scores():
    row = {"repo": "a/b", "skill_path": "s", "category": "c", "task_set": None, "repeats": 1}
    rec = pilot.crashed(row, 1, "pilot-r", "CalledProcessError: clone failed", None, "2026-11-12T00:00:00+00:00")
    assert (rec["status"], rec["scores"], rec["mode"]) == ("failed", None, "tier3")
    assert rec["reason"].startswith("CalledProcessError")


def test_a_same_day_rerun_gets_a_new_run_id():
    import datetime as dt

    row = {"repo": "a/b", "skill_path": "s", "category": "c", "task_set": None, "repeats": 1}
    first = pilot.run_id_for(row, 1, dt.datetime(2026, 11, 12, 14, 15, 2, tzinfo=dt.UTC))
    retry = pilot.run_id_for(row, 1, dt.datetime(2026, 11, 12, 16, 0, 0, tzinfo=dt.UTC))
    assert first == "pilot-20261112T141502-a__b__s-1"
    assert first != retry


def test_no_gateway_stops_the_pilot_plainly(tmp_path, monkeypatch, capsys):
    row = {"repo": "a/b", "skill_path": "s", "category": "c", "task_set": None, "repeats": 1}
    monkeypatch.setattr(pilot, "load_skills", lambda: [row])
    monkeypatch.setattr(pilot, "RECORDS", tmp_path / "results.jsonl")

    def down(*a, **k):
        raise subprocess.CalledProcessError(1, "docker")

    monkeypatch.setattr(pilot, "issue_token", down)
    assert pilot.main([]) == 2
    assert "pilot stopped" in capsys.readouterr().err
    assert not (tmp_path / "results.jsonl").exists()


def test_a_failed_revoke_is_warned_about(monkeypatch, capsys):
    monkeypatch.setattr(pilot, "gateway", lambda *a, **k: subprocess.CompletedProcess(a, 1, "", ""))
    assert pilot.revoke("pilot-r") is False
    assert "not revoked" in capsys.readouterr().err


def test_timeout_output_is_decoded():
    assert tier3._text(b"ok \xff") == "ok �"
    assert tier3._text(None) == "" and tier3._text("s") == "s"
