"""Run SkillEvaluator on one public skill and record a provenance-complete result.

Usage: python3 -I pipeline/evaluate.py OWNER/REPO SKILL_PATH [CATEGORY] [--tiers 1]

Every record stores: source repo, skill path, exact commit, evaluator version+commit,
tiers run, agent/model (if any), scores, per-check statuses, duration, exit code,
and the path to the full underlying reports.
"""

import datetime as dt
import errno
import functools
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# A SkillEvaluator checkout, for local runs (README, Setup). The default is the README's own path; the app container
# doesn't need one (its scans go to the scanner sidecar), and the pilot sets /opt/skillevaluator (#177).
EVALUATOR_DIR = Path(os.environ.get("SKILLEVALUATOR_DIR") or Path.home() / "vendor" / "SkillEvaluator")


class EvaluatorMissing(FileNotFoundError):
    pass


def require_evaluator() -> Path:
    """EVALUATOR_DIR, or a clear error that names the variable to set, before anything runs in it."""
    if not (EVALUATOR_DIR / "pyproject.toml").is_file():
        raise EvaluatorMissing(
            f"no SkillEvaluator checkout at {EVALUATOR_DIR}: set SKILLEVALUATOR_DIR to one (README, Setup)"
        )
    return EVALUATOR_DIR


CACHE = ROOT / "cache" / "repos"
REPORTS = ROOT / "reports"
SEED_RESULTS = ROOT / "catalog" / "results.jsonl"
RESULTS = Path(os.environ.get("RESULTS_PATH", str(SEED_RESULTS)))
UV = shutil.which("uv") or str(Path.home() / ".local/bin/uv")
TIER1_TIMEOUT = int(os.environ.get("TIER1_TIMEOUT", "900"))
# Deployed (#65): the app runs no evaluator. It hands each skill to the scanner sidecar through two volumes
# (deploy/scanner/scanner.py describes the protocol). Unset, as on a dev machine, Tier 1 runs as a local subprocess.
SCANNER_IN = os.environ.get("SCANNER_IN")
SCANNER_OUT = os.environ.get("SCANNER_OUT")
SCANNER_IMAGE = os.environ.get("SCANNER_IMAGE")
SCANNER_PICKUP_TIMEOUT = float(os.environ.get("SCANNER_PICKUP_TIMEOUT", "120"))
SCANNER_POLL_S = float(os.environ.get("SCANNER_POLL_S", "1"))


# Stored evaluation data stays bounded whatever visitors submit (#168).
REPORTS_KEPT_PER_SKILL = int(os.environ.get("REPORTS_KEPT_PER_SKILL", "3"))
if REPORTS_KEPT_PER_SKILL < 1:  # 0 would read as "keep none", and the newest record's report must stay
    raise ValueError("REPORTS_KEPT_PER_SKILL must be 1 or more")
MIN_FREE_DATA_BYTES = int(os.environ.get("MIN_FREE_DATA_BYTES", str(5 * 1024**3)))


class ScannerUnavailable(RuntimeError):
    """No scanner claimed the job in time. The worker puts the job back in the queue; it is never a failed score."""


class LowDisk(RuntimeError):
    """Too little free space to clone and scan. Nothing ran, so there is no result and no score (#168)."""


def check_free_data(path: Path | None = None) -> None:
    """Refuse an evaluation while the volume holding the reports has less than MIN_FREE_DATA_BYTES free."""
    path = path or REPORTS
    while not path.exists():
        path = path.parent
    free = shutil.disk_usage(path).free
    if free < MIN_FREE_DATA_BYTES:
        raise LowDisk(f"{free // 1024**2} MB free, below the {MIN_FREE_DATA_BYTES // 1024**2} MB minimum")


