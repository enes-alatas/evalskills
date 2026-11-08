"""Run SkillEvaluator on one public skill and record a provenance-complete result.

Usage: python3 -I pipeline/evaluate.py OWNER/REPO SKILL_PATH [CATEGORY] [--tiers 1]

Every record stores: source repo, skill path, exact commit, evaluator version+commit,
tiers run, agent/model (if any), scores, per-check statuses, duration, exit code,
and the path to the full underlying reports.
"""

import datetime as dt
import functools
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVALUATOR_DIR = Path(os.environ.get("SKILLEVALUATOR_DIR", "/home/loop/vendor/SkillEvaluator"))
CACHE = ROOT / "cache" / "repos"
REPORTS = ROOT / "reports"
SEED_RESULTS = ROOT / "catalog" / "results.jsonl"
RESULTS = Path(os.environ.get("RESULTS_PATH", str(SEED_RESULTS)))
UV = shutil.which("uv") or str(Path.home() / ".local/bin/uv")
TIER1_TIMEOUT = int(os.environ.get("TIER1_TIMEOUT", "600"))

# Only these variables reach the evaluator subprocess; nothing else from our env leaks.
ENV_ALLOW = {
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "http_proxy",
    "https_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "no_proxy",
    "NO_PROXY",
    "UV_CACHE_DIR",
    "XDG_CACHE_HOME",
}
# Opt-in provider vars, passed only for LLM-backed tiers.
PROVIDER_VARS = {
    "SKILL_EVAL_LLM_PROVIDER",
    "SKILL_EVAL_LLM_MODEL",
    "ANTHROPIC_API_KEY",
    "NVIDIA_API_KEY",
    "OPENAI_API_KEY",
}

GIT_LOCK = threading.RLock()  # guards the shared clone cache
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def sh(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw).stdout.strip()


def git_env():
    env = {k: v for k, v in os.environ.items() if k in ENV_ALLOW}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


MAX_REPO_KB = 300_000  # refuse repos over ~300 MB


class RepoTooLarge(ValueError):  # the worker shows a ValueError as "unsupported"
    pass


def dir_kb(d: Path) -> int:
    """On-disk size of a checkout in KB, without following symlinks."""
    total = 0
    for root, _dirs, files in os.walk(d):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total // 1024


def checkout(repo: str, ref: str | None = None) -> tuple[Path, str]:
    """Shallow-clone (or refresh) repo; return (dir, exact commit sha). A checkout over MAX_REPO_KB is deleted and
    refused, whatever the GitHub API said about its size."""
    if not REPO_RE.match(repo):
        raise ValueError(f"bad repo slug: {repo}")
    d = CACHE / repo.replace("/", "__")
    env = git_env()
    if not d.exists():
        d.parent.mkdir(parents=True, exist_ok=True)
        sh(
            [
                "git",
                "clone",
                "-q",
                "--depth",
                "1",
                "--no-tags",
                "-c",
                "core.symlinks=false",
                f"https://github.com/{repo}.git",
                str(d),
            ],
            env=env,
            timeout=300,
        )
    else:
        sh(["git", "-C", str(d), "fetch", "-q", "--depth", "1", "origin", ref or "HEAD"], env=env, timeout=300)
        sh(["git", "-C", str(d), "reset", "-q", "--hard", "FETCH_HEAD"], env=env)
    kb = dir_kb(d)
    if kb > MAX_REPO_KB:
        shutil.rmtree(d, ignore_errors=True)
        raise RepoTooLarge(f"Repository too large ({kb // 1024} MB > {MAX_REPO_KB // 1024} MB)")
    return d, sh(["git", "-C", str(d), "rev-parse", "HEAD"])


@functools.lru_cache(maxsize=1)
def evaluator_version() -> dict:
    ver = sh(
        [UV, "run", "-q", "--python", "3.13", "skillevaluator", "--version"], cwd=EVALUATOR_DIR, env=git_env()
    ).split()[-1]
    commit = sh(["git", "-C", str(EVALUATOR_DIR), "rev-parse", "HEAD"])
    return {"version": ver, "commit": commit}


def tree_digest(skill_dir: Path) -> str:
    """Content hash of the skill folder, so unchanged skills are not re-run."""
    h = hashlib.sha256()
    for p in sorted(skill_dir.rglob("*")):
        if p.is_file() and not p.is_symlink() and ".git" not in p.parts:
            h.update(str(p.relative_to(skill_dir)).encode())
            h.update(hashlib.sha256(p.read_bytes()).digest())
    return "sha256:" + h.hexdigest()


