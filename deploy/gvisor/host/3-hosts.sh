#!/bin/sh
# Step 3 (#137): one /etc/hosts line naming the model gateway's address on the egress network (deploy/live/compose.yaml)
# evals-gateway. The runner hands SkillEvaluator one URL, http://evals-gateway:8443/v1. Its host side (the judge)
# reaches the gateway container's address directly; in a trial the socket proxy points the same name at the gateway's
# address on that trial's network.
set -eu
. "$(dirname "$0")/common.sh"
plan "  1. add the line '$HOSTS_LINE' to /etc/hosts, if it isn't there
Undo: 3-hosts-undo.sh"
grep -qxF "$HOSTS_LINE" "$P/etc/hosts" 2>/dev/null || printf '%s\n' "$HOSTS_LINE" >> "$P/etc/hosts"
say "done"
