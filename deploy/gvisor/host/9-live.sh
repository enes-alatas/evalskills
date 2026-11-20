#!/bin/sh
# Step 9 (#137): the model gateway and the egress proxy (deploy/live/compose.yaml) on the main daemon, on their own
# egress network (172.31.248.0/24) at fixed addresses, with nothing published. The socket proxy attaches them to each
# trial network. The model key must already be in place (deploy/live/README.md, step 1): this script never reads,
# prints or writes it.
# Both services read their config once, at start, from a single-file bind mount. Compose doesn't recreate a
# container when only a mounted file changed, and 5-code.sh's checkout replaces the file, so a running container
# keeps the old one (#234). So this step always recreates both, refuses while trials may be using them, and checks
# that each container reads the checked-out file.
set -eu
. "$(dirname "$0")/common.sh"
LIVE="$P/opt/evalskills/deploy/live"
COMPOSE="$LIVE/compose.yaml"
plan "  1. check that the model key file exists and the socket proxy is stopped
  2. docker compose -f /opt/evalskills/deploy/live/compose.yaml up -d --build --force-recreate
  3. check that each container reads the checked-out gateway.json and egress-proxy.json
Needs steps 3 and 5. Undo: 9-live-undo.sh"
[ -f "$P/etc/evalskills/secrets/model_key" ] || die "no model key file: see deploy/live/README.md, step 1"
[ -f "$COMPOSE" ] || die "no /opt/evalskills: run 5-code.sh first"
systemctl is-active -q evalskills-docker-proxy 2>/dev/null && die "the socket proxy is running: stop it first"
docker compose -f "$COMPOSE" up -d --build --force-recreate
for pair in evalskills-gateway:gateway.json evalskills-egress-proxy:egress-proxy.json; do
  c=${pair%%:*} f=${pair#*:}
  docker exec "$c" cat "/etc/evalskills/$f" | cmp -s - "$LIVE/$f" ||
    die "$c doesn't read the checked-out $f: check docker logs $c"
done
say "done: both read the checked-out config; docker logs evalskills-gateway should say listening"
