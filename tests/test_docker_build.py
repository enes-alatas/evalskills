"""Trial image builds through the proxy (#137, PR B): only allowlisted Dockerfiles, built from the committed final file
with the legacy builder and no network; the tar is read, never unpacked; client options are refused, not dropped."""

import contextlib
import hashlib
import http.client
import io
import json
import os
import socket
import socketserver
import sys
import tarfile
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "gateway"))
import docker_build as db  # noqa: E402
import docker_proxy as dp  # noqa: E402

BUILD_DIR = ROOT / "deploy" / "gvisor" / "build"
BASE = "evalskills/trial-base:d136a597"
BASE_ID = "sha256:" + "ab" * 32
ORIGINAL = b"FROM python:3.12-slim\n\nRUN true\n"
FINAL = f"FROM {BASE}\n\nRUN true\n".encode()


def make_tar(members: list, gz: bool = False) -> bytes:
    """members: (name, bytes) for files, or a prepared TarInfo."""
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz" if gz else "w") as t:
        for m in members:
            if isinstance(m, tarfile.TarInfo):
                t.addfile(m)
            else:
                info = tarfile.TarInfo(m[0])
                info.size = len(m[1])
                t.addfile(info, io.BytesIO(m[1]))
    return out.getvalue()


def allowlist():
    return {hashlib.sha256(ORIGINAL).hexdigest(): FINAL}


def prepare(data: bytes, max_files: int = 100):
    out = io.BytesIO()
    sha = db.prepare_context(io.BytesIO(data), out, allowlist(), max_files=max_files)
    return sha, out.getvalue()


# ------------------------------------------------------------------ the context


def test_an_allowed_context_gets_the_final_dockerfile_added_and_keeps_everything_else():
    sha, out = prepare(make_tar([("Dockerfile", ORIGINAL), ("skills/s/SKILL.md", b"hi")]))
    assert sha == hashlib.sha256(ORIGINAL).hexdigest()
    with tarfile.open(fileobj=io.BytesIO(out)) as t:
        names = t.getnames()
        assert names == ["Dockerfile", "skills/s/SKILL.md", db.FINAL_NAME]
        assert t.extractfile(db.FINAL_NAME).read() == FINAL
        assert t.extractfile("Dockerfile").read() == ORIGINAL  # the original is kept, never edited


def link(name, target, kind=tarfile.SYMTYPE):
    info = tarfile.TarInfo(name)
    info.type, info.linkname = kind, target
    return info


def special(name, kind):
    info = tarfile.TarInfo(name)
    info.type = kind
    return info


def folder(name):
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    return info


@pytest.mark.parametrize(
    "members,why",
    [
        ([("Dockerfile", b"FROM evil\n")], "not on the allowlist"),
        ([("skills/x", b"x")], "no top-level Dockerfile"),
        ([("Dockerfile", ORIGINAL), ("Dockerfile", ORIGINAL)], "one regular file"),
        ([link("Dockerfile", "/etc/passwd")], "one regular file"),
        ([folder("Dockerfile")], "one regular file"),
        ([("Dockerfile", ORIGINAL), (db.FINAL_NAME, b"FROM evil\n")], db.FINAL_NAME),
        ([("./" + db.FINAL_NAME, b"x"), ("Dockerfile", ORIGINAL)], db.FINAL_NAME),
        ([("Dockerfile", ORIGINAL), ("../escape", b"x")], "unsafe path"),
        ([("Dockerfile", ORIGINAL), ("/abs", b"x")], "unsafe path"),
        ([("Dockerfile", ORIGINAL), special("dev", tarfile.CHRTYPE)], "files, folders and links"),
        ([("Dockerfile", ORIGINAL), special("fifo", tarfile.FIFOTYPE)], "files, folders and links"),
        ([("Dockerfile", ORIGINAL), link("h", "/etc/passwd", tarfile.LNKTYPE)], "earlier file"),
        ([("Dockerfile", ORIGINAL), ("a", b"x"), link("h", "../a", tarfile.LNKTYPE)], "earlier file"),
        ([("Dockerfile", ORIGINAL), link("h", "a", tarfile.LNKTYPE), ("a", b"x")], "earlier file"),
        ([("Dockerfile", ORIGINAL), folder("d"), link("h", "d", tarfile.LNKTYPE)], "earlier file"),
    ],
)
def test_bad_contexts_are_refused(members, why):
    with pytest.raises(db.BuildError) as e:
        prepare(make_tar(members))
    assert why in str(e.value)


