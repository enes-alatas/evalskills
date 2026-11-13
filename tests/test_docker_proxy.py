"""The Docker socket proxy (#137): allowlisted routes and fields, runsc forced, own objects, safe binds, no smuggling.

A fake daemon on a unix socket records what reaches it, so every test can check both the answer the client got and
that a refused request never got upstream."""

import json
import os
import socket
import socketserver
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gateway"))
import docker_proxy as dp  # noqa: E402

OWNED = {dp.OWNER_LABEL: "1"}


class FakeDaemon(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, path):
        self.seen = []  # (method, target, headers dict, body bytes) for every request the proxy forwarded
        self.objects = {
            ("container", "mine"): {"Config": {"Labels": OWNED}},
            ("container", "theirs"): {"Config": {"Labels": {}}},
            ("image", "trial-img"): {"Config": {"Labels": OWNED}},
            ("image", "nginx"): {"Config": {"Labels": {}}},
            ("network", "trialnet"): {"Labels": OWNED, "IPAM": {"Config": [{"Subnet": "172.31.240.0/28"}]}},
            ("network", "appnet"): {"Labels": {}},
            ("exec", "e-mine"): {"ContainerID": "mine"},
            ("exec", "e-theirs"): {"ContainerID": "theirs"},
        }
        super().__init__(path, DaemonHandler)