def prune_reports(repo: str, skill_path: str, slug: str, results: list[dict] | None = None) -> list[str]:
    """Remove the report folders of this skill's older evaluations (#168). Kept: the newest REPORTS_KEPT_PER_SKILL
    evaluations (the site shows the newest), and the newest completed one of each mode: that is the result reusable()
    hands back when unchanged content is resubmitted, and the last good report behind a run of failures. Result
    records stay; a removed folder's report links are simply no longer offered. Returns the folders removed."""
    mine = [
        r
        for r in (load_results() if results is None else results)
        if (r.get("source") or {}).get("repo") == repo
        and (r.get("source") or {}).get("skill_path") == skill_path
        and r.get("report_dir")
    ]
    mine.sort(key=lambda r: r.get("started_at") or "")
    keep = {r["report_dir"] for r in mine[-REPORTS_KEPT_PER_SKILL:]}
    newest = {}
    for r in mine:
        if r.get("status") == "completed":
            newest[r.get("mode")] = r["report_dir"]
    keep |= set(newest.values())
    removed = []
    base = REPORTS / slug
    # Only folders an older record names: a folder with no record yet may be a scan still running.
    for rel in sorted({r["report_dir"] for r in mine} - keep):
        d = ROOT / rel
        if d.parent == base and d.is_dir() and not d.is_symlink() and not base.is_symlink():
            shutil.rmtree(d)
            removed.append(rel)
    return removed


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
    "UV_FROZEN",
    "UV_NO_SYNC",
    "UV_PYTHON_PREFERENCE",
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
# GitHub's own shapes: an owner starts with a letter or digit (letters, digits, hyphens; at most 39); a repo name is
# letters, digits, "_", "." and "-", at most 100, never "." or "..", and never starting with "-". \Z, not $, so a
# trailing newline is refused too.
REPO_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9-]{0,38}/(?!\.\.?\Z)(?!-)[A-Za-z0-9_.-]{1,100}\Z")


def sh(cmd, **kw):
    kw.setdefault("env", git_env())  # a child gets the allowlisted env only, never a token (ADR-0008)
    try:
        # Its own session: a Ctrl-C meant for the live pilot stops it between runs, not a git call inside one (#259).
        kw.setdefault("start_new_session", True)
        return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw).stdout.strip()
    except subprocess.CalledProcessError as e:
        # The traceback alone hides why a tool failed; log the tail of its stderr (our own tools, never a skill's).
        print(f"command failed ({e.returncode}): {cmd[:4]} stderr: {(e.stderr or '')[-2000:]}", file=sys.stderr)
        raise


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


CLONE_TIMEOUT_S = 300
CLONE_POLL_S = 1.0  # how often a running clone or fetch is measured against MAX_REPO_KB


class CloneTimeout(RuntimeError):
    pass


def _bounded_git(cmd: list[str], d: Path, env: dict) -> None:
    """Run a git clone, fetch or reset that writes into d, killed as soon as d passes MAX_REPO_KB or CLONE_TIMEOUT_S
    passes (#169). git runs in its own process group, so its helpers (git-remote-https, index-pack) die with it."""
    deadline = time.monotonic() + CLONE_TIMEOUT_S
    with tempfile.TemporaryFile() as err:
        p = subprocess.Popen(
            cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err, start_new_session=True
        )
        try:
            while True:
                try:
                    code = p.wait(timeout=CLONE_POLL_S)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if d.exists() and (kb := dir_kb(d)) > MAX_REPO_KB:
                    raise RepoTooLarge(
                        f"Repository too large (over {MAX_REPO_KB // 1024} MB, stopped at {kb // 1024} MB)"
                    )
                if time.monotonic() > deadline:
                    raise CloneTimeout(f"the clone took longer than {CLONE_TIMEOUT_S} s and was stopped")
        except BaseException:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            p.wait()
            raise
        if code:
            err.seek(0)
            tail = err.read()[-2000:].decode("utf-8", "replace")
            print(f"command failed ({code}): {cmd[:4]} stderr: {tail}", file=sys.stderr)
            raise subprocess.CalledProcessError(code, cmd, stderr=tail)