SECURITY_CHECKS = (
    "Security Scan",
    "Secrets Detection",
    "Code Risk Analysis",
    "PII Scan",
    "Dependency Vulnerability Audit",
    "Unicode Smuggling Detection",
)
SEVERITIES = ("critical", "high", "medium", "low", "info")


def finding_title(f: dict) -> str:
    return (f.get("message") or f.get("title") or "").split(":")[0].strip()[:80] or "unnamed"


def summarize_tier1(report: dict) -> dict:
    q = (report.get("quality_summary") or [{}])[0]
    checks, sec_types = [], {}
    sec_raw = dict.fromkeys(SEVERITIES, 0)
    for r in report.get("results", []):
        fs = r.get("findings") or []
        sev = dict.fromkeys(SEVERITIES, 0)
        for f in fs:
            sv = (f.get("severity") or "info").lower()
            sev[sv if sv in sev else "info"] += 1
            if r.get("validator") in SECURITY_CHECKS:
                sec_raw[sv if sv in sec_raw else "info"] += 1
                key = (sv, finding_title(f))
                sec_types[key] = sec_types.get(key, 0) + 1
        checks.append(
            {
                "name": r.get("validator"),
                "status": r.get("status"),
                "incomplete": [str(x) for x in (r.get("incomplete_scans") or [])][:5],
                "findings": len(fs),
                "severity": {k: v for k, v in sev.items() if v},
            }
        )
    rank = {s: i for i, s in enumerate(SEVERITIES)}
    distinct = dict.fromkeys(SEVERITIES, 0)
    for (sv, _), _n in sec_types.items():
        distinct[sv if sv in distinct else "info"] += 1
    return {
        "overall_status": report.get("overall_status"),
        "overall_passed": report.get("overall_passed"),
        "quality": {
            "score": q.get("overall_score"),
            "grade": q.get("grade"),
            "skill_type": q.get("skill_type"),
            "dimensions": {k: v.get("score") for k, v in (q.get("dimensions") or {}).items()},
        },
        "severity_counts": report.get("severity_counts"),
        "unique_findings": (report.get("finding_counts") or {}).get("unique_findings"),
        "security": {
            "raw": sec_raw,
            "distinct": distinct,  # distinct (severity, finding type) pairs: 85 unpinned npx calls count once
            "top": [
                {"severity": sv, "title": t, "count": n}
                for (sv, t), n in sorted(sec_types.items(), key=lambda kv: (rank.get(kv[0][0], 9), -kv[1]))[:10]
            ],
        },
        "incomplete_scans": [str(x) for x in report.get("incomplete_scans") or []],
        "scanner_errors": [
            str(e)[:200]
            for r in report.get("results", [])
            for e in (r.get("legacy") or {}).get("errors") or []
            if "unexpected exit code" in str(e)
        ],
        "checks": checks,
        "publication_status": report.get("publication_status"),
    }


def scanner_crash(rep_path: Path) -> str | None:
    """Return the error if a scanner crashed (as opposed to finishing with partial analysis)."""
    if not rep_path.is_file():
        return None
    for r in json.loads(rep_path.read_text()).get("results", []):
        for e in (r.get("legacy") or {}).get("errors") or []:
            if "unexpected exit code" in str(e):
                return str(e)[:200]
    return None


def run_tier1(skill_dir: Path, out: Path, llm: bool) -> tuple[int, float, str]:
    env = git_env()
    if llm:
        env.update({k: v for k, v in os.environ.items() if k in PROVIDER_VARS})
    cmd = [UV, "run", "-q", "--python", "3.13", "--extra", "security"]
    if llm:
        cmd += ["--extra", "llm"]
    cmd += [
        "skillevaluator",
        "tier1",
        str(skill_dir),
        "--llm" if llm else "--no-llm",
        "-r",
        "json",
        "-r",
        "html",
        "-r",
        "markdown",
        "-o",
        str(out),
    ]
    t0 = time.monotonic()
    try:
        p = subprocess.run(cmd, cwd=EVALUATOR_DIR, env=env, capture_output=True, text=True, timeout=TIER1_TIMEOUT)
        code, log = p.returncode, p.stdout + "\n--- stderr ---\n" + p.stderr
    except subprocess.TimeoutExpired as e:
        code, log = -9, f"TIMEOUT after {TIER1_TIMEOUT}s\n{e.stdout or ''}"
    return code, round(time.monotonic() - t0, 1), log


