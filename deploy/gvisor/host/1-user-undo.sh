#!/bin/sh
# Undo of step 1: removes the trial user and the proxy group, only if they are the ones step 1 made.
set -eu
. "$(dirname "$0")/common.sh"
plan "  1. userdel $EVALS_USER (only with uid $EVALS_UID), and its group $EVALS_USER
  2. groupdel $EVALS_GROUP (only once it has no members)
  3. rm -rf /var/lib/evals/.cache; rmdir /var/lib/evals (only if then empty)
It refuses while a process runs as $EVALS_USER, or while the run disk (step 2) is still there."
[ -e "$P/var/lib/evals/run.img" ] && die "the run disk is still there: run 2-run-disk-undo.sh first"
if entry=$(getent passwd "$EVALS_USER"); then
  [ "$(echo "$entry" | cut -d: -f3)" = "$EVALS_UID" ] || die "user $EVALS_USER has another uid; it isn't step 1's"
  pgrep -u "$EVALS_UID" >/dev/null 2>&1 && die "processes still run as $EVALS_USER"
  userdel "$EVALS_USER"
fi
if entry=$(getent group "$EVALS_GROUP"); then
  [ -z "$(echo "$entry" | cut -d: -f4)" ] || die "group $EVALS_GROUP still has members"
  groupdel "$EVALS_GROUP"
fi
rm -rf "$P/var/lib/evals/.cache"
rmdir "$P/var/lib/evals" 2>/dev/null || say "/var/lib/evals is not empty; left in place"
say "done"
