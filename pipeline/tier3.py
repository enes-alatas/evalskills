"""Tier 3 (live) evaluation. Prepared, not yet run: needs a provider key + sandbox.

We copy the skill snapshot and run `skillevaluator tier3 evaluate` with the baseline arm on. By default the skill
runs on SkillEvaluator's own content (ADR-0006): its published evals/, or the starter case SkillEvaluator prepares
when there are none. Those results are per-skill Skill Lift and not comparable across skills.

With TASK_SET, the skill's evals/ are replaced by shared-tasks/<set>/ so every skill in that set faces the same
cases. A set is accepted only if it is vendored from a public benchmark and carries a SOURCE.md saying so.

Usage (once unblocked), as the eval user (ADR-0003, amended 2026-11-13):
  DOCKER_HOST=unix:///run/evalskills/docker.sock TIER3_GATEWAY_URL=... TIER3_GATEWAY_TOKEN_FILE=... \
  python3 -I pipeline/tier3.py OWNER/REPO SKILL_PATH [TASK_SET] [--dry-run]
It refuses to start anywhere but the Docker socket proxy with --env-mode gvisor, while the proxy reports trial
isolation broken, with a real provider key in its environment, or without a gateway token (see trial_env). Each
trial runs in a fresh folder under TIER3_RUN_ROOT (/srv/evals/run), the only place the proxy accepts binds from,
and its report is moved out of there when it ends.
"""

import http.client
import json
import os
import pwd
import re
import resource
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import evaluate as ev  # noqa: E402

SHARED = ev.ROOT / "shared-tasks"
TIER3_TIMEOUT = int(os.environ.get("TIER3_TIMEOUT", "3600"))
AGENT = os.environ.get("TIER3_AGENT", "opencode")  # nv_build default agent
ENV_MODE = os.environ.get("TIER3_ENV_MODE", "gvisor")  # evalskills_gvisor (#137); never "local" for untrusted skills
N_ATTEMPTS = os.environ.get("TIER3_N_ATTEMPTS", "1")
# The agent's model, as the gateway lists it. OpenCode's "openai/" prefix picks its OpenAI-compatible adapter.
AGENT_MODEL = os.environ.get("TIER3_AGENT_MODEL", "")
EVALS_SOCKET = "unix:///run/evalskills/docker.sock"  # the Docker socket proxy (gateway/docker_proxy.py, #137)
RUN_ROOT = Path(os.environ.get("TIER3_RUN_ROOT", "/srv/evals/run"))  # the proxy's bind_root
# SkillEvaluator and Harbor run as this user (#137 condition 5). The pilot itself stays the operator: it holds the
# gateway token file and moves reports out, so the trial user can read neither the token file nor other runs.
TRIAL_USER = os.environ.get("TIER3_TRIAL_USER", "evals")
DAEMON_SOCKETS = (Path("/var/run/docker.sock"), Path("/run/docker.sock"))  # what the trial user must not reach
# Trial images grow /var/lib/docker on /, outside the capped trial disk (#137, step 2): below this, no trial starts.
MIN_FREE_ROOT = int(os.environ.get("TIER3_MIN_FREE_ROOT", str(8 * 1024**3)))
MAX_LOG_BYTES = int(os.environ.get("TIER3_MAX_LOG_BYTES", str(64 * 1024**2)))  # beyond it the trial is stopped
# RLIMIT_FSIZE on the trial: no file it writes, the log included, grows past this, even between two log checks
MAX_FILE_BYTES = int(os.environ.get("TIER3_MAX_FILE_BYTES", str(256 * 1024**2)))
# GvisorEnvironment's message when the proxy refuses a trial's Dockerfile (custom environment or dependencies)
# Where GvisorEnvironment notes it (gvisor_env); outside results/ and TMPDIR, the only folders a container is bound to.
# Not the log: a skill can make the agent print anything.
UNSUPPORTED_FILE_ENV = "EVALSKILLS_UNSUPPORTED_FILE"
REAL_KEYS = ("ANTHROPIC_API_KEY", "NVIDIA_API_KEY", "OPENAI_API_KEY", "SKILL_EVAL_LLM_API_KEY")


class Unsafe(SystemExit):
    pass


class _Unix(http.client.HTTPConnection):
    def __init__(self, path: str):
        super().__init__("localhost", timeout=15)
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


