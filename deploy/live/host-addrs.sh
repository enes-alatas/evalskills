#!/bin/sh
# Prints this host's own addresses, comma-separated, for the egress proxy's --deny (#236). A trial must not reach the
# host through a name that resolves to one of its public addresses: the host would see the connection come from the
# proxy's private address. Behind 1:1 NAT the public address isn't on an interface: add it in EVALSKILLS_DENY_EXTRA.
set -eu
command -v ip > /dev/null || { echo "host-addrs: no ip command" >&2; exit 1; }
addrs=$(ip -o addr show scope global | awk '{ sub(/\/.*/, "", $4); print $4 }' | paste -sd, -)
extra=${EVALSKILLS_DENY_EXTRA:-}
out="${addrs}${addrs:+${extra:+,}}${extra}"
[ -n "$out" ] || { echo "host-addrs: no global address found" >&2; exit 1; }
printf '%s\n' "$out"
