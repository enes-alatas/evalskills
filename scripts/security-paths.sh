#!/usr/bin/env bash
# Prints each changed path that matches a glob in .github/security-review-paths. Changed paths come on stdin, one per
# line. Exit 0 always; an empty output means no security review is needed.
set -euo pipefail
patterns_file="$(git rev-parse --show-toplevel)/.github/security-review-paths"
mapfile -t patterns < <(grep -vE '^\s*(#|$)' "$patterns_file")
while IFS= read -r path; do
  for pattern in "${patterns[@]}"; do
    # shellcheck disable=SC2053  # the pattern is a glob on purpose
    if [[ "$path" == $pattern ]]; then echo "$path"; break; fi
  done
done
