"""Re-scan the catalog through the digest-pinned Tier 1 image (#110).

The catalog was scanned by SkillEvaluator d136a59 with uv-provisioned scanners, which had no semgrep (#102). This
re-scans the latest record of every catalog skill, at its recorded commit, through the pinned image with the same
contained command as scripts/engine_parity.py, so only the engine changes. It writes, under OUT_DIR:

  results.jsonl                     one new record per skill, in the catalog's schema (append to catalog/results.jsonl)
  reports/<slug>/<run>/report.public.json   the sanitized report; the raw report and logs are never written here
  rescan.json, rescan.md            per skill: old and new summary fields and the checks that changed

Usage: python3 -I scripts/catalog_rescan.py IMAGE OUT_DIR [LIMIT] [--sample N]   (needs git and Docker; no secrets)
  --sample N   a fixed sample of N skills from N different repositories, as a pull request's check does (#233)
"""

import datetime as dt
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import engine_parity as par  # noqa: E402

ev = par.ev


def latest_per_skill(results: list[dict]) -> list[dict]:
    """The record the board shows for each catalog skill: its newest Tier 1 static run."""
    out: dict[tuple, dict] = {}
    for r in results:
        if r.get("mode") != "tier1-static":
            continue
        key = (r["source"]["repo"], r["source"]["skill_path"])
        if key not in out or r["started_at"] > out[key]["started_at"]:
            out[key] = r
    return sorted(out.values(), key=lambda r: (r["source"]["repo"], r["source"]["skill_path"]))