def isolation_ok(sock: str = EVALS_SOCKET) -> tuple[bool, str]:
    """Ask the proxy whether trial isolation holds (its networks' flags, the services' ip_forward and networks)."""
    conn = _Unix(sock.removeprefix("unix://"))
    try:
        conn.request("GET", "/_evalskills/isolation", headers={"Host": "docker"})
        resp = conn.getresponse()
        doc = json.loads(resp.read(65536) or b"{}")
    except (OSError, ValueError) as e:
        return False, f"the proxy did not answer ({type(e).__name__})"
    finally:
        conn.close()
    return resp.status == 200 and doc.get("ok") is True, str(doc.get("why", resp.status))


def trial_user(name: str | None = None) -> pwd.struct_passwd:
    """Who runs the trial. As root we switch to it; as anyone else we must already be it."""
    name = name or TRIAL_USER
    try:
        user = pwd.getpwnam(name)
    except KeyError as e:
        raise Unsafe(f"refusing: no trial user {name} (TIER3_TRIAL_USER)") from e
    if user.pw_uid == 0:
        raise Unsafe("refusing: the trial user may not be root")
    if os.geteuid() not in (0, user.pw_uid):
        raise Unsafe(f"refusing: run as root or as {name}")
    for sock in DAEMON_SOCKETS:
        if _can_connect(sock, user):
            raise Unsafe(f"refusing: trial user {name} can open {sock}; its only Docker access must be the proxy")
    return user


_CONNECT = """import socket, sys
s = socket.socket(socket.AF_UNIX)
s.settimeout(5)
try:
    s.connect(sys.argv[1])
except PermissionError:
    sys.exit(3)
except OSError:
    pass  # refused, reset, ...: past the permission check, so it can open it
"""


def _can_connect(path: Path, user: pwd.struct_passwd) -> bool:
    """Whether the user can connect to a Unix socket, tried for real as that user, so ACLs, folder permissions and
    groups all count. It connects and sends nothing. Fails closed: only a PermissionError means it can't; a stopped
    daemon (ECONNREFUSED), a timeout or a child that didn't start all count as "can"."""
    if not os.path.lexists(path):
        return False
    if os.geteuid() != 0:  # trial_user() made sure we are the user
        try:
            with socket.socket(socket.AF_UNIX) as s:
                s.settimeout(5)
                s.connect(str(path))
        except PermissionError:
            return False
        except OSError:
            pass
        return True
    try:
        p = subprocess.run(
            [sys.executable, "-I", "-c", _CONNECT, str(path)], capture_output=True, timeout=15, **_as_user(user)
        )
    except subprocess.TimeoutExpired:
        return True  # it didn't refuse: assume the worst
    return p.returncode != 3


def check_run_root(root: Path, uid: int) -> None:
    """The proxy takes binds only from folders the eval user owns, with no symlink on the way (#137 condition 4)."""
    if root.is_symlink() or not root.is_dir():
        raise Unsafe(f"refusing: {root} must be a real folder (TIER3_RUN_ROOT)")
    if root.stat().st_uid != uid:
        raise Unsafe(f"refusing: {root} must belong to the trial user")


def check_free_root(path: Path = Path("/"), minimum: int | None = None) -> None:
    """Refuse a trial while the disk that holds Docker's images has less than TIER3_MIN_FREE_ROOT free."""
    minimum = MIN_FREE_ROOT if minimum is None else minimum
    free = shutil.disk_usage(path).free
    if free < minimum:
        raise Unsafe(
            f"refusing: {free // 1024**2} MiB free on {path}, below TIER3_MIN_FREE_ROOT ({minimum // 1024**2} MiB)"
        )


def _hand_over(work: Path, user: pwd.struct_passwd) -> None:
    """Give the trial folder to the trial user; links are changed, never followed."""
    if os.geteuid() != 0:
        return
    for p in [work, *work.rglob("*")]:
        os.lchown(p, user.pw_uid, user.pw_gid)


XDG_DIRS = {"XDG_CONFIG_HOME": "config", "XDG_DATA_HOME": "data", "XDG_STATE_HOME": "state"}


