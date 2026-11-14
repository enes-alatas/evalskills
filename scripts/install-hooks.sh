#!/usr/bin/env bash
# Links the repo's git hooks (scripts/hooks/) into this clone. Run once per clone; running it again is harmless.
# A different hook already in place is kept and reported, unless --force.
set -euo pipefail
root="$(git rev-parse --show-toplevel)"
hooks="$(git rev-parse --path-format=absolute --git-path hooks)"
force=0
[[ "${1:-}" == "--force" ]] && force=1
mkdir -p "$hooks"
status=0
for src in "$root"/scripts/hooks/*; do
  name="$(basename "$src")"
  dest="$hooks/$name"
  if [[ -L "$dest" && "$(readlink -f "$dest")" == "$(readlink -f "$src")" ]]; then
    echo "$name: already installed"
    continue
  fi
  if [[ -e "$dest" || -L "$dest" ]] && ((!force)); then
    # The old scripts/pre-commit link is ours too; replace it.
    if [[ ! -L "$dest" || "$(readlink "$dest")" != *scripts/pre-commit ]]; then
      echo "$name: a different hook is installed at $dest; kept (rerun with --force to replace it)" >&2
      status=1
      continue
    fi
  fi
  ln -sfn "$src" "$dest"
  echo "$name: installed"
done
exit "$status"
