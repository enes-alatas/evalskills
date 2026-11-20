"""Do the two Tier 1 engines score the same content the same way? (#65 review)

The catalog was scanned by SkillEvaluator d136a59 with uv-provisioned scanners. Deployed scans now come from the
digest-pinned Tier 1 image. This re-scans one completed catalog skill per category, at its recorded commit, through
that image, using the command deploy/scanner/scanner.py runs. It checks the folder digest is unchanged and compares
the summary fields the leaderboard shows.

Usage: python3 -I scripts/engine_parity.py IMAGE OUT_DIR [PER_CATEGORY]   (needs git and Docker; no secrets)
The image runs with no network, read-only, all capabilities dropped. It writes OUT_DIR/parity.json and parity.md.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))
import evaluate as ev  # noqa: E402

FIELDS = (
    "overall_status",
    "quality.score",
    "quality.grade",
    "quality.dimensions",
    "severity_counts",
    "security.distinct",
)


def pick(per_category: int) -> list[dict]:
    seen, out = {}, []
    for r in ev.load_results():
        if r["status"] != "completed" or r["mode"] != "tier1-static" or not r.get("category"):
            continue
        key = (r["source"]["repo"], r["source"]["skill_path"])
        if any(key == (o["source"]["repo"], o["source"]["skill_path"]) for o in out):
            continue
        if seen.get(r["category"], 0) < per_category:
            seen[r["category"]] = seen.get(r["category"], 0) + 1
            out.append(r)
    return out


def field(summary: dict, path: str):
    for part in path.split("."):
        summary = (summary or {}).get(part)
    return summary


def changed_checks(old: dict, new: dict, report: dict) -> dict:
    """Per check: old and new (status, findings, severity, incomplete scanners) where they differ, with the new
    finding titles. Titles from the secrets and PII validators are never printed: they can quote what was matched."""

    def key(c: dict) -> tuple:
        return c["status"], c["findings"], c["severity"], c.get("incomplete") or []

    before = {c["name"]: key(c) for c in old.get("checks", [])}
    after = {c["name"]: key(c) for c in new.get("checks", [])}
    out = {}
    for name in sorted(set(before) | set(after), key=str):
        if before.get(name) != after.get(name):
            titles = []
            if name not in ev.SCANNER_VALIDATORS:
                for r in report.get("results", []):
                    if r.get("validator") == name:
                        titles = sorted(f"{f.get('severity')}: {ev.finding_title(f)}" for f in r.get("findings") or [])
            out[str(name)] = {"old": before.get(name), "new": after.get(name), "new_titles": titles}
    return out


def scan(image: str, skill: Path, reports: Path) -> tuple[int, float]:
    reports.mkdir(parents=True)
    reports.chmod(0o777)
    cmd = [
        "docker", "run", "--rm", "--network", "none", "--user", f"{os.getuid()}:{os.getgid()}", "--read-only",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--tmpfs", "/tmp:size=1g,mode=1777", "-e", "HOME=/tmp", "-e",
        "TMPDIR=/tmp", "--mount", f"type=bind,src={skill.parent},dst=/scan,readonly",
        "--mount", f"type=bind,src={reports},dst=/reports", "-w", "/tmp", "--entrypoint", "skillevaluator", image,
        "tier1", f"/scan/{skill.name}", "--no-llm", "-r", "json", "-o", "/reports",
    ]  # fmt: skip
    deadlines = [x for k, v in ev.skillspector_deadlines(ev.TIER1_TIMEOUT).items() for x in ("-e", f"{k}={v}")]
    cmd[cmd.index("--mount") : cmd.index("--mount")] = deadlines  # the sidecar's SkillSpector deadlines (#112)
    t0 = time.monotonic()
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=ev.TIER1_TIMEOUT)
    return p.returncode, round(time.monotonic() - t0, 1)


def main(image: str, out_dir: Path, per_category: int = 1) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for rec in pick(per_category):
        src = rec["source"]
        row = {
            "repo": src["repo"],
            "skill_path": src["skill_path"],
            "category": rec["category"],
            "commit": src["commit"],
        }
        try:
            d, _ = ev.checkout(src["repo"], src["commit"])
            work = Path(tempfile.mkdtemp(prefix="parity-"))
            skill = work / "in" / Path(src["skill_path"]).name
            shutil.copytree(d / src["skill_path"], skill, symlinks=True)
            row["digest_match"] = ev.tree_digest(skill) == src["tree_digest"]
            code, secs = scan(image, skill, work / "reports")
            rep = work / "reports" / "skillevaluator-tier1.json"
            row.update(exit_code=code, duration_s=secs, old_duration_s=rec["duration_s"])
            if not rep.is_file():
                row["status"] = "no report"
            else:
                report = json.loads(rep.read_text())
                new = ev.summarize_tier1(report)
                diffs = {f: [field(rec["tier1"], f), field(new, f)] for f in FIELDS}
                row["diffs"] = {f: v for f, v in diffs.items() if v[0] != v[1]}
                row["checks_changed"] = changed_checks(rec["tier1"], new, report)
                row["status"] = "same" if not row["diffs"] else "differs"
            shutil.rmtree(work, ignore_errors=True)
        except Exception as e:  # noqa: BLE001 -- one skill's failure is a row, not the end of the run
            row["status"] = f"error: {type(e).__name__}: {str(e)[:200]}"
        rows.append(row)
        print(json.dumps(row), flush=True)
    (out_dir / "parity.json").write_text(json.dumps({"image": image, "rows": rows}, indent=1))
    lines = [
        f"Engine parity: `{image}` against the catalog (SkillEvaluator d136a59)",
        "",
        "| skill | category | digest | result | old s | new s | differences |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        diffs = "; ".join(f"{k}: {v[0]} → {v[1]}" for k, v in r.get("diffs", {}).items())
        lines.append(
            f"| {r['repo']}:{r['skill_path']} | {r['category']} | {'same' if r.get('digest_match') else 'CHANGED'} "
            f"| {r['status']} | {r.get('old_duration_s', '')} | {r.get('duration_s', '')} | {diffs} |"
        )
    (out_dir / "parity.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], Path(sys.argv[2]), int(sys.argv[3]) if len(sys.argv) > 3 else 1))
