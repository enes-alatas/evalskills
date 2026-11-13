"""Generate SkillEvaluator's task Dockerfiles for the pilot skills and list their sha256s (#137, PR B).

The gVisor build endpoint only builds a Dockerfile whose sha256 is on deploy/gvisor/allowed-dockerfiles.txt. This
shows which originals the pinned SkillEvaluator produces. It runs SkillEvaluator's own ``_write_dockerfile``, the
function its task generator calls, for every pilot skill: with and without the skill, with and without task input,
and with the linked and the full repo context. Then it prints one line per variant, plus the distinct sha256s.
With ``--write-allowlist DIR`` it also writes the reviewed build inputs: ``original/<sha256>.Dockerfile`` (what
SkillEvaluator generates), ``final/<sha256>.Dockerfile`` (``FROM`` the trial base, then the original's fixed steps
after its dependency prefix) and ``allowed-dockerfiles.txt`` (``<original sha256> final/<sha256>.Dockerfile``). The
proxy never rewrites a Dockerfile; it only looks the original up and builds the committed final one.

A skill-specific Dockerfile (a custom ``evals/environment/Dockerfile``, ``requirements.txt`` or
``apt-packages.txt``) shows up as its own sha256, so it's never allowed by accident.

It reads the skills' files and copies them into a temp dir. It never runs them.

Run inside the pinned SkillEvaluator (it imports it):
    cd $SKILLEVALUATOR_DIR && uv run -q --python 3.13 --extra tier3 python -I <repo>/scripts/gvisor_dockerfiles.py \
        --sources <repo>/sources --skills <repo>/pilot/skills.tsv [--write-allowlist <repo>/deploy/gvisor/build]
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import shutil
import sys
import tempfile
from pathlib import Path

from skillevaluator.tier3.harbor import adapter


def pilot_rows(tsv: Path) -> list[tuple[str, str]]:
    rows = []
    for line in tsv.read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            repo, skill_path = line.split("\t")[:2]
            rows.append((repo, skill_path))
    return list(dict.fromkeys(rows))


def generate(skill: Path, *, has_skill: bool, with_input: bool, repo_mode: str, work: Path) -> tuple[bytes, dict]:
    task = Path(tempfile.mkdtemp(dir=work))
    evals_dir = Path(tempfile.mkdtemp(dir=work))
    entry = {"id": "case-1", "prompt": "p"}
    input_dir = None
    if with_input:
        input_dir = evals_dir / "files"
        input_dir.mkdir()
        (input_dir / "data.txt").write_text("synthetic input\n")
    adapter._write_dockerfile(
        task,
        skill_path=skill,
        reference_skills_dir=None,
        workspace_skill_paths=None,
        has_skill=has_skill,
        input_files_dir=input_dir,
        entry=entry,
        evals_dir=evals_dir,
        exclude_skill_name=skill.name,
        repo_context_skill_path=skill,
        repo_context_mode=repo_mode,
    )
    env = task / "environment"
    shape = {k: (env / k).exists() for k in ("input", "repo", "repo-linked-root")}
    return (env / "Dockerfile").read_bytes(), shape


BASE_TAG = "evalskills/trial-base:d136a597"  # loaded off-host, checked by image ID before every build
# What SkillEvaluator d136a597 puts before "RUN mkdir -p /workspace/…": the base image, the apt line, the verifier's
# pip install and its import smoke. The trial base image holds all of it (deploy/gvisor/build/base/Dockerfile).
PREFIX_LINES = 9
TAIL_START = "RUN mkdir -p /workspace/skills /workspace/input /workspace/output \\"


def final_dockerfile(original: bytes) -> bytes:
    lines = original.decode().split("\n")
    head = lines[:PREFIX_LINES]
    if head[0] != "FROM python:3.12-slim" or "apt-get" not in head[2] or "pip" not in head[7]:
        raise ValueError("unexpected dependency prefix: was SkillEvaluator repinned? regenerate and review")
    if lines[PREFIX_LINES] != "" or lines[PREFIX_LINES + 1] != TAIL_START:
        raise ValueError("the fixed steps don't start where expected")
    return ("\n".join([f"FROM {BASE_TAG}", *lines[PREFIX_LINES:]])).encode()


def write_allowlist(out: Path, originals: dict[str, bytes]) -> None:
    (out / "original").mkdir(parents=True, exist_ok=True)
    (out / "final").mkdir(parents=True, exist_ok=True)
    lines = ["# <sha256 of SkillEvaluator's generated Dockerfile> <the committed Dockerfile the proxy builds instead>"]
    for sha in sorted(originals):
        (out / "original" / f"{sha}.Dockerfile").write_bytes(originals[sha])
        (out / "final" / f"{sha}.Dockerfile").write_bytes(final_dockerfile(originals[sha]))
        lines.append(f"{sha} final/{sha}.Dockerfile")
    (out / "allowed-dockerfiles.txt").write_text("\n".join(lines) + "\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sources", type=Path, required=True)
    ap.add_argument("--skills", type=Path, required=True)
    ap.add_argument("--write-allowlist", type=Path, help="write original/, final/ and allowed-dockerfiles.txt here")
    args = ap.parse_args(argv)
    seen: dict[str, bytes] = {}
    failed = 0
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        for repo, skill_path in pilot_rows(args.skills):
            skill = args.sources / repo.replace("/", "_") / skill_path
            for has_skill, with_input, repo_mode in itertools.product((True, False), (False, True), ("linked", "full")):
                variant = {"skill": f"{repo}/{skill_path}", "has_skill": has_skill, "input": with_input}
                variant["repo_context"] = repo_mode
                try:
                    data, shape = generate(
                        skill, has_skill=has_skill, with_input=with_input, repo_mode=repo_mode, work=work
                    )
                except Exception as e:  # report and keep going: one odd skill mustn't hide the others' results
                    failed += 1
                    print(json.dumps({**variant, "error": f"{type(e).__name__}: {str(e)[:200]}"}))
                    continue
                sha = hashlib.sha256(data).hexdigest()
                seen.setdefault(sha, data)
                mentions = sorted({s for s in (skill.name, repo.split("/")[1]) if s.encode() in data})
                print(json.dumps({**variant, "sha256": sha, "shape": shape, "mentions": mentions}))
            shutil.rmtree(work, ignore_errors=True)
            work.mkdir()
    print(json.dumps({"distinct": sorted(seen), "failed": failed}))
    if args.write_allowlist:
        write_allowlist(args.write_allowlist, seen)
    return 0


if __name__ == "__main__":
    sys.exit(main())
