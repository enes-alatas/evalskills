import json
import os
import socketserver
import sys
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

import tier3  # noqa: E402

FAKE_TOKEN = "gw-test-0000-not-a-real-token"


def OK():
    return True, "ok"


@pytest.fixture
def safe(tmp_path):
    tok = tmp_path / "token"
    tok.write_text(FAKE_TOKEN + "\n")
    return {
        "DOCKER_HOST": tier3.EVALS_SOCKET,
        "TIER3_GATEWAY_URL": "http://10.0.0.1:4000/v1",
        "TIER3_GATEWAY_TOKEN_FILE": str(tok),
    }


def test_a_safe_setup_gets_only_the_gateway_token(safe):
    env = tier3.trial_env(safe, OK)
    assert env["DOCKER_HOST"] == tier3.EVALS_SOCKET
    assert "unix://" + env["EVALSKILLS_DOCKER_SOCKET"] == tier3.EVALS_SOCKET  # the name Harbor is given
    assert env["SKILL_EVAL_LLM_PROVIDER"] == "openai-compatible"
    assert env["SKILL_EVAL_LLM_API_KEY"] == FAKE_TOKEN
    assert not any(k in env for k in ("ANTHROPIC_API_KEY", "NVIDIA_API_KEY", "OPENAI_API_KEY"))


@pytest.mark.parametrize("mode", ["local", "docker", "e2b", "modal", "daytona", "kata"])
def test_refuses_any_env_mode_but_gvisor(safe, monkeypatch, mode):
    monkeypatch.setattr(tier3, "ENV_MODE", mode)
    with pytest.raises(tier3.Unsafe, match="env-mode"):
        tier3.trial_env(safe, OK)


@pytest.mark.parametrize(
    "host", [None, "", "unix:///var/run/docker.sock", "unix:///run/docker-evals.sock", "tcp://10.0.0.2:2375"]
)
def test_refuses_anything_but_the_socket_proxy(safe, host):
    safe.pop("DOCKER_HOST")
    if host is not None:
        safe["DOCKER_HOST"] = host
    with pytest.raises(tier3.Unsafe, match="DOCKER_HOST"):
        tier3.trial_env(safe, OK)


@pytest.mark.parametrize("key", tier3.REAL_KEYS)
def test_refuses_when_a_real_provider_key_is_present(safe, key):
    safe[key] = "sk-test-0000-not-a-real-key"
    with pytest.raises(tier3.Unsafe, match=key):
        tier3.trial_env(safe, OK)


@pytest.mark.parametrize("missing", ["TIER3_GATEWAY_URL", "TIER3_GATEWAY_TOKEN_FILE"])
def test_refuses_without_the_gateway(safe, missing):
    safe.pop(missing)
    with pytest.raises(tier3.Unsafe, match="gateway"):
        tier3.trial_env(safe, OK)


def test_refuses_an_unreadable_or_empty_token(safe, tmp_path):
    safe["TIER3_GATEWAY_TOKEN_FILE"] = str(tmp_path / "absent")
    with pytest.raises(tier3.Unsafe, match="cannot read"):
        tier3.trial_env(safe, OK)
    (tmp_path / "empty").write_text("\n")
    safe["TIER3_GATEWAY_TOKEN_FILE"] = str(tmp_path / "empty")
    with pytest.raises(tier3.Unsafe, match="empty"):
        tier3.trial_env(safe, OK)


def test_refuses_while_the_proxy_reports_isolation_broken(safe):
    with pytest.raises(tier3.Unsafe, match="isolation does not hold .evalskills-gateway may forward"):
        tier3.trial_env(safe, lambda: (False, "evalskills-gateway may forward"))


def test_the_isolation_question_goes_to_the_proxy(tmp_path):
    sock = Path("/tmp") / f"t3fw-{os.getpid()}.sock"
    seen = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def address_string(self):
            return "unix"

        def do_GET(self):
            seen.append(self.path)
            body = json.dumps({"ok": self.server.ok, "why": "x"}).encode()
            self.send_response(200 if self.server.ok else 503)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = socketserver.UnixStreamServer(str(sock), H)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        srv.ok = True
        assert tier3.isolation_ok(f"unix://{sock}") == (True, "x")
        srv.ok = False
        assert tier3.isolation_ok(f"unix://{sock}") == (False, "x")
    finally:
        srv.shutdown()
        sock.unlink()
    assert seen == ["/_evalskills/isolation"] * 2
    ok, why = tier3.isolation_ok(f"unix://{tmp_path}/absent.sock")
    assert not ok and "did not answer" in why


