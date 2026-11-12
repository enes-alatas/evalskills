#!/bin/sh
# DRAFT — review first. Installs runsc and a second, isolated Docker daemon for evals.
set -eu
# 1. gVisor from the official apt repo (https://gvisor.dev/docs/user_guide/install/)
curl -fsSL https://gvisor.dev/archive.key | gpg --dearmor -o /usr/share/keyrings/gvisor-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main" \
  > /etc/apt/sources.list.d/gvisor.list
apt-get update && apt-get install -y runsc
# 2. config + unit for the second daemon
groupadd -f docker-evals
install -d -m 0755 /etc/docker-evals /var/lib/docker-evals
install -m 0644 daemon-evals.json /etc/docker-evals/daemon.json
install -m 0644 docker-evals.service /etc/systemd/system/docker-evals.service
install -m 0755 firewall.sh /etc/docker-evals/firewall.sh
[ -e /etc/docker-evals/firewall.env ] || install -m 0644 firewall.env /etc/docker-evals/firewall.env
systemctl daemon-reload
echo "Review /etc/docker-evals/daemon.json and firewall.env, then:"
echo "  systemctl enable --now docker-evals   # applies firewall.sh before every start"
