"""A check after the steps (#137, #181): one trial through the socket proxy, as the runner would start it. No API
budget, no skill code.

Run as the trial user, with the proxy started and a probe image built (README, "Checks after the steps"):
    sudo -u evals python3 -I /opt/evalskills/deploy/gvisor/host/probe.py evalskills/probe:host

Through the proxy it checks that a create asking for runc is refused. It then makes one trial network, runs one
container under runsc there that tries each target below, and removes both, also when a check fails. It exits 0 only
when every target is reached or refused as expected and nothing is left behind; otherwise it names what is left."""

import http.client
import json
import os
import socket
import sys

SOCK = os.environ.get("EVALS_PROXY_SOCKET", "/run/evalskills/docker.sock")
TARGETS = [  # (host, port, reachable): what a trial may reach, and what it must not
    ("evals-gateway", 8443, True),  # the model gateway, attached to the trial's network by the proxy
    ("evals-proxy", 3128, True),  # the egress proxy, likewise
    ("172.31.248.2", 8443, False),  # the gateway's address on the services' own network
    ("172.31.248.1", 22, False),  # the host, on the services' bridge (sshd listens on every address)
    ("172.17.0.1", 22, False),  # the host, on docker0
    ("1.1.1.1", 443, False),  # the internet
]
# Runs inside the trial: one line per target, exit 1 on any surprise.
PROBE = """
import json, socket, sys
bad = 0
for host, port, want in json.loads(sys.argv[1]):
    try:
        socket.create_connection((host, port), timeout=4).close()
        got = True
    except OSError:
        got = False
    print(f"{host}:{port} reached={got} expected={want}")
    bad += got != want
sys.exit(1 if bad else 0)
"""


class Conn(http.client.HTTPConnection):
    def __init__(self):
        super().__init__("docker", timeout=120)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(120)
        self.sock.connect(SOCK)


def call(method: str, url: str, body=None) -> tuple[int, bytes]:
    c = Conn()
    data = None if body is None else json.dumps(body)
    c.request(method, "/v1.47" + url, body=data, headers={"Content-Type": "application/json"} if data else {})
    r = c.getresponse()
    out = r.read()
    c.close()
    return r.status, out


def ok(method: str, url: str, body=None) -> bytes:
    status, out = call(method, url, body)
    if status not in (200, 201, 204):
        raise SystemExit(f"probe: {method} {url}: {status} {out[:300]!r}")
    return out


def demux(b: bytes) -> str:
    out = b""
    while len(b) >= 8:
        n = int.from_bytes(b[4:8], "big")
        out, b = out + b[8 : 8 + n], b[8 + n :]
    return out.decode(errors="replace")


def trial(image: str, net: str, made: list) -> int:
    status, out = call(
        "POST", "/containers/create", {"Image": image, "HostConfig": {"NetworkMode": net, "Runtime": "runc"}}
    )
    if status in (200, 201):
        made.append(json.loads(out)["Id"])
    if status != 403 or b"HostConfig.Runtime" not in out:  # refused for the runtime, not for something else
        print(f"probe: a create asking for runc was not refused for its runtime: {status} {out[:300]!r}")
        return 1
    print("a create asking for runc: refused (HostConfig.Runtime)")
    cmd = ["python", "-I", "-c", PROBE, json.dumps(TARGETS)]
    cid = json.loads(
        ok("POST", "/containers/create", {"Image": image, "Cmd": cmd, "HostConfig": {"NetworkMode": net}})
    )["Id"]
    made.append(cid)
    ok("POST", f"/containers/{cid}/start")
    code = json.loads(ok("POST", f"/containers/{cid}/wait"))["StatusCode"]
    print(demux(ok("GET", f"/containers/{cid}/logs?stdout=1&stderr=1")), end="")
    runtime = json.loads(ok("GET", f"/containers/{cid}/json"))["HostConfig"]["Runtime"]
    print(f"runtime={runtime}")
    return 0 if code == 0 and runtime == "runsc" else 1


def cleanup(net: str, made: list) -> list:
    """Removes the containers, then the network (which detaches the services); returns what is left behind."""
    left = []
    for url, what in [
        *((f"/containers/{c}?force=1", f"container {c[:12]}") for c in made),
        (f"/networks/{net}", f"network {net}"),
    ]:
        try:
            status, _ = call("DELETE", url)
        except OSError:
            status = None
        if status not in (204, 404):
            left.append(f"{what} ({status})")
    if left:
        print(f"probe: left behind, remove by hand as root: {', '.join(left)}")
    return left


def main(image: str) -> int:
    """0 only when every check passed and nothing is left behind."""
    net = f"probe-{os.getpid()}"
    ok("POST", "/networks/create", {"Name": net})
    made = []
    try:
        result = trial(image, net, made)
    finally:
        left = cleanup(net, made)
    return 1 if left else result


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: probe.py IMAGE (an image built for a trial: README, 'Checks after the steps')")
    sys.exit(main(sys.argv[1]))
