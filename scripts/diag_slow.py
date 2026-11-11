"""Throwaway diagnostic for #112: per-check timing of the slowest catalog skills in the pinned image. Not for merge."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import catalog_rescan as cr  # noqa: E402

ev = cr.ev
SLOW = {"skills/taste-skill", "skills/diagram-design", ".", "skills/claude-api"}
ROOTS = {"alchaincyf/huashu-design", "platxa/platxa-skill-generator", "alchaincyf/nuwa-skill"}


def main(image: str) -> int:
    for old in cr.latest_per_skill(ev.load_results()):
        src = old["source"]
        if src["skill_path"] not in SLOW or (src["skill_path"] == "." and src["repo"] not in ROOTS):
            continue
        d, sha = ev.checkout(src["repo"], src["commit"])
        work = Path(tempfile.mkdtemp())
        skill = work / "in" / old["skill_name"]
        shutil.copytree(d / src["skill_path"], skill, symlinks=True, ignore=shutil.ignore_patterns(".git"))
        files = [p for p in skill.rglob("*") if p.is_file()]
        big = sorted(((p.stat().st_size, str(p.relative_to(skill))) for p in files), reverse=True)[:5]
        exts = {}
        for p in files:
            exts[p.suffix] = exts.get(p.suffix, 0) + 1
        print(f"=== {src['repo']}:{src['skill_path']} files={len(files)} bytes={sum(p.stat().st_size for p in files)}")
        print("    largest:", big)
        print("    by ext:", dict(sorted(exts.items(), key=lambda x: -x[1])[:8]))
        (work / "r").mkdir()
        (work / "r").chmod(0o777)
        cmd = [
            "docker", "run", "--rm", "--network", "none", "--user", f"{os.getuid()}:{os.getgid()}",
            "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--tmpfs", "/tmp:size=1g,mode=1777", "-e", "HOME=/tmp",
            "-e", "TMPDIR=/tmp", "--mount", f"type=bind,src={skill.parent},dst=/scan,readonly",
            "--mount", f"type=bind,src={work / 'r'},dst=/reports", "-w", "/tmp", "--entrypoint", "skillevaluator",
            image, "tier1", f"/scan/{skill.name}", "--no-llm", "-r", "json", "-o", "/reports",
        ]  # fmt: skip
        t0 = time.monotonic()
        p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        for line in p.stderr:
            if line.startswith("["):
                print(f"    {time.monotonic() - t0:7.1f}s {line.rstrip()[:200]}", flush=True)
        p.wait()
        print(f"    total {time.monotonic() - t0:.1f}s exit {p.returncode}")
        rep = work / "r" / "skillevaluator-tier1.json"
        if rep.is_file():
            for r in json.loads(rep.read_text()).get("results", []):
                if r.get("validator") in ("Security Scan", "Code Risk Analysis"):
                    errs = (r.get("legacy") or {}).get("errors") or []
                    print("   ", r["validator"], r["status"], [str(e)[:160] for e in errs][:3])
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
