import json
import socket

import pytest


def raw_post(addr, path, body: bytes, length: str) -> tuple[int, dict]:
    with socket.create_connection(addr, timeout=5) as s:
        s.sendall(
            f"POST {path} HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nContent-Length: {length}\r\n"
            f"Connection: close\r\n\r\n".encode()
            + body
        )
        s.shutdown(socket.SHUT_WR)  # an unbounded read would otherwise hang until this close, then answer
        data = b""
        while chunk := s.recv(65536):
            data += chunk
    head, _, payload = data.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(payload or b"null")


@pytest.mark.parametrize("length", ["-1", "20001", "abc"])
def test_a_content_length_out_of_range_is_refused(app, length):
    code, body = raw_post(app, "/api/discover", b"{}", length)
    assert code == 400 and body == {"error": "invalid JSON body"}


@pytest.mark.parametrize("payload", [b"[]", b'"x"', b"1", b"null"])
@pytest.mark.parametrize("path", ["/api/discover", "/api/submit"])
def test_a_body_that_is_not_an_object_gets_a_400(app, path, payload):
    code, body = raw_post(app, path, payload, str(len(payload)))
    assert code == 400 and body == {"error": "invalid JSON body"}
