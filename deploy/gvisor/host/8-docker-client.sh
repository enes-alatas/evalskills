#!/bin/sh
# Step 8 (#137): the trial user's Docker client config. Docker then sets HTTP(S)_PROXY and NO_PROXY to the egress
# proxy (evals-proxy:3128, step 9) in every container that user creates, so agent installs go through it. Root
# owns the file, so the trial user can't change it. Without it, installs fail: the safe failure.
set -eu
. "$(dirname "$0")/common.sh"
D="$P/var/lib/evals/.docker"
plan "  1. install /var/lib/evals/.docker (root 0755) and config.json (root 0644) from docker-client-config.json
Needs step 1. Undo: 8-docker-client-undo.sh"
[ -d "$P/var/lib/evals" ] || die "no /var/lib/evals: run 1-user.sh first"
install -d -o root -g root -m 0755 "$D"
install -o root -g root -m 0644 "$HERE/docker-client-config.json" "$D/config.json"
say "done"