def _trial_env_for(env: dict, user: pwd.struct_passwd, scratch: Path, xdg: Path) -> dict:
    """TMPDIR and the XDG config, data and state folders inside the trial folder, so nothing a trial writes there
    outlives it (#186: SkillEvaluator keeps a key in XDG_STATE_HOME, Harbor its telemetry id in XDG_CONFIG_HOME).
    HOME stays the trial user's root-owned home: its ~/.docker/config.json holds the egress proxy."""
    out = {k: v for k, v in env.items() if k != "XDG_CACHE_HOME"} if os.geteuid() == 0 else dict(env)
    out["TMPDIR"] = str(scratch)
    out.update({k: str(xdg / d) for k, d in XDG_DIRS.items()})
    if os.geteuid() == 0:
        out["HOME"] = user.pw_dir
    return out


def _limit_files() -> None:
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_FILE_BYTES, MAX_FILE_BYTES))


def _as_user(user: pwd.struct_passwd) -> dict:
    if os.geteuid() != 0:
        return {}
    groups = os.getgrouplist(user.pw_name, user.pw_gid)  # the proxy socket's group comes from here
    return {"user": user.pw_uid, "group": user.pw_gid, "extra_groups": groups}


def trial_env(environ=os.environ, isolation=isolation_ok) -> dict:
    """The environment for the SkillEvaluator → agent chain, or Unsafe before anything runs or spends.

    ADR-0003 (amended 2026-11-14): trials run only under gVisor, through the socket proxy, on isolated networks.
    ADR-0004: this chain never holds a real provider key; it gets the model gateway's URL and a per-run token (read
    from a file the gateway issues), as SkillEvaluator's openai-compatible provider."""
    if ENV_MODE != "gvisor":
        raise Unsafe(f"refusing --env-mode {ENV_MODE}: only gvisor through the socket proxy is approved (ADR-0003)")
    if environ.get("DOCKER_HOST") != EVALS_SOCKET:
        raise Unsafe(f"refusing: DOCKER_HOST must be {EVALS_SOCKET}, the socket proxy, not a Docker daemon (ADR-0003)")
    held = [k for k in REAL_KEYS if environ.get(k)]
    if held:
        raise Unsafe(f"refusing: {', '.join(held)} is set; real keys live only in the gateway (ADR-0004)")
    url, token_file = environ.get("TIER3_GATEWAY_URL"), environ.get("TIER3_GATEWAY_TOKEN_FILE")
    if not url or not token_file:
        raise Unsafe("refusing: set TIER3_GATEWAY_URL and TIER3_GATEWAY_TOKEN_FILE (a per-run gateway token)")
    try:
        token = Path(token_file).read_text().strip()
    except OSError as e:
        raise Unsafe(f"refusing: cannot read TIER3_GATEWAY_TOKEN_FILE ({e.strerror})") from e
    if not token:
        raise Unsafe("refusing: TIER3_GATEWAY_TOKEN_FILE is empty")
    ok, why = isolation()
    if not ok:
        raise Unsafe(f"refusing: trial isolation does not hold ({why})")
    env = ev.git_env()
    env.update(
        {
            "DOCKER_HOST": EVALS_SOCKET,
            "EVALSKILLS_DOCKER_SOCKET": EVALS_SOCKET.removeprefix("unix://"),  # what Harbor gets (gvisor_env SPEC)
            "SKILL_EVAL_LLM_PROVIDER": "openai-compatible",
            "SKILL_EVAL_LLM_BASE_URL": url,
            "SKILL_EVAL_LLM_API_KEY": token,
        }
    )
    if environ.get("SKILL_EVAL_LLM_MODEL"):
        env["SKILL_EVAL_LLM_MODEL"] = environ["SKILL_EVAL_LLM_MODEL"]
    return env


# What a skill may not bring to its own live run (ADR-0006): anything that grades it, configures grading, or shapes
# its trial environment. Its cases (evals.json/jsonl/yaml) and their input files stay; SkillEvaluator's default
# judge scores them, and the result is labelled self-reported.
OWN_EVALS_STRIPPED = ("grader.py", "grader.sh", "tests", "harbor", "config.yml", "config.yaml", "environment")
CASES_OWN = "self-reported"  # the skill's own cases, default judge
CASES_SHARED = "shared"  # a vendored public set
CASES_STARTER = "starter"  # no evals/: SkillEvaluator prepares its starter case


def cases_label(skill: Path, task_set: str | None) -> str:
    if task_set:
        return CASES_SHARED
    evals = skill / "evals"
    return CASES_OWN if evals.is_dir() and any(evals.iterdir()) else CASES_STARTER


