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

    def go(nap="0", mark="", timeout=60):
        monkeypatch.setattr(tier3, "TIER3_TIMEOUT", timeout)
        removed = []
        env = {"PATH": os.environ["PATH"], "NAP": nap, "MARK": mark}
        res = tier3.run("a/b", "skills/demo", None, env, run_root=run_root, cleanup=lambda: removed.append(1) or [])
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
    assert oct((out / "suid").stat().st_mode & 0o7777) == "0o644"  # never a setuid file in reports/
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
    assert tier3.remove_trial_objects(f"unix://{tmp_path}/absent.sock") == ["container list", "network list"]


def test_lift_is_never_read_through_a_link(tmp_path):
    (tmp_path / "real" / "opencode").mkdir(parents=True)
    (tmp_path / "real" / "opencode" / "lift.json").write_text('{"execution_status": "succeeded"}')
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "x").symlink_to(tmp_path / "real")
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