def evaluate(repo: str, skill_path: str, category: str = "", llm: bool = False, force: bool = False) -> dict:
    snap_root = Path(tempfile.mkdtemp(prefix="skill-snap-"))
    try:
        with GIT_LOCK:
            repo_dir, commit = checkout(repo)
            src = (repo_dir / skill_path).resolve()
            if (
                not (str(src) + "/").startswith(str(repo_dir.resolve()) + "/")
                and src != repo_dir.resolve()
                or not (src / "SKILL.md").is_file()
            ):
                raise ValueError(f"{repo}:{skill_path} has no SKILL.md at the current commit")
            # Snapshot only the skill folder (no symlinks, no .git) so the scan sees exactly what is recorded.
            skill_dir = snap_root / src.name
            shutil.copytree(src, skill_dir, symlinks=True, ignore=shutil.ignore_patterns(".git"))
        return _evaluate(repo, skill_path, category, llm, force, skill_dir, commit)
    finally:
        shutil.rmtree(snap_root, ignore_errors=True)


def _evaluate(repo, skill_path, category, llm, force, skill_dir, commit):
    digest = tree_digest(skill_dir)
    ev = evaluator_version()
    mode = "tier1-llm" if llm else "tier1-static"
    if not force:
        for prev in load_results():
            if (
                prev["source"]["repo"],
                prev["source"]["skill_path"],
                prev["source"]["tree_digest"],
                prev["evaluator"]["commit"],
                prev["mode"],
            ) == (repo, skill_path, digest, ev["commit"], mode):
                prev["_cached"] = True
                return prev
    slug = f"{repo.replace('/', '__')}__{skill_path.strip('/').replace('/', '__')}"
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    out = REPORTS / slug / f"{commit[:12]}-{mode}-{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    rep_path = out / "skillevaluator-tier1.json"
    attempts = []
    for attempt in range(2):  # SkillSpector crashes transiently (~1 in 20 runs); retry once
        code, secs, log = run_tier1(skill_dir, out, llm)
        (out / f"run{'' if attempt == 0 else attempt + 1}.log").write_text(log)
        crash = scanner_crash(rep_path)
        attempts.append({"exit_code": code, "duration_s": secs, "scanner_crash": crash})
        if not crash:
            break
    secs = round(sum(a["duration_s"] for a in attempts), 1)
    rec = {
        "id": f"{slug}@{commit[:12]}:{mode}:{stamp}",
        "skill_name": skill_dir.name,
        "category": category,
        "mode": mode,
        "source": {
            "repo": repo,
            "url": f"https://github.com/{repo}",
            "skill_path": skill_path,
            "commit": commit,
            "tree_digest": digest,
            "tree_url": f"https://github.com/{repo}/tree/{commit}/{skill_path}",
        },
        "evaluator": ev,
        "agent": None,
        "model": None,  # no live agent in Tier 1 static mode
        "test_cases": None,
        "baseline": None,
        "started_at": stamp,
        "duration_s": secs,
        "exit_code": code,
        "cost_usd": 0.0 if not llm else None,
        "report_dir": str(out.relative_to(ROOT)),
        "attempts": attempts,
    }
    if rep_path.is_file():
        write_public(out)
        rec["status"] = "completed"
        rec["tier1"] = summarize_tier1(json.loads(rep_path.read_text()))
    else:
        rec["status"] = "failed"
        rec["error"] = log.strip().splitlines()[-1][:300] if log.strip() else "no output"
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    with RESULTS.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


SCANNER_VALIDATORS = {"PII Scan", "Secrets Detection"}
SCANNER_CATEGORIES = {"PII", "SECRET"}
SENSITIVE_TITLE = re.compile(r"secret|credential|token|key|password|exfil|email|phone", re.I)
MATCHED_TEXT_KEYS = {"line_content", "match", "matched_value", "secret", "snippet", "evidence", "context"}
# Inside a PII Scan / Secrets Detection dict, only these string fields are published as they are; any other string
# (a new field a future SkillEvaluator adds, say) is replaced, so the sanitizer fails closed. `message` and the legacy
# lists are cut separately, keeping their titles.
SCANNER_SAFE_KEYS = {
    "status",
    "validator",
    "description",
    "category",
    "severity",
    "confidence",
    "check",
    "check_id",
    "check_name",
    "finding_id",
    "occurrence_id",
    "file",
    "file_path",
    "suggestion",
    "reason",
    "producer",
    "skill_name",
    "skill_digest",
    "skill_digest_algorithm",
    "evidence_sha256",
    "gitleaks",
}
LEGACY_TAG = re.compile(r"^\[([A-Z_]+)-[A-Z]+\]")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
OMITTED = ": [matched text omitted]"


def _cut(message: str) -> str:
    """Keep a scanner message's title ("Non-placeholder email address") and drop what it matched."""
    title = message.split(":")[0]
    return title + (OMITTED if ":" in message else "")


def _scanner_owned(d: dict, inherited: bool) -> bool:
    names = [d.get("validator"), *(d.get("validators") or [])]
    validators = {n for n in names if isinstance(n, str)}
    return (
        inherited
        or bool(validators & SCANNER_VALIDATORS)
        or d.get("category") in SCANNER_CATEGORIES
        or d.get("check_name") in ("emails", "phones")
    )