class DaemonHandler(socketserver.StreamRequestHandler):
    def handle(self):
        while True:
            line = self.rfile.readline()
            if not line:
                return
            method, target, _ = line.decode().split(" ")
            headers = {}
            while (h := self.rfile.readline()) not in (b"\r\n", b""):
                k, _, v = h.decode().partition(":")
                headers[k.strip().lower()] = v.strip()
            body = b""
            if "content-length" in headers:
                body = self.rfile.read(int(headers["content-length"]))
            elif headers.get("transfer-encoding") == "chunked":
                while (size := int(self.rfile.readline().strip(), 16)) != 0:
                    body += self.rfile.read(size)
                    self.rfile.readline()
                self.rfile.readline()
            path = target.split("?")[0]
            check = "connection" not in headers  # the proxy's own inspect calls; forwarded requests always carry one
            if not (check and self.answer_check(method, path, target)):
                if check:
                    self.send(404, {})
                    return
                self.server.seen.append((method, target, headers, body))
                self.forwarded(method, path, headers, body)
            if headers.get("connection", "").lower() != "keep-alive":
                return

    def send(self, code, doc, extra=""):
        body = json.dumps(doc).encode()
        self.wfile.write(
            f"HTTP/1.1 {code} X\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n{extra}\r\n".encode()
            + body
        )

    def answer_check(self, method, path, target):
        """Answer the proxy's own inspect and list calls from the fake objects."""
        objs = self.server.objects
        parts = path.strip("/").split("/")
        if method != "GET":
            return False
        if parts[0] == "containers" and len(parts) == 3 and parts[2] == "json" and ("container", parts[1]) in objs:
            self.send(200, objs[("container", parts[1])])
            return True
        if parts[0] == "images" and parts[-1] == "json" and len(parts) >= 3:
            key = ("image", "/".join(parts[1:-1]))
            self.send(200, objs[key]) if key in objs else self.send(404, {})
            return True
        if parts[0] == "exec" and len(parts) == 3 and parts[2] == "json":
            key = ("exec", parts[1])
            self.send(200, objs[key]) if key in objs else self.send(404, {})
            return True
        if parts[0] == "networks" and len(parts) == 1 and "label" in target and "io.evalskills" in target:
            self.send(200, [v for (k, _), v in objs.items() if k == "network" and v["Labels"]])
            return True
        if parts[0] == "networks" and len(parts) == 2 and ("network", parts[1]) in objs:
            self.send(200, objs[("network", parts[1])])
            return True
        if parts[0] == "containers" and len(parts) == 3 and parts[2] == "json":
            self.send(404, {})
            return True
        return False

    def forwarded(self, method, path, headers, body):
        if headers.get("upgrade") == "tcp":
            self.wfile.write(b"HTTP/1.1 101 UPGRADED\r\nConnection: Upgrade\r\nUpgrade: tcp\r\n\r\n")
            self.wfile.flush()
            data = self.request.recv(1024)
            self.wfile.write(b"echo:" + data)
            return
        self.send(200, {"ok": True, "path": path}, "Connection: close\r\n")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(dp, "ABOVE_ROOT_UIDS", {0, os.getuid()})
    sockdir = Path("/tmp") / f"dp-{os.getpid()}-{id(tmp_path)}"  # unix socket paths must be short
    sockdir.mkdir()
    daemon = FakeDaemon(str(sockdir / "d.sock"))
    threading.Thread(target=daemon.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    root = tmp_path / "run"
    (root / "t1" / "logs").mkdir(parents=True)
    cfg = dp.load_config(
        {
            "listen": str(sockdir / "p.sock"),
            "listen_group": "",
            "upstream": str(sockdir / "d.sock"),
            "bind_root": str(root),
            "eval_uid": os.getuid() or 1000,
        }
    )
    proxy = dp.make_server(cfg)
    threading.Thread(target=proxy.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield {"daemon": daemon, "sock": cfg["listen"], "root": root, "cfg": cfg}
    proxy.shutdown()
    daemon.shutdown()
    for p in sockdir.iterdir():
        p.unlink()
    sockdir.rmdir()


def raw(env, data: bytes, read_after: bytes = b"") -> bytes:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(5)
    s.connect(env["sock"])
    s.sendall(data)
    out = b""
    try:
        while chunk := s.recv(65536):
            out += chunk
            if read_after and b"\r\n\r\n" in out:
                s.sendall(read_after)
                read_after = b""
    except TimeoutError:
        pass
    s.close()
    return out


def call(env, method, target, body=None):
    data = b"" if body is None else json.dumps(body).encode()
    head = f"{method} {target} HTTP/1.1\r\nHost: docker\r\nContent-Type: application/json\r\n"
    head += f"Content-Length: {len(data)}\r\n\r\n"
    out = raw(env, head.encode() + data)
    status = int(out.split(b" ", 2)[1])
    return status, json.loads(out.partition(b"\r\n\r\n")[2] or b"{}")


def forwarded(env):
    return [(m, t) for m, t, _, _ in env["daemon"].seen]


def create_body(env, **host):
    return {
        "Image": "trial-img",
        "Cmd": ["sh", "-c", "sleep infinity"],
        "Labels": {"com.docker.compose.project": "p"},
        "HostConfig": {
            "NetworkMode": "trialnet",
            "Privileged": False,
            "MaskedPaths": None,
            "Binds": [f"{env['root']}/t1/logs:/logs/agent:z"],
            **host,
        },
        "NetworkingConfig": {"EndpointsConfig": {"trialnet": {"Aliases": ["main"]}}},
    }


# ------------------------------------------------------------------ routes


def test_versioned_and_plain_paths_route_the_same(env):
    assert call(env, "GET", "/v1.47/_ping")[0] == 200
    assert call(env, "GET", "/_ping")[0] == 200
    assert forwarded(env) == [("GET", "/v1.47/_ping"), ("GET", "/_ping")]


@pytest.mark.parametrize(
    "method,target",
    [
        ("POST", "/v1.47/build"),
        ("POST", "/session"),
        ("POST", "/grpc"),
        ("POST", "/v1.47/images/create?fromImage=x"),
        ("POST", "/containers/prune"),
        ("GET", "/containers/mine/export"),
        ("POST", "/containers/mine/update"),
        ("POST", "/v1.47/swarm/init"),
        ("GET", "/v1.47/containers/mine%2F../json"),
        ("GET", "/v1.47//containers/json"),
        ("POST", "/volumes/create"),
        ("POST", "/plugins/pull"),
    ],
)
def test_unlisted_routes_are_refused_and_never_forwarded(env, method, target):
    status, doc = call(env, method, target, {} if method != "GET" else None)
    assert status == 403 and "evalskills docker proxy" in doc["message"]
    assert env["daemon"].seen == []


def test_someone_elses_container_is_invisible(env):
    for target in ("/v1.47/containers/theirs/json", "/containers/theirs/start", "/containers/theirs/archive?path=/"):
        method = "POST" if target.endswith("start") else "GET"
        assert call(env, method, target)[0] == 403
    assert call(env, "DELETE", "/v1.47/containers/theirs")[0] == 403
    assert call(env, "POST", "/v1.47/containers/mine/start")[0] == 200
    assert forwarded(env) == [("POST", "/v1.47/containers/mine/start")]


def test_lists_and_events_are_filtered_to_the_owner_label(env):
    filters = json.dumps({"label": ["com.docker.compose.project=p"]})
    call(env, "GET", "/v1.47/containers/json?all=1&filters=" + filters.replace(" ", ""))
    call(env, "GET", "/v1.47/events")
    for _, target in forwarded(env):
        from urllib.parse import parse_qs, urlsplit

        got = json.loads(parse_qs(urlsplit(target).query)["filters"][0])
        assert f"{dp.OWNER_LABEL}=1" in got["label"]
    first = parse_qs(urlsplit(forwarded(env)[0][1]).query)
    assert "com.docker.compose.project=p" in json.loads(first["filters"][0])["label"] and first["all"] == ["1"]


def test_none_network_mode_is_allowed(env):
    body = create_body(env, NetworkMode="none")
    body["NetworkingConfig"] = None
    assert call(env, "POST", "/containers/create", body)[0] == 200


def test_foreign_image_metadata_is_not_readable(env):
    assert call(env, "GET", "/v1.47/images/nginx/json")[0] == 403  # its Config.Env could hold secrets
    assert call(env, "GET", "/v1.47/images/trial-img/json")[0] == 200


def test_only_owned_images_may_be_deleted(env):
    assert call(env, "DELETE", "/v1.47/images/nginx")[0] == 403
    assert call(env, "DELETE", "/v1.47/images/trial-img")[0] == 200


# ------------------------------------------------------------------ container create


def test_create_forces_runsc_and_the_owner_label(env):
    status, _ = call(env, "POST", "/v1.47/containers/create?name=p-main-1", create_body(env))
    assert status == 200
    _, target, _, body = env["daemon"].seen[0]
    sent = json.loads(body)
    assert target == "/v1.47/containers/create?name=p-main-1"
    assert sent["HostConfig"]["Runtime"] == "runsc"
    assert sent["Labels"] == {"com.docker.compose.project": "p", dp.OWNER_LABEL: "1"}


@pytest.mark.parametrize(
    "host,why",
    [
        ({"Privileged": True}, "Privileged"),
        ({"Runtime": "runc"}, "Runtime"),
        ({"CapAdd": ["SYS_ADMIN"]}, "CapAdd"),
        ({"PidMode": "host"}, "PidMode"),
        ({"NetworkMode": "host"}, "NetworkMode"),
        ({"NetworkMode": ""}, "NetworkMode"),  # "", default, bridge and a missing mode all mean docker0, with NAT
        ({"NetworkMode": "default"}, "NetworkMode"),
        ({"NetworkMode": "bridge"}, "NetworkMode"),
        ({"NetworkMode": None}, "NetworkMode"),
        ({"NetworkMode": "container:theirs"}, "NetworkMode"),
        ({"NetworkMode": "appnet"}, "NetworkMode"),
        ({"UsernsMode": "host"}, "UsernsMode"),
        ({"Devices": [{"PathOnHost": "/dev/kvm"}]}, "Devices"),
        ({"SecurityOpt": ["seccomp=unconfined"]}, "SecurityOpt"),
        ({"MaskedPaths": []}, "MaskedPaths"),
        ({"PortBindings": {"80/tcp": [{"HostPort": "80"}]}}, "PortBindings"),
        ({"Sysctls": {"net.ipv4.ip_forward": "1"}}, "Sysctls"),
        ({"VolumesFrom": ["theirs"]}, "VolumesFrom"),
        ({"CgroupParent": "/"}, "CgroupParent"),
        ({"Binds": ["/etc:/host-etc"]}, "outside the trial root"),
        ({"Binds": ["/var/run/docker.sock:/var/run/docker.sock"]}, "outside the trial root"),
        ({"Mounts": [{"Type": "volume", "Source": "x", "Target": "/x"}]}, "Type"),
        ({"BrandNewField": 1}, "unknown field HostConfig.BrandNewField"),
    ],
)
def test_create_refuses_dangerous_host_config(env, host, why):
    status, doc = call(env, "POST", "/v1.47/containers/create", create_body(env, **host))
    assert status == 403 and why in doc["message"]
    assert env["daemon"].seen == []


def test_create_refuses_unknown_top_level_fields_and_foreign_images(env):
    body = create_body(env)
    body["Image"] = "nginx"
    assert "not an image built for a trial" in call(env, "POST", "/containers/create", body)[1]["message"]
    body = create_body(env)
    body["Future"] = True
    assert "unknown field Config.Future" in call(env, "POST", "/containers/create", body)[1]["message"]
    for key in ("appnet", "default", "bridge", ""):
        body = create_body(env)
        body["NetworkingConfig"]["EndpointsConfig"] = {key: {}}
        assert call(env, "POST", "/containers/create", body)[0] == 403, key
    body = create_body(env)
    del body["HostConfig"]["NetworkMode"]
    assert call(env, "POST", "/containers/create", body)[0] == 403
    assert env["daemon"].seen == []


def test_binds_must_stay_inside_one_trial_folder_without_symlinks(env, tmp_path):
    root = env["root"]
    (root / "t1" / "link").symlink_to("/etc")
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "t2").symlink_to(outside)
    for src, why in [
        (f"{root}/t1", "inside one trial's folder"),
        (f"{root}/t1/link", "symlink"),
        (f"{root}/t2/x", "symlink"),
        (f"{root}/t1/../../outside", "normalized"),
        (f"{root}/t1/missing", "does not exist"),
        (f"{root}x/t1/logs", "outside the trial root"),
    ]:
        status, doc = call(env, "POST", "/containers/create", create_body(env, Binds=[f"{src}:/x"]))
        assert status == 403 and why in doc["message"], src
    status, _ = call(
        env,
        "POST",
        "/containers/create",
        create_body(env, Binds=None, Mounts=[{"Type": "bind", "Source": f"{root}/t1/logs", "Target": "/logs"}]),
    )
    assert status == 200


def test_bind_owner_must_be_the_eval_user(env):
    env["cfg"]["eval_uid"] = os.getuid() + 1
    status, doc = call(env, "POST", "/containers/create", create_body(env))
    assert status == 403 and "wrong owner" in doc["message"]


def test_resolv_conf_bind_must_be_read_only_and_name_only_localhost(env):
    resolv = env["root"] / "t1" / "resolv.conf"
    resolv.write_text("nameserver 127.0.0.1\n")
    ok = [f"{resolv}:/etc/resolv.conf:ro"]
    assert call(env, "POST", "/containers/create", create_body(env, Binds=ok))[0] == 200
    assert (
        "read-only"
        in call(env, "POST", "/containers/create", create_body(env, Binds=[f"{resolv}:/etc/resolv.conf"]))[1]["message"]
    )
    resolv.write_text("nameserver 8.8.8.8\n")
    assert "127.0.0.1" in call(env, "POST", "/containers/create", create_body(env, Binds=ok))[1]["message"]


# ------------------------------------------------------------------ exec, networks


def test_exec_privileged_and_foreign_exec_are_refused(env):
    assert call(env, "POST", "/v1.47/containers/mine/exec", {"Cmd": ["id"], "Privileged": True})[0] == 403
    assert call(env, "POST", "/v1.47/containers/theirs/exec", {"Cmd": ["id"]})[0] == 403
    assert call(env, "POST", "/v1.47/exec/e-theirs/start", {"Detach": True})[0] == 403
    assert call(env, "POST", "/v1.47/containers/mine/exec", {"Cmd": ["id"], "AttachStdout": True})[0] == 200
    assert call(env, "POST", "/v1.47/exec/e-mine/start", {"Detach": True})[0] == 200
    assert forwarded(env) == [("POST", "/v1.47/containers/mine/exec"), ("POST", "/v1.47/exec/e-mine/start")]


def test_network_create_is_internal_without_masquerade_in_a_free_pool_subnet(env):
    status, _ = call(
        env, "POST", "/v1.47/networks/create", {"Name": "p_default", "Driver": "bridge", "Labels": {"a": "b"}}
    )
    assert status == 200
    sent = json.loads(env["daemon"].seen[0][3])
    assert sent["Internal"] is True and sent["EnableIPv6"] is False
    assert sent["Options"] == {"com.docker.network.bridge.enable_ip_masquerade": "false"}
    assert sent["IPAM"]["Config"] == [{"Subnet": "172.31.240.16/28"}]  # .0/28 is taken by trialnet
    assert sent["Labels"] == {"a": "b", dp.OWNER_LABEL: "1"}


def test_concurrent_network_creates_get_different_subnets(env):
    policy = dp.Policy(env["cfg"], dp.Daemon(env["cfg"]["upstream"]))
    first = policy.rewrite_network_create({"Name": "a"})
    second = policy.rewrite_network_create({"Name": "b"})  # before the first create has returned
    assert first["IPAM"]["Config"] != second["IPAM"]["Config"]
    policy.release_subnet(first)
    assert policy.rewrite_network_create({"Name": "c"})["IPAM"]["Config"] == first["IPAM"]["Config"]


def test_a_network_create_releases_its_reservation(env):
    assert call(env, "POST", "/networks/create", {"Name": "a"})[0] == 200
    assert call(env, "POST", "/networks/create", {"Name": "b"})[0] == 200
    sent = [json.loads(b)["IPAM"]["Config"] for _, _, _, b in env["daemon"].seen]
    assert sent[0] == sent[1]  # the fake daemon never records it, so the subnet is free again


@pytest.mark.parametrize(
    "extra",
    [{"Driver": "macvlan"}, {"Options": {"parent": "eth0"}}, {"IPAM": {"Config": [{"Subnet": "10.0.0.0/8"}]}}],
)
def test_network_create_refuses_other_drivers_options_and_subnets(env, extra):
    assert call(env, "POST", "/networks/create", {"Name": "n", **extra})[0] == 403
    assert env["daemon"].seen == []


def test_network_connect_needs_both_to_be_owned(env):
    assert call(env, "POST", "/networks/appnet/connect", {"Container": "mine"})[0] == 403
    assert call(env, "POST", "/networks/trialnet/connect", {"Container": "theirs"})[0] == 403
    assert call(env, "POST", "/networks/trialnet/connect", {"Container": "mine"})[0] == 200


# ------------------------------------------------------------------ framing


def test_exec_start_hijack_streams_both_ways_after_101(env):
    body = json.dumps({"Detach": False, "Tty": False}).encode()
    head = (
        "POST /v1.47/exec/e-mine/start HTTP/1.1\r\nHost: docker\r\nContent-Type: application/json\r\n"
        f"Connection: Upgrade\r\nUpgrade: tcp\r\nContent-Length: {len(body)}\r\n\r\n"
    ).encode()
    out = raw(env, head + body, read_after=b"stdin-bytes")
    assert out.startswith(b"HTTP/1.1 101") and out.endswith(b"echo:stdin-bytes")


def test_attach_hijack_on_a_foreign_container_is_refused(env):
    head = b"POST /v1.47/containers/theirs/attach?stream=1 HTTP/1.1\r\nHost: d\r\nUpgrade: tcp\r\n\r\n"
    assert raw(env, head).startswith(b"HTTP/1.1 403")
    assert env["daemon"].seen == []


def test_a_second_request_on_the_same_connection_is_never_forwarded(env):
    smuggled = b"POST /v1.47/build HTTP/1.1\r\nHost: d\r\nContent-Length: 0\r\n\r\n"
    out = raw(env, b"GET /_ping HTTP/1.1\r\nHost: d\r\n\r\n" + smuggled)
    assert out.startswith(b"HTTP/1.1 200")
    assert forwarded(env) == [("GET", "/_ping")]
    assert env["daemon"].seen[0][2]["connection"] == "close"


def test_chunked_archive_upload_is_re_framed_and_stops_at_the_end(env):
    head = b"PUT /v1.47/containers/mine/archive?path=/x HTTP/1.1\r\nHost: d\r\nTransfer-Encoding: chunked\r\n\r\n"
    body = b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n"
    out = raw(env, head + body + b"GET /info HTTP/1.1\r\n\r\n")
    assert out.startswith(b"HTTP/1.1 200")
    assert [(m, b) for m, _, _, b in env["daemon"].seen] == [("PUT", b"hello world")]


@pytest.mark.parametrize(
    "head",
    [
        b"POST /containers/mine/start HTTP/1.1\r\nContent-Length: 3\r\nTransfer-Encoding: chunked\r\n\r\n",
        b"POST /containers/mine/start HTTP/1.1\r\nContent-Length: 3\r\nContent-Length: 4\r\n\r\n",
        b"GET /_ping HTTP/1.1\r\nBad Header\r\n\r\n",
        b"GET /_ping HTTP/2.0\r\n\r\n",
    ],
)
def test_ambiguous_framing_is_refused(env, head):
    assert raw(env, head + b"abc").startswith(b"HTTP/1.1 4")
    assert env["daemon"].seen == []


def test_client_headers_are_not_passed_through(env):
    call(env, "GET", "/_ping")
    raw(env, b"GET /_ping HTTP/1.1\r\nHost: d\r\nX-Registry-Auth: c3ludGhldGlj\r\n\r\n")
    assert all("x-registry-auth" not in h for _, _, h, _ in env["daemon"].seen)


# ------------------------------------------------------------------ firewall, config


class FakeIptables:
    def __init__(self, chains):
        self.chains = chains

    def __call__(self, argv, **_):
        chain = argv[-1]
        rules = self.chains.get(chain)

        class P:
            returncode = 0 if rules is not None else 1
            stdout = "\n".join([f"-N {chain}", *(rules or [])])

        return P()


GOOD = {
    "FORWARD": ["-A FORWARD -s 172.31.240.0/20 -j EVALS-EGRESS", "-A FORWARD -j DOCKER-USER"],
    "DOCKER-USER": ["-A DOCKER-USER -s 172.31.240.0/20 -j EVALS-EGRESS", "-A DOCKER-USER -j RETURN"],
    "INPUT": ["-A INPUT -s 172.31.240.0/20 -j EVALS-INPUT"],
    "EVALS-EGRESS": ["-A EVALS-EGRESS -d 172.31.248.1/32 -p tcp -j ACCEPT", "-A EVALS-EGRESS -j DROP"],
    "EVALS-INPUT": ["-A EVALS-INPUT -j DROP"],
}


def test_firewall_status_needs_every_jump_first_and_drop_last():
    cfg = dp.load_config({"eval_uid": 1000})
    assert dp.firewall_status(cfg, FakeIptables(GOOD)) == (True, "ok")
    open_end = {**GOOD, "EVALS-EGRESS": GOOD["EVALS-EGRESS"][:1]}
    assert "DROP" in dp.firewall_status(cfg, FakeIptables(open_end))[1]
    docker_on_top = {**GOOD, "FORWARD": ["-A FORWARD -j DOCKER-USER", *GOOD["FORWARD"]]}
    assert dp.firewall_status(cfg, FakeIptables(docker_on_top)) == (True, "ok")  # after every dockerd start
    other_on_top = {**GOOD, "FORWARD": ["-A FORWARD -j ACCEPT", *GOOD["FORWARD"]]}
    assert dp.firewall_status(cfg, FakeIptables(other_on_top))[0] is False
    user_moved = {**GOOD, "DOCKER-USER": GOOD["DOCKER-USER"][::-1]}
    assert dp.firewall_status(cfg, FakeIptables(docker_on_top | user_moved))[0] is False
    missing = {k: v for k, v in GOOD.items() if k != "EVALS-INPUT"}
    assert dp.firewall_status(cfg, FakeIptables(missing))[0] is False


@pytest.mark.parametrize(
    "raw_cfg",
    [
        {},
        {"eval_uid": 0},
        {"eval_uid": 1000, "bind_root": "/"},
        {"eval_uid": 1000, "bind_root": "/srv/../etc"},
        {"eval_uid": 1000, "network_pool": "10.0.0.0/8"},
        {"eval_uid": 1000, "typo": 1},
        {"eval_uid": 1000, "idle_timeout_s": 600},  # shorter than a trial
    ],
)
def test_bad_config_is_refused(raw_cfg):
    with pytest.raises(dp.ConfigError):
        dp.load_config(raw_cfg)


def test_example_config_loads():
    path = Path(__file__).resolve().parents[1] / "deploy" / "gvisor" / "docker-proxy.example.json"
    cfg = dp.load_config(json.loads(path.read_text()))
    assert cfg["bind_root"] == "/srv/evals/run" and cfg["network_pool"].subnet_of(cfg["firewall_scope"])


def test_socket_is_born_0660_and_its_folder_must_be_private(env, tmp_path):
    assert os.stat(env["sock"]).st_mode & 0o777 == 0o660
    shared = Path("/tmp") / f"dp-shared-{os.getpid()}"
    shared.mkdir()
    try:
        shared.chmod(0o777)
        with pytest.raises(dp.ConfigError):
            dp.make_server({**env["cfg"], "listen": str(shared / "p.sock")})
    finally:
        shared.rmdir()
