#!/bin/sh
# Step 7 (#137): the socket proxy's config and unit. The config pins the base image to the ID this host's daemon
# reports (step 6), never the one CI built. The unit is installed, not enabled and not started.
set -eu
. "$(dirname "$0")/common.sh"
TAG=evalskills/trial-base:d136a597
CFG="$P/etc/evals/docker-proxy.json"
plan "  1. write /etc/evals/docker-proxy.json (root 0640) from docker-proxy.json, with builds.base_image_id set to
     docker image inspect $TAG on this host (replaced on a rerun, so a reloaded image is re-pinned)
  2. install evalskills-docker-proxy.service; daemon-reload. Not enabled, not started.
Needs steps 1, 2, 4, 5 and 6. Undo: 7-docker-proxy-undo.sh"
getent group "$EVALS_GROUP" >/dev/null || die "no group $EVALS_GROUP: run 1-user.sh first"
[ -f "$P/opt/evalskills/gateway/docker_proxy.py" ] || die "no /opt/evalskills: run 5-code.sh first"
id=$(docker image inspect --format '{{.Id}}' "$TAG") || die "no $TAG on this host: run 6-base-image.sh first"
install -d -m 0755 "$P/etc/evals"
python3 -I -c '
import json, re, sys
src, dst, image_id = sys.argv[1:]
if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
    sys.exit("not an image ID: " + image_id)
cfg = json.load(open(src))
cfg["builds"]["base_image_id"] = image_id
with open(dst + ".new", "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
' "$HERE/docker-proxy.json" "$CFG" "$id"
chmod 0640 "$CFG.new"
mv "$CFG.new" "$CFG"
install -d -m 0755 "$P/etc/systemd/system"
install -m 0644 "$HERE/evalskills-docker-proxy.service" "$P/etc/systemd/system/evalskills-docker-proxy.service"
systemctl daemon-reload
say "done: base_image_id $id. Start it for a pilot: systemctl start evalskills-docker-proxy"