def checkout(repo: str, ref: str | None = None) -> tuple[Path, str]:
    """Shallow-clone (or refresh) repo at ref, or at its default branch; return (dir, exact commit sha). A clone,
    fetch or reset that fails, times out or grows past MAX_REPO_KB is stopped, and its folder deleted, so the next
    lookup starts clean (#169). A checkout over the cap is refused whatever the GitHub API said about its size."""
    if not REPO_RE.match(repo):
        raise ValueError(f"bad repo slug: {repo}")
    if ref is not None and (not ref or ref.startswith("-")):
        raise ValueError("bad ref: it must be non-empty and not start with '-'")  # never read as a git option (#220)
    d = CACHE / repo.replace("/", "__")
    env = git_env()
    try:
        cloned = not d.exists()
        if cloned:
            d.parent.mkdir(parents=True, exist_ok=True)
            _bounded_git(
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
                d,
                env,
            )
        if not cloned or ref is not None:  # a fresh clone is at the default branch; a ref is fetched onto it (#222)
            _bounded_git(
                ["git", "-C", str(d), "fetch", "-q", "--depth", "1", "--end-of-options", "origin", ref or "HEAD"],
                d,
                env,
            )
            # The fetch brings only the pack; reset writes the working tree, so it is watched too.
            _bounded_git(["git", "-C", str(d), "reset", "-q", "--hard", "FETCH_HEAD"], d, env)
    except BaseException:
        shutil.rmtree(d, ignore_errors=True)
        raise
    kb = dir_kb(d)
    if kb > MAX_REPO_KB:
        shutil.rmtree(d, ignore_errors=True)
        raise RepoTooLarge(f"Repository too large ({kb // 1024} MB > {MAX_REPO_KB // 1024} MB)")
    os.utime(d)  # last use, for prune_cache()
    return d, sh(["git", "-C", str(d), "rev-parse", "HEAD"])


def prune_cache(max_age_s: float, max_repos: int, keep: set[str]) -> list[str]:
    """Delete clones not used for max_age_s, then the least recently used ones beyond max_repos. Clones named in keep
    (repo slugs a queued or running job needs) are never deleted. Call under GIT_LOCK. Returns the slugs removed."""
    if not CACHE.is_dir():
        return []
    keep_dirs = {r.replace("/", "__") for r in keep}
    clones = sorted(
        (d for d in CACHE.iterdir() if d.is_dir() and not d.is_symlink() and d.name not in keep_dirs),
        key=lambda d: d.stat().st_mtime,
        reverse=True,  # newest first
    )
    now = time.time()
    room = max(0, max_repos - len(keep_dirs))
    removed = []
    for i, d in enumerate(clones):
        if now - d.stat().st_mtime > max_age_s or i >= room:
            shutil.rmtree(d, ignore_errors=True)
            removed.append(d.name.replace("__", "/", 1))
    return removed


def evaluator_version() -> dict:
    """The evaluator a run uses. With the sidecar, it is the scanner image (by digest) and the version it reports."""
    if not SCANNER_IN:
        return _local_evaluator_version()
    try:
        ident = json.loads((Path(SCANNER_OUT) / ".identity.json").read_text())
    except (OSError, ValueError):
        ident = {}
    return {"version": ident.get("version"), "commit": None, "image": SCANNER_IMAGE or ident.get("image")}


def evaluator_key(ev: dict) -> str | None:
    """What makes two runs' evaluators the same: the image digest when there is one, else the source commit."""
    return ev.get("image") or ev.get("commit")


@functools.lru_cache(maxsize=1)
def _local_evaluator_version() -> dict:
    require_evaluator()
    ver = sh(
        [UV, "run", "-q", "--python", "3.13", "skillevaluator", "--version"], cwd=EVALUATOR_DIR, env=git_env()
    ).split()[-1]
    commit = sh(["git", "-C", str(EVALUATOR_DIR), "rev-parse", "HEAD"])
    return {"version": ver, "commit": commit}


HEAD_BYTES = 20_000  # enough for any SKILL.md frontmatter we show


