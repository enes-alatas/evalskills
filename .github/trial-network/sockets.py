"""Prints the sockets a service container listens on from outside: TCP in LISTEN and every UDP socket.

Run inside the container: docker exec -i <name> python3 -I - < sockets.py
"""

out = set()
for f in ("tcp", "tcp6", "udp", "udp6"):
    try:
        rows = open(f"/proc/net/{f}").read().splitlines()[1:]
    except OSError:
        continue
    for r in rows:
        p = r.split()
        addr, port = p[1].split(":")
        if addr == "0B00007F":  # 127.0.0.11, the embedded DNS of Docker, on loopback: not reachable from outside
            continue
        if f.startswith("udp") or p[3] == "0A":  # every UDP socket; TCP in LISTEN
            out.add(f"{f}:{int(port, 16)}")
print(" ".join(sorted(out)))
