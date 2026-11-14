#!/bin/sh
# Undo of step 6: removes the trial base image. Docker refuses while a container uses it.
set -eu
. "$(dirname "$0")/common.sh"
TAG=evalskills/trial-base:d136a597
plan "  1. docker image rm $TAG (no -f: refused while anything uses it)"
if docker image inspect "$TAG" >/dev/null 2>&1; then docker image rm "$TAG"; fi
say "done"
