"""The app <-> scanner handoff (#65), with a stub evaluator in place of the real image."""

import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))
sys.path.insert(0, str(ROOT / "deploy" / "scanner"))

import evaluate as ev  # noqa: E402
import scanner  # noqa: E402

STUB = """#!{python}
import json, os, sys, pathlib
if sys.argv[1] == "--version":
    print("skillevaluator 9.9.9-test"); sys.exit(0)
skill, out = pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[sys.argv.index("-o") + 1])
out.mkdir(parents=True)
body = {{"skill": skill.name, "files": sorted(p.name for p in skill.iterdir())}}
(out / "skillevaluator-tier1.json").write_text(json.dumps(body))
print("scanned", skill.name)
env = os.environ.get
print("deadlines", env("SKILLSPECTOR_MAX_WORKFLOW_SECONDS"), env("SKILLEVALUATOR_SKILLSPECTOR_TIMEOUT_SECONDS"))
"""


@pytest.fixture
def exchange(tmp_path, monkeypatch):
    inbox, outbox = tmp_path / "in", tmp_path / "out"
    inbox.mkdir()
    outbox.mkdir()
    stub = tmp_path / "skillevaluator"
    stub.write_text(STUB.format(python=sys.executable))
    stub.chmod(0o755)
    for mod, name, val in (
        (ev, "SCANNER_IN", str(inbox)),
        (ev, "SCANNER_OUT", str(outbox)),
        (ev, "SCANNER_IMAGE", "ghcr.io/example/tier1@sha256:" + "a" * 64),
        (ev, "SCANNER_POLL_S", 0.02),
        (scanner, "INBOX", inbox),
        (scanner, "OUTBOX", outbox),
        (scanner, "IMAGE", "ghcr.io/example/tier1@sha256:" + "a" * 64),
        (scanner, "POLL_S", 0.02),
        (scanner, "EVALUATOR", str(stub)),
    ):
        monkeypatch.setattr(mod, name, val)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    skill = tmp_path / "snap" / "demo-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: demo-skill\n---\n")
    return inbox, outbox, skill, tmp_path


def test_round_trip_copies_the_report_and_cleans_up(exchange):
    inbox, outbox, skill, tmp = exchange
    t = threading.Thread(target=scanner.serve, kwargs={"once": True, "max_wait_s": 10})
    t.start()
    out = tmp / "report"
    out.mkdir()
    code, secs, log = ev.run_tier1_sidecar(skill, out)
    t.join(10)
    assert code == 0 and secs >= 0 and "scanned demo-skill" in log
    assert "deadlines 630 720" in log  # SkillSpector's own deadlines reach the evaluator, inside the 900 s cap (#112)
    assert json.loads((out / "skillevaluator-tier1.json").read_text()) == {"skill": "demo-skill", "files": ["SKILL.md"]}
    assert list(inbox.iterdir()) == []
    assert [p.name for p in outbox.iterdir()] == [".identity.json"]
    ident = ev.evaluator_version()
    assert ident == {"version": "9.9.9-test", "commit": None, "image": "ghcr.io/example/tier1@sha256:" + "a" * 64}
    assert ev.evaluator_key(ident) == ident["image"]


def test_no_scanner_means_unavailable_not_a_failure(exchange, monkeypatch):
    inbox, _outbox, skill, tmp = exchange
    monkeypatch.setattr(ev, "SCANNER_PICKUP_TIMEOUT", 0.1)
    with pytest.raises(ev.ScannerUnavailable):
        ev.run_tier1_sidecar(skill, tmp)
    assert list(inbox.iterdir()) == []  # the job is withdrawn, so a late scanner can't pick up a stale one


def test_a_claimed_job_that_never_finishes_times_out(exchange, monkeypatch):
    _inbox, outbox, skill, tmp = exchange
    monkeypatch.setattr(ev, "SCANNER_PICKUP_TIMEOUT", 0.05)
    monkeypatch.setattr(ev, "TIER1_TIMEOUT", -59.8)  # the deadline is pickup + timeout + 60 s: about 0.25 s here
    real_rename = ev.os.rename

    def rename_and_claim(src, dst):
        real_rename(src, dst)
        (outbox / Path(dst).name).mkdir()  # a scanner claims it and then hangs

    monkeypatch.setattr(ev.os, "rename", rename_and_claim)
    code, _secs, log = ev.run_tier1_sidecar(skill, tmp)
    assert code == -9 and "scan timed out" in log


def _outbox_job(tmp_path):
    """An app-only file the scanner must never reach, and a finished job in an outbox."""
    secret = tmp_path / "app-only"
    secret.mkdir()
    (secret / "skillevaluator-tier1.json").write_text('{"leaked": true}')
    (secret / "result.json").write_text('{"exit_code": 0, "duration_s": 1}')
    outbox = tmp_path / "out"
    job = outbox / "job1"
    (job / "report").mkdir(parents=True)
    (job / "result.json").write_text('{"exit_code": 0, "duration_s": 2.5, "log": "ok"}')
    (job / "report" / "skillevaluator-tier1.json").write_text("{}")
    copied = tmp_path / "copied"
    copied.mkdir()
    return secret, outbox, job, copied


