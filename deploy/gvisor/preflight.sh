#!/bin/sh
# Refuses to start the eval daemon on a host where it could disturb other services (step-0 incident, 2026-11-12).
# install.sh runs it, and docker-evals.service runs it before every start.
set -eu
SCOPE=172.31.240.0/20
# Two dockerds on one host share its network namespace: on 2026-11-12 starting this one took down the main
# daemon's networks twice (shared iptables chains, then docker0 deleted; #137). Never alongside another dockerd.
if [ -S "${MAIN_DOCKER_SOCK:-/var/run/docker.sock}" ] || pgrep -x dockerd >/dev/null 2>&1; then
  echo "preflight: another dockerd is running on this host; refusing (#137)" >&2
  exit 1
fi
[ "$(id -u)" = 0 ] || { echo "preflight: run as root" >&2; exit 1; }
command -v runsc >/dev/null || [ -x /usr/bin/runsc ] || { echo "preflight: runsc is not installed" >&2; exit 1; }
# Any host route that overlaps the eval range, other than br-evals itself, belongs to something else on this host
# (a main-daemon network, a VPN, ...). Starting the eval daemon would hijack or shadow it.
clash=$(ip -4 route show | grep -v ' dev br-evals ' | awk '{print $1}' | python3 -I -c '
import ipaddress, sys
scope = ipaddress.ip_network(sys.argv[1])
for line in sys.stdin:
    net = line.strip()
    if net in ("default", ""):
        continue
    try:
        n = ipaddress.ip_network(net, strict=False)
    except ValueError:
        continue
    if n.overlaps(scope):
        print(net)
' "$SCOPE")
if [ -n "$clash" ]; then
  echo "preflight: these host routes overlap the eval range $SCOPE; refusing to start:" >&2
  echo "$clash" >&2
  exit 1
fi
echo "preflight: ok"
