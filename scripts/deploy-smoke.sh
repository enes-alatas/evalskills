#!/usr/bin/env bash
# Build and run the deploy stack exactly as the README says, then check it end to end (#73), including the scanner
# sidecar's isolation (#65).
# Usage: scripts/deploy-smoke.sh   (needs Docker with compose v2, curl and jq; leaves the stack running unless
# SMOKE_DOWN=1). The scan is static Tier 1: no skill code runs and no model is called.
set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE=(docker compose -f deploy/compose.yaml)
BASE="http://127.0.0.1:${EVALSKILLS_PORT:-8088}"
REPO="NVIDIA/SkillEvaluator"
SKILL="src/skillevaluator/tier3/reference_skills/text-analyzer"
BOOT_TIMEOUT="${SMOKE_BOOT_TIMEOUT:-180}"
SCAN_TIMEOUT="${SMOKE_SCAN_TIMEOUT:-900}"

fail() {
  echo "::error::deploy smoke: $*"
  "${COMPOSE[@]}" ps -a || true
  "${COMPOSE[@]}" logs --no-color --tail 300 || true
  exit 1
}

[ -e deploy/.env ] && fail "deploy/.env already exists; the smoke test starts from env.example"
cp deploy/env.example deploy/.env
trap 'rm -f deploy/.env; [ "${SMOKE_DOWN:-0}" = 1 ] && "${COMPOSE[@]}" down -v >/dev/null 2>&1 || true' EXIT

"${COMPOSE[@]}" up -d --build

echo "waiting for /api/meta"
for ((i = 0; i < BOOT_TIMEOUT; i += 3)); do
  [ "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/meta" || true)" = 200 ] && break
  sleep 3
done
[ "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/meta" || true)" = 200 ] || fail "/api/meta not 200 after ${BOOT_TIMEOUT}s"

[ "$(docker inspect -f '{{.State.Running}}' evalskills)" = true ] || fail "container is not running"
"${COMPOSE[@]}" exec -T evalskills test -s /home/app/inventory/data/results.jsonl || fail "results.jsonl not seeded"
seeded=$(curl -sf "$BASE/api/skills" | jq length) || fail "/api/skills unreadable"
[ "$seeded" -gt 0 ] || fail "/api/skills lists no skills"
echo "booted: $seeded catalog skills"

# The scanner sidecar (#65): no network, and the app runs no evaluator and holds no Docker socket.
[ "$(docker inspect -f '{{.HostConfig.NetworkMode}}' evalskills-scanner)" = none ] || fail "the scanner has a network"
if "${COMPOSE[@]}" exec -T scanner python -I -c \
  "import socket; socket.create_connection(('140.82.112.3', 443), 5)" >/dev/null 2>&1; then
  fail "the scanner opened a network connection"
fi
"${COMPOSE[@]}" exec -T evalskills sh -c '! command -v skillevaluator && ! command -v uv && ! [ -e /var/run/docker.sock ]' ||
  fail "the app container has an evaluator, uv or a Docker socket"
echo "isolated: the scanner has no network; the app runs no evaluator"

# POST a JSON body; sets CODE and BODY so a failure can say what the server answered (#85).
post() {
  local out
  out=$(curl -s -w '\n%{http_code}' -X POST -H 'Content-Type: application/json' -d "$2" "$BASE$1" || true)
  CODE=${out##*$'\n'}
  BODY=${out%$'\n'*}
}
said() { echo "HTTP ${CODE:-none}: $(head -c 2000 <<<"$BODY")"; }

post /api/discover "{\"url\": \"https://github.com/$REPO\"}"
jq -e --arg p "$SKILL" '.skills | map(.path) | index($p) != null' <<<"$BODY" >/dev/null 2>&1 ||
  fail "discover did not list $SKILL; $(said)"
post /api/submit "{\"repo\": \"$REPO\", \"paths\": [\"$SKILL\"]}"
jq -e '.submitted[0].job != null' <<<"$BODY" >/dev/null 2>&1 || fail "submit did not queue a job; $(said)"

id=$(jq -rn --arg s "$REPO:$SKILL" '$s | @uri')
echo "waiting for $REPO:$SKILL"
state=""
for ((i = 0; i < SCAN_TIMEOUT; i += 5)); do
  row=$(curl -sf "$BASE/api/skills/$id" || true)
  state=$(jq -r '.state // ""' <<<"$row" 2>/dev/null || true)
  case "$state" in
    completed) break ;;
    failed) fail "scan failed: $(jq -c '.evaluation // {}' <<<"$row" | head -c 2000)" ;;
  esac
  sleep 5
done
[ "$state" = completed ] || fail "scan still '$state' after ${SCAN_TIMEOUT}s"
score=$(jq -r '.structural.score' <<<"$row")
[ "$score" != null ] || fail "completed without a structural score"
image=$(grep -o 'ghcr.io/nvidia/skillevaluator-tier1@sha256:[0-9a-f]*' deploy/compose.yaml | head -1)
[ "$(jq -r '.evaluation.evaluator.image' <<<"$row")" = "$image" ] ||
  fail "the result does not name the scanner image: $(jq -c '.evaluation.evaluator' <<<"$row")"
echo "scanned by: $(jq -c '.evaluation.evaluator' <<<"$row"), incomplete: $(jq -c '.evaluation.tier1.incomplete_scans' <<<"$row")"
[ "$(docker inspect -f '{{.State.Running}}' evalskills)" = true ] || fail "container stopped after the scan"
echo "deploy smoke passed: structural score $score for $REPO:$SKILL"