def test_links_stay_link_entries():
    _, out = prepare(make_tar([("Dockerfile", ORIGINAL), link("skills/l", "/etc/shadow")]))
    with tarfile.open(fileobj=io.BytesIO(out)) as t:
        m = t.getmember("skills/l")
        assert m.issym() and m.linkname == "/etc/shadow"  # the daemon jails it to the context; never followed here


def test_a_hard_link_back_to_an_earlier_file_is_kept():
    _, out = prepare(make_tar([("Dockerfile", ORIGINAL), ("a", b"x"), link("h", "./a", tarfile.LNKTYPE)]))
    with tarfile.open(fileobj=io.BytesIO(out)) as t:
        assert t.getmember("h").islnk() and t.extractfile("h").read() == b"x"


def test_a_real_http_client_build_reaches_the_daemon(env):
    """http.client adds Accept-Encoding: identity by itself (#142 review); the proxy must take it."""
    conn = Unix(env["sock"])
    body = make_tar([("Dockerfile", ORIGINAL)])
    conn.request(
        "POST", "/_evalskills/build", body=body, headers={"Host": "docker", "Content-Type": "application/x-tar"}
    )
    resp = conn.getresponse()
    assert resp.status == 200 and json.loads(resp.read())["tag"].startswith(db.TAG_REPO + ":")
    conn.close()
    status, doc = post(env, body, headers=(("Accept-Encoding", "gzip"),))
    assert status == 400 and "other than identity" in doc["message"]


def test_file_count_and_compression_are_capped():
    many = [("Dockerfile", ORIGINAL), *[(f"f{i}", b"") for i in range(10)]]
    with pytest.raises(db.BuildError, match="more than 5 files"):
        prepare(make_tar(many), max_files=5)
    with pytest.raises(db.BuildError, match="uncompressed tar"):
        prepare(make_tar([("Dockerfile", ORIGINAL)], gz=True))
    with pytest.raises(db.BuildError, match="larger than"):
        db.spool([b"x" * 10, b"y" * 10], max_bytes=15)


# ------------------------------------------------------------------ the committed allowlist


def test_the_committed_allowlist_loads_and_each_final_is_the_original_after_its_dependency_prefix():
    allowed = db.load_allowlist(BUILD_DIR / "allowed-dockerfiles.txt", BASE)
    assert len(allowed) == 4  # the four task shapes of SkillEvaluator d136a597 (input, repo: yes or no)
    for sha, final in allowed.items():
        original = (BUILD_DIR / "original" / f"{sha}.Dockerfile").read_bytes()
        assert hashlib.sha256(original).hexdigest() == sha
        lines = original.decode().split("\n")
        assert lines[0] == "FROM python:3.12-slim" and "pip" in lines[7] and "import" in lines[8]
        assert final.decode().split("\n") == [f"FROM {BASE}", *lines[9:]]
        assert b"apt-get" not in final and b"pip" not in final  # those live in the base image now


def test_the_base_dockerfile_pins_its_image_and_installs_by_hash():
    text = (BUILD_DIR / "base" / "Dockerfile").read_text()
    assert "FROM python:3.12-slim@sha256:" in text
    assert "--require-hashes" in text
    lock = (BUILD_DIR / "base" / "requirements.lock").read_text()
    pins = [ln for ln in lock.splitlines() if ln and not ln.startswith((" ", "#"))]
    assert pins and all("==" in p for p in pins)


@pytest.mark.parametrize(
    "line,why",
    [
        ("nothex final/x.Dockerfile", "expected"),
        ("a" * 64 + " ../../etc/passwd", "next to the list"),
    ],
)
def test_bad_allowlists_are_refused(tmp_path, line, why):
    (tmp_path / "list.txt").write_text(line + "\n")
    with pytest.raises(ValueError, match=why):
        db.load_allowlist(tmp_path / "list.txt", BASE)


