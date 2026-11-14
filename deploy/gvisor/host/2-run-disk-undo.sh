#!/bin/sh
# Undo of step 2: unmounts and removes the trial folders' filesystem. Every trial folder on it goes with it.
set -eu
. "$(dirname "$0")/common.sh"
plan "  1. systemctl disable --now srv-evals-run.mount; remove the unit; daemon-reload
  2. rm /var/lib/evals/run.img; rmdir /srv/evals/run /srv/evals (only if empty)
It refuses while the socket proxy (step 7) runs."
systemctl is-active -q evalskills-docker-proxy 2>/dev/null && die "the socket proxy is running: stop it first"
if [ -e "$P/etc/systemd/system/srv-evals-run.mount" ]; then
  systemctl disable --now srv-evals-run.mount
  rm -f "$P/etc/systemd/system/srv-evals-run.mount"
  systemctl daemon-reload
fi
mountpoint -q "$P/srv/evals/run" 2>/dev/null && die "/srv/evals/run is still mounted"
rm -f "$P/var/lib/evals/run.img"
rmdir "$P/srv/evals/run" "$P/srv/evals" 2>/dev/null || true
say "done"
