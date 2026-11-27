"""#121 PR B: the internal live pilot's runner."""

import json
import os
import pwd
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))
import evaluate as ev  # noqa: E402
import live_pilot as pilot  # noqa: E402
import tier3  # noqa: E402

REAL_WAIT_FOR_PROXY = tier3.wait_for_proxy


@pytest.fixture(autouse=True)
def proxy_up(monkeypatch):
    """No test here talks to a real proxy; the wait itself is tested with REAL_WAIT_FOR_PROXY."""
    monkeypatch.setattr(tier3, "wait_for_proxy", lambda *a, **k: True)


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
    d = tmp_path / "out" / "demo" / "20261112_000000_1_abc" / agent  # <skill>/<run>/<agent>, as SkillEvaluator writes
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


def test_a_failed_run_records_no_scores(tmp_path):
    out = ev.ROOT / "reports" / "pilot-test-out"
    res = {"exit": 1, "cases": "starter", "out": out, "commit": "c" * 40, "tree_digest": "sha256:0", "duration_s": 1.0}
    row = {"repo": "a/b", "skill_path": "s", "category": "c", "task_set": None, "repeats": 1}
    ev_ = {"version": "x", "commit": "y"}
    rec = pilot.record(row, 1, "pilot-r", res, {}, None, "2026-11-12T00:00:00+00:00", ev_, egress="open")
    assert rec["status"] == "failed" and rec["scores"] is None
    assert rec["mode"] == "tier3" and rec["pilot"] is True and ev.is_pilot(rec)
    assert rec["cost_usd"] is None  # unbilled trial key: not a zero
    assert rec["evaluator"] == {"version": "x", "commit": "y"}  # as read before the trials (#200)
    assert rec["egress"] == "open"  # what the trial could reach (#236)


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
    evaluator = {"version": "x", "commit": "y"}
    rec = pilot.crashed(
        row,
        1,
        "pilot-r",
        "CalledProcessError: clone failed",
        None,
        "2026-11-12T00:00:00+00:00",
        evaluator,
        egress="allowlist",
    )
    assert (rec["status"], rec["scores"], rec["mode"]) == ("failed", None, "tier3")
    assert rec["evaluator"] == evaluator  # provenance as complete as a finished run's
    assert rec["egress"] == "allowlist"
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

    monkeypatch.setattr(tier3, "evaluator_version", lambda: {"version": "x", "commit": "y"})
    monkeypatch.setattr(pilot, "egress_mode", lambda: "open")
    monkeypatch.setattr(pilot, "issue_token", down)
    assert pilot.main([]) == 2
    assert "could not issue a gateway token" in capsys.readouterr().err
    assert not (tmp_path / "results.jsonl").exists()


def test_no_evaluator_stops_the_pilot_before_any_token_or_trial(tmp_path, monkeypatch, capsys):
    """#200: the host run looked for SkillEvaluator at the workstation default and failed. It must stop up front."""
    row = {"repo": "a/b", "skill_path": "s", "category": "c", "task_set": None, "repeats": 1}
    monkeypatch.setattr(pilot, "load_skills", lambda: [row])
    monkeypatch.setattr(pilot, "RECORDS", tmp_path / "results.jsonl")
    monkeypatch.setattr(ev, "EVALUATOR_DIR", tmp_path / "missing")
    issued = []
    monkeypatch.setattr(pilot, "issue_token", lambda *a, **k: issued.append(a))
    assert pilot.main([]) == 2
    err = capsys.readouterr().err
    assert "no SkillEvaluator at" in err and "SKILLEVALUATOR_DIR" in err
    assert issued == [] and not (tmp_path / "results.jsonl").exists()


