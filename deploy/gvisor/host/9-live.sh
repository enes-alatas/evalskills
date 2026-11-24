#!/bin/sh
# Step 9 (#137): the model gateway and the egress proxy (deploy/live/compose.yaml) on the main daemon, on their own
# egress network (172.31.248.0/24) at fixed addresses, with nothing published. The socket proxy attaches them to each
# trial network. The model key must already be in place (deploy/live/README.md, step 1): this script never reads,
# prints or writes it. It hands compose the host's env file, which sets GATEWAY_MODEL_KEY for the gateway (ADR-0008).
# Both services read their config once, at start, from a single-file bind mount. Compose doesn't recreate a
# container when only a mounted file changed, and 5-code.sh's checkout replaces the file, so a running container
# keeps the old one (#234). So this step always recreates both, refuses while trials may be using them, and checks
# that each container reads the checked-out file.
set -eu
. "$(dirname "$0")/common.sh"
LIVE="$P/opt/evalskills/deploy/live"
COMPOSE="$LIVE/compose.yaml"
plan "  1. check that the model key's env file exists and the socket proxy is stopped
  2. docker compose --env-file /etc/evalskills/secrets/gateway.env -f /opt/evalskills/deploy/live/compose.yaml
     up -d --build --force-recreate, with this host's own
     addresses (deploy/live/host-addrs.sh, plus EVALSKILLS_DENY_EXTRA) as the egress proxy's --deny list
  3. check that each container reads the checked-out gateway.json and egress-proxy.json
Needs steps 3 and 5. Undo: 9-live-undo.sh"
KEY_ENV="$P/etc/evalskills/secrets/gateway.env"
[ -f "$KEY_ENV" ] || die "no model key env file: see deploy/live/README.md, step 1"
[ -f "$COMPOSE" ] || die "no /opt/evalskills: run 5-code.sh first"
systemctl is-active -q evalskills-docker-proxy 2>/dev/null && die "the socket proxy is running: stop it first"
EVALSKILLS_HOST_ADDRS=$(sh "$HERE/../../live/host-addrs.sh") || die "could not list this host's addresses"
export EVALSKILLS_HOST_ADDRS
# Shown here, on the operator's terminal, so a public address only a NAT knows can be seen to be missing. The proxy
# logs only the count.
say "the egress proxy denies this host's addresses: $EVALSKILLS_HOST_ADDRS"
say "if the host's public address isn't listed, run again with EVALSKILLS_DENY_EXTRA=<address>"
docker compose --env-file "$KEY_ENV" -f "$COMPOSE" up -d --build --force-recreate
for pair in evalskills-gateway:gateway.json evalskills-egress-proxy:egress-proxy.json; do
  c=${pair%%:*} f=${pair#*:}
  docker exec "$c" cat "/etc/evalskills/$f" | cmp -s - "$LIVE/$f" ||
    die "$c doesn't read the checked-out $f: check docker logs $c"
done
say "done: both read the checked-out config; docker logs evalskills-gateway should say listening"
