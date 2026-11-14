#!/bin/sh
# Undo of step 5: removes the two code folders.
set -eu
. "$(dirname "$0")/common.sh"
plan "  1. rm -rf /opt/evalskills /opt/skillevaluator
It refuses while the socket proxy (step 7) runs: it runs from /opt/evalskills."
systemctl is-active -q evalskills-docker-proxy 2>/dev/null && die "the socket proxy is running: stop it first"
rm -rf "$P/opt/evalskills" "$P/opt/skillevaluator"
say "done"