def test_the_evaluator_version_comes_from_its_venv_and_checkout_not_uv(tmp_path, monkeypatch):
    d = tmp_path / "se"
    (d / ".venv" / "bin").mkdir(parents=True)
    exe = d / ".venv" / "bin" / "skillevaluator"
    exe.write_text("#!/bin/sh\necho 'skillevaluator 0.8.0'\n")
    exe.chmod(0o755)
    subprocess.run(["git", "init", "-q", str(d)], check=True, timeout=30)
    subprocess.run(
        [
            "git",
            "-C",
            str(d),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "x",
        ],
        check=True,
        timeout=30,
    )
    head = subprocess.run(["git", "-C", str(d), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # no uv to fall back on
    assert tier3.evaluator_version(d) == {"version": "0.8.0", "commit": head}


def test_a_failed_revoke_is_warned_about(monkeypatch, capsys):
    monkeypatch.setattr(pilot, "gateway", lambda *a, **k: subprocess.CompletedProcess(a, 1, "", ""))
    assert pilot.revoke("pilot-r") is False
    assert "not revoked" in capsys.readouterr().err


def test_a_refused_trial_dockerfile_is_unsupported_not_failed():
    got, reason = pilot.status_of(1, {}, unsupported=True)
    assert got == "unsupported" and "custom environment" in reason
    assert pilot.status_of(1, {}, unsupported=False)[0] == "failed"


TRIAL = r"""
r=$1; skill=$2; seen=$3
echo "$r $TMPDIR $skill $EVALSKILLS_UNSUPPORTED_FILE" > "$seen/paths"
echo "$XDG_CONFIG_HOME $XDG_DATA_HOME $XDG_STATE_HOME" > "$seen/xdg"
mkdir -p "$XDG_STATE_HOME/skillevaluator" && echo key > "$XDG_STATE_HOME/skillevaluator/output-provenance.key"
mkdir -p "$r/_harbor-jobs/t"
echo real > "$r/_harbor-jobs/t/real.txt"
ln -s /etc/passwd "$r/_harbor-jobs/planted"
ln -s /etc "$r/_harbor-jobs/dirlink"
mkfifo "$r/_harbor-jobs/fifo"
echo x > "$r/suid" && chmod 4755 "$r/suid"
[ -n "$MARK" ] && echo '{}' > "$EVALSKILLS_UNSUPPORTED_FILE"
sleep 300 &
echo $! > "$seen/grandchild"
echo hello from the trial
eval "sleep $NAP"
"""


@pytest.fixture
def trial(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "skills" / "demo").mkdir(parents=True)
    (repo / "skills" / "demo" / "SKILL.md").write_text("---\nname: demo\ndescription: fixture\n---\n")
    run_root, seen = tmp_path / "run", tmp_path / "seen"
    run_root.mkdir()
    seen.mkdir()
    monkeypatch.setattr(ev, "checkout", lambda r, ref=None: (repo, "c" * 40))
    monkeypatch.setattr(ev, "REPORTS", tmp_path / "reports")
    monkeypatch.setattr(ev, "EVALUATOR_DIR", tmp_path)
    monkeypatch.setattr(tier3, "TRIAL_USER", pwd.getpwuid(os.geteuid()).pw_name)
    monkeypatch.setattr(tier3, "DAEMON_SOCKETS", (tmp_path / "no-daemon.sock",))  # CI's runner is in group docker
    monkeypatch.setattr(tier3, "MIN_FREE_ROOT", 0)  # a CI runner's / may hold less than the host's minimum
    monkeypatch.setattr(tier3, "command", lambda sk, res, *_: ["sh", "-c", TRIAL, "sh", str(res), str(sk), str(seen)])

    def go(nap="0", mark="", timeout=60, should_stop=lambda: False):
        monkeypatch.setattr(tier3, "TIER3_TIMEOUT", timeout)
        removed = []
        env = {"PATH": os.environ["PATH"], "NAP": nap, "MARK": mark}
        clean = lambda: removed.append(1) or []  # noqa: E731
        res = tier3.run("a/b", "skills/demo", None, env, run_root=run_root, cleanup=clean, should_stop=should_stop)
        return res, removed

    return {"go": go, "run_root": run_root, "seen": seen, "reports": tmp_path / "reports"}


def gone(pid: int) -> bool:
    try:
        state = next(ln for ln in Path(f"/proc/{pid}/status").read_text().splitlines() if ln.startswith("State:"))
    except FileNotFoundError:
        return True
    return "Z" in state  # dead, waiting for its new parent to reap it


def test_a_trial_runs_in_a_fresh_folder_and_root_copies_only_plain_files_out(trial):
    res, removed = trial["go"]()
    results, tmp, skill, mark = (Path(x) for x in (trial["seen"] / "paths").read_text().split())
    work = results.parent
    assert work.parent == trial["run_root"] and tmp.parent == work and skill.is_relative_to(work)
    assert mark.parent.parent == work and mark.parent not in (results, tmp)  # no container is bound there
    xdg = [Path(x) for x in (trial["seen"] / "xdg").read_text().split()]
    assert [x.name for x in xdg] == ["config", "data", "state"]  # #186: the trial's own, not the shared home's
    assert all(x.parent.parent == work and x.parent not in (results, tmp) for x in xdg)  # no container is bound there
    assert list(trial["run_root"].iterdir()) == []  # nothing of the trial stays where the next trial's binds come from
    assert gone(int((trial["seen"] / "grandchild").read_text()))  # its session was killed with it
    assert removed == [1]  # containers and networks are always cleaned up
    out = res["out"]
    assert out.is_relative_to(trial["reports"]) and res["exit"] == 0 and res["unsupported"] is False
    assert (out / "_harbor-jobs" / "t" / "real.txt").read_text() == "real\n"
    assert not any(p.is_symlink() for p in out.rglob("*"))
    assert oct((out / "suid").stat().st_mode & 0o7777) == "0o600"  # never a setuid file, root-only (#256)
    skipped = {d["path"]: d["why"] for d in json.loads((out / "skipped.json").read_text())}
    assert skipped == {"_harbor-jobs/planted": "link", "_harbor-jobs/dirlink": "link", "_harbor-jobs/fifo": "fifo"}
    assert "hello from the trial" in (out / "run.log").read_text()


def test_a_timed_out_trial_is_stopped_with_everything_it_started(trial):
    res, removed = trial["go"](nap="30", timeout=1)
    assert res["exit"] is None and res["duration_s"] < 10
    assert gone(int((trial["seen"] / "grandchild").read_text()))
    assert removed == [1]
    assert (res["out"] / "run.log").read_text().startswith("timed out after 1 s")
    assert list(trial["run_root"].iterdir()) == []


def test_a_trial_that_floods_its_log_is_stopped_and_its_log_capped(trial, monkeypatch):
    monkeypatch.setattr(tier3, "MAX_LOG_BYTES", 10_000)
    res, _ = trial["go"](nap="0; yes flood | head -c 1000000; sleep 30")
    assert res["exit"] is None and res["stopped"] == "stopped: its output passed 10000 bytes"
    assert len((res["out"] / "run.log").read_bytes()) < 10_200
    assert pilot.status_of(res["exit"], {}, stopped=res["stopped"]) == ("failed", res["stopped"])


def test_a_trial_user_who_can_reach_a_docker_daemon_is_refused(tmp_path, monkeypatch):
    import socket

    sock = Path("/tmp") / f"t3dk-{os.getpid()}.sock"
    s = socket.socket(socket.AF_UNIX)
    s.bind(str(sock))
    s.listen()
    try:
        monkeypatch.setattr(tier3, "DAEMON_SOCKETS", (sock,))
        me = pwd.getpwuid(os.geteuid()).pw_name
        os.chmod(sock, 0o600)  # ours: a real connect works
        with pytest.raises(tier3.Unsafe, match="can open"):
            tier3.trial_user(me)
        os.chmod(sock, 0o400)  # the connect is refused
        assert tier3.trial_user(me).pw_uid == os.geteuid()
        s.close()  # bound, not listening: a stopped daemon refuses, but the user could still open it
        os.chmod(sock, 0o600)
        with pytest.raises(tier3.Unsafe, match="can open"):
            tier3.trial_user(me)
        monkeypatch.setattr(tier3, "DAEMON_SOCKETS", (tmp_path / "absent.sock",))
        assert tier3.trial_user(me).pw_uid == os.geteuid()
    finally:
        s.close()
        sock.unlink()


def test_no_file_of_the_trial_grows_past_the_hard_cap(trial, monkeypatch):
    monkeypatch.setattr(tier3, "MAX_FILE_BYTES", 50_000)
    res, _ = trial["go"](nap="0; head -c 200000 /dev/zero > big; echo size=$(wc -c < big)")
    assert "size=50000" in (res["out"] / "run.log").read_text()  # the writer got SIGXFSZ at the cap


def test_unsupported_comes_from_the_environment_mark_not_from_the_log(trial):
    res, _ = trial["go"](mark="1")
    assert res["unsupported"] is True


def test_a_surviving_trial_process_stops_everything_and_keeps_the_folder(trial, monkeypatch):
    monkeypatch.setattr(tier3, "stop_trial", lambda p, uid: (_ for _ in ()).throw(tier3.Unsafe("survived")))
    with pytest.raises(tier3.Unsafe, match="survived"):
        trial["go"]()
    assert len(list(trial["run_root"].iterdir())) == 1  # root does not read or delete a live trial's folder
    os.kill(int((trial["seen"] / "grandchild").read_text()), 9)


def test_leftover_containers_and_networks_are_removed_through_the_proxy(tmp_path):
    import socketserver
    import threading
    from http.server import BaseHTTPRequestHandler

    calls = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def address_string(self):
            return "unix"

        def reply(self, code, doc):
            body = json.dumps(doc).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            calls.append(("GET", self.path))
            if self.path.startswith("/containers"):
                return self.reply(200, [{"Id": "a" * 64}])
            if self.path.startswith("/images"):
                return self.reply(
                    200,
                    [
                        {"Id": "sha256:" + "c" * 64, "RepoTags": ["evalskills-trial:0123456789abcdef"]},
                        {"Id": "sha256:" + "d" * 64, "RepoTags": None},  # untagged, but built by the proxy
                        {"Id": "sha256:" + "e" * 64, "RepoTags": ["evalskills/trial-base:1"]},  # never removed
                        {"Id": "sha256:" + "f" * 64, "RepoTags": ["evalskills-trial:abc", "x:1"]},
                    ],
                )
            self.reply(200, [{"Id": "n" * 64, "Name": "t3_default"}, {"Id": "b" * 64, "Name": "bridge"}])

        def do_DELETE(self):
            calls.append(("DELETE", self.path))
            self.reply(500 if self.path.startswith("/networks") else 204, {})

    sock = Path("/tmp") / f"t3rm-{os.getpid()}.sock"
    srv = socketserver.UnixStreamServer(str(sock), H)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        left = tier3.remove_trial_objects(f"unix://{sock}")
    finally:
        srv.shutdown()
        sock.unlink()
    assert ("DELETE", f"/containers/{'a' * 64}?force=1&v=1") in calls
    assert ("DELETE", f"/networks/{'n' * 64}") in calls and not any("b" * 64 in c[1] for c in calls)
    assert left == ["network " + "n" * 12]
    assert ("GET", "/images/json") in calls  # the proxy filters this listing to owner-labelled images
    images = [c[1] for c in calls if c[0] == "DELETE" and c[1].startswith("/images/")]
    assert images == [f"/images/sha256:{'c' * 64}?force=1", f"/images/sha256:{'d' * 64}?force=1"]
    assert calls.index(("GET", "/images/json")) > max(i for i, c in enumerate(calls) if c[1].startswith("/networks"))
    absent = tier3.remove_trial_objects(f"unix://{tmp_path}/absent.sock")
    assert absent == ["container list", "network list", "image list"]


def test_lift_is_never_read_through_a_link(tmp_path):
    (tmp_path / "real" / "opencode").mkdir(parents=True)
    (tmp_path / "real" / "opencode" / "lift.json").write_text('{"execution_status": "succeeded"}')
    (tmp_path / "out" / "demo").mkdir(parents=True)
    (tmp_path / "out" / "demo" / "x").symlink_to(tmp_path / "real")
    assert pilot.read_lift(tmp_path / "out") == {}


def test_the_trial_user_is_never_root_and_must_be_who_runs_it(monkeypatch):
    with pytest.raises(tier3.Unsafe, match="may not be root"):
        tier3.trial_user("root")
    with pytest.raises(tier3.Unsafe, match="no trial user"):
        tier3.trial_user("no-such-user-evalskills")
    if os.geteuid() != 0:
        other = next(u for u in pwd.getpwall() if u.pw_uid not in (0, os.geteuid()))
        with pytest.raises(tier3.Unsafe, match="run as root or as"):
            tier3.trial_user(other.pw_name)


def test_the_run_root_must_be_a_real_folder_of_the_trial_user(tmp_path):
    tier3.check_run_root(tmp_path, os.geteuid())
    with pytest.raises(tier3.Unsafe, match="belong to the trial user"):
        tier3.check_run_root(tmp_path, os.geteuid() + 1)
    (tmp_path / "link").symlink_to(tmp_path)
    for bad in (tmp_path / "link", tmp_path / "absent"):
        with pytest.raises(tier3.Unsafe, match="real folder"):
            tier3.check_run_root(bad, os.geteuid())


def test_skillevaluator_runs_from_its_venv_not_through_uv(tmp_path):
    cmd = tier3.command(tmp_path, tmp_path / "out", "a/b", "c" * 40)
    assert cmd[0] == str(ev.EVALUATOR_DIR / ".venv" / "bin" / "skillevaluator")
    assert cmd[cmd.index("--env-mode") + 1] == "gvisor"


def test_harbor_job_folders_are_kept_for_the_private_report(tmp_path):
    """#254: without the flag SkillEvaluator deletes _harbor-jobs, and with it each trial's verifier output."""
    assert "--harbor-keep-jobs" in tier3.command(tmp_path, tmp_path / "out", "a/b", "c" * 40)


def test_as_root_a_trial_keeps_the_root_owned_home_and_its_own_xdg_folders(tmp_path, monkeypatch):
    monkeypatch.setattr(tier3.os, "geteuid", lambda: 0)
    me = pwd.getpwuid(os.getuid())
    shared = {"PATH": "/usr/bin", "HOME": "/root", "XDG_CACHE_HOME": "/root/.cache", "XDG_STATE_HOME": "/root/.local"}
    env = tier3._trial_env_for(shared, me, tmp_path / "tmp", tmp_path / "xdg")
    assert env["HOME"] == me.pw_dir and env["TMPDIR"] == str(tmp_path / "tmp")  # ~/.docker/config.json stays root's
    assert "XDG_CACHE_HOME" not in env
    for name, sub in tier3.XDG_DIRS.items():
        assert env[name] == str(tmp_path / "xdg" / sub)  # never the runner's, never the shared home's ~/.local


@pytest.mark.parametrize("outcome, can", [(3, False), (0, True), (1, True), (-9, True), ("timeout", True)])
def test_as_root_only_a_permission_error_in_the_child_means_it_cannot_connect(tmp_path, monkeypatch, outcome, can):
    sock = tmp_path / "d.sock"
    sock.touch()
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw)
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(cmd, 15)
        return subprocess.CompletedProcess(cmd, outcome)

    monkeypatch.setattr(tier3.os, "geteuid", lambda: 0)
    monkeypatch.setattr(tier3.subprocess, "run", fake_run)
    me = pwd.getpwuid(os.getuid())
    assert tier3._can_connect(sock, me) is can
    assert seen["user"] == me.pw_uid and seen["group"] == me.pw_gid  # tried as the trial user


@pytest.mark.parametrize(
    "config,mode", [({"open_egress": True}, "open"), ({"open_egress": False}, "allowlist"), ({}, "allowlist")]
)
def test_the_egress_mode_is_read_from_the_running_proxy(monkeypatch, config, mode):
    seen = []

    def fake_run(cmd, **kw):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"allowed_hosts": ["x.org"], **config}), "")

    monkeypatch.setattr(pilot.subprocess, "run", fake_run)
    assert pilot.egress_mode() == mode
    assert seen[0][-3:] == ["evalskills-egress-proxy", "cat", "/etc/evalskills/egress-proxy.json"]


