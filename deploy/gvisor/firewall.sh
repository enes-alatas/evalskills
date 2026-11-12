#!/bin/sh
# DRAFT, untested until step 0 of #121. docker-evals.service runs it before every start; safe to run again.
# Harbor creates a compose network per trial, so the rules match the eval daemon's whole range (172.31.240.0/20):
# the trial pool 172.31.240.0/21 and the default bridge 172.31.248.0/24.
# Trials get no DNS and reach nothing but the optional model gateway (GATEWAY_IP:GATEWAY_PORT) and the optional
# egress proxy (PROXY_IP:PROXY_PORT). With neither set, this installs deny-all: test that first.
set -eu
SCOPE=172.31.240.0/20
GATEWAY_IP="${GATEWAY_IP:-}" GATEWAY_PORT="${GATEWAY_PORT:-8443}"
PROXY_IP="${PROXY_IP:-}" PROXY_PORT="${PROXY_PORT:-3128}"

# fill CHAIN: replies, then each configured allow, then drop
fill() {
  iptables -N "$1" 2>/dev/null || iptables -F "$1"
  iptables -A "$1" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
  if [ -n "$GATEWAY_IP" ]; then iptables -A "$1" -p tcp -d "$GATEWAY_IP" --dport "$GATEWAY_PORT" -j ACCEPT; fi
  if [ -n "$PROXY_IP" ]; then iptables -A "$1" -p tcp -d "$PROXY_IP" --dport "$PROXY_PORT" -j ACCEPT; fi
  iptables -A "$1" -j DROP
}

# jump FROM TO: exactly one jump from the trial range, at position 1, so no earlier ACCEPT (ufw, Docker) wins
jump() {
  while iptables -D "$1" -s "$SCOPE" -j "$2" 2>/dev/null; do :; done
  iptables -I "$1" 1 -s "$SCOPE" -j "$2"
}

# hold / release: while the chains are rebuilt on a rerun, trial traffic is dropped, never let through an empty chain
hold() { for c in INPUT DOCKER-USER; do iptables -I "$c" 1 -s "$SCOPE" -j DROP; done; }
release() { for c in INPUT DOCKER-USER; do while iptables -D "$c" -s "$SCOPE" -j DROP 2>/dev/null; do :; done; done; }

iptables -N DOCKER-USER 2>/dev/null || true  # dockerd keeps an existing DOCKER-USER chain and jumps to it
hold
fill EVALS-EGRESS  # routed traffic: other hosts, the internet, the main Docker networks
fill EVALS-INPUT   # traffic to the host itself
jump DOCKER-USER EVALS-EGRESS
# The test in tests/test_deploy_config.py reads the scope from this line: -s 172.31.240.0/20 -j EVALS-EGRESS
jump INPUT EVALS-INPUT
release