def skill(tmp_path, with_evals=True):
    d = tmp_path / "src" / "demo"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: demo\ndescription: fixture\n---\n")
    if with_evals:
        (d / "evals").mkdir()
        (d / "evals" / "evals.json").write_text('{"skill_name": "demo", "evals": [{"id": "own"}]}')
    return d


def shared_set(root, name, source=True):
    s = root / name
    s.mkdir(parents=True)
    (s / "evals.json").write_text('{"skill_name": "x", "evals": [{"id": "vendored"}]}')
    if source:
        (s / "SOURCE.md").write_text("Upstream: example benchmark @ rev, licence: MIT\n")
    return s


def test_no_task_set_keeps_the_skills_own_cases(tmp_path):
    out = tier3.prepare(skill(tmp_path), None, tmp_path / "out" / "demo")
    assert '"own"' in (out / "evals" / "evals.json").read_text()


def test_no_task_set_and_no_evals_leaves_it_to_skillevaluator(tmp_path):
    out = tier3.prepare(skill(tmp_path, with_evals=False), None, tmp_path / "out" / "demo")
    assert (out / "SKILL.md").is_file() and not (out / "evals").exists()


def test_a_set_without_source_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(tier3, "SHARED", tmp_path / "shared")
    shared_set(tmp_path / "shared", "made-up", source=False)
    with pytest.raises(ValueError, match="SOURCE.md"):
        tier3.prepare(skill(tmp_path), "made-up", tmp_path / "out" / "demo")
    assert not (tmp_path / "out" / "demo").exists()


def test_a_vendored_set_replaces_the_skills_cases(tmp_path, monkeypatch):
    monkeypatch.setattr(tier3, "SHARED", tmp_path / "shared")
    shared_set(tmp_path / "shared", "public")
    out = tier3.prepare(skill(tmp_path), "public", tmp_path / "out" / "demo")
    data = json.loads((out / "evals" / "evals.json").read_text())
    assert [e["id"] for e in data["evals"]] == ["vendored"] and data["skill_name"] == "demo"


def test_the_repo_ships_no_self_authored_sets():
    for d in tier3.SHARED.iterdir():
        if d.is_dir() and not d.name.startswith("__"):
            assert (d / "SOURCE.md").is_file(), d.name


STRIPPED_FIXTURES = {
    "grader.py": "print('reward 1.0')",
    "grader.sh": "echo 1.0",
    "tests/grader.py": "print('reward 1.0')",
    "tests/grader.sh": "echo 1.0",
    "harbor/case-1/tests/test.sh": "echo 1.0",
    "harbor/case-1/task.toml": "",
    "config.yml": "grading:\n  mode: custom_only\n",
    "config.yaml": "grading:\n  mode: custom_only\n",
    "environment/Dockerfile": "FROM scratch\n",
    "environment/mcp_servers.toml": "",
}


@pytest.mark.parametrize("rel", sorted(STRIPPED_FIXTURES))
def test_own_cases_lose_the_skills_grading_and_environment(tmp_path, rel):
    src = skill(tmp_path)
    (src / "evals" / "files").mkdir()
    (src / "evals" / "files" / "input.txt").write_text("fixture")
    f = src / "evals" / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(STRIPPED_FIXTURES[rel])
    out = tier3.prepare(src, None, tmp_path / "out" / "demo")
    assert not (out / "evals" / rel).exists()
    assert not (out / "evals" / rel.split("/")[0]).exists()
    assert (out / "evals" / "evals.json").is_file() and (out / "evals" / "files" / "input.txt").is_file()


def test_a_linked_evals_dir_is_dropped(tmp_path):
    src = skill(tmp_path, with_evals=False)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "grader.py").write_text("print(1)")
    (src / "evals").symlink_to(elsewhere)
    out = tier3.prepare(src, None, tmp_path / "out" / "demo")
    assert not (out / "evals").exists() and not (out / "evals").is_symlink()
    assert (elsewhere / "grader.py").is_file()  # the original is untouched


def test_a_linked_grader_is_removed_not_followed(tmp_path):
    src = skill(tmp_path)
    target = tmp_path / "real.py"
    target.write_text("print(1)")
    (src / "evals" / "grader.py").symlink_to(target)
    out = tier3.prepare(src, None, tmp_path / "out" / "demo")
    assert not (out / "evals" / "grader.py").exists() and not (out / "evals" / "grader.py").is_symlink()
    assert target.is_file()


