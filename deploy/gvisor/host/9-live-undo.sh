#!/bin/sh
# Undo of step 9: stops and removes the gateway, the egress proxy and their egress network. The gateway's counters
# volume is kept. It refuses while the socket proxy runs: a trial may be using them.
set -eu
. "$(dirname "$0")/common.sh"
COMPOSE="$P/opt/evalskills/deploy/live/compose.yaml"
plan "  1. docker compose -f /opt/evalskills/deploy/live/compose.yaml down (volumes kept; add -v by hand to drop them)"
systemctl is-active -q evalskills-docker-proxy 2>/dev/null && die "the socket proxy is running: stop it first"
[ -f "$COMPOSE" ] || die "no /opt/evalskills/deploy/live/compose.yaml"
# Compose refuses any command while GATEWAY_MODEL_KEY is unset; down needs no key, so it gets an empty one.
GATEWAY_MODEL_KEY='' docker compose -f "$COMPOSE" down
say "done"
