"""An attacker with NET_ADMIN on a trial network, in the trial-network workflow (#137). CI only.

It makes each service its default route in turn and tries to get past it: to a trial on another network, to the
host through the egress network, and to the internet. Both services must refuse to forward (ip_forward=0)."""

import os
import socket
import subprocess
import sys

e = os.environ


def reach(host, port):
    try:
        socket.create_connection((host, port), timeout=4).close()
        return True
    except OSError:
        return False


if not reach(e["VIA_GATEWAY"], 8443):
    sys.exit("control failed: the gateway is not reachable on this network")
print("control: the gateway is reachable directly")
bad = 0
for via in ("VIA_GATEWAY", "VIA_PROXY"):
    subprocess.run(["ip", "route", "replace", "default", "via", e[via]], check=True)
    for host, port in ((e["NEIGHBOUR"], 8000), ("172.31.248.1", 9999), (e["HOST_IP"], 9999), ("1.1.1.1", 443)):
        got = reach(host, port)
        print(f"via {via.removeprefix('VIA_').lower()}: {host}:{port} reached={got} expected=False")
        bad += got
sys.exit(1 if bad else 0)
