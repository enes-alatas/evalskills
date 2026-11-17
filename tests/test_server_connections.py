"""#171: the public server drops a silent connection, and serves only so many at once."""

import socket
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
import server  # noqa: E402


@pytest.fixture
def serve(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DB_PATH", tmp_path / "inventory.db")
    monkeypatch.setattr(server.ev, "evaluator_version", lambda: {"version": "0", "commit": "e" * 40})
    server.init_db()
    started = []

    def start(limit=8, timeout=0.5, deadline=60, per_client=4):
        monkeypatch.setattr(server, "MAX_CONNECTIONS_PER_CLIENT", per_client)
        monkeypatch.setattr(server.H, "timeout", timeout)
        monkeypatch.setattr(server, "REQUEST_DEADLINE_S", deadline)
        httpd = server.Server(("127.0.0.1", 0), server.H, limit=limit)
        threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        started.append(httpd)
        return httpd.server_address

    yield start
    for httpd in started:
        httpd.shutdown()
        httpd.server_close()


def closed_within(sock: socket.socket, seconds: float) -> bool:
    sock.settimeout(seconds)
    try:
        return sock.recv(1) == b""
    except TimeoutError:
        return False
    except ConnectionResetError:
        return True


def get(addr, client: str | None = None) -> bytes:
    with socket.create_connection(addr, timeout=5) as s:
        xff = f"X-Forwarded-For: {client}\r\n".encode() if client else b""
        s.sendall(b"GET /api/meta HTTP/1.0\r\nHost: x\r\n" + xff + b"\r\n")
        return s.recv(64)


def test_the_handler_has_a_timeout():
    assert server.H.timeout == server.REQUEST_TIMEOUT_S > 0


def test_a_silent_connection_is_closed_within_the_timeout(serve):
    addr = serve(timeout=0.5)
    with socket.create_connection(addr) as s:
        t = time.monotonic()
        assert closed_within(s, 5)
        assert time.monotonic() - t < 3


def test_a_connection_sending_one_byte_then_stalling_is_closed(serve):
    addr = serve(timeout=0.5)
    with socket.create_connection(addr) as s:
        s.sendall(b"G")
        assert closed_within(s, 5)


def drip(sock: socket.socket, data: bytes, every: float, until: float) -> bool:
    """Send one byte at a time, each well inside the per-read timeout. → True if the server closed the connection."""
    for b in data:
        if time.monotonic() > until:
            return False
        try:
            sock.sendall(bytes([b]))
        except (BrokenPipeError, ConnectionResetError):
            return True
        time.sleep(every)
        sock.setblocking(False)
        try:
            sock.recv(1024)  # a close (b"") or any answer: the server is done with this request
            return True
        except BlockingIOError:
            pass
        except ConnectionResetError:
            return True
        finally:
            sock.setblocking(True)
    return False


def test_headers_dripped_one_byte_at_a_time_are_cut_off_at_the_deadline(serve):
    addr = serve(timeout=0.5, deadline=1.0)
    request = b"GET /api/meta HTTP/1.0\r\n" + b"X-Slow: " + b"a" * 400 + b"\r\n\r\n"
    with socket.create_connection(addr) as s:
        t = time.monotonic()
        assert drip(s, request, every=0.05, until=t + 6)  # each byte well inside 0.5 s; the request takes ~20 s
        assert time.monotonic() - t < 4


def test_a_body_dripped_one_byte_at_a_time_is_cut_off_at_the_deadline(serve):
    addr = serve(timeout=0.5, deadline=1.0)
    body = b'{"repo": "' + b"a" * 400 + b'"}'
    head = b"POST /api/submit HTTP/1.0\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n" % len(body)
    with socket.create_connection(addr) as s:
        s.sendall(head)
        t = time.monotonic()
        assert drip(s, body, every=0.05, until=t + 6)
        assert time.monotonic() - t < 4


def test_a_request_inside_the_deadline_is_served(serve):
    addr = serve(timeout=0.5, deadline=1.0)
    assert get(addr).startswith(b"HTTP/1.0 200")


def test_beyond_the_limit_a_connection_is_closed_and_the_site_recovers(serve):
    addr = serve(limit=2, timeout=2)
    holders = [socket.create_connection(addr) for _ in range(2)]
    time.sleep(0.3)  # both accepted and holding a slot
    try:
        with socket.create_connection(addr) as extra:
            assert closed_within(extra, 1.5)  # refused on arrival, long before the 2 s timeout
    finally:
        for h in holders:
            h.close()
    time.sleep(0.3)
    assert get(addr).startswith(b"HTTP/1.0 ")  # slots come back once the holders go


def test_a_limit_below_one_is_refused():
    with pytest.raises(ValueError):
        server.Server(("127.0.0.1", 0), server.H, limit=0)


def test_one_client_is_capped_while_another_is_still_served(serve):
    """#171 review: with only a global cap, one client re-dripping MAX_CONNECTIONS bodies keeps everyone out. Only
    requests with a body count, so a visitor's own page load (all GETs) is never refused."""
    addr = serve(limit=8, timeout=2, deadline=10, per_client=2)
    head = (
        b"POST /api/submit HTTP/1.0\r\nX-Forwarded-For: 192.0.2.1\r\n"
        b"Content-Type: application/json\r\nContent-Length: 100\r\n\r\n"
    )
    holders = []
    try:
        for _ in range(2):  # client A holds its cap: headers in, body never finished
            h = socket.create_connection(addr)
            h.sendall(head)
            holders.append(h)
        time.sleep(0.3)
        with socket.create_connection(addr, timeout=5) as third:  # A's next body, refused before it is read
            third.sendall(head)
            assert third.recv(64).startswith(b"HTTP/1.0 429")
        assert get(addr, "192.0.2.1").startswith(b"HTTP/1.0 200")  # a GET from A, at its cap, still gets through
        assert get(addr, "192.0.2.2").startswith(b"HTTP/1.0 200")  # B is still served
    finally:
        for h in holders:
            h.close()
    time.sleep(0.3)
    assert server._CLIENT_CONNECTIONS == {}  # A's slots come back once its connections end