def test_only_the_named_regular_report_files_are_copied(tmp_path):
    secret, outbox, job, copied = _outbox_job(tmp_path)
    (job / "report" / "skillevaluator-tier1.md").symlink_to(secret / "skillevaluator-tier1.json")
    (job / "report" / "extra.txt").write_text("not a report file the app uses")
    assert ev.read_job(outbox, "job1", copied, 9.0) == (0, 2.5, "ok")
    assert sorted(p.name for p in copied.iterdir()) == ["skillevaluator-tier1.json"]
    assert (copied / "skillevaluator-tier1.json").read_text() == "{}"


@pytest.mark.parametrize("swap", ["report", "job", "result"])
def test_a_linked_path_component_is_never_followed(tmp_path, swap):
    secret, outbox, job, copied = _outbox_job(tmp_path)
    if swap == "report":
        shutil.rmtree(job / "report")
        (job / "report").symlink_to(secret)
    elif swap == "job":
        shutil.rmtree(job)
        job.symlink_to(secret)
    else:
        (job / "result.json").unlink()
        (job / "result.json").symlink_to(secret / "result.json")
    code, _secs, _log = ev.read_job(outbox, "job1", copied, 9.0)
    assert list(copied.iterdir()) == []
    assert code == (0 if swap == "report" else -1)


def test_a_link_swapped_in_after_the_listing_is_not_followed(tmp_path, monkeypatch):
    """The race #101's review found: the file is replaced by a link just before the app opens it."""
    secret, outbox, job, copied = _outbox_job(tmp_path)
    real_open = os.open

    def racing_open(name, flags, *args, dir_fd=None, **kw):
        if name == "skillevaluator-tier1.json" and dir_fd is not None:
            target = job / "report" / name
            target.unlink()
            target.symlink_to(secret / name)
        return real_open(name, flags, *args, dir_fd=dir_fd, **kw)

    monkeypatch.setattr(ev.os, "open", racing_open)
    ev.read_job(outbox, "job1", copied, 9.0)
    assert list(copied.iterdir()) == []


def test_a_fifo_or_oversized_report_is_skipped(tmp_path, monkeypatch):
    _secret, outbox, job, copied = _outbox_job(tmp_path)
    os.mkfifo(job / "report" / "skillevaluator-tier1.md")
    (job / "report" / "skillevaluator-tier1.html").write_text("x" * 100)
    monkeypatch.setattr(ev, "MAX_REPORT_BYTES", 50)
    ev.read_job(outbox, "job1", copied, 9.0)
    assert sorted(p.name for p in copied.iterdir()) == ["skillevaluator-tier1.json"]


def test_a_timeout_kills_grandchildren_holding_the_pipes(tmp_path, monkeypatch):
    """A grandchild that keeps stdout open must not hang the scanner past its cap (#101 review)."""
    monkeypatch.setattr(scanner, "TIMEOUT", 1)
    t0 = time.monotonic()
    code, log = scanner.run_capped(["sh", "-c", "sleep 60 & sleep 60"], tmp_path)
    assert code == -9 and "timed out" in log
    assert time.monotonic() - t0 < 15


def test_a_finished_scan_leaves_no_process_behind(tmp_path):
    marker = tmp_path / "alive"
    code, _log = scanner.run_capped(["sh", "-c", f"(sleep 2; touch {marker}) >/dev/null 2>&1 & echo started"], tmp_path)
    assert code == 0
    time.sleep(3)
    assert not marker.exists()


def test_the_watchdog_exits_the_process(monkeypatch):
    fired = threading.Event()
    monkeypatch.setattr(scanner.os, "_exit", lambda code: fired.set())
    scanner.start_watchdog(0.05)
    assert fired.wait(5)


def test_stale_exchange_entries_are_swept(exchange):
    inbox, outbox, _skill, _tmp = exchange
    (outbox / ".identity.json").write_text("{}")
    for d in (inbox / "old", outbox / "old", outbox / "fresh"):
        d.mkdir()
    for d in (inbox / "old", outbox / "old", outbox / ".identity.json"):
        os.utime(d, (time.time() - 7200, time.time() - 7200))
    assert sorted(ev.sweep_exchange(3600)) == ["old", "old"]
    assert sorted(p.name for p in outbox.iterdir()) == [".identity.json", "fresh"]


def test_a_bad_job_is_reported_not_left_claimed(exchange):
    inbox, outbox, _skill, _tmp = exchange
    (inbox / "bad").mkdir()
    (inbox / "bad" / "job.json").write_text("{not json")
    assert scanner.serve(once=True, max_wait_s=5)
    result = json.loads((outbox / "bad" / "result.json").read_text())
    assert result["exit_code"] == -1 and (outbox / "bad" / "done").exists()


