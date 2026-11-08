#!/bin/sh
# DRAFT. Harbor creates a compose network per trial, so rules match the eval daemon's whole range (172.31.240.0/20): the trial pool 172.31.240.0/21 and the default bridge 172.31.248.0/24.
# Trials may only reach the model gateway (GATEWAY_IP:GATEWAY_PORT) and DNS; everything else is dropped,
# including the host's own services and the main Docker networks.
set -eu
: "${GATEWAY_IP:?set GATEWAY_IP}" "${GATEWAY_PORT:=8443}"
iptables -N EVALS-EGRESS 2>/dev/null || iptables -F EVALS-EGRESS
iptables -A EVALS-EGRESS -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A EVALS-EGRESS -p tcp -d "$GATEWAY_IP" --dport "$GATEWAY_PORT" -j ACCEPT
iptables -A EVALS-EGRESS -j DROP
iptables -C DOCKER-USER -s 172.31.240.0/20 -j EVALS-EGRESS 2>/dev/null || iptables -I DOCKER-USER -s 172.31.240.0/20 -j EVALS-EGRESS
# Block trial -> host (INPUT) except the gateway port
iptables -C INPUT -s 172.31.240.0/20 -p tcp --dport "$GATEWAY_PORT" -j ACCEPT 2>/dev/null || iptables -I INPUT -s 172.31.240.0/20 -p tcp --dport "$GATEWAY_PORT" -j ACCEPT
iptables -C INPUT -s 172.31.240.0/20 -j DROP 2>/dev/null || iptables -A INPUT -s 172.31.240.0/20 -j DROP
