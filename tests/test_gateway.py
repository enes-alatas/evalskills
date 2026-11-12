"""The model gateway (#121, PR A): per-run tokens, model and endpoint allowlists, caps that persist, metering."""

import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gateway"))
import gateway as gw  # noqa: E402

FAKE_KEY = "fake-upstream-key-for-tests"
MODEL = "vendor/test-model"


class Upstream(BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Upstream.seen.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n')
            self.wfile.write(b'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":3}}\n\n')
            self.wfile.write(b"data: [DONE]\n\n")
            return
        out = json.dumps({"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def start(server):
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}"


@pytest.fixture
def env(tmp_path):
    Upstream.seen = []
    up = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    cfg = {
        **gw.DEFAULTS,
        "upstream_base_url": start(up) + "/v1",
        "allowed_models": [MODEL],
        "state_db": str(tmp_path / "state.db"),
        "per_run_requests": 3,
        "per_run_tokens": 1000,
        "per_day_requests": 100,
        "requests_per_minute": 100,
    }
    state = gw.State(cfg["state_db"])
    srv = ThreadingHTTPServer(("127.0.0.1", 0), gw.make_handler(cfg, state, gw.RateLimiter(100), FAKE_KEY))
    base = start(srv)
    yield {"cfg": cfg, "state": state, "base": base}
    srv.shutdown()
    up.shutdown()


def call(base, path, token, body=None, method="POST"):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def chat(**extra):
    return {"model": MODEL, "messages": [{"role": "user", "content": "x"}], **extra}


def test_forwards_with_the_real_key_and_meters(env):
    token = env["state"].issue("run-1")
    code, _ = call(env["base"], "/v1/chat/completions", token, chat())
    assert code == 200
    assert Upstream.seen[0]["auth"] == f"Bearer {FAKE_KEY}"  # the real key goes upstream only
    assert Upstream.seen[0]["path"] == "/v1/chat/completions"
    (u,) = env["state"].usage("run-1")
    assert (u["requests"], u["prompt_tokens"], u["completion_tokens"]) == (1, 10, 5)


@pytest.mark.parametrize("token", [None, "egw-not-issued", ""])
def test_unknown_tokens_are_refused_before_upstream(env, token):
    code, _ = call(env["base"], "/v1/chat/completions", token, chat())
    assert code == 401
    assert Upstream.seen == []


def test_revoked_token_is_refused(env):
    token = env["state"].issue("run-1")
    env["state"].revoke("run-1")
    assert call(env["base"], "/v1/chat/completions", token, chat())[0] == 401


def test_model_and_endpoint_allowlists(env):
    token = env["state"].issue("run-1")
    assert call(env["base"], "/v1/chat/completions", token, chat(model="other/model"))[0] == 403
    assert call(env["base"], "/v1/responses", token, chat())[0] == 404
    assert call(env["base"], "/v1/files", token, chat())[0] == 404
    assert Upstream.seen == []


def test_model_list_is_answered_locally(env):
    token = env["state"].issue("run-1")
    code, body = call(env["base"], "/v1/models", token, method="GET")
    assert code == 200
    assert [m["id"] for m in json.loads(body)["data"]] == [MODEL]
    assert Upstream.seen == []


def test_run_request_cap(env):
    token = env["state"].issue("run-1")
    codes = [call(env["base"], "/v1/chat/completions", token, chat())[0] for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    assert len(Upstream.seen) == 3
    assert env["state"].usage("run-1")[0]["refused"] == 1


def test_run_token_cap_and_output_clamp(env):
    token = env["state"].issue("run-1")
    env["state"].meter("run-1", {"prompt_tokens": 990, "completion_tokens": 0})
    assert call(env["base"], "/v1/chat/completions", token, chat(max_tokens=500))[0] == 200
    assert Upstream.seen[-1]["body"]["max_tokens"] == 10  # what the run had left
    assert call(env["base"], "/v1/chat/completions", token, chat())[0] == 429  # now over the cap


def test_caps_survive_a_restart(env):
    token = env["state"].issue("run-1")
    for _ in range(3):
        call(env["base"], "/v1/chat/completions", token, chat())
    reopened = gw.State(env["cfg"]["state_db"])
    assert reopened.admit("run-1", env["cfg"]) == "run request cap reached"


def test_daily_cap_spans_runs(env):
    cfg = {**env["cfg"], "per_day_requests": 2}
    s = env["state"]
    s.issue("a")
    s.issue("b")
    assert s.admit("a", cfg) is None
    assert s.admit("b", cfg) is None
    assert s.admit("b", cfg) == "daily request cap reached"


def test_streaming_is_relayed_and_metered(env):
    token = env["state"].issue("run-1")
    code, body = call(env["base"], "/v1/chat/completions", token, chat(stream=True))
    assert code == 200
    assert b'"content":"hi"' in body and b"[DONE]" in body
    assert Upstream.seen[0]["body"]["stream_options"] == {"include_usage": True}
    (u,) = env["state"].usage("run-1")
    assert (u["prompt_tokens"], u["completion_tokens"], u["unmetered"]) == (7, 3, 0)


def test_tokens_are_stored_hashed(env):
    token = env["state"].issue("run-1")
    raw = Path(env["cfg"]["state_db"]).read_bytes()
    assert token.encode() not in raw


def test_issue_writes_a_private_token_file(tmp_path, capsys):
    cfg_path = tmp_path / "gw.json"
    cfg_path.write_text(json.dumps({"allowed_models": [MODEL], "state_db": str(tmp_path / "s.db")}))
    out = tmp_path / "token"
    assert gw.main(["issue", "--config", str(cfg_path), "--run", "r1", "--out", str(out)]) == 0
    token = out.read_text().strip()
    assert token.startswith(gw.TOKEN_PREFIX)
    assert out.stat().st_mode & 0o777 == 0o600
    assert token not in capsys.readouterr().out  # the CLI never prints the token
    with pytest.raises(FileExistsError):  # never overwrites, and issues nothing when it cannot write
        gw.main(["issue", "--config", str(cfg_path), "--run", "r2", "--out", str(out)])
    assert [r["run_id"] for r in gw.State(str(tmp_path / "s.db")).usage()] == ["r1"]


@pytest.mark.parametrize(
    "raw, why",
    [
        ({"allowed_models": [MODEL], "upstream_base_url": "http://plain.example/v1"}, "https"),
        ({"allowed_models": []}, "allowed_models"),
        ({"allowed_models": [MODEL], "per_run_tokens": 0}, "per_run_tokens"),
        ({"allowed_models": [MODEL], "per_run_token": 5}, "unknown config keys"),
    ],
)
def test_config_is_strict(tmp_path, raw, why):
    p = tmp_path / "gw.json"
    p.write_text(json.dumps(raw))
    with pytest.raises(gw.ConfigError, match=why):
        gw.load_config(str(p))


def test_rate_limiter_waits_then_gives_up():
    now = [0.0]
    lim = gw.RateLimiter(2, clock=lambda: now[0], sleep=lambda s: now.__setitem__(0, now[0] + s))
    assert lim.acquire(0) and lim.acquire(0)
    assert not lim.acquire(5)  # the window frees up only after 60 s
    assert lim.acquire(120)
    assert now[0] >= 60


class Redirector(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        self.send_response(302)
        self.send_header("Location", Redirector.target)
        self.send_header("Content-Length", "0")
        self.end_headers()


class Catcher(BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        Catcher.seen.append(self.headers.get("Authorization"))
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    do_POST = do_GET


class KeyEcho(BaseHTTPRequestHandler):
    code = 401

    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        key = self.headers["Authorization"].split()[-1]
        msg = {401: f"Incorrect API key provided: {key}", 400: f"bad request for key ...{key[-10:]}"}.get(
            KeyEcho.code, "model overloaded"
        )
        out = json.dumps({"error": {"message": msg}}).encode()
        self.send_response(KeyEcho.code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def gateway_for(tmp_path, handler):
    up = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    cfg = {
        **gw.DEFAULTS,
        "upstream_base_url": start(up) + "/v1",
        "allowed_models": [MODEL],
        "state_db": str(tmp_path / "s.db"),
        "requests_per_minute": 100,
    }
    state = gw.State(cfg["state_db"])
    srv = ThreadingHTTPServer(("127.0.0.1", 0), gw.make_handler(cfg, state, gw.RateLimiter(100), FAKE_KEY))
    return start(srv), state, (up, srv)


def test_upstream_redirects_are_never_followed(tmp_path):
    Catcher.seen = []
    catcher = ThreadingHTTPServer(("127.0.0.1", 0), Catcher)
    Redirector.target = start(catcher) + "/steal"
    base, state, servers = gateway_for(tmp_path, Redirector)
    code, body = call(base, "/v1/chat/completions", state.issue("r"), chat())
    assert code == 502 and json.loads(body)["error"]["code"] == 302
    assert Catcher.seen == []  # the key never reached the Location
    for s in (*servers, catcher):
        s.shutdown()


@pytest.mark.parametrize("upstream_code", [401, 400])
def test_upstream_errors_never_carry_the_key_back(tmp_path, upstream_code):
    KeyEcho.code = upstream_code
    base, state, servers = gateway_for(tmp_path, KeyEcho)
    code, body = call(base, "/v1/chat/completions", state.issue("r"), chat())
    assert code == 502  # replaced by the gateway's own message
    assert FAKE_KEY.encode() not in body and FAKE_KEY[-10:].encode() not in body
    for s in servers:
        s.shutdown()


def test_other_upstream_errors_are_relayed(tmp_path):
    KeyEcho.code = 503
    base, state, servers = gateway_for(tmp_path, KeyEcho)
    code, body = call(base, "/v1/chat/completions", state.issue("r"), chat())
    assert code == 503 and b"model overloaded" in body  # the agent can see why and retry
    for s in servers:
        s.shutdown()


def test_issue_refuses_a_reused_run_id_cleanly(tmp_path, capsys):
    cfg_path = tmp_path / "gw.json"
    cfg_path.write_text(json.dumps({"allowed_models": [MODEL], "state_db": str(tmp_path / "s.db")}))
    assert gw.main(["issue", "--config", str(cfg_path), "--run", "r1", "--out", str(tmp_path / "t1")]) == 0
    assert gw.main(["issue", "--config", str(cfg_path), "--run", "r1", "--out", str(tmp_path / "t2")]) == 1
    assert not (tmp_path / "t2").exists()  # no empty token file left behind
    assert "already has a token" in capsys.readouterr().err


def test_handler_has_a_socket_timeout():
    cfg = {**gw.DEFAULTS, "allowed_models": [MODEL]}
    assert gw.make_handler(cfg, None, None, FAKE_KEY).timeout == 60
