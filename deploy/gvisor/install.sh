#!/bin/sh
# DRAFT — review first. Installs runsc and a second, isolated Docker daemon for evals.
set -eu
# WITHDRAWN (#137): a second dockerd broke the main daemon's networks twice on the shared host. Do not install it
# on any host that runs another dockerd. The replacement design (runsc on the main daemon) is agreed in #137 first.
if [ -S "${MAIN_DOCKER_SOCK:-/var/run/docker.sock}" ] || pgrep -x dockerd >/dev/null 2>&1; then
  echo "install.sh: another dockerd is running on this host; the second eval daemon is withdrawn (#137)" >&2
  exit 1
fi
[ "$(id -u)" = 0 ] || { echo "install.sh: run as root (sudo sh install.sh)" >&2; exit 1; }
# 1. gVisor from the official apt repo (https://gvisor.dev/docs/user_guide/install/)
curl -fsSL https://gvisor.dev/archive.key | gpg --dearmor -o /usr/share/keyrings/gvisor-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main" \
  > /etc/apt/sources.list.d/gvisor.list
apt-get update  # on its own line: under set -e, a failure inside an && chain does not stop the script
apt-get install -y runsc
# 2. config + unit for the second daemon
groupadd -f docker-evals
install -d -m 0755 /etc/docker-evals /var/lib/docker-evals
install -m 0644 daemon-evals.json /etc/docker-evals/daemon.json
install -m 0644 docker-evals.service /etc/systemd/system/docker-evals.service
install -m 0755 firewall.sh /etc/docker-evals/firewall.sh
install -m 0755 preflight.sh /etc/docker-evals/preflight.sh
[ -e /etc/docker-evals/firewall.env ] || install -m 0644 firewall.env /etc/docker-evals/firewall.env
systemctl daemon-reload
sh /etc/docker-evals/preflight.sh
echo "Review /etc/docker-evals/daemon.json and firewall.env, then:"
echo "  systemctl enable --now docker-evals   # applies firewall.sh before every start"
