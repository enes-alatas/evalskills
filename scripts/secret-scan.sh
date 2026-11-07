#!/usr/bin/env bash
# Scans for credential shapes with gitleaks; CI and the pre-commit hook run this same script.
#   scripts/secret-scan.sh            commits on this branch not yet on BASE (default origin/main), or all history
#                                     when BASE does not exist (first push)
#   scripts/secret-scan.sh --staged   what is staged for the next commit (pre-commit hook)
# Findings are printed redacted. Exit status 1 means a finding.
set -euo pipefail
gitleaks_bin="${GITLEAKS:-gitleaks}"
command -v "$gitleaks_bin" >/dev/null || { echo "gitleaks not found; set GITLEAKS or install 8.30.1" >&2; exit 2; }
cd "$(git rev-parse --show-toplevel)"
config=()
[[ -f .gitleaks.toml ]] && config=(--config .gitleaks.toml)
if [[ "${1:-}" == "--staged" ]]; then
  exec "$gitleaks_bin" git --pre-commit --staged --redact --no-banner "${config[@]}" .
fi
base="${BASE:-origin/main}"
if git rev-parse --verify --quiet "$base" >/dev/null; then
  range="$(git merge-base "$base" HEAD)..HEAD"
  [[ "$(git rev-list --count "$range")" == 0 ]] && { echo "no commits beyond $base"; exit 0; }
  exec "$gitleaks_bin" git --log-opts="$range" --redact --no-banner "${config[@]}" .
fi
exec "$gitleaks_bin" git --redact --no-banner "${config[@]}" .