def test_a_final_dockerfile_from_another_base_is_refused(tmp_path):
    (tmp_path / "f.Dockerfile").write_text("FROM python:3.12-slim\n")
    (tmp_path / "list.txt").write_text("a" * 64 + " f.Dockerfile\n")
    with pytest.raises(ValueError, match="must start with FROM"):
        db.load_allowlist(tmp_path / "list.txt", BASE)


# ------------------------------------------------------------------ through the proxy


class Daemon(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def address_string(self):
        return "unix"

    def reply(self, code, payload: bytes, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)
        self.close_connection = True

    def do_GET(self):
        st = self.server.state
        path = urlsplit(self.path).path
        if path == f"/images/{BASE}/json":
            return self.reply(200, json.dumps({"Id": st["base_id"]}).encode())
        if path.startswith(f"/images/{db.TAG_REPO}:") and st.get("built"):
            labels = st.get("labels", {dp.OWNER_LABEL: "1"})
            return self.reply(200, json.dumps({"Id": "sha256:" + "cd" * 32, "Config": {"Labels": labels}}).encode())
        self.reply(404, b"{}")

    def do_POST(self):
        st = self.server.state
        body = self.rfile.read(int(self.headers["Content-Length"]))
        st["builds"].append((self.path, body))
        if st.get("refuse"):
            return self.reply(400, b'{"message":"legacy builder removed"}')
        if st.get("fail"):
            return self.reply(200, b'{"stream":"Step 1"}\n{"errorDetail":{"message":"boom"},"error":"boom"}\n')
        st["built"] = True
        self.reply(200, json.dumps({"aux": {"ID": "sha256:" + "cd" * 32}}).encode() + b"\n")


@contextlib.contextmanager
def running_proxy(tmp_path):
    """The real proxy Handler in front of a fake daemon; also used by test_gvisor_env_mode's end-to-end build."""
    tmp_path.mkdir(exist_ok=True)
    sockdir = Path("/tmp") / f"db-{os.getpid()}-{id(tmp_path)}"
    sockdir.mkdir()
    daemon = socketserver.ThreadingUnixStreamServer(str(sockdir / "d.sock"), Daemon)
    daemon.daemon_threads = True
    daemon.state = {"builds": [], "base_id": BASE_ID}
    threading.Thread(target=daemon.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    (tmp_path / "final.Dockerfile").write_bytes(FINAL)
    (tmp_path / "allowed.txt").write_text(f"{hashlib.sha256(ORIGINAL).hexdigest()} final.Dockerfile\n")
    builds = {"allowed_dockerfiles": str(tmp_path / "allowed.txt"), "base_image": BASE, "base_image_id": BASE_ID}
    (tmp_path / "run").mkdir()
    cfg = dp.load_config(
        {
            "listen": str(sockdir / "p.sock"),
            "listen_group": "",
            "upstream": str(sockdir / "d.sock"),
            "bind_root": str(tmp_path / "run"),
            "eval_uid": os.getuid() or 1000,
            "builds": builds,
        }
    )
    proxy = dp.make_server(cfg)
    threading.Thread(target=proxy.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield {"sock": cfg["listen"], "state": daemon.state, "cfg": cfg}
    proxy.shutdown()
    daemon.shutdown()
    for p in sockdir.iterdir():
        p.unlink()
    sockdir.rmdir()


@pytest.fixture
def env(tmp_path):
    with running_proxy(tmp_path) as e:
        yield e


class Unix(http.client.HTTPConnection):
    def __init__(self, path: str):
        super().__init__("localhost", timeout=10)
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


def post(env, body: bytes, target="/_evalskills/build", headers=()):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(10)
    s.connect(env["sock"])
    head = f"POST {target} HTTP/1.1\r\nHost: docker\r\nContent-Type: application/x-tar\r\n"
    head += "".join(f"{k}: {v}\r\n" for k, v in headers)
    s.sendall((head + f"Content-Length: {len(body)}\r\n\r\n").encode() + body)
    out = b""
    while chunk := s.recv(65536):
        out += chunk
    s.close()
    return int(out.split(b" ", 2)[1]), json.loads(out.partition(b"\r\n\r\n")[2] or b"{}")


def test_a_build_is_written_by_the_proxy(env):
    status, doc = post(env, make_tar([("Dockerfile", ORIGINAL), ("skills/s/SKILL.md", b"hi")]))
    assert status == 200 and doc["tag"].startswith(db.TAG_REPO + ":") and doc["id"] == "sha256:" + "cd" * 32
    [(target, body)] = env["state"]["builds"]
    q = {k: v[0] for k, v in parse_qs(urlsplit(target).query).items()}
    assert urlsplit(target).path == "/build"
    assert q == {
        "version": "1",
        "dockerfile": db.FINAL_NAME,
        "t": doc["tag"],
        "networkmode": "none",
        "pull": "0",
        "rm": "1",
        "forcerm": "1",
        "q": "1",
        "labels": json.dumps({dp.OWNER_LABEL: "1"}),
    }
    with tarfile.open(fileobj=io.BytesIO(body)) as t:
        assert t.extractfile(db.FINAL_NAME).read() == FINAL


@pytest.mark.parametrize(
    "target,headers",
    [
        ("/_evalskills/build?t=mine", ()),
        ("/_evalskills/build?networkmode=host", ()),
        ("/_evalskills/build?", ()),
        ("/_evalskills/build", (("X-Registry-Config", "e30="),)),
        ("/_evalskills/build", (("X-Docker-Expose-Session-Uuid", "x"),)),
        ("/_evalskills/build", (("Upgrade", "h2c"),)),
    ],
)
def test_client_build_options_are_refused_not_dropped(env, target, headers):
    status, doc = post(env, make_tar([("Dockerfile", ORIGINAL)]), target, headers)
    assert status == 400 and "builds take no" in doc["message"]
    assert env["state"]["builds"] == []


def test_a_missing_or_different_base_image_stops_the_build(env):
    env["state"]["base_id"] = "sha256:" + "00" * 32
    status, doc = post(env, make_tar([("Dockerfile", ORIGINAL)]))
    assert status == 503 and "base image" in doc["message"]
    assert env["state"]["builds"] == []


def test_a_daemon_without_the_legacy_builder_fails_closed(env):
    env["state"]["refuse"] = True
    status, _ = post(env, make_tar([("Dockerfile", ORIGINAL)]))
    assert status == 502
    assert len(env["state"]["builds"]) == 1  # one attempt, no fallback to BuildKit


def test_a_failed_build_reports_its_error(env):
    env["state"]["fail"] = True
    status, doc = post(env, make_tar([("Dockerfile", ORIGINAL)]))
    assert status == 502 and "boom" in doc["message"]


def test_an_unlabelled_result_is_not_handed_out(env):
    env["state"]["labels"] = {}
    assert post(env, make_tar([("Dockerfile", ORIGINAL)]))[0] == 502


def test_an_unlisted_dockerfile_never_reaches_the_daemon(env):
    status, doc = post(env, make_tar([("Dockerfile", b"FROM python:3.12-slim\nRUN pip install evil\n")]))
    assert status == 403 and "allowlist" in doc["message"]
    assert env["state"]["builds"] == []


def test_pass_through_build_routes_stay_refused(env):
    for target in ("/build", "/v1.47/build?version=1", "/session", "/grpc"):
        assert post(env, make_tar([("Dockerfile", ORIGINAL)]), target)[0] == 403
    assert env["state"]["builds"] == []


def test_builds_off_means_503(env):
    env["cfg"]["builds"] = None
    assert post(env, make_tar([("Dockerfile", ORIGINAL)]))[0] == 503


@pytest.mark.parametrize(
    "builds",
    [
        {"base_image": BASE, "base_image_id": "sha256:short"},
        {"base_image": "UPPER:x", "base_image_id": BASE_ID},
        {"base_image": BASE, "base_image_id": BASE_ID, "allowed_dockerfiles": "/nonexistent"},
        {"base_image": BASE, "base_image_id": BASE_ID, "extra": 1},
    ],
)
def test_bad_build_config_is_refused(builds):
    with pytest.raises(dp.ConfigError):
        dp.load_config({"eval_uid": 1000, "builds": builds})