def test_an_unreadable_egress_config_stops_the_pilot_before_any_token(tmp_path, monkeypatch, capsys):
    row = {"repo": "a/b", "skill_path": "s", "category": "c", "task_set": None, "repeats": 1}
    monkeypatch.setattr(pilot, "load_skills", lambda: [row])
    monkeypatch.setattr(pilot, "RECORDS", tmp_path / "results.jsonl")
    monkeypatch.setattr(tier3, "evaluator_version", lambda: {"version": "x", "commit": "y"})

    def down():
        raise subprocess.CalledProcessError(1, "docker")

    issued = []
    monkeypatch.setattr(pilot, "egress_mode", down)
    monkeypatch.setattr(pilot, "issue_token", lambda *a, **k: issued.append(a))
    assert pilot.main([]) == 2
    assert "could not read the egress proxy's config" in capsys.readouterr().err
    assert issued == [] and not (tmp_path / "results.jsonl").exists()


def smoke_pilot(tmp_path, monkeypatch, argv, refuse=False, during=None, code=0):
    """pilot.main with everything outside it faked; returns the tier3.run calls and the records written."""
    rows = [
        {"repo": "a/b", "skill_path": "s", "category": "c", "task_set": "quixbugs-python", "repeats": 3},
        {"repo": "c/d", "skill_path": "t", "category": "c", "task_set": None, "repeats": 1},
    ]
    monkeypatch.setattr(pilot, "load_skills", lambda: rows)
    monkeypatch.setattr(pilot, "RECORDS", tmp_path / "results.jsonl")
    monkeypatch.setattr(tier3, "evaluator_version", lambda: {"version": "x", "commit": "y"})
    monkeypatch.setattr(pilot, "egress_mode", lambda: "open")
    monkeypatch.setattr(pilot, "issue_token", lambda run_id, f: f.write_text("synthetic-not-a-token"))
    monkeypatch.setattr(pilot, "revoke", lambda run_id: True)
    monkeypatch.setattr(pilot, "usage", lambda run_id: {"requests": 0})
    monkeypatch.setattr(pilot, "read_lift", lambda out: {})
    monkeypatch.setattr(tier3, "trial_env", lambda env: env)
    calls = []

    def fake_run(repo, skill_path, task_set, env, label=None, max_cases=None, should_stop=lambda: False):
        if during:
            during(len(calls))
        calls.append({"repo": repo, "label": label, "max_cases": max_cases, "stop_now": should_stop()})
        if refuse:
            raise tier3.NoSmokeCase("smoke: can't take one case from evals.jsonl")
        out = pilot.ev.ROOT / "reports" / "x"
        return {"exit": 1, "cases": "shared", "out": out, "commit": "c" * 40, "tree_digest": "d", "duration_s": 1.0}

    monkeypatch.setattr(tier3, "run", fake_run)
    assert pilot.main(argv) == code
    recs = [json.loads(line) for line in (tmp_path / "results.jsonl").read_text().splitlines()]
    return calls, recs