def sanitize_report(report: dict) -> dict:
    """Public copy of a report: drop matched source text so scanner hits (possible credentials, PII) are never
    re-published. Findings keep file, line number, rule and severity.

    SkillEvaluator copies a finding's message into several places (findings, finding_groups, occurrences, legacy
    errors/warnings/messages), so this redacts by what a value is, at every depth, not by where it sits. As a
    backstop, anything shaped like an email address is replaced wherever it appears. Idempotent."""

    def text(s: str) -> str:
        s = s.replace(str(ROOT), "").replace(str(Path.home()), "~")
        return EMAIL.sub("[email omitted]", s)

    def finding(s: str, owned: bool) -> str:
        """A finding message, bare or in legacy form ("[PII-HIGH] title: matched text in file:line")."""
        tag = LEGACY_TAG.match(s)
        prefix, body = (tag.group(0) + " ", s[tag.end() :].strip()) if tag else ("", s)
        sensitive = owned or (tag and tag.group(1) in SCANNER_CATEGORIES) or SENSITIVE_TITLE.search(body.split(":")[0])
        if sensitive and not body.endswith(OMITTED):
            body = _cut(body)
        return text(prefix + body)

    def walk(o, scanner=False):
        if isinstance(o, dict):
            owned = _scanner_owned(o, scanner)
            out = {}
            for k, v in o.items():
                if k == "message" and isinstance(v, str):
                    out[k] = finding(v, owned)
                elif isinstance(v, str) and v and (k in MATCHED_TEXT_KEYS or (owned and k not in SCANNER_SAFE_KEYS)):
                    out[k] = "[omitted]"
                elif k in ("errors", "warnings", "messages") and isinstance(v, list):
                    out[k] = [finding(x, owned) if isinstance(x, str) else walk(x, owned) for x in v]
                elif owned and isinstance(v, list) and k != "validators":  # unknown string lists fail closed too
                    out[k] = ["[omitted]" if isinstance(x, str) and x else walk(x, owned) for x in v]
                else:
                    out[k] = walk(v, owned)
            return out
        if isinstance(o, list):
            return [walk(x, scanner) for x in o]
        if isinstance(o, str):
            return text(o)
        return o

    return walk(report)


def write_public(out: Path) -> None:
    rp = out / "skillevaluator-tier1.json"
    if rp.is_file():
        (out / "report.public.json").write_text(json.dumps(sanitize_report(json.loads(rp.read_text())), indent=1))


def resanitize_public(root: Path) -> int:
    """Re-run the sanitizer over every published report.public.json under root (it is idempotent), so a deployment
    seeded by an older sanitizer is brought up to date in place."""
    n = 0
    for p in root.glob("*/*/report.public.json"):
        try:
            old = json.loads(p.read_text())
            new = sanitize_report(old)
        except (OSError, ValueError) as e:
            # Unreadable or truncated: it can't be checked, so it isn't served. The server stays up.
            print(f"removing unreadable public report {p}: {type(e).__name__}", file=sys.stderr)
            p.unlink(missing_ok=True)
            continue
        if new != old:
            p.write_text(json.dumps(new, indent=1))
            n += 1
    return n


def resummarize() -> int:
    """Rebuild tier1 summaries in results.jsonl from the stored reports (after summary format changes)."""
    out, n = [], 0
    for rec in load_results():
        rp = ROOT / rec["report_dir"] / "skillevaluator-tier1.json"
        if rp.is_file():
            rec["tier1"] = summarize_tier1(json.loads(rp.read_text()))
            write_public(rp.parent)
            n += 1
        out.append(json.dumps(rec))
    RESULTS.write_text("\n".join(out) + "\n")
    return n


def load_results() -> list[dict]:
    if not RESULTS.exists():
        return []
    return [json.loads(line) for line in RESULTS.read_text().splitlines() if line.strip()]


if __name__ == "__main__":
    if "--resummarize" in sys.argv:
        print(resummarize(), "records updated")
        sys.exit(0)
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    r = evaluate(
        args[0], args[1], args[2] if len(args) > 2 else "", llm="--llm" in sys.argv, force="--force" in sys.argv
    )
    t1 = r.get("tier1") or {}
    print(
        json.dumps(
            {
                "id": r["id"],
                "status": r["status"],
                "secs": r["duration_s"],
                "exit": r["exit_code"],
                "quality": (t1.get("quality") or {}).get("score"),
                "overall": t1.get("overall_status"),
                "cached": r.get("_cached", False),
            }
        )
    )
