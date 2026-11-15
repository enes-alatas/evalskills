#!/bin/sh
# Step 1 (#137): the proxy socket's group `evalskills`, and the trial user `evals` (uid 10010), in that group and in
# no other: never `docker`. Its home /var/lib/evals belongs to root, so the trial user can't swap what root keeps
# there (the run disk's image, its Docker client config). Only ~/.cache is its own: Harbor writes ~/.cache by path,
# whatever XDG_CACHE_HOME says. Each trial gets its own XDG config, data and state folders in its trial folder (#186).
set -eu
. "$(dirname "$0")/common.sh"
HOME_DIR="$P/var/lib/evals"
plan "  1. groupadd --system $EVALS_GROUP (if missing)
  2. useradd --system --uid $EVALS_UID --user-group --groups $EVALS_GROUP --home-dir /var/lib/evals --no-create-home
     --shell /usr/sbin/nologin $EVALS_USER (if missing; refuses if the name or the uid is taken otherwise)
  3. /var/lib/evals root:root 0755, and /var/lib/evals/.cache $EVALS_USER 0700
Undo: 1-user-undo.sh"

getent group "$EVALS_GROUP" >/dev/null || groupadd --system "$EVALS_GROUP"
if entry=$(getent passwd "$EVALS_USER"); then
  [ "$(echo "$entry" | cut -d: -f3)" = "$EVALS_UID" ] || die "user $EVALS_USER exists with another uid; not touching it"
  say "user $EVALS_USER already exists"
else
  getent passwd "$EVALS_UID" >/dev/null && die "uid $EVALS_UID is taken by another user"
  useradd --system --uid "$EVALS_UID" --user-group --groups "$EVALS_GROUP" --home-dir /var/lib/evals \
    --no-create-home --shell /usr/sbin/nologin --comment "evalskills trial user (#137)" "$EVALS_USER"
fi
# shellcheck disable=SC2046
set -- $(id -nG "$EVALS_USER")
for g in "$@"; do
  case "$g" in "$EVALS_USER" | "$EVALS_GROUP") ;; *) die "$EVALS_USER is also in group $g; remove it first" ;; esac
done
install -d -o root -g root -m 0755 "$HOME_DIR"
install -d -o "$EVALS_USER" -g "$EVALS_USER" -m 0700 "$HOME_DIR/.cache"
say "done: $(id "$EVALS_USER")"