def strip_own_grading(evals: Path) -> list[str]:
    """Remove a skill's own graders, grading config and trial environment from its evals/. Returns what went."""
    gone = []
    for name in OWN_EVALS_STRIPPED:
        p = evals / name
        if p.is_symlink() or p.is_file():
            p.unlink()
        elif p.is_dir():
            shutil.rmtree(p)
        else:
            continue
        gone.append(name)
    return gone


def prepare(skill_dir: Path, task_set: str | None, dest: Path) -> Path:
    """Copy the skill to dest. Without a task set it stays as published; with one, the set replaces its evals/."""
    if task_set:
        src = SHARED / task_set
        if not (src / "evals.json").is_file():
            raise ValueError(f"no shared task set {task_set}")
        if not (src / "SOURCE.md").is_file():
            raise ValueError(f"task set {task_set} has no SOURCE.md naming its public benchmark (ADR-0006)")
    shutil.copytree(skill_dir, dest, symlinks=True, ignore=shutil.ignore_patterns(".git"))
    if not task_set:
        evals = dest / "evals"
        if evals.is_symlink():
            evals.unlink()  # a linked evals/ could point anywhere; the skill then runs on SkillEvaluator's starter case
        elif evals.is_dir():
            strip_own_grading(evals)
        return dest
    shutil.rmtree(dest / "evals", ignore_errors=True)  # a shared set replaces the skill's own cases
    (dest / "evals").mkdir()
    data = json.loads((src / "evals.json").read_text())
    data["skill_name"] = dest.name
    for e in data["evals"]:
        if "expected_skill" not in e:
            e["expected_skill"] = dest.name
    (dest / "evals" / "evals.json").write_text(json.dumps(data, indent=1))
    for name in ("grader.py", "config.yml"):
        if (src / name).is_file():
            shutil.copy2(src / name, dest / "evals" / name)
    if (src / "files").is_dir():
        shutil.copytree(src / "files", dest / "evals" / "files")
    return dest


def evaluator_version(evaluator_dir: Path | None = None) -> dict:
    """The version and commit of the SkillEvaluator the trials run, from its synced venv and its checkout. Not
    `uv run` (evaluate._local_evaluator_version): that would sync the venv without the tier3 extra, and the host
    has no uv on root's PATH (#200). Raises if the evaluator isn't there, so a caller can stop before any trial."""
    d = evaluator_dir or ev.EVALUATOR_DIR
    out = subprocess.run(
        [str(d / ".venv" / "bin" / "skillevaluator"), "--version"],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    ).stdout.split()
    commit = subprocess.run(
        ["git", "-C", str(d), "rev-parse", "HEAD"], capture_output=True, text=True, check=True, timeout=60
    ).stdout.strip()
    if not out or not commit:
        raise RuntimeError(f"no SkillEvaluator version or commit at {d}")
    return {"version": out[-1], "commit": commit}


def command(skill_dir: Path, out: Path, repo: str, commit: str) -> list[str]:
    """SkillEvaluator from its synced venv, with evalskills_gvisor installed there once (deploy/gvisor/README.md).
    Not `uv run`: that would sync, so the trial user would write to the evaluator's environment."""
    return [
        str(ev.EVALUATOR_DIR / ".venv" / "bin" / "skillevaluator"),
        "tier3",
        "evaluate",
        str(skill_dir),
        "--agents",
        AGENT,
        "--env-mode",
        ENV_MODE,
        "--n-attempts",
        N_ATTEMPTS,
        "--evaluated-source-repository",
        repo,
        "--evaluated-source-revision",
        commit,
        "--results-dir",
        str(out),
    ] + (["--agent-model", f"{AGENT}=openai/{AGENT_MODEL}"] if AGENT_MODEL else [])


def _trial_pids(uid: int, proc: Path = Path("/proc")) -> list[int]:
    """Every process whose real, effective or saved uid is the trial user's."""
    pids = []
    for d in proc.iterdir():
        if not d.name.isdigit():
            continue
        try:
            line = next(ln for ln in (d / "status").read_text().splitlines() if ln.startswith("Uid:"))
        except (OSError, StopIteration):
            continue  # gone already
        if str(uid) in line.split()[1:4]:
            pids.append(int(d.name))
    return pids


