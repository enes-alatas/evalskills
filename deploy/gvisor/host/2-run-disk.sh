#!/bin/sh
# Step 2 (#137): /srv/evals/run, the proxy's bind_root, on its own 5 GB filesystem: a preallocated ext4 image
# (fallocate, not sparse, so the space is really there), loop-mounted nodev,nosuid by srv-evals-run.mount. The
# mounted root is evals:evals 0700. The image stays root's 0600, in a folder the trial user can't write (step 1).
set -eu
. "$(dirname "$0")/common.sh"
IMG="$P/var/lib/evals/run.img"
UNIT="$P/etc/systemd/system/srv-evals-run.mount"
SIZE="${EVALS_RUN_SIZE:-5G}"
plan "  1. fallocate -l $SIZE /var/lib/evals/run.img (0600 root), then mkfs.ext4 -E nodiscard on it (only if the image
     is new); refuse an image that isn't fully allocated
  2. install srv-evals-run.mount to /etc/systemd/system, daemon-reload, enable --now
  3. chown $EVALS_USER:$EVALS_USER and chmod 0700 the mounted /srv/evals/run
Needs step 1. Undo: 2-run-disk-undo.sh"
getent passwd "$EVALS_USER" >/dev/null || die "no user $EVALS_USER: run 1-user.sh first"
[ -d "$P/var/lib/evals" ] || die "no /var/lib/evals: run 1-user.sh first"
if [ ! -e "$IMG" ]; then
  (umask 077; fallocate -l "$SIZE" "$IMG")
  # nodiscard: by default mkfs.ext4 discards its target, which on a file punches holes in it again (#179)
  mkfs.ext4 -q -F -L evals-run -m 0 -E nodiscard "$IMG"
else
  say "image already there; not reformatting it"
fi
allocated "$IMG"
chmod 0600 "$IMG"
install -d -m 0755 "$P/srv/evals" "$P/srv/evals/run"
install -d -m 0755 "$P/etc/systemd/system"
install -m 0644 "$HERE/srv-evals-run.mount" "$UNIT"
systemctl daemon-reload
systemctl enable --now srv-evals-run.mount
mountpoint -q "$P/srv/evals/run" || die "/srv/evals/run is not mounted"
chown "$EVALS_USER:$EVALS_USER" "$P/srv/evals/run"
chmod 0700 "$P/srv/evals/run"
say "done: $(df -h "$P/srv/evals/run" | tail -1)"
