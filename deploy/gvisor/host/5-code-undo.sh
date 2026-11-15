#!/bin/sh
# Undo of step 5: removes the two code folders, and the Python and cache uv put in /opt/uv for them. uv itself
# stays: it was installed by hand (README, "uv on the host"), and its undo is there.
set -eu
. "$(dirname "$0")/common.sh"
plan "  1. rm -rf /opt/evalskills /opt/skillevaluator /opt/uv/python /opt/uv/cache
It refuses while the socket proxy (step 7) runs: it runs from /opt/evalskills."
systemctl is-active -q evalskills-docker-proxy 2>/dev/null && die "the socket proxy is running: stop it first"
rm -rf "$P/opt/evalskills" "$P/opt/skillevaluator" "$P/opt/uv/python" "$P/opt/uv/cache"
say "done"
