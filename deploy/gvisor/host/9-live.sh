#!/bin/sh
# Step 9 (#137): the model gateway and the egress proxy (deploy/live/compose.yaml) on the main daemon, on their own
# egress network (172.31.248.0/24) at fixed addresses, with nothing published. The socket proxy attaches them to each
# trial network. The model key must already be in place (deploy/live/README.md, step 1): this script never reads,
# prints or writes it.
set -eu
. "$(dirname "$0")/common.sh"
COMPOSE="$P/opt/evalskills/deploy/live/compose.yaml"
plan "  1. check that the model key file exists
  2. docker compose -f /opt/evalskills/deploy/live/compose.yaml up -d --build
Needs steps 3 and 5. Undo: 9-live-undo.sh"
[ -f "$P/etc/evalskills/secrets/model_key" ] || die "no model key file: see deploy/live/README.md, step 1"
[ -f "$COMPOSE" ] || die "no /opt/evalskills: run 5-code.sh first"
docker compose -f "$COMPOSE" up -d --build
say "done: docker logs evalskills-gateway should say listening"