def stop_trial(p: subprocess.Popen, uid: int) -> None:
    """Nothing of the trial may run while root reads its folder: its session first, then anything of the trial
    user's that left it (a trial user runs one trial at a time). Unsafe, and the folder stays, if any survives."""
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    p.wait()
    if os.geteuid() != 0:
        return
    for _ in range(50):
        left = _trial_pids(uid)
        if not left:
            return
        for pid in left:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        time.sleep(0.1)
    raise Unsafe(f"refusing to go on: processes of uid {uid} survived SIGKILL; the trial folder is left in place")


def remove_trial_objects(sock: str = EVALS_SOCKET) -> list[str]:
    """Remove every container and network the proxy shows us (only ours carry its owner label). Harbor does this
    when a trial ends normally; after a timeout or a kill nothing else would. Returns what could not be removed."""
    failed = []

    def call(method, url):
        conn = _Unix(sock.removeprefix("unix://"))
        try:
            conn.request(method, url, headers={"Host": "docker"})
            resp = conn.getresponse()
            return resp.status, resp.read(4 * 1024 * 1024)
        finally:
            conn.close()

    for kind, listing, remove in (
        ("container", "/containers/json?all=1", "/containers/{}?force=1&v=1"),
        ("network", "/networks", "/networks/{}"),
    ):
        try:
            status, body = call("GET", listing)
            items = json.loads(body) if status == 200 else None
        except (OSError, ValueError):
            items = None
        if not isinstance(items, list):
            failed.append(f"{kind} list")
            continue
        for item in items:
            oid = str(item.get("Id", ""))
            if kind == "network" and item.get("Name") in ("bridge", "host", "none"):
                continue
            try:
                ok = call("DELETE", remove.format(oid))[0] in (200, 204, 404)
            except OSError:
                ok = False
            if not ok:
                failed.append(f"{kind} {oid[:12]}")
    return failed


MAX_REPORT_BYTES = int(os.environ.get("TIER3_MAX_REPORT_BYTES", str(1024**3)))
_KINDS = {
    stat.S_IFLNK: "link",
    stat.S_IFIFO: "fifo",
    stat.S_IFSOCK: "socket",
    stat.S_IFCHR: "device",
    stat.S_IFBLK: "device",
}


def copy_report(src: Path, dst: Path, max_bytes: int = MAX_REPORT_BYTES) -> list[dict]:
    """Copy the trial's results into a fresh folder root owns: folders and the bytes of regular files only, as
    0755/0644, never following a link and never opening anything that would block. Everything else is skipped and
    returned (#143 review: the trial user could write src, so nothing in it is trusted)."""
    dst.mkdir(mode=0o755)
    skipped, total = [], 0
    for top, dirs, files in os.walk(src):  # never descends through a link
        rel = Path(top).relative_to(src)
        for name in [*dirs, *files]:
            path = Path(top) / name
            st = os.lstat(path)
            if stat.S_ISDIR(st.st_mode):
                (dst / rel / name).mkdir(mode=0o755)
                continue
            if name in dirs:
                dirs.remove(name)
            if not stat.S_ISREG(st.st_mode):
                skipped.append({"path": str(rel / name), "why": _KINDS.get(stat.S_IFMT(st.st_mode), "special")})
                continue
            if total + st.st_size > max_bytes:
                skipped.append({"path": str(rel / name), "why": "over the report size cap"})
                continue
            total += st.st_size  # nothing of the trial runs any more, so sizes don't change
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as fin:
                if not stat.S_ISREG(os.fstat(fd).st_mode):  # swapped since lstat
                    skipped.append({"path": str(rel / name), "why": "changed while copying"})
                    continue
                out = os.open(dst / rel / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
                with os.fdopen(out, "wb") as fout:
                    shutil.copyfileobj(fin, fout)
    return skipped


# Progress (#239): SkillEvaluator's own output is passed to the operator's terminal as it arrives, as well as kept in
# run.log. It can carry trial-controlled text, so each line is cleaned before it is shown. TIER3_PROGRESS=0 turns
# it off.
PROGRESS = os.environ.get("TIER3_PROGRESS", "1") != "0"
PROGRESS_WIDTH = 200  # characters shown per line
PROGRESS_LINES_PER_PUMP = 50  # lines shown per second at most; the rest are counted, and stay in run.log
PROGRESS_MAX_PARTIAL = 4096  # bytes of an unfinished line held before it is shown anyway
PROGRESS_BYTES_PER_PUMP = 1024**2  # read per second at most, so the time and log caps are still checked
_ESCAPE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[PX^_][^\x1b]*(?:\x1b\\)?|[ -/]*[0-~])"
)
# C0 and C1 controls, and the Unicode format characters that reorder or hide text (bidi overrides, zero-width).
_CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f-\x9f\u061c\u200b-\u200f\u2028-\u202e\u2060-\u2069\ufeff]")