def test_a_smoke_run_is_one_run_per_skill_on_its_first_case(tmp_path, monkeypatch):
    """#257: the fast check before every full run. Repeats are ignored, each run gets one case, and both the run ID
    and the record say smoke, so it is never read as a pilot measurement."""
    calls, recs = smoke_pilot(tmp_path, monkeypatch, ["--smoke"])
    assert [c["repo"] for c in calls] == ["a/b", "c/d"]  # one each, though a/b repeats 3 times
    assert all(c["max_cases"] == 1 for c in calls)
    assert all(c["label"].startswith("smoke-") for c in calls)
    assert all(r["smoke"] is True and r["id"].startswith("smoke-") for r in recs)


def test_a_full_run_keeps_every_repeat_and_case(tmp_path, monkeypatch):
    calls, recs = smoke_pilot(tmp_path, monkeypatch, [])
    assert [c["repo"] for c in calls] == ["a/b"] * 3 + ["c/d"]
    assert all(c["max_cases"] is None and c["label"].startswith("pilot-") for c in calls)
    assert all(r["smoke"] is False for r in recs)


def test_the_dry_run_shows_what_a_smoke_run_would_do(tmp_path, monkeypatch, capsys):
    rows = [{"repo": "a/b", "skill_path": "s", "category": "c", "task_set": "quixbugs-python", "repeats": 3}]
    monkeypatch.setattr(pilot, "load_skills", lambda: rows)
    assert pilot.main(["--smoke", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "(first 1 only)" in out and out.strip().endswith("1 runs, smoke: one case each, both arms")


def test_a_smoke_run_that_cant_cut_the_cases_is_recorded_failed_not_widened(tmp_path, monkeypatch):
    calls, recs = smoke_pilot(tmp_path, monkeypatch, ["--smoke"], refuse=True)
    assert len(calls) == 2  # each skill tried once, never retried on every case
    for r in recs:
        assert r["status"] == "failed" and r["scores"] is None and r["smoke"] is True
        assert r["reason"].startswith("NoSmokeCase: smoke: can't take one case")


def test_a_trial_the_operator_stops_is_contained_and_recorded_failed(trial):
    """#259: a second Ctrl-C or SIGTERM ends the trial like a timeout does, never with a score."""
    res, removed = trial["go"](nap="30", should_stop=lambda: True)
    assert res["exit"] is None and res["stopped"] == tier3.STOPPED_BY_OPERATOR and res["duration_s"] < 10
    assert gone(int((trial["seen"] / "grandchild").read_text()))
    assert removed == [1] and list(trial["run_root"].iterdir()) == []
    assert (res["out"] / "run.log").read_text().startswith("stopped by the operator")
    assert pilot.status_of(res["exit"], {}, stopped=res["stopped"]) == ("failed", "stopped by the operator")


def test_one_ctrl_c_finishes_the_current_run_and_starts_no_other(tmp_path, monkeypatch, capsys):
    import signal

    before = signal.getsignal(signal.SIGINT)
    calls, recs = smoke_pilot(
        tmp_path, monkeypatch, [], during=lambda n: n == 1 and os.kill(os.getpid(), signal.SIGINT), code=130
    )
    assert len(calls) == 2 and len(recs) == 2  # the second run finished and was recorded; runs 3 and 4 never began
    assert not any(c["stop_now"] for c in calls)
    assert "2 run(s) not started" in capsys.readouterr().err
    assert signal.getsignal(signal.SIGINT) is before  # the pilot's handler is gone with it


@pytest.mark.parametrize("signals", [["SIGINT", "SIGINT"], ["SIGTERM"]])
def test_a_second_ctrl_c_or_sigterm_stops_the_current_trial_now(tmp_path, monkeypatch, signals):
    import signal

    def send(n):
        if n == 0:
            for name in signals:
                os.kill(os.getpid(), getattr(signal, name))

    calls, recs = smoke_pilot(tmp_path, monkeypatch, [], during=send, code=130)
    assert len(calls) == 1 and calls[0]["stop_now"] is True and len(recs) == 1


def test_a_pilot_is_refused_while_another_holds_the_lock(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(pilot, "RECORDS", tmp_path / "pilot" / "results.jsonl")
    monkeypatch.setattr(pilot, "load_skills", lambda: [{"repo": "a/b", "skill_path": "s", "repeats": 1}])
    monkeypatch.setattr(tier3, "evaluator_version", lambda: {"version": "x", "commit": "y"})
    held = pilot.take_lock()
    try:
        assert pilot.main([]) == 2 and "holds the lock" in capsys.readouterr().err
        monkeypatch.setattr(pilot.os, "geteuid", lambda: 0)
        assert pilot.main(["--cleanup"]) == 2 and "cleanup refused" in capsys.readouterr().err
    finally:
        os.close(held)


def as_root_cleanup(tmp_path, monkeypatch):
    monkeypatch.setattr(pilot, "RECORDS", tmp_path / "pilot" / "results.jsonl")
    monkeypatch.setattr(pilot.os, "geteuid", lambda: 0)
    monkeypatch.setattr(tier3, "TRIAL_USER", pwd.getpwuid(os.getuid()).pw_name)
    monkeypatch.setattr(tier3, "kill_trial_user", lambda uid: True)
    monkeypatch.setattr(tier3, "remove_trial_objects", lambda: [])
    monkeypatch.setattr(pilot, "gateway", lambda *a, **k: subprocess.CompletedProcess(a, 0, "[]", ""))


def test_cleanup_is_root_only(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(pilot, "RECORDS", tmp_path / "pilot" / "results.jsonl")
    monkeypatch.setattr(pilot.os, "geteuid", lambda: 1000)
    (tmp_path / "run" / "t3-a").mkdir(parents=True)
    assert pilot.cleanup(tmp_path / "run") == 2 and "as root" in capsys.readouterr().err
    assert (tmp_path / "run" / "t3-a").is_dir()


def test_a_run_folder_cleanup_cannot_remove_is_reported_not_a_traceback(tmp_path, monkeypatch, capsys):
    as_root_cleanup(tmp_path, monkeypatch)
    (tmp_path / "run" / "t3-a").mkdir(parents=True)

    def busy(path, *a, **k):
        raise OSError(16, "busy")

    monkeypatch.setattr(pilot.shutil, "rmtree", busy)
    assert pilot.cleanup(tmp_path / "run") == 1
    out = json.loads(capsys.readouterr().out)
    assert out["problems"] == ["run folder t3-a not removed (OSError)"] and out["run_folders_removed"] == 0


def test_cleanup_revokes_open_tokens_and_removes_only_real_run_folders(tmp_path, monkeypatch, capsys):
    as_root_cleanup(tmp_path, monkeypatch)
    run_root = tmp_path / "run"
    (run_root / "t3-a" / "results").mkdir(parents=True)
    (tmp_path / "keep").mkdir()
    (run_root / "t3-link").symlink_to(tmp_path / "keep")
    (run_root / "other").mkdir()
    rows = [{"run_id": "pilot-1", "revoked": 1}, {"run_id": "pilot-2", "revoked": 0}]
    monkeypatch.setattr(pilot, "gateway", lambda *a, **k: subprocess.CompletedProcess(a, 0, json.dumps(rows), ""))
    revoked = []
    monkeypatch.setattr(pilot, "revoke", lambda run_id: revoked.append(run_id) or True)
    assert pilot.cleanup(run_root) == 0
    assert revoked == ["pilot-2"]
    assert sorted(p.name for p in run_root.iterdir()) == ["other", "t3-link"] and (tmp_path / "keep").is_dir()
    out = json.loads(capsys.readouterr().out)
    assert out == {"tokens_revoked": ["pilot-2"], "run_folders_removed": 1, "problems": []}


def test_cleanup_says_what_it_could_not_do(tmp_path, monkeypatch, capsys):
    as_root_cleanup(tmp_path, monkeypatch)

    def down(*a, **k):
        raise subprocess.CalledProcessError(1, "docker")

    monkeypatch.setattr(pilot, "gateway", down)
    monkeypatch.setattr(tier3, "remove_trial_objects", lambda: ["network list"])
    assert pilot.cleanup(tmp_path / "run") == 1
    problems = json.loads(capsys.readouterr().out)["problems"]
    assert any("socket proxy" in p for p in problems) and any("gateway" in p for p in problems)


def test_a_ctrl_c_meant_for_the_pilot_never_reaches_a_git_call(monkeypatch):
    seen = {}

    def fake(cmd, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(cmd, 0, "ok", "")

    monkeypatch.setattr(ev.subprocess, "run", fake)
    assert ev.sh(["git", "status"]) == "ok" and seen["start_new_session"] is True


def summaries(tmp_path, activation="never_activated", with_status="succeeded", without_status="succeeded"):
    agent = tmp_path / "out" / "systematic-debugging" / "20261127_160355_2435509_d0b537eb7bc8" / "opencode"
    for arm, status in (("with-skill", with_status), ("without-skill", without_status)):
        (agent / arm).mkdir(parents=True)
        doc = {"execution_status": status, "overall_score": 0.68}
        if arm == "with-skill":
            doc["skill_activation"] = {"status": activation, "positive_trials": 1, "activated_trials": 0}
        (agent / arm / "summary.json").write_text(json.dumps(doc))
    return tmp_path / "out"


def test_a_graded_run_whose_skill_never_activated_is_inconclusive_not_failed(tmp_path):
    """#265: SkillEvaluator writes no lift.json when the agent never loads the skill. That run ran and was graded:
    inconclusive, in SkillEvaluator's words, and still no score."""
    out = summaries(tmp_path)
    assert pilot.read_lift(out) == {} and pilot.never_activated(out) is True
    status, reason = pilot.status_of(0, {}, skill_never_activated=True)
    assert status == "inconclusive" and "never activated" in reason
    res = {
        "exit": 0,
        "cases": "shared",
        "out": ev.ROOT / "reports" / "x",
        "commit": "c" * 40,
        "tree_digest": "d",
        "duration_s": 1.0,
        "never_activated": True,
    }
    row = {"repo": "a/b", "skill_path": "s", "category": "c", "task_set": "quixbugs-python"}
    rec = pilot.record(row, 1, "smoke-x", res, {}, None, "t", {"version": "v"}, egress="open", smoke=True)
    assert rec["status"] == "inconclusive" and rec["scores"] is None


@pytest.mark.parametrize(
    "kw",
    [
        {"activation": "activated"},
        {"activation": "unknown"},
        {"with_status": "failed"},
        {"without_status": "failed"},
    ],
)
def test_anything_short_of_a_clean_never_activated_verdict_stays_failed(tmp_path, kw):
    out = summaries(tmp_path, **kw)
    assert pilot.never_activated(out) is False
    assert pilot.status_of(0, {}, skill_never_activated=False)[0] == "failed"


def test_a_crashed_or_stopped_run_is_never_called_inconclusive(tmp_path):
    assert pilot.status_of(None, {}, stopped="timed out after 1 s", skill_never_activated=True)[0] == "failed"
    assert pilot.status_of(1, {}, skill_never_activated=True)[0] == "failed"


def test_the_activation_verdict_is_never_read_through_a_link(tmp_path):
    real = summaries(tmp_path / "real")
    (tmp_path / "out" / "systematic-debugging").mkdir(parents=True)
    (tmp_path / "out" / "systematic-debugging" / "run").symlink_to(
        real / "systematic-debugging" / "20261127_160355_2435509_d0b537eb7bc8"
    )
    assert pilot.never_activated(tmp_path / "out") is False


def test_waiting_for_the_proxy_ends_once_it_answers_or_after_the_timeout(tmp_path):
    """#264: systemctl start returns before the proxy listens."""
    import socketserver
    import threading
    import time
    from http.server import BaseHTTPRequestHandler

    assert REAL_WAIT_FOR_PROXY(f"unix://{tmp_path}/absent.sock", timeout=0.3, poll=0.1) is False

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def address_string(self):
            return "unix"

        def do_GET(self):
            self.send_response(200 if self.path == "/_ping" else 404)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"OK")

    sock = Path("/tmp") / f"t3ping-{os.getpid()}.sock"

    def start_late():
        time.sleep(0.4)
        srv = socketserver.UnixStreamServer(str(sock), H)
        started.append(srv)
        srv.serve_forever(poll_interval=0.05)

    started = []
    threading.Thread(target=start_late, daemon=True).start()
    try:
        assert REAL_WAIT_FOR_PROXY(f"unix://{sock}", timeout=5, poll=0.1) is True
    finally:
        while not started:
            time.sleep(0.05)
        started[0].shutdown()
        sock.unlink()


def test_a_pilot_waits_for_the_proxy_and_stops_before_any_token(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(tier3, "wait_for_proxy", lambda *a, **k: False)
    monkeypatch.setattr(pilot, "RECORDS", tmp_path / "pilot" / "results.jsonl")
    monkeypatch.setattr(pilot, "load_skills", lambda: [{"repo": "a/b", "skill_path": "s", "repeats": 1}])
    monkeypatch.setattr(tier3, "evaluator_version", lambda: {"version": "x", "commit": "y"})
    monkeypatch.setattr(pilot, "issue_token", lambda *a: pytest.fail("no token without a proxy"))
    assert pilot.main([]) == 2 and "didn't answer" in capsys.readouterr().err
    lock = pilot.take_lock()
    assert lock is not None  # the lock was released
    os.close(lock)


def test_cleanup_says_so_when_the_proxy_never_answers(tmp_path, monkeypatch, capsys):
    as_root_cleanup(tmp_path, monkeypatch)
    monkeypatch.setattr(tier3, "wait_for_proxy", lambda *a, **k: False)
    monkeypatch.setattr(tier3, "remove_trial_objects", lambda: pytest.fail("not without a proxy"))
    assert pilot.cleanup(tmp_path / "run") == 1
    assert any("didn't answer" in p for p in json.loads(capsys.readouterr().out)["problems"])
