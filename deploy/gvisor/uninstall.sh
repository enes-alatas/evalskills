#!/bin/sh
# Removes the withdrawn second eval daemon (#137) from a host. Run as root by the host's operator.
# Without --yes it only prints what it would do. It touches nothing outside the eval daemon's own files, its bridge
# and its EVALS-* rules (scoped to 172.31.240.0/20), and it ends with an iptables-save diff so you can check that.
set -eu
SCOPE=172.31.240.0/20
[ "$(id -u)" = 0 ] || { echo "uninstall.sh: run as root" >&2; exit 1; }

cat <<'PLAN'
uninstall.sh will:
  1. systemctl disable --now docker-evals; remove /etc/systemd/system/docker-evals.service; daemon-reload
  2. remove the EVALS jumps from INPUT, FORWARD and DOCKER-USER, then flush and delete EVALS-INPUT and EVALS-EGRESS
  3. ip link del br-evals
  4. rm -rf /etc/docker-evals /var/lib/docker-evals /run/docker-evals /run/docker-evals.sock /run/docker-evals.pid
  5. remove the docker-evals group
It refuses if docker-evals is still running (stopping it is its own announced step), if deploy/live is up, or if
anything is still mounted under /var/lib/docker-evals.
It leaves alone: the runsc package (the #137 design reuses it), the main daemon and its config, deploy/live, and the
dockremap user and subuid/subgid ranges (not uniquely ours; it reports them so you can decide).
Undo: there is nothing to restore; reinstalling is withdrawn (#137).
PLAN
[ "${1:-}" = "--yes" ] || { echo "Dry run. Run again with --yes to act."; exit 0; }

if systemctl is-active -q docker-evals 2>/dev/null; then
  echo "docker-evals is running. Stopping it changes the host's network, so do it as its own announced step:" >&2
  echo "  systemctl stop docker-evals" >&2
  exit 1
fi
# Any mount at or under the data root (findmnt -R only matches if the path itself is a mountpoint).
mounted=$(awk '$2 ~ "^/var/lib/docker-evals(/|$)" {print $2}' "${MOUNTS_FILE:-/proc/self/mounts}")
if [ -n "$mounted" ]; then
  echo "something is still mounted under /var/lib/docker-evals; refusing to delete through it:" >&2
  echo "$mounted" >&2
  exit 1
fi
if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx evalskills-gateway; then
  echo "note: deploy/live is up and listens on br-evals; stop it first: GATEWAY_MODEL_KEY='' docker compose -f deploy/live/compose.yaml down" >&2
  exit 1
fi

before=$(mktemp) after=$(mktemp)
iptables-save | grep -v -e '^#' -e '^:' > "$before"

systemctl disable --now docker-evals 2>/dev/null || true
rm -f /etc/systemd/system/docker-evals.service
systemctl daemon-reload

for pair in INPUT:EVALS-INPUT FORWARD:EVALS-EGRESS DOCKER-USER:EVALS-EGRESS; do
  parent=${pair%%:*} target=${pair#*:}
  while iptables -D "$parent" -s "$SCOPE" -j "$target" 2>/dev/null; do :; done
  while iptables -D "$parent" -s "$SCOPE" -j DROP 2>/dev/null; do :; done  # a hold left by an interrupted firewall.sh
done
for c in EVALS-INPUT EVALS-EGRESS; do
  iptables -F "$c" 2>/dev/null || true
  iptables -X "$c" 2>/dev/null || true
done

ip link show br-evals >/dev/null 2>&1 && ip link del br-evals
rm -rf /etc/docker-evals /var/lib/docker-evals /run/docker-evals /run/docker-evals.sock /run/docker-evals.pid

getent group docker-evals >/dev/null && groupdel docker-evals
if getent passwd dockremap >/dev/null; then
  echo "Kept: user dockremap and its /etc/subuid, /etc/subgid lines (anything using userns-remap: default creates them)."
  echo "  Remove them only if nothing uses them: docker info --format '{{.SecurityOptions}}' must not show name=userns."
  echo "  Then: userdel dockremap; sed -i '/^dockremap:/d' /etc/subuid /etc/subgid"
fi

iptables-save | grep -v -e '^#' -e '^:' > "$after"
echo "--- iptables changes (every line should mention EVALS or $SCOPE):"
diff "$before" "$after" | grep '^[<>]' || echo "(none)"
if diff "$before" "$after" | grep '^[<>]' | grep -v -e EVALS -e "$SCOPE" | grep -q .; then
  echo "WARNING: a rule outside the eval range changed. Check the lines above." >&2
  rm -f "$before" "$after"
  exit 2
fi
rm -f "$before" "$after"
# The first start ran with Docker's iptables on, so it may have left rules in Docker's shared chains. Report them only:
# those chains belong to the main daemon, so the operator decides.
left=$(iptables-save | grep -e br-evals -e '172\.31\.24[0-9]\.' -e '172\.31\.25[0-5]\.' | grep -v EVALS || true)
[ -z "$left" ] || { echo "Rules still naming br-evals or the eval range (not removed, review them):"; echo "$left"; }
echo "Left over, by design: runsc; the 'evals' containerd namespace if any (ctr namespaces ls). Done."
