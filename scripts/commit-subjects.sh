#!/usr/bin/env bash
# Checks every commit on this branch beyond BASE (default origin/main): a Conventional Commit subject with at most one
# scope and at most 80 characters, and no closing keyword anywhere in the message (only PR bodies close issues).
# Merge commits are skipped: CI checks out the PR merged onto its base, and rebase-merge never lands one.
#   scripts/commit-subjects.sh            check the branch
#   scripts/commit-subjects.sh --stdin    check one message per NUL-separated record on stdin (self-test)
set -euo pipefail
types='feat|fix|docs|chore|refactor|test|ci|perf|build|revert|style'
subject_re="^($types)(\([a-z0-9._/-]+\))?!?: [^ ].*$"
close_re='\b(close[sd]?|fix(e[sd])?|resolve[sd]?) +#[0-9]+'
fail=0
check() {
  local message="$1" subject="${1%%$'\n'*}"
  if ! [[ "$subject" =~ $subject_re ]]; then echo "not a Conventional Commit subject: $subject"; fail=1; fi
  if (( ${#subject} > 80 )); then echo "subject over 80 characters: $subject"; fail=1; fi
  if grep -qiE "$close_re" <<<"$message"; then echo "closing keyword in commit message: $subject"; fail=1; fi
}
if [[ "${1:-}" == "--stdin" ]]; then
  while IFS= read -r -d '' message; do check "$message"; done
else
  base="${BASE:-origin/main}"
  range="$(git merge-base "$base" HEAD)..HEAD"
  while IFS= read -r -d '' message; do check "$message"; done < <(git log -z --no-merges --format=%B "$range")
fi
exit "$fail"