def progress_text(raw: bytes) -> str:
    """One line of trial output, safe for a terminal: escapes and controls removed, cut to PROGRESS_WIDTH."""
    text = _ESCAPE.sub("", raw.decode(errors="replace"))
    shown = [part for part in text.split("\r") if part.strip()]  # a \r redraw: keep what is finally on screen
    return _CONTROL.sub("", (shown[-1] if shown else "").replace("\t", " ")).strip()[:PROGRESS_WIDTH]


class _Progress:
    """Reads the trial's log from where it last stopped and passes whole lines on. Reads with pread, so the log's
    own file offset, which the trial writes at, is never moved."""

    def __init__(self, fd: int, label: str, out=None):
        self.fd, self.label, self.out = fd, label, out or sys.stderr
        self.offset, self.partial = 0, b""

    def pump(self, final: bool = False) -> None:
        lines, budget = [], PROGRESS_BYTES_PER_PUMP
        while budget > 0 and self.offset < MAX_LOG_BYTES:
            chunk = os.pread(self.fd, min(65536, budget, MAX_LOG_BYTES - self.offset), self.offset)
            if not chunk:
                break
            self.offset += len(chunk)
            budget -= len(chunk)
            *done, self.partial = (self.partial + chunk).split(b"\n")
            lines += done
            if len(self.partial) > PROGRESS_MAX_PARTIAL:  # a line with no end: show what there is
                lines.append(self.partial)
                self.partial = b""
        if final and self.partial:
            lines.append(self.partial)
            self.partial = b""
        shown = [text for text in map(progress_text, lines) if text]
        for text in shown[:PROGRESS_LINES_PER_PUMP]:
            print(f"[{self.label}] {text}", file=self.out, flush=True)
        if len(shown) > PROGRESS_LINES_PER_PUMP:
            n = len(shown) - PROGRESS_LINES_PER_PUMP
            print(f"[{self.label}] … {n} more line(s) not shown, kept in run.log", file=self.out, flush=True)


def progress_for(log_f, label: str, out=None) -> "_Progress | None":
    return _Progress(log_f.fileno(), label, out) if PROGRESS else None


def _pump(progress: "_Progress | None", final: bool = False) -> "_Progress | None":
    """Progress is a convenience, and it never changes an outcome or skips containment: if showing it fails (a closed
    pipe, a lost terminal), it stops for the rest of the run, and the run goes on. → the progress to use next time."""
    if progress is None:
        return None
    try:
        progress.pump(final)
        return progress
    except Exception as e:  # noqa: BLE001 - whatever the display hit, the trial is still waited for and stopped
        try:
            print(f"progress display stopped: {type(e).__name__}", file=sys.stderr, flush=True)
        except Exception:  # noqa: BLE001, S110 - stderr itself may be what failed
            pass
        return None


def _wait(p: subprocess.Popen, log_f, started: float, progress=None) -> tuple[int | None, str | None]:
    """Wait for the trial within its time and log caps. → (exit code, or None when stopped; why it was stopped)."""
    while True:
        progress = _pump(progress)
        try:
            code = p.wait(timeout=1)
            _pump(progress, final=True)
            return code, None
        except subprocess.TimeoutExpired:
            pass
        if time.monotonic() - started > TIER3_TIMEOUT:
            return None, f"timed out after {TIER3_TIMEOUT} s"
        if os.fstat(log_f.fileno()).st_size > MAX_LOG_BYTES:
            return None, f"stopped: its output passed {MAX_LOG_BYTES} bytes"