def read_head(path: Path, limit: int = HEAD_BYTES) -> str:
    """At most `limit` bytes of a file in a cloned repository, decoded with replacement (#170). The file is never read
    whole, so a huge SKILL.md can't exhaust memory. A link or anything but a regular file (a FIFO would block, a
    device never ends) reads as "": its frontmatter is simply absent."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as e:
        if e.errno == errno.ELOOP:  # the last part of the path is a link
            return ""
        raise
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        return ""
    with os.fdopen(fd, "rb") as f:
        return f.read(limit).decode(errors="replace")


def tree_digest(skill_dir: Path) -> str:
    """Content hash of the skill folder, so unchanged skills are not re-run. Each file is hashed in chunks (#170)."""
    h = hashlib.sha256()
    for p in sorted(skill_dir.rglob("*")):
        if p.is_file() and not p.is_symlink() and ".git" not in p.parts:
            h.update(str(p.relative_to(skill_dir)).encode())
            with p.open("rb") as f:
                h.update(hashlib.file_digest(f, "sha256").digest())
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


def skillspector_deadlines(timeout: int) -> dict[str, str]:
    """SkillSpector's own deadlines, set inside the scan cap (#112). When they expire, only the Security Scan is
    incomplete and the rest of the report survives; the cap would lose the whole run. Upstream's defaults (1080 s and
    1200 s) are above our cap. The scanner sidecar has its own copy; a test keeps the two in step."""
    return {
        "SKILLSPECTOR_MAX_WORKFLOW_SECONDS": str(int(timeout * 0.7)),
        "SKILLEVALUATOR_SKILLSPECTOR_TIMEOUT_SECONDS": str(int(timeout * 0.8)),
    }


def run_tier1(skill_dir: Path, out: Path, llm: bool, on_start=None) -> tuple[int, float, str]:
    """Run Tier 1 on skill_dir. on_start() is called once the scan really begins: at once for a local run, and when
    the scanner claims the job for the sidecar (#103), so a job waiting for the scanner is never shown as running."""
    if SCANNER_IN and not llm:
        return run_tier1_sidecar(skill_dir, out, on_start)
    require_evaluator()
    if on_start:
        on_start()
    env = git_env() | skillspector_deadlines(TIER1_TIMEOUT)
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


# The report files the app uses, read by name; nothing else the scanner writes is copied (#65).
REPORT_FILES = ("skillevaluator-tier1.json", "skillevaluator-tier1.html", "skillevaluator-tier1.md")
MAX_REPORT_BYTES = 64 * 1024 * 1024
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def open_dir(name: str | Path, dir_fd: int | None = None) -> int | None:
    """A directory fd that is never reached through a link, or None."""
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
    except OSError:
        return None


def read_regular(dir_fd: int, name: str, limit: int | None = None) -> bytes | None:
    """The bytes of dir_fd/name if it is a regular file (checked on the open fd, so a link or a swap can't redirect
    it) of at most limit bytes, else None."""
    limit = MAX_REPORT_BYTES if limit is None else limit
    try:
        fd = os.open(name, _FILE_FLAGS, dir_fd=dir_fd)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
            return None
        with os.fdopen(fd, "rb", closefd=False) as f:
            data = f.read(limit + 1)
        return data if len(data) <= limit else None
    finally:
        os.close(fd)


def copy_report(job_fd: int, out: Path) -> None:
    """Copy the scanner's report files into out. Every path component is opened relative to the job's directory fd
    with O_NOFOLLOW, so nothing in the scanner's outbox can make the app read one of its own files."""
    rep = open_dir("report", dir_fd=job_fd)
    if rep is None:
        return
    try:
        for name in REPORT_FILES:
            data = read_regular(rep, name)
            if data is not None:
                (out / name).write_bytes(data)
    finally:
        os.close(rep)


def read_job(outbox: Path, job: str, out: Path, waited: float) -> tuple[int, float, str]:
    """The finished job's exit code, duration and log, with its report files copied into out."""
    box = open_dir(outbox)
    job_fd = open_dir(job, dir_fd=box) if box is not None else None
    try:
        raw = read_regular(job_fd, "result.json", 1024 * 1024) if job_fd is not None else None
        try:
            result = json.loads(raw) if raw else None
            code, secs, log = int(result["exit_code"]), float(result["duration_s"]), str(result.get("log", ""))
        except (TypeError, KeyError, ValueError, AttributeError):
            return -1, waited, "scanner error: no readable result"
        copy_report(job_fd, out)
        return code, secs, log
    finally:
        for fd in (job_fd, box):
            if fd is not None:
                os.close(fd)


def sweep_exchange(max_age_s: float) -> list[str]:
    """Remove inbox and outbox entries older than max_age_s: jobs left behind by a scan that timed out or an app
    restart mid-scan. The scanner's .identity.json is kept."""
    removed, now = [], time.time()
    for root in (SCANNER_IN, SCANNER_OUT):
        if not root or not Path(root).is_dir():
            continue
        for entry in Path(root).iterdir():
            if entry.name == ".identity.json":
                continue
            try:
                if now - entry.lstat().st_mtime < max_age_s:
                    continue
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
                removed.append(entry.name)
            except OSError:
                continue
    return removed


def run_tier1_sidecar(skill_dir: Path, out: Path, on_start=None) -> tuple[int, float, str]:
    """Hand skill_dir to the scanner sidecar and copy its report files into out. Raises ScannerUnavailable if no
    scanner claims the job within SCANNER_PICKUP_TIMEOUT; a claimed job that never finishes is a timeout (-9)."""
    inbox, outbox = Path(SCANNER_IN), Path(SCANNER_OUT)
    job = f"{dt.datetime.now(dt.UTC).strftime('%Y%m%dT%H%M%S')}-{os.urandom(6).hex()}"
    staging = inbox / f".{job}"
    shutil.copytree(skill_dir, staging / "skill" / skill_dir.name, symlinks=True)
    (staging / "job.json").write_text(json.dumps({"name": skill_dir.name}))
    os.rename(staging, inbox / job)  # the scanner sees the job only once it is complete
    claimed, done = outbox / job, outbox / job / "done"
    t0 = time.monotonic()
    started = False
    try:
        while not done.exists():
            waited = time.monotonic() - t0
            if not started and claimed.exists():
                started = True
                if on_start:
                    on_start()
            if not started and waited > SCANNER_PICKUP_TIMEOUT:
                raise ScannerUnavailable("No scanner picked up the job; it stays queued.")
            if waited > SCANNER_PICKUP_TIMEOUT + TIER1_TIMEOUT + 60:
                return -9, round(waited, 1), "TIMEOUT: scan timed out"
            time.sleep(SCANNER_POLL_S)
        if not started and on_start:
            on_start()  # claimed and finished between two polls
        return read_job(outbox, job, out, round(time.monotonic() - t0, 1))
    finally:
        shutil.rmtree(inbox / job, ignore_errors=True)
        if done.exists() or not claimed.exists():
            shutil.rmtree(claimed, ignore_errors=True)


def evaluate(
    repo: str, skill_path: str, category: str = "", llm: bool = False, force: bool = False, on_start=None
) -> dict:
    check_free_data()  # before the clone, the snapshot and the scan: each of them writes to disk
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
        return _evaluate(repo, skill_path, category, llm, force, skill_dir, commit, on_start)
    finally:
        shutil.rmtree(snap_root, ignore_errors=True)


def reusable(repo: str, skill_path: str, digest: str, mode: str, results: list[dict] | None = None) -> dict | None:
    """The completed result a run of this exact content would reuse: same repo, path, tree digest, evaluator commit
    and mode. A failure is retried on resubmission, never reused."""
    key = (repo, skill_path, digest, evaluator_key(evaluator_version()), mode)
    for prev in load_results() if results is None else results:
        if (
            prev.get("status") == "completed"
            and (
                prev["source"]["repo"],
                prev["source"]["skill_path"],
                prev["source"]["tree_digest"],
                evaluator_key(prev["evaluator"]),
                prev["mode"],
            )
            == key
        ):
            return prev
    return None


def up_to_date(repo: str, paths: list[str], repo_dir: Path, mode: str = "tier1-static") -> set[str]:
    """Paths under repo_dir whose current content a submission would only reuse (#83). Callers vet the paths."""
    results = load_results()
    return {p for p in paths if reusable(repo, p, tree_digest(repo_dir / p), mode, results)}


def _evaluate(repo, skill_path, category, llm, force, skill_dir, commit, on_start=None):
    digest = tree_digest(skill_dir)
    ev = evaluator_version()
    mode = "tier1-llm" if llm else "tier1-static"
    if not force:
        prev = reusable(repo, skill_path, digest, mode)
        if prev:
            prev["_cached"] = True
            return prev
    slug = f"{repo.replace('/', '__')}__{skill_path.strip('/').replace('/', '__')}"
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    out = REPORTS / slug / f"{commit[:12]}-{mode}-{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    rep_path = out / "skillevaluator-tier1.json"
    attempts = []
    for attempt in range(2):  # SkillSpector crashes transiently (~1 in 20 runs); retry once
        code, secs, log = run_tier1(skill_dir, out, llm, on_start if attempt == 0 else None)
        (out / f"run{'' if attempt == 0 else attempt + 1}.log").write_text(log)
        crash = scanner_crash(rep_path)
        attempts.append({"exit_code": code, "duration_s": secs, "scanner_crash": crash})
        if not crash:
            break
    secs = round(sum(a["duration_s"] for a in attempts), 1)
    if SCANNER_IN:
        ev = evaluator_version()  # the scanner has now written its identity
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
    try:
        prune_reports(repo, skill_path, slug)
    except Exception as e:  # noqa: BLE001 - pruning never changes an outcome or stops the queue
        print(f"prune_reports {slug}: {type(e).__name__}: {e}", file=sys.stderr)
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


def _scanner_categories(d: dict) -> set[str]:
    """The PII/SECRET categories a result carries in its findings or its legacy strings, whatever its validator is
    called. A result that carries one is scanner-owned even under a name SCANNER_VALIDATORS doesn't know (#243)."""
    found = {f.get("category") for f in d.get("findings") or [] if isinstance(f, dict)}
    legacy = d.get("legacy") if isinstance(d.get("legacy"), dict) else {}
    for key in ("errors", "warnings", "messages"):
        for s in legacy.get(key) or []:
            tag = LEGACY_TAG.match(s) if isinstance(s, str) else None
            if tag:
                found.add(tag.group(1))
    return found & SCANNER_CATEGORIES


def unknown_scanner_validators(report: dict) -> set[str]:
    """Validators that report PII or secret findings under a name the sanitizer doesn't list. The sanitizer still
    redacts them (fail closed), but a repin that renames a validator should be noticed, not absorbed silently."""
    return {
        str(r.get("validator"))
        for r in report.get("results") or []
        if isinstance(r, dict) and _scanner_categories(r) and r.get("validator") not in SCANNER_VALIDATORS
    }


def _scanner_owned(d: dict, inherited: bool) -> bool:
    names = [d.get("validator"), *(d.get("validators") or [])]
    validators = {n for n in names if isinstance(n, str)}
    return (
        inherited
        or bool(validators & SCANNER_VALIDATORS)
        or d.get("category") in SCANNER_CATEGORIES
        or d.get("check_name") in ("emails", "phones")
        or bool(_scanner_categories(d))
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


class PilotRecord(ValueError):
    pass


def is_pilot(rec: dict) -> bool:
    """A live (Tier 3) record. The trial key's pilot results never reach the public build or the board (ADR-0007)."""
    return rec.get("mode") == "tier3" or bool(rec.get("pilot"))


def refuse_pilot(records: list[dict], where: str) -> None:
    bad = [r.get("id", "?") for r in records if is_pilot(r)]
    if bad:
        raise PilotRecord(f"{where} holds {len(bad)} live pilot record(s), e.g. {bad[0]} (ADR-0007)")


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
