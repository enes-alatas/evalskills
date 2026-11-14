"""Runs inside a trial in the trial-network workflow (#137): what this trial can reach. CI only, no skill code."""

import os
import socket
import sys

e = os.environ
print("".join(ln for ln in open("/etc/hosts") if "evals-" in ln), end="")
want = {
    ("evals-gateway", 8443): True,  # the gateway, on this trial's network
    ("evals-proxy", 3128): True,  # the egress proxy, on this trial's network
    (e["GW_EGRESS"], 8443): False,  # the gateway's egress-side address
    (e["NEIGHBOUR"], 8000): False,  # a trial on another trial network
    (e["FIRST"], 9999): False,  # the network's first address, never put on the host's bridge
    ("172.17.0.1", 9999): False,  # docker0, the host
    (e["HOST_IP"], 9999): False,  # the host's own address
    ("1.1.1.1", 443): False,  # the internet
}
bad = 0
for (host, port), ok in want.items():
    try:
        socket.create_connection((host, port), timeout=4).close()
        got = True
    except OSError:
        got = False
    print(f"{host}:{port} reached={got} expected={ok}")
    bad += got != ok
sys.exit(1 if bad else 0)
