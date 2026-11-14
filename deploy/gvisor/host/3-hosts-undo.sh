#!/bin/sh
# Undo of step 3: removes its one /etc/hosts line, and nothing else.
set -eu
. "$(dirname "$0")/common.sh"
plan "  1. remove exactly the line '$HOSTS_LINE' from /etc/hosts"
if grep -qxF "$HOSTS_LINE" "$P/etc/hosts" 2>/dev/null; then
  grep -vxF "$HOSTS_LINE" "$P/etc/hosts" > "$P/etc/hosts.evals-undo" || true
  cat "$P/etc/hosts.evals-undo" > "$P/etc/hosts"  # rewrite in place: keeps the file's owner, mode and inode
  rm -f "$P/etc/hosts.evals-undo"
fi
say "done"
