#!/bin/sh
# Step 6 (#137): load the trial base image, built in CI by the trial-base workflow, never on this host.
# First check the run on GitHub and download its artifact as deploy/gvisor/build/README.md says (gh run view, then
# gh run download by run id). This script then checks the tarball against trial-base.json, loads it, and prints the
# image ID as this host's daemon reports it: step 7 pins that ID, not the one CI built.
#   6-base-image.sh --yes DIR      (DIR holds trial-base.tar.gz and trial-base.json)
set -eu
. "$(dirname "$0")/common.sh"
TAG=evalskills/trial-base:d136a597
DIR=""
for a in "$@"; do [ "$a" = --yes ] || DIR="$a"; done
plan "  1. check DIR/trial-base.json: image $TAG, \"loadable\": true, \"pinned\": \"committed\"
  2. check sha256 of DIR/trial-base.tar.gz against its tar_sha256
  3. check the tarball's manifest.json tags exactly $TAG, so the load can't retag another service's image
  4. gunzip -c | docker load, then docker image inspect $TAG and print its ID (for step 7)
Undo: 6-base-image-undo.sh"
[ -n "$DIR" ] && [ -f "$DIR/trial-base.json" ] && [ -f "$DIR/trial-base.tar.gz" ] ||
  die "give the folder that holds trial-base.tar.gz and trial-base.json"
field() { python3 -I -c 'import json,sys; v=json.load(open(sys.argv[1]))[sys.argv[2]]; print(json.dumps(v) if isinstance(v, bool) else v)' "$DIR/trial-base.json" "$1"; }
[ "$(field image)" = "$TAG" ] || die "the artifact is not $TAG"
[ "$(field loadable)" = true ] || die "the artifact is not loadable (a PR or branch run): don't load it"
[ "$(field pinned)" = committed ] || die "the artifact was built from an uncommitted digest: don't load it"
want=$(field tar_sha256)
got=$(sha256sum "$DIR/trial-base.tar.gz" | cut -d' ' -f1)
[ "$got" = "$want" ] || die "tarball sha256 $got does not match trial-base.json"
tags=$(python3 -I -c 'import json, sys, tarfile
with tarfile.open(sys.argv[1], "r:gz") as t:
    m = json.load(t.extractfile("manifest.json"))
print(json.dumps([tag for entry in m for tag in (entry.get("RepoTags") or [])]))' "$DIR/trial-base.tar.gz") ||
  die "could not read manifest.json in the tarball"
[ "$tags" = "[\"$TAG\"]" ] || die "the tarball tags $tags, not exactly $TAG: don't load it"
gunzip -c "$DIR/trial-base.tar.gz" | docker load
id=$(docker image inspect --format '{{.Id}}' "$TAG")
say "loaded $TAG as $id (run $(field run_id), commit $(field commit))"
say "put this ID in /etc/evals/docker-proxy.json as builds.base_image_id (7-docker-proxy.sh does it)"
