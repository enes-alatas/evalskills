"""deploy/gvisor/host/probe.py (#181), against a fake Docker API on a Unix socket: no daemon, no proxy."""

import http.server
import importlib.util
import json
import socket
import socketserver
import subprocess
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("probe", ROOT / "deploy/gvisor/host/probe.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


@pytest.fixture
def api(tmp_path, monkeypatch):
    state = {"calls": [], "runc": 403, "why": "HostConfig.Runtime has a value the proxy doesn't allow", "exit": 0}
    state.update({"runtime": "runsc", "net_delete": 204})

    class Handler(http.server.BaseHTTPRequestHandler):
        def address_string(self):
            return "fake"

        def log_message(self, *a):
            pass

        def reply(self, status, doc=None, raw=None):
            body = raw if raw is not None else (b"" if doc is None else json.dumps(doc).encode())
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def handle_one(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n)) if n else None
            path = self.path.removeprefix("/v1.47")
            state["calls"].append((self.command, path, body))
            if path == "/containers/create":
                if body["HostConfig"].get("Runtime") == "runc":
                    doc = (
                        {"Id": "r" * 12}
                        if state["runc"] == 201
                        else {"message": f"evalskills docker proxy: {state['why']}"}
                    )
                    return self.reply(state["runc"], doc)
                return self.reply(201, {"Id": "c" * 12})
            if path.endswith("/wait"):
                return self.reply(200, {"StatusCode": state["exit"]})
            if "/logs" in path:
                line = b"evals-gateway:8443 reached=True expected=True\n"
                return self.reply(200, raw=b"\x01\x00\x00\x00" + len(line).to_bytes(4, "big") + line)
            if path.endswith("/json"):
                return self.reply(200, {"HostConfig": {"Runtime": state["runtime"]}})
            if self.command == "DELETE" and path.startswith("/networks/"):
                return self.reply(state["net_delete"])
            return self.reply(201 if path == "/networks/create" else 204)

        do_GET = do_POST = do_DELETE = handle_one

    sock = tmp_path / "p.sock"
    srv = Server(str(sock), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(probe, "SOCK", str(sock))
    yield state
    srv.shutdown()
    srv.server_close()


def deleted(state):
    return [p for m, p, _ in state["calls"] if m == "DELETE"]


def test_one_trial_runs_under_runsc_and_everything_is_removed(api, capsys):
    assert probe.main("evalskills/probe:host") == 0
    out = capsys.readouterr().out
    assert (
        "a create asking for runc: refused (HostConfig.Runtime)" in out
        and "reached=True" in out
        and "runtime=runsc" in out
    )
    creates = [b for m, p, b in api["calls"] if p == "/containers/create"]
    assert creates[1]["HostConfig"] == {"NetworkMode": creates[0]["HostConfig"]["NetworkMode"]}  # runsc by the proxy
    assert json.loads(creates[1]["Cmd"][-1]) == [list(t) for t in probe.TARGETS]
    assert deleted(api) == ["/containers/cccccccccccc?force=1", f"/networks/{creates[0]['HostConfig']['NetworkMode']}"]


@pytest.mark.parametrize(("setting", "value"), [("exit", 1), ("runtime", "runc")])
def test_a_surprise_inside_the_trial_fails_and_still_cleans_up(api, setting, value):
    api[setting] = value
    assert probe.main("evalskills/probe:host") == 1
    assert len(deleted(api)) == 2


def test_a_runc_create_that_is_not_refused_fails_and_is_removed(api):
    api["runc"] = 201
    assert probe.main("evalskills/probe:host") == 1
    assert deleted(api)[0] == "/containers/rrrrrrrrrrrr?force=1" and deleted(api)[-1].startswith("/networks/probe-")


def test_the_in_trial_probe_counts_reached_and_refused():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    port = srv.getsockname()[1]
    free = socket.socket()
    free.bind(("127.0.0.1", 0))
    closed = free.getsockname()[1]
    free.close()
    try:
        good = json.dumps([["127.0.0.1", port, True], ["127.0.0.1", closed, False]])
        bad = json.dumps([["127.0.0.1", port, False]])
        run = [sys.executable, "-I", "-c", probe.PROBE]
        assert subprocess.run([*run, good], capture_output=True, timeout=30).returncode == 0
        p = subprocess.run([*run, bad], capture_output=True, text=True, timeout=30)
        assert p.returncode == 1 and "reached=True expected=False" in p.stdout
    finally:
        srv.close()


def test_a_refusal_for_another_reason_does_not_count_as_runc_refused(api, capsys):
    api["why"] = "Config.Image is not an image built for a trial"
    assert probe.main("evalskills/probe:host") == 1
    assert "not refused for its runtime" in capsys.readouterr().out
    assert len(deleted(api)) == 1  # only the network: no container was made


def test_a_failed_cleanup_names_what_is_left_and_fails(api, capsys):
    api["net_delete"] = 500
    assert probe.main("evalskills/probe:host") == 1  # the trial itself passed
    out = capsys.readouterr().out
    assert "left behind, remove by hand as root: network probe-" in out and "(500)" in out
