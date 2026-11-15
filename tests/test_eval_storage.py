"""Stored evaluation data stays bounded (#168): old report folders are removed per skill, and nothing is evaluated
while the data volume is nearly full. Synthetic records and folders; no scan runs."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))

import evaluate as ev  # noqa: E402

REPO, PATH, SLUG = "example/repo", "skills/demo", "example__repo__skills__demo"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "ROOT", tmp_path)
    monkeypatch.setattr(ev, "REPORTS", tmp_path / "reports")

    def rec(stamp, status="completed", mode="tier1-static", repo=REPO, path=PATH, slug=SLUG):
        rel = f"reports/{slug}/cccccccccccc-{mode}-{stamp}"
        (tmp_path / rel).mkdir(parents=True)
        (tmp_path / rel / "report.public.json").write_text("{}")
        return {
            "source": {"repo": repo, "skill_path": path},
            "status": status,
            "mode": mode,
            "started_at": stamp,
            "report_dir": rel,
        }

    return tmp_path, rec


def left(tmp_path, slug=SLUG):
    return sorted(p.name.rsplit("-", 1)[1] for p in (tmp_path / "reports" / slug).iterdir())


def test_only_the_newest_evaluations_keep_their_reports(store):
    tmp_path, rec = store
    results = [rec(f"2026111{i}T000000Z") for i in range(5)]  # out of order on purpose: sorted by time, not name
    results.reverse()
    removed = ev.prune_reports(REPO, PATH, SLUG, results)
    assert len(removed) == 2 and ev.REPORTS_KEPT_PER_SKILL == 3
    assert left(tmp_path) == ["20261112T000000Z", "20261113T000000Z", "20261114T000000Z"]


def test_the_result_the_site_shows_keeps_its_report_behind_newer_failures(store):
    tmp_path, rec = store
    results = [rec("20261110T000000Z"), rec("20261111T000000Z")]
    results += [rec(f"2026111{i}T000000Z", status="failed") for i in (2, 3, 4)]
    ev.prune_reports(REPO, PATH, SLUG, results)
    assert left(tmp_path) == ["20261111T000000Z", "20261112T000000Z", "20261113T000000Z", "20261114T000000Z"]


def test_a_running_scan_and_other_skills_are_never_touched(store):
    tmp_path, rec = store
    other = [rec(f"2026111{i}T000000Z", path="skills/other", slug="example__repo__skills__other") for i in range(5)]
    results = [rec(f"2026111{i}T000000Z") for i in range(4)] + other
    running = tmp_path / "reports" / SLUG / "cccccccccccc-tier1-static-20261119T000000Z"  # no record yet
    running.mkdir()
    ev.prune_reports(REPO, PATH, SLUG, results)
    assert running.is_dir() and len(left(tmp_path)) == 4  # three kept, plus the running one
    assert len(left(tmp_path, "example__repo__skills__other")) == 5


def test_nothing_is_evaluated_below_the_free_space_minimum(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "REPORTS", tmp_path / "reports")  # not made yet: the check climbs to tmp_path
    monkeypatch.setattr(ev, "MIN_FREE_DATA_BYTES", 1 << 62)

    def no_clone(*_a, **_k):
        raise AssertionError("cloned although the disk is full")

    monkeypatch.setattr(ev, "checkout", no_clone)
    with pytest.raises(ev.LowDisk, match="below the"):
        ev.evaluate(REPO, PATH)
    monkeypatch.setattr(ev, "MIN_FREE_DATA_BYTES", 0)
    ev.check_free_data()  # passes with room to spare


def test_the_worker_fails_a_low_disk_job_with_a_note_and_no_result(tmp_path, monkeypatch):
    import server

    monkeypatch.setattr(server, "DB_PATH", tmp_path / "inventory.db")
    server.init_db()
    with server.db() as c:
        c.execute(
            "INSERT INTO skills VALUES (?,?,?,?,?,?,?,?)",
            ("example/repo:skills/demo", REPO, PATH, "demo", "Fixture", "docs", "fixture", 0),
        )
        c.execute(
            "INSERT INTO jobs (skill_id, status, created_at) VALUES (?, 'queued', 0)", ("example/repo:skills/demo",)
        )

    def full(*_a, **_k):
        raise ev.LowDisk("900 MB free, below the 5120 MB minimum")

    monkeypatch.setattr(ev, "evaluate", full)
    monkeypatch.setattr(server, "prune_clones", lambda: None)

    class Stop(Exception):
        pass

    calls = {"n": 0}

    def wait(timeout=None):  # noqa: ARG001
        calls["n"] += 1
        if calls["n"] > 1:
            raise Stop

    monkeypatch.setattr(server.WAKE, "wait", wait)
    with pytest.raises(Stop):
        server.worker()
    with server.db() as c:
        job = c.execute("SELECT status, note, evaluation_id FROM jobs").fetchone()
        assert job["status"] == "failed" and job["evaluation_id"] is None
        assert job["note"].startswith("Not evaluated: the server is low on disk space")
        assert c.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0] == 0  # never a score


def test_a_prune_error_never_changes_a_completed_result(tmp_path, monkeypatch, capsys):
    skill = tmp_path / "demo"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: demo\ndescription: fixture\n---\n")
    monkeypatch.setattr(ev, "ROOT", tmp_path)
    monkeypatch.setattr(ev, "REPORTS", tmp_path / "reports")
    monkeypatch.setattr(ev, "RESULTS", tmp_path / "results.jsonl")
    monkeypatch.setattr(ev, "evaluator_version", lambda: {"commit": "e" * 40})

    def run_tier1(_skill, out, *_a, **_k):
        (out / "skillevaluator-tier1.json").write_text("{}")
        return 0, 1.0, "ok"

    def broken(*_a, **_k):
        raise PermissionError("cannot remove")

    monkeypatch.setattr(ev, "run_tier1", run_tier1)
    monkeypatch.setattr(ev, "write_public", lambda out: None)
    monkeypatch.setattr(ev, "summarize_tier1", lambda rep: {})
    monkeypatch.setattr(ev, "prune_reports", broken)
    rec = ev._evaluate(REPO, PATH, "", False, True, skill, "c" * 40)
    assert rec["status"] == "completed" and ev.load_results()[-1]["id"] == rec["id"]
    assert "PermissionError: cannot remove" in capsys.readouterr().err  # logged, not raised


def test_a_record_without_a_source_does_not_stop_pruning(store):
    tmp_path, rec = store
    results = [{"status": "completed", "report_dir": "reports/x/y"}] + [rec(f"2026111{i}T000000Z") for i in range(4)]
    assert len(ev.prune_reports(REPO, PATH, SLUG, results)) == 1


def test_keeping_zero_reports_is_refused_at_load():
    import os
    import subprocess

    env = {**os.environ, "REPORTS_KEPT_PER_SKILL": "0"}
    p = subprocess.run(
        [sys.executable, "-c", "import evaluate"], cwd=ROOT / "pipeline", env=env, capture_output=True, text=True
    )
    assert p.returncode != 0 and "REPORTS_KEPT_PER_SKILL must be 1 or more" in p.stderr
