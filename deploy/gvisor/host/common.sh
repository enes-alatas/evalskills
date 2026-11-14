# shellcheck shell=sh disable=SC2034
# Sourced by every step script in this folder (#137, PR C2). Not run on its own.
#
# Each step prints its plan, then acts only with --yes. Each is safe to run twice, and its -undo script removes only
# what the step added. All host paths are under $EVALS_PREFIX (empty on the host), so the tests run every script
# against a temporary folder and stub binaries, and never touch a real system.
P="${EVALS_PREFIX:-}"
HERE=$(cd "$(dirname "$0")" && pwd)
POOL=172.31.240.0/21     # the trial networks (docker-proxy.json network_pool)
EGRESS=172.31.248.0/24   # the services' egress network (deploy/live/compose.yaml)
EVALS_UID=10010
EVALS_USER=evals
EVALS_GROUP=evalskills   # the proxy socket's group; evals is in it, and in nothing else
GATEWAY_ADDR=172.31.248.2  # the gateway container on the egress network (deploy/live/compose.yaml)
GATEWAY_HOST=evals-gateway  # the name the runner and trials use for it (gateway/docker_proxy.py services)
HOSTS_LINE="$GATEWAY_ADDR $GATEWAY_HOST # evalskills #137"
STEP=$(basename "$0" .sh)

need_root() { [ "$(id -u)" = 0 ] || { echo "$STEP: run as root" >&2; exit 1; }; }

# plan TEXT: print what the step does, then stop here unless --yes was given
plan() {
  printf '%s will:\n%s\n' "$STEP" "$1"
  if [ "$YES" != 1 ]; then
    echo "Dry run: nothing changed. Run again with --yes to act."
    exit 0
  fi
}

say() { echo "$STEP: $*"; }
die() { echo "$STEP: $*" >&2; exit 1; }

YES=0
for a in "$@"; do
  case "$a" in
    --yes) YES=1 ;;
    *) ;;
  esac
done
need_root
