# Trial image builds for `--env-mode gvisor` (#137)

The Docker socket proxy builds a trial's image only when SkillEvaluator's generated Dockerfile is on
`allowed-dockerfiles.txt`. It then builds the matching committed file under `final/`, FROM the trial base image,
with the legacy builder and no network. See `gateway/docker_build.py` and the 2026-11-13 amendment to ADR-0003.

| Path | What |
| --- | --- |
| `original/<sha256>.Dockerfile` | what SkillEvaluator d136a597 generates, one per task shape (input yes/no, repo copy yes/no) |
| `final/<sha256>.Dockerfile` | what the proxy builds instead: `FROM evalskills/trial-base:d136a597`, then the original after its dependency prefix |
| `allowed-dockerfiles.txt` | `<original sha256> final/<sha256>.Dockerfile` |
| `base/` | the trial base image: the template's apt line and the verifier's pip packages, pinned by hash |

## Regenerate (after a SkillEvaluator repin)

    cd $SKILLEVALUATOR_DIR && uv run -q --python 3.13 --extra tier3 python -I <repo>/scripts/gvisor_dockerfiles.py \
        --sources <repo>/sources --skills <repo>/pilot/skills.tsv --write-allowlist <repo>/deploy/gvisor/build

Then review the diff. A new shape or a changed template shows up as a new sha256. Update the base image too if the
dependency prefix changed: the script refuses an unexpected prefix. The pip lock:

    cd deploy/gvisor/build/base && uv pip compile requirements.in --generate-hashes --python-version 3.12 \
        --python-platform x86_64-manylinux_2_28 -o requirements.lock

## The base image (off-host, never on the shared host)

1. On a machine with Docker, set the `FROM` digest in `base/Dockerfile` to the current `python:3.12-slim` digest
   (`docker buildx imagetools inspect python:3.12-slim`). Commit that by PR: it is all zeros until the first build.
2. `docker build -t evalskills/trial-base:d136a597 deploy/gvisor/build/base`
3. `docker save evalskills/trial-base:d136a597 | gzip > trial-base.tar.gz`. Copy the file to the host, and announce
   the step first (CONVENTIONS, shared hosts).
4. On the host: `docker load < trial-base.tar.gz`, then
   `docker image inspect --format '{{.Id}}' evalskills/trial-base:d136a597`. Put that ID in the proxy config's
   `builds.base_image_id`. The proxy refuses to build on any other image under that tag.

The proxy also refuses a build when the base image is missing: `pull=0` alone wouldn't stop the legacy builder from
pulling a missing `FROM` image.
