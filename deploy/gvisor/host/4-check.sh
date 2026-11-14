#!/bin/sh
# Step 4 (#137): read-only checks of the main daemon before anything trial-related runs on it. It changes nothing, so
# it has no undo. Trials need no host firewall: each trial network is internal with no address on the host, and the
# socket proxy forces both (ADR-0003, amended 2026-11-14). What the host must give is Docker 28 or later, runsc as
# a runtime, and the two ranges free.
set -eu
. "$(dirname "$0")/common.sh"
plan "  1. docker version: the engine must be 28 or later
  2. docker info: runsc must be a registered runtime
  3. docker network ls/inspect: no other network may use $POOL (trial networks) or $EGRESS (the services)
Changes nothing."
v=$(docker version --format '{{.Server.Version}}')
[ "${v%%.*}" -ge 28 ] 2>/dev/null || die "Docker Engine $v: 28 or later is needed"
docker info --format '{{json .Runtimes}}' | grep -q '"runsc"' || die "runsc is not a registered runtime"
taken=$(docker network ls -q | while read -r id; do
  docker network inspect -f '{{.Name}}|{{index .Labels "io.evalskills.owned"}}|{{range .IPAM.Config}}{{.Subnet}} {{end}}' "$id"
done | python3 -I -c '
import ipaddress, sys
pool, egress = (ipaddress.ip_network(a) for a in sys.argv[1:3])
for line in sys.stdin:
    name, owned, subnets = (line.rstrip("\n").split("|") + ["", ""])[:3]
    if name == "evalskills-egress" or owned == "1":
        continue
    for s in subnets.split():
        try:
            net = ipaddress.ip_network(s, strict=False)
        except ValueError:
            continue
        if net.version == 4 and (net.overlaps(pool) or net.overlaps(egress)):
            print(f"{name} {net}")
' "$POOL" "$EGRESS")
[ -z "$taken" ] || die "a network already uses our ranges: $taken"
say "ok: Docker $v, runsc registered, $POOL and $EGRESS free"
