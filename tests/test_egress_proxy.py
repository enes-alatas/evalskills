"""The trial egress proxy (#123, item 6): allowlisted hostnames only, public addresses only, no paths logged."""

import ipaddress
import json
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gateway"))
import egress_proxy as ep  # noqa: E402

ALLOWED = "registry.example.org"


class Origin(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = f"path={self.path} host={self.headers['Host']}".encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(server):
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return server.server_address[1]


@pytest.fixture
def proxy():
    origin = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
    oport = serve(origin)

    def resolver(host, port, type=None):  # every allowed name points at the local origin
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", oport))]

    cfg = {
        **ep.DEFAULTS,
        "allowed_hosts": {ALLOWED},
        "allowed_clients": [ipaddress.ip_network("127.0.0.0/8")],
        "idle_timeout_s": 5,
    }
    srv = ThreadingHTTPServer(("127.0.0.1", 0), ep.make_handler(cfg, resolver, public=lambda ip: True))
    srv.daemon_threads = True
    port = serve(srv)
    yield port
    srv.shutdown()
    origin.shutdown()


def raw(port, data: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(data)
        out = b""
        while chunk := s.recv(65536):
            out += chunk
    return out


def test_plain_get_to_an_allowed_host(proxy):
    out = raw(proxy, f"GET http://{ALLOWED}/dists/stable/Release HTTP/1.1\r\nHost: {ALLOWED}\r\n\r\n".encode())
    assert out.startswith(b"HTTP/1.0 200") or out.startswith(b"HTTP/1.1 200")
    assert f"path=/dists/stable/Release host={ALLOWED}".encode() in out


@pytest.mark.parametrize(
    "request_line, code",
    [
        ("GET http://other.example.org/ HTTP/1.1", b" 403 "),
        ("GET http://127.0.0.1/ HTTP/1.1", b" 403 "),
        (f"GET https://{ALLOWED}/ HTTP/1.1", b" 403 "),
        (f"GET http://{ALLOWED}:8080/ HTTP/1.1", b" 403 "),
        (f"POST http://{ALLOWED}/ HTTP/1.1", b" 405 "),
        ("CONNECT other.example.org:443 HTTP/1.1", b" 403 "),
        (f"CONNECT {ALLOWED}:22 HTTP/1.1", b" 403 "),
        ("CONNECT 10.0.0.1:443 HTTP/1.1", b" 403 "),
    ],
)
def test_everything_else_is_refused(proxy, request_line, code):
    out = raw(proxy, f"{request_line}\r\nHost: x\r\nContent-Length: 0\r\n\r\n".encode())
    assert code in out.split(b"\r\n", 1)[0]


def test_connect_tunnels_to_an_allowed_host(proxy):
    with socket.create_connection(("127.0.0.1", proxy), timeout=5) as s:
        s.sendall(f"CONNECT {ALLOWED}:443 HTTP/1.1\r\nHost: {ALLOWED}:443\r\n\r\n".encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += s.recv(1)
        assert b" 200 " in head.split(b"\r\n", 1)[0]
        s.sendall(b"GET /through-the-tunnel HTTP/1.0\r\nHost: t\r\n\r\n")  # the fake origin speaks plain HTTP
        out = b""
        while chunk := s.recv(65536):
            out += chunk
    assert b"path=/through-the-tunnel" in out


@pytest.mark.parametrize("addr", ["127.0.0.1", "10.1.2.3", "172.31.248.1", "169.254.169.254", "192.168.1.1", "::1"])
def test_names_resolving_to_private_addresses_are_refused(addr):
    def resolver(host, port, type=None):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, port))]

    assert ep.resolve_public(ALLOWED, 443, resolver) == []


def test_one_private_address_refuses_the_whole_name():
    def resolver(host, port, type=None):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
        ]

    assert ep.resolve_public(ALLOWED, 443, resolver) == []
    assert ep.is_public(ipaddress.ip_address("93.184.215.14"))


def test_allowlist_is_exact():
    hosts = {ALLOWED}
    assert ep.allowed(ALLOWED.upper() + ".", hosts)
    assert not ep.allowed("evil." + ALLOWED, hosts)
    assert not ep.allowed(ALLOWED + ".evil.example", hosts)


@pytest.mark.parametrize("hosts", [[], ["*.example.org"], ["host:443"], ["http://x.org"], [""]])
def test_config_needs_bare_hostnames(tmp_path, hosts):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"allowed_hosts": hosts}))
    with pytest.raises(ep.ConfigError):
        ep.load_config(str(p))


def test_log_carries_no_path(proxy, capfd):
    raw(proxy, f"GET http://{ALLOWED}/secret-looking-path HTTP/1.1\r\nHost: {ALLOWED}\r\n\r\n".encode())
    assert "secret-looking-path" not in capfd.readouterr().err


def test_a_bad_port_is_refused_not_crashed(proxy):
    out = raw(proxy, f"GET http://{ALLOWED}:abc/ HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    assert b" 400 " in out.split(b"\r\n", 1)[0]


def test_only_allowed_clients_may_use_it():
    origin_calls = []

    def resolver(host, port, type=None):
        origin_calls.append(host)
        return []

    cfg = {**ep.DEFAULTS, "allowed_hosts": {ALLOWED}, "allowed_clients": [ipaddress.ip_network("172.31.240.0/20")]}
    srv = ThreadingHTTPServer(("127.0.0.1", 0), ep.make_handler(cfg, resolver))
    srv.daemon_threads = True
    port = serve(srv)
    out = raw(port, f"CONNECT {ALLOWED}:443 HTTP/1.1\r\nHost: x\r\n\r\n".encode())  # from 127.0.0.1
    srv.shutdown()
    assert b" 403 " in out.split(b"\r\n", 1)[0]
    assert origin_calls == []  # refused before any lookup


def test_handler_has_a_socket_timeout():
    assert ep.make_handler({**ep.DEFAULTS, "allowed_hosts": {ALLOWED}}).timeout == 60


@pytest.mark.parametrize("clients", [[], ["not-a-network"], "172.31.240.0/20"])
def test_config_needs_client_networks(tmp_path, clients):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"allowed_hosts": [ALLOWED], "allowed_clients": clients}))
    with pytest.raises(ep.ConfigError):
        ep.load_config(str(p))