def run(
    repo: str,
    skill_path: str,
    task_set: str | None,
    env: dict,
    ref: str | None = None,
    run_root: Path = RUN_ROOT,
    cleanup=remove_trial_objects,
    label: str | None = None,
    progress_out=None,
) -> dict:
    """One live evaluation. Returns its exit code, cases, report dir, commit and tree digest; never a score.

    The skill copy, SkillEvaluator's results (Harbor's trial folders are binds) and TMPDIR all live in one fresh
    folder under run_root, which the trial user can write. When the run ends, every process of the trial user is
    stopped, its containers and networks are removed, root copies the report out (copy_report) and the folder goes,
    so a later trial can't read an earlier one's files (#137 condition 5). The log never touches that folder."""
    user = trial_user()
    check_run_root(run_root, user.pw_uid)
    check_free_root()
    with ev.GIT_LOCK:
        repo_dir, commit = ev.checkout(repo, ref)
    work = Path(tempfile.mkdtemp(prefix="t3-", dir=run_root))  # 0700
    keep = False
    try:
        skill = prepare(repo_dir / skill_path, task_set, work / "skill" / Path(skill_path).name)
        cases = cases_label(skill, task_set)
        out = (
            ev.REPORTS
            / f"{repo.replace('/', '__')}__{skill_path.replace('/', '__')}"
            / f"{commit[:12]}-tier3-{task_set or 'own'}-{time.strftime('%Y%m%dT%H%M%S')}"
        )
        results, scratch, marks, xdg = work / "results", work / "tmp", work / "marks", work / "xdg"
        for d in (results, scratch, marks, *(xdg / n for n in XDG_DIRS.values())):
            d.mkdir(parents=True)
        _hand_over(work, user)
        cmd = command(skill, results, repo, commit)
        trial_env_ = {**_trial_env_for(env, user, scratch, xdg), UNSUPPORTED_FILE_ENV: str(marks / "unsupported")}
        started = time.monotonic()
        with tempfile.TemporaryFile() as log_f:  # root's, outside the trial folder
            p = subprocess.Popen(
                cmd,
                cwd=ev.EVALUATOR_DIR,
                env=trial_env_,
                stdin=subprocess.DEVNULL,
                stdout=log_f,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                preexec_fn=_limit_files,  # the pilot is single-threaded
                **_as_user(user),
            )
            code, why = _wait(p, log_f, started, progress_for(log_f, label or Path(skill_path).name, progress_out))
            duration = round(time.monotonic() - started, 1)
            keep = True  # until stop_trial returns, nothing of the trial is touched
            stop_trial(p, user.pw_uid)
            keep = False
            left = cleanup()
            log_f.seek(0)
            log = log_f.read(MAX_LOG_BYTES).decode(errors="replace")
        if why:
            log = f"{why}\n{log}"
        if left:
            log += f"\n--- cleanup ---\nnot removed: {', '.join(left)}"
        try:
            unsupported = stat.S_ISREG(os.lstat(marks / "unsupported").st_mode)
        except FileNotFoundError:
            unsupported = False
        out.parent.mkdir(parents=True, exist_ok=True)
        skipped = copy_report(results, out)
        (out / "run.log").write_text(log)
        (out / "cases.json").write_text(json.dumps({"cases": cases, "task_set": task_set}))
        (out / "skipped.json").write_text(json.dumps(skipped, indent=1))
        return {
            "exit": code,
            "stopped": why,
            "unsupported": unsupported,
            "cases": cases,
            "out": out,
            "commit": commit,
            "tree_digest": ev.tree_digest(repo_dir / skill_path),
            "duration_s": duration,
        }
    finally:
        if not keep:
            shutil.rmtree(work, ignore_errors=True)  # fd-based on Linux: never follows a link


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    repo, skill_path = args[0], args[1]
    task_set = args[2] if len(args) > 2 else None
    if "--dry-run" in sys.argv:
        with ev.GIT_LOCK:
            repo_dir, commit = ev.checkout(repo)
        tmp = Path(tempfile.mkdtemp(prefix="t3-"))
        try:
            skill = prepare(repo_dir / skill_path, task_set, tmp / Path(skill_path).name)
            evals = sorted(p.name for p in (skill / "evals").iterdir()) if (skill / "evals").is_dir() else []
            cmd = command(skill, tmp / "out", repo, commit)
            print(json.dumps({"cmd": cmd, "cases": cases_label(skill, task_set), "evals": evals}, indent=1))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return
    res = run(repo, skill_path, task_set, trial_env())  # trial_env refuses unsafe setups before anything runs
    print(json.dumps({**res, "out": str(res["out"])}))


if __name__ == "__main__":
    main()
