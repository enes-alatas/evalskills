"""Drives the Docker socket proxy the way the runner would, in the trial-network workflow (#137). CI only.

Usage: drive.py SOCKET IMAGE ROUTER_IMAGE TAG. Through the proxy it creates two trial networks with a listener and a
probing trial on each, and checks: neither network has an address on the host; each trial reaches the two services
and nothing else, the other network's listener included (both directions); an attacker with NET_ADMIN (started on the
daemon directly) can't route through either service. Then it removes everything through the proxy and checks the
services were detached."""

import http.client
import ipaddress
import json
import socket
import subprocess
import sys
import time

SOCK, IMAGE, ROUTER, TAG = sys.argv[1:5]
SERVICES = ("evalskills-gateway", "evalskills-egress-proxy")


class Conn(http.client.HTTPConnection):
    def __init__(self):
        super().__init__("docker", timeout=120)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(120)
        self.sock.connect(SOCK)


def call(method, url, body=None, version="/v1.47"):
    c = Conn()
    data = None if body is None else json.dumps(body)
    c.request(method, version + url, body=data, headers={"Content-Type": "application/json"} if data else {})
    r = c.getresponse()
    out = r.read()
    c.close()
    if r.status not in (200, 201, 204):
        sys.exit(f"{method} {url}: {r.status} {out[:300]!r}")
    return json.loads(out) if (r.getheader("Content-Type") or "").startswith("application/json") and out else out


def demux(b: bytes) -> str:
    out = b""
    while len(b) >= 8:
        n = int.from_bytes(b[4:8], "big")
        out, b = out + b[8 : 8 + n], b[8 + n :]
    return out.decode(errors="replace")


def create(net, cmd, env=()):
    body = {"Image": IMAGE, "Cmd": cmd, "Env": list(env), "HostConfig": {"NetworkMode": net}}
    cid = call("POST", "/containers/create", body)["Id"]
    call("POST", f"/containers/{cid}/start")
    return cid


def run(net, cmd, env=()):
    cid = create(net, cmd, env)
    code = call("POST", f"/containers/{cid}/wait")["StatusCode"]
    print(demux(call("GET", f"/containers/{cid}/logs?stdout=1&stderr=1")), end="")
    return cid, code


def ip_on(cid, net):
    return call("GET", f"/containers/{cid}/json")["NetworkSettings"]["Networks"][net]["IPAddress"]


def docker(*args):
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=True).stdout


def service_networks():
    out = docker("inspect", "-f", "{{json .NetworkSettings.Networks}}", *SERVICES)
    return [sorted(json.loads(ln)) for ln in out.splitlines()]


def service_ip(name, net):
    return docker("inspect", "-f", f'{{{{(index .NetworkSettings.Networks "{net}").IPAddress}}}}', name).strip()


route = subprocess.run(["ip", "-4", "-o", "route", "get", "1.1.1.1"], capture_output=True, text=True, check=True).stdout
host_ip = route.split(" src ")[1].split()[0]
nets = [f"trial-{TAG}-a", f"trial-{TAG}-b"]
listeners, made, bad = {}, [], 0
for n in nets:
    call("POST", "/networks/create", {"Name": n})
    bridge = "br-" + call("GET", f"/networks/{n}")["Id"][:12]
    addr = subprocess.run(["ip", "-4", "-o", "addr", "show", "dev", bridge], capture_output=True, text=True).stdout
    if addr.strip():
        sys.exit(f"{n} has an address on the host: {addr}")
    print(f"{n} ({bridge}): no IPv4 address on the host")
    listeners[n] = create(n, ["python", "-m", "http.server", "8000"])
    made.append(listeners[n])
time.sleep(3)
# the runner's precondition (pipeline/tier3.py): the proxy reads back every network's flags and the services' state
print("isolation:", call("GET", "/_evalskills/isolation", version=""))

for n, other in ((nets[0], nets[1]), (nets[1], nets[0])):
    mine, theirs = ip_on(listeners[n], n), ip_on(listeners[other], other)
    check = f"import socket; socket.create_connection(('{mine}', 8000), timeout=4).close(); print('control: ok')"
    cid, code = run(n, ["python", "-c", check])
    made.append(cid)
    if code:
        sys.exit(f"control failed: the listener on {n} is not reachable on its own network")
    first = ipaddress.ip_network(call("GET", f"/networks/{n}")["IPAM"]["Config"][0]["Subnet"])[1]
    env = [f"NEIGHBOUR={theirs}", f"HOST_IP={host_ip}", f"FIRST={first}", "GW_EGRESS=172.31.248.2"]
    print(f"--- a trial on {n}, the other network's listener at {theirs}")
    cid, code = run(n, ["python", "/probe.py"], env)
    made.append(cid)
    bad += code
    print(f"--- an attacker with NET_ADMIN on {n}, routing through each service")
    p = subprocess.run(
        ["docker", "run", "--rm", "--cap-add", "NET_ADMIN", "--network", n,
         "-e", f"VIA_GATEWAY={service_ip(SERVICES[0], n)}", "-e", f"VIA_PROXY={service_ip(SERVICES[1], n)}",
         "-e", f"NEIGHBOUR={theirs}", "-e", f"HOST_IP={host_ip}", ROUTER, "python", "/router.py"],
        capture_output=True, text=True,
    )  # fmt: skip
    print(p.stdout + p.stderr, end="")
    bad += p.returncode != 0
print("services on:", service_networks())

for cid in made:
    call("DELETE", f"/containers/{cid}?force=1")
for n in nets:
    call("DELETE", f"/networks/{n}")
left = service_networks()
print("after the networks are removed, services on:", left)
if any(found != ["evalskills-egress"] for found in left):
    sys.exit("a service is still attached to a trial network")
sys.exit(1 if bad else 0)
