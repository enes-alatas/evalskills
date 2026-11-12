"""Throwaway proof for #112: do the SkillSpector deadlines end slow scans inside the cap in the pinned image?"""

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import catalog_rescan as cr  # noqa: E402

par, ev = cr.par, cr.ev
SLOW = {"skills/taste-skill", "skills/diagram-design"}

print("deadlines:", ev.skillspector_deadlines(ev.TIER1_TIMEOUT), "cap:", ev.TIER1_TIMEOUT)
for old in cr.latest_per_skill(ev.load_results()):
    src = old["source"]
    if src["skill_path"] not in SLOW:
        continue
    d, _ = ev.checkout(src["repo"], src["commit"])
    work = Path(tempfile.mkdtemp())
    skill = work / "in" / old["skill_name"]
    shutil.copytree(d / src["skill_path"], skill, symlinks=True, ignore=shutil.ignore_patterns(".git"))
    try:
        code, secs = par.scan(sys.argv[1], skill, work / "r")
    except Exception as e:  # noqa: BLE001
        print(src["skill_path"], "ERROR", type(e).__name__)
        continue
    rep = work / "r" / "skillevaluator-tier1.json"
    print(src["skill_path"], "exit", code, "secs", secs, "report", rep.is_file())
    if rep.is_file():
        for r in json.loads(rep.read_text())["results"]:
            if r["validator"] == "Security Scan":
                print("  Security Scan", r["status"], r.get("incomplete_scans"))
                print("  errors:", [str(e)[:300] for e in (r.get("legacy") or {}).get("errors") or []])
                print("  findings:", len(r.get("findings") or []))
