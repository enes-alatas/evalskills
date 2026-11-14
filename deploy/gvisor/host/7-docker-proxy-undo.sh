#!/bin/sh
# Undo of step 7: stops the socket proxy and removes its unit and config.
set -eu
. "$(dirname "$0")/common.sh"
plan "  1. systemctl stop evalskills-docker-proxy (if running); remove the unit; daemon-reload
  2. rm /etc/evals/docker-proxy.json"
systemctl stop evalskills-docker-proxy 2>/dev/null || true
rm -f "$P/etc/systemd/system/evalskills-docker-proxy.service" "$P/etc/evals/docker-proxy.json"
systemctl daemon-reload
say "done"