def sample(records: list[dict], n: int) -> list[dict]:
    """A fixed sample for a PR's check (#233): n skills, each from a different repository, spread evenly over the
    repositories in name order, and the first skill by path in each. The same catalog always gives the same sample."""
    by_repo: dict[str, list[dict]] = {}
    for r in records:
        by_repo.setdefault(r["source"]["repo"], []).append(r)
    repos = sorted(by_repo)
    n = min(n, len(repos))
    picked = [repos[i * len(repos) // n] for i in range(n)] if n else []
    return [min(by_repo[repo], key=lambda r: r["source"]["skill_path"]) for repo in picked]


def image_version(image: str) -> str | None:
    p = subprocess.run(
        ["docker", "run", "--rm", "--network", "none", "--entrypoint", "skillevaluator", image, "--version"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    return p.stdout.split()[-1] if p.returncode == 0 and p.stdout.split() else None


def new_record(old: dict, evaluator: dict, stamp: str, attempts: list[dict], report: dict | None) -> dict:
    src = dict(old["source"])
    slug = f"{src['repo'].replace('/', '__')}__{src['skill_path'].strip('/').replace('/', '__')}"
    rec = {
        "id": f"{slug}@{src['commit'][:12]}:tier1-static:{stamp}",
        "skill_name": old["skill_name"],
        "category": old.get("category", ""),
        "mode": "tier1-static",
        "source": src,
        "evaluator": evaluator,
        "agent": None,
        "model": None,
        "test_cases": None,
        "baseline": None,
        "started_at": stamp,
        "duration_s": round(sum(a["duration_s"] for a in attempts), 1),
        "exit_code": attempts[-1]["exit_code"],
        "cost_usd": 0.0,
        "report_dir": f"reports/{slug}/{src['commit'][:12]}-tier1-static-{stamp}",
        "attempts": attempts,
    }
    if report is not None:
        rec["status"] = "completed"
        rec["tier1"] = ev.summarize_tier1(report)
    else:
        rec["status"] = "failed"
        rec["error"] = f"no report (exit code {rec['exit_code']})"
    return rec


UNKNOWN_VALIDATOR = "refused: PII or secret findings under a validator the sanitizer doesn't list"


def rescan(image: str, old: dict, evaluator: dict, out_dir: Path) -> tuple[dict | None, dict]:
    src = old["source"]
    row = {"repo": src["repo"], "skill_path": src["skill_path"], "category": old.get("category", "")}
    d, _ = ev.checkout(src["repo"], src["commit"])
    work = Path(tempfile.mkdtemp(prefix="rescan-"))
    try:
        # The folder keeps the name the catalog run gave it (a root skill is named after its cache folder), since
        # SkillEvaluator compares it with the skill's name.
        skill = work / "in" / old["skill_name"]
        shutil.copytree(d / src["skill_path"], skill, symlinks=True, ignore=shutil.ignore_patterns(".git"))
        if ev.tree_digest(skill) != src["tree_digest"]:
            row["status"] = "skipped: content at the recorded commit no longer matches its digest"
            return None, row
        stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        attempts = []
        for attempt in range(2):  # SkillSpector crashes transiently (~1 in 20 runs); retry once, as evaluate.py does
            reports = work / f"reports{attempt}"
            code, secs = par.scan(image, skill, reports)
            rep_path = reports / "skillevaluator-tier1.json"
            crash = ev.scanner_crash(rep_path)
            attempts.append({"exit_code": code, "duration_s": secs, "scanner_crash": crash})
            if not crash:
                break
        report = json.loads(rep_path.read_text()) if rep_path.is_file() else None
        unknown = ev.unknown_scanner_validators(report) if report else set()
        if unknown:
            # A renamed PII/secret validator: the sanitizer still redacts it, but the pin needs a look first (#243).
            row["status"] = f"{UNKNOWN_VALIDATOR}: {', '.join(sorted(unknown))}"
            return None, row
        rec = new_record(old, evaluator, stamp, attempts, report)
        if report is not None:
            pub = out_dir / rec["report_dir"]
            pub.mkdir(parents=True)
            (pub / "report.public.json").write_text(json.dumps(ev.sanitize_report(report), indent=1))
            new = rec["tier1"]
            diffs = {f: [par.field(old.get("tier1"), f), par.field(new, f)] for f in par.FIELDS}
            row["diffs"] = {f: v for f, v in diffs.items() if v[0] != v[1]}
            row["checks_changed"] = par.changed_checks(old.get("tier1") or {}, new, report)
        row.update(status=rec["status"], old_duration_s=old.get("duration_s"), duration_s=rec["duration_s"])
        return rec, row
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main(image: str, out_dir: Path, limit: int | None = None, sample_size: int | None = None) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    evaluator = {"version": image_version(image), "commit": None, "image": image}
    if not evaluator["version"]:
        print("the image did not report a SkillEvaluator version", file=sys.stderr)
        return 1
    todo = latest_per_skill(ev.load_results())
    todo = sample(todo, sample_size) if sample_size else todo[:limit]
    records, rows = [], []
    for old in todo:
        try:
            rec, row = rescan(image, old, evaluator, out_dir)
        except Exception as e:  # noqa: BLE001 -- one skill's failure is a row, not the end of the run
            rec = None
            row = {"repo": old["source"]["repo"], "skill_path": old["source"]["skill_path"]}
            row["status"] = f"error: {type(e).__name__}: {str(e)[:200]}"
        if rec:
            records.append(rec)
        rows.append(row)
        print(json.dumps(row), flush=True)
    (out_dir / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
    (out_dir / "rescan.json").write_text(json.dumps({"evaluator": evaluator, "rows": rows}, indent=1))
    changed = [r for r in rows if r.get("diffs") or r.get("checks_changed")]
    lines = [
        f"Catalog re-scan: `{image}` (SkillEvaluator {evaluator['version']})",
        "",
        f"{len(rows)} skills, {len(records)} new records, "
        f"{sum(r.get('status') == 'completed' for r in rows)} completed, {len(changed)} with a changed check.",
        "",
        "| skill | result | old s | new s | differences | changed checks |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        diffs = "; ".join(f"{k}: {v[0]} → {v[1]}" for k, v in r.get("diffs", {}).items())
        checks = "; ".join(
            f"{k}: {v['old'][0] if v['old'] else '-'} → {v['new'][0] if v['new'] else '-'}"
            for k, v in r.get("checks_changed", {}).items()
        )
        lines.append(
            f"| {r['repo']}:{r['skill_path']} | {r['status']} | {r.get('old_duration_s', '')} "
            f"| {r.get('duration_s', '')} | {diffs} | {checks} |"
        )
    (out_dir / "rescan.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines[:3]))
    refused = [r for r in rows if str(r.get("status", "")).startswith(UNKNOWN_VALIDATOR)]
    if refused:
        print(f"{len(refused)} skill(s) {refused[0]['status']}; add it to SCANNER_VALIDATORS", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    args, size = sys.argv[1:], None
    if "--sample" in args:
        i = args.index("--sample")
        size = int(args[i + 1])
        del args[i : i + 2]
    sys.exit(main(args[0], Path(args[1]), int(args[2]) if len(args) > 2 else None, size))