def test_worker_requeues_when_the_scanner_is_unavailable(tmp_path, monkeypatch):
    import server

    monkeypatch.setattr(server, "DB_PATH", tmp_path / "inventory.db")
    server.init_db()
    with server.db() as c:
        c.execute(
            "INSERT INTO skills VALUES (?,?,?,?,?,?,?,?)",
            ("example/repo:skills/demo", "example/repo", "skills/demo", "demo", "Fixture", "docs", "fixture", 0),
        )
        c.execute(
            "INSERT INTO jobs (skill_id, status, created_at) VALUES (?, 'queued', 0)", ("example/repo:skills/demo",)
        )

    def unavailable(*_a, **_k):
        raise ev.ScannerUnavailable("No scanner picked up the job; it stays queued.")

    monkeypatch.setattr(ev, "evaluate", unavailable)
    monkeypatch.setattr(server, "prune_clones", lambda: None)

    class Stop(Exception):
        pass

    calls = {"n": 0}

    def wait(timeout=None):  # noqa: ARG001
        calls["n"] += 1
        if calls["n"] > 1:
            raise Stop

    monkeypatch.setattr(server.WAKE, "wait", wait)
    monkeypatch.setattr(server, "SCANNER_RETRY_S", 0)
    with pytest.raises(Stop):
        server.worker()
    with server.db() as c:
        job = c.execute("SELECT status, note, started_at, finished_at FROM jobs").fetchone()
        assert job["status"] == "queued" and job["note"] == server.WAITING_NOTE
        assert job["started_at"] is None and job["finished_at"] is None
        assert c.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0] == 0


def test_the_minimum_life_is_spent_before_the_claim(exchange):
    """No job is claimed during the scanner's minimum life, so nothing waits idle after a scan (#101 review)."""
    inbox, outbox, skill, tmp = exchange
    job = inbox / "job1"
    (job / "skill").mkdir(parents=True)
    shutil.copytree(skill, job / "skill" / skill.name)
    (job / "job.json").write_text(json.dumps({"name": skill.name}))
    t = threading.Thread(
        target=scanner.serve, kwargs={"once": True, "max_wait_s": 10, "not_before": time.monotonic() + 1}
    )
    t.start()
    time.sleep(0.5)
    assert not (outbox / "job1").exists()
    t.join(15)
    assert (outbox / "job1" / "done").exists()


def _seed_job(server, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DB_PATH", tmp_path / "inventory.db")
    server.init_db()
    with server.db() as c:
        c.execute(
            "INSERT INTO skills VALUES (?,?,?,?,?,?,?,?)",
            ("example/repo:skills/demo", "example/repo", "skills/demo", "demo", "Fixture", "docs", "fixture", 0),
        )
        c.execute(
            "INSERT INTO jobs (skill_id, status, created_at) VALUES (?, 'queued', 0)", ("example/repo:skills/demo",)
        )
    monkeypatch.setattr(server, "prune_clones", lambda: None)


def test_a_job_is_running_only_once_the_scan_starts(tmp_path, monkeypatch):
    """#103: the worker leaves the job queued until evaluate() reports the scan started, then running."""
    import server

    _seed_job(server, tmp_path, monkeypatch)
    seen = []

    def state():
        with server.db() as c:
            return c.execute("SELECT status FROM jobs").fetchone()["status"]

    def evaluate(*_a, on_start=None, **_k):
        seen.append(state())
        on_start()
        seen.append(state())
        raise ValueError("stop here")

    monkeypatch.setattr(ev, "evaluate", evaluate)

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
    assert seen == ["queued", "running"]


def test_the_sidecar_reports_the_start_when_the_scanner_claims(exchange):
    inbox, outbox, skill, tmp = exchange
    starts = []

    def on_start():
        starts.append(sorted(p.name for p in outbox.iterdir() if not p.name.startswith(".")))

    t = threading.Thread(target=scanner.serve, kwargs={"once": True, "max_wait_s": 10})
    t.start()
    out = tmp / "report-out"
    out.mkdir()
    code, _secs, _log = ev.run_tier1_sidecar(skill, out, on_start)
    t.join(10)
    assert code == 0 and len(starts) == 1 and len(starts[0]) == 1  # called once, after the claim


def test_no_scanner_means_no_start(exchange, monkeypatch):
    _inbox, _outbox, skill, tmp = exchange
    monkeypatch.setattr(ev, "SCANNER_PICKUP_TIMEOUT", 0.2)
    starts = []
    with pytest.raises(ev.ScannerUnavailable):
        ev.run_tier1_sidecar(skill, tmp, lambda: starts.append(1))
    assert starts == []


@pytest.mark.parametrize("timeout", [120, 600, 900, 1200])
def test_skillspector_deadlines_end_inside_the_cap_and_match(timeout):
    """SkillSpector's own deadline ends a slow scan before its watchdog, and both before our cap (#112)."""
    d = scanner.skillspector_deadlines(timeout)
    assert d == ev.skillspector_deadlines(timeout)
    assert int(d["SKILLSPECTOR_MAX_WORKFLOW_SECONDS"]) < int(d["SKILLEVALUATOR_SKILLSPECTOR_TIMEOUT_SECONDS"]) < timeout