def test_refuses_a_trial_while_the_root_disk_is_low(tmp_path, monkeypatch):
    """#137: trial images grow /var/lib/docker on /, outside the capped run disk."""
    free = 3 * 1024**3
    monkeypatch.setattr(tier3.shutil, "disk_usage", lambda p: type("U", (), {"free": free})())
    with pytest.raises(tier3.Unsafe, match="TIER3_MIN_FREE_ROOT"):
        tier3.check_free_root(tmp_path, minimum=8 * 1024**3)
    tier3.check_free_root(tmp_path, minimum=2 * 1024**3)  # enough: no refusal


def test_the_default_minimum_is_8_gb():
    assert tier3.MIN_FREE_ROOT == 8 * 1024**3 or "TIER3_MIN_FREE_ROOT" in os.environ


def test_a_smoke_run_keeps_only_the_first_shared_case(tmp_path):
    """#257: --smoke runs the shared set's first case, from the real vendored set."""
    out = tier3.prepare(skill(tmp_path), "quixbugs-python", tmp_path / "out" / "demo", max_cases=1)
    full = json.loads((tier3.SHARED / "quixbugs-python" / "evals.json").read_text())["evals"]
    kept = json.loads((out / "evals" / "evals.json").read_text())["evals"]
    assert len(full) > 1 and kept == [full[0] | {"expected_skill": "demo"}]


def test_a_smoke_run_keeps_only_the_first_own_case(tmp_path):
    src = skill(tmp_path)
    (src / "evals" / "evals.json").write_text(json.dumps({"evals": [{"id": "a"}, {"id": "b"}], "extra": 1}))
    out = tier3.prepare(src, None, tmp_path / "out" / "demo", max_cases=1)
    assert json.loads((out / "evals" / "evals.json").read_text()) == {"evals": [{"id": "a"}], "extra": 1}


def test_a_full_run_keeps_every_case(tmp_path):
    src = skill(tmp_path)
    (src / "evals" / "evals.json").write_text(json.dumps({"evals": [{"id": "a"}, {"id": "b"}]}))
    out = tier3.prepare(src, None, tmp_path / "out" / "demo")
    assert len(json.loads((out / "evals" / "evals.json").read_text())["evals"]) == 2


@pytest.mark.parametrize("content", ["not json", '["a list"]', '{"evals": "not a list"}', '{"evals": []}'])
def test_a_smoke_run_refuses_an_evals_json_it_cant_cut(tmp_path, content):
    """Keen on #258: a smoke run fails closed, never widening to every case."""
    src = skill(tmp_path)
    (src / "evals" / "evals.json").write_text(content)
    with pytest.raises(tier3.NoSmokeCase):
        tier3.prepare(src, None, tmp_path / "out" / "demo", max_cases=1)


@pytest.mark.parametrize("name", ["evals.jsonl", "evals.yaml", "evals.yml", "dataset.json", "dataset.jsonl"])
def test_a_smoke_run_refuses_case_files_other_than_evals_json(tmp_path, name):
    src = skill(tmp_path)
    (src / "evals" / name).write_text("id: a\n")
    with pytest.raises(tier3.NoSmokeCase, match=name):
        tier3.prepare(src, None, tmp_path / "out" / "demo", max_cases=1)


def test_a_smoke_run_refuses_a_linked_evals_json(tmp_path):
    src = skill(tmp_path)
    (tmp_path / "elsewhere.json").write_text('{"evals": [{"id": "a"}, {"id": "b"}]}')
    (src / "evals" / "evals.json").unlink()
    (src / "evals" / "evals.json").symlink_to(tmp_path / "elsewhere.json")
    with pytest.raises(tier3.NoSmokeCase, match="regular file"):
        tier3.prepare(src, None, tmp_path / "out" / "demo", max_cases=1)


def test_a_smoke_run_without_case_files_runs_the_starter_case(tmp_path):
    src = skill(tmp_path)
    (src / "evals" / "evals.json").unlink()
    (src / "evals" / "input.txt").write_text("data")
    out = tier3.prepare(src, None, tmp_path / "out" / "demo", max_cases=1)
    assert (out / "evals" / "input.txt").is_file()


def test_a_full_run_leaves_any_case_file_as_published(tmp_path):
    src = skill(tmp_path)
    (src / "evals" / "evals.json").write_text("not json")
    out = tier3.prepare(src, None, tmp_path / "out" / "demo")
    assert (out / "evals" / "evals.json").read_text() == "not json"
