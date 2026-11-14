#!/bin/sh
# Undo of step 8: removes the trial user's Docker client config.
set -eu
. "$(dirname "$0")/common.sh"
plan "  1. rm /var/lib/evals/.docker/config.json; rmdir /var/lib/evals/.docker (if then empty)"
rm -f "$P/var/lib/evals/.docker/config.json"
rmdir "$P/var/lib/evals/.docker" 2>/dev/null || true
say "done"
