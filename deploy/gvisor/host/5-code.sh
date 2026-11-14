#!/bin/sh
# Step 5 (#137): the code the pilot runs, root-owned and read-only to everyone else, so the trial user can't change
# what runs as root or as itself. /opt/evalskills is this repository at a commit on main, /opt/skillevaluator is
# SkillEvaluator at the pinned commit, with its .venv synced (tier3 extra) and evalskills_gvisor installed into it
# non-editable. This step downloads Python packages (uv sync), so it needs the network and is announced as such.
#   5-code.sh --yes EVALSKILLS_COMMIT [EVALSKILLS_REPO] [SKILLEVALUATOR_REPO]
set -eu
. "$(dirname "$0")/common.sh"
PIN=d136a597c54784ad8a0ce28542c208602b397a5c   # SkillEvaluator 0.8.0, as pinned everywhere else (README, images)
args=""
for a in "$@"; do [ "$a" = --yes ] || args="$args $a"; done
# shellcheck disable=SC2086
set -- $args
COMMIT="${1:-}"
REPO="${2:-https://github.com/enes-alatas/evalskills.git}"
SE_REPO="${3:-https://github.com/NVIDIA/SkillEvaluator.git}"
EV="$P/opt/evalskills"
SE="$P/opt/skillevaluator"
plan "  1. git clone $REPO to /opt/evalskills and check out ${COMMIT:-<commit>} (detached; refused unless on origin/main)
  2. git clone $SE_REPO to /opt/skillevaluator and check out $PIN
  3. in /opt/skillevaluator: uv sync --frozen --python 3.13 --extra tier3, then
     uv pip install --python .venv/bin/python --no-deps /opt/evalskills/gvisor_env (non-editable)
  4. chown -R root:root both; chmod -R go-w, a+rX
Both folders are replaced only if they are missing. Undo: 5-code-undo.sh"
[ -n "$COMMIT" ] || die "give the evalskills commit to install (a commit on main)"
if [ ! -d "$EV/.git" ]; then
  git clone -q "$REPO" "$EV"
fi
git -C "$EV" fetch -q origin
git -C "$EV" merge-base --is-ancestor "$COMMIT" origin/main || die "$COMMIT is not on origin/main"
git -C "$EV" checkout -q --detach "$COMMIT"
if [ ! -d "$SE/.git" ]; then
  git clone -q "$SE_REPO" "$SE"
fi
git -C "$SE" checkout -q --detach "$PIN"
[ "$(git -C "$SE" rev-parse HEAD)" = "$PIN" ] || die "SkillEvaluator is not at $PIN"
(cd "$SE" && uv sync -q --frozen --python 3.13 --extra tier3)
(cd "$SE" && uv pip install -q --python .venv/bin/python --no-deps --reinstall "$EV/gvisor_env")
for d in "$EV" "$SE"; do
  chown -R root:root "$d"
  chmod -R go-w,a+rX "$d"
done
say "done: evalskills $(git -C "$EV" rev-parse --short HEAD), SkillEvaluator $(git -C "$SE" rev-parse --short HEAD)"
