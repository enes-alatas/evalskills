#!/usr/bin/env bash
# The checks the `ci` job runs, in one place: CI calls this script, and so does the pre-push hook, so the two can't
# drift. It runs every check and then reports, so one round fixes everything it found.
#   scripts/check.sh          every check
#   scripts/check.sh --fast   every check but the tests
# Environment:
#   BASE       what the branch is compared to for the secret scan and commit subjects (default origin/main)
#   SUBJECTS   0 skips the commit subject check (a push to main, a diag/* branch)
#   CI         when set (GitHub sets CI=true), a missing tool fails the run. Locally it warns and that check is
#              skipped: CI stays the gate.
#   GITLEAKS, ACTIONLINT, SHELLCHECK   paths to those tools, when they aren't on PATH
# Pinned versions; ci.yml installs exactly these (tests/test_hooks.py checks that they match).
GITLEAKS_VERSION=8.30.1
RUFF_VERSION=0.17.4
PYTEST_VERSION=9.2.0
ACTIONLINT_VERSION=1.7.12
SHELLCHECK_VERSION=0.11.0
set -uo pipefail
cd "$(git rev-parse --show-toplevel)" || exit 2
fast=0
[[ "${1:-}" == "--fast" ]] && fast=1
failed=() skipped=()

missing() {
  if [[ -n "${CI:-}" ]]; then
    echo "check.sh: $1 not found; CI must have it" >&2
    failed+=("$2")
  else
    echo "check.sh: WARNING: $1 not found, so '$2' is skipped here. CI still runs it." >&2
    skipped+=("$2")
  fi
}

run() { # run <name> <command...>
  local name="$1"
  shift
  echo "== $name"
  "$@" || failed+=("$name")
}

pinned() { # pinned <name> <actual version> <expected>: a different version fails in CI and warns locally
  [[ "$2" == "$3" ]] && return 0
  if [[ -n "${CI:-}" ]]; then
    echo "check.sh: $1 is $2, but $3 is pinned" >&2
    failed+=("$1 version")
  else
    echo "check.sh: WARNING: $1 is $2 here, CI pins $3; results may differ." >&2
  fi
}

tool() { # prints the command for a tool, or nothing: tool <env var> <binary>
  local path="${!1:-$2}"
  command -v "$path" 2>/dev/null
}

ruff_cmd=() pytest_cmd=()
if command -v ruff >/dev/null; then
  ruff_cmd=(ruff)
elif command -v uvx >/dev/null; then
  ruff_cmd=(uvx -q "ruff@$RUFF_VERSION")
fi
if python3 -c 'import pytest' 2>/dev/null; then
  pytest_cmd=(python3 -m pytest)
elif command -v uvx >/dev/null; then
  pytest_cmd=(uvx -q "pytest@$PYTEST_VERSION")
fi

if gitleaks_bin="$(tool GITLEAKS gitleaks)"; then
  pinned gitleaks "$("$gitleaks_bin" version)" "$GITLEAKS_VERSION"
  run "secret scan" env GITLEAKS="$gitleaks_bin" scripts/secret-scan.sh
else
  missing gitleaks "secret scan"
fi

if [[ "${SUBJECTS:-1}" != 0 ]]; then
  run "commit subjects" scripts/commit-subjects.sh
fi

if shellcheck_bin="$(tool SHELLCHECK shellcheck)"; then
  pinned shellcheck "$("$shellcheck_bin" --version | sed -n 's/^version: //p')" "$SHELLCHECK_VERSION"
  mapfile -t shell_files < <(git ls-files '*.sh' 'scripts/hooks/*')
  run "shellcheck" "$shellcheck_bin" -x -P SCRIPTDIR --severity=warning "${shell_files[@]}"
else
  missing shellcheck "shellcheck"
  shellcheck_bin=""
fi

if actionlint_bin="$(tool ACTIONLINT actionlint)"; then
  pinned actionlint "$("$actionlint_bin" -version | head -1)" "$ACTIONLINT_VERSION"
  # actionlint runs shellcheck on each run: block; without it, only the workflow syntax is checked.
  run "actionlint" "$actionlint_bin" ${shellcheck_bin:+-shellcheck "$shellcheck_bin"}
else
  missing actionlint "actionlint"
fi

if ((${#ruff_cmd[@]})); then
  run "ruff check" "${ruff_cmd[@]}" check .
  run "ruff format" "${ruff_cmd[@]}" format --check .
else
  missing ruff "ruff"
fi

if ((!fast)); then
  if ((${#pytest_cmd[@]})); then
    run "tests" "${pytest_cmd[@]}" -q # exit 5, no tests collected, fails too
  else
    missing pytest "tests"
  fi
fi

((${#skipped[@]})) && echo "check.sh: skipped here: ${skipped[*]}" >&2
if ((${#failed[@]})); then
  echo "check.sh: FAILED: ${failed[*]}" >&2
  exit 1
fi
echo "check.sh: all checks passed"
