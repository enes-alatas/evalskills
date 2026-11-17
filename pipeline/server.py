"""evalskills: tiny stdlib HTTP server + SQLite store + single evaluation worker.

Run: python3 -I pipeline/server.py [--port 8080]
"""

import hashlib
import ipaddress
import json
import os
import re
import sqlite3
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import discover as disc  # noqa: E402
import evaluate as ev  # noqa: E402

ROOT = ev.ROOT
DB_PATH = ROOT / "data" / "inventory.db"
STATIC = ROOT / "web"
# Fixed allowlist: font files are served by exact name, never by a path built from the request.
FONTS = {"Geist-Regular.woff2", "Geist-Medium.woff2", "GeistMono-Regular.woff2", "OFL.txt"}
CATEGORIES = json.loads((ROOT / "catalog" / "categories.json").read_text())

# Limits (env-overridable)
MAX_SKILLS_PER_SUBMISSION = int(os.environ.get("MAX_SKILLS_PER_SUBMISSION", "10"))
MAX_QUEUE = int(os.environ.get("MAX_QUEUE", "100"))
MAX_BODY = 20_000  # bytes per POST body; fixed, not read from the environment
SUBMISSIONS_PER_HOUR_PER_CLIENT = int(os.environ.get("SUBMISSIONS_PER_HOUR", "20"))
DISCOVERIES_PER_HOUR_PER_CLIENT = int(os.environ.get("DISCOVERIES_PER_HOUR", "30"))


# The public source link in the header (#77). Only an https://github.com/OWNER/REPO URL is accepted; anything else,
# or unset, hides the link (the repo is private until it is open-sourced).
def source_repo_url(value: str) -> str:
    value = (value or "").strip()
    return value if re.fullmatch(r"https://github\.com/[A-Za-z0-9-]{1,39}/[A-Za-z0-9_.-]{1,100}", value) else ""


SOURCE_REPO_URL = source_repo_url(os.environ.get("SOURCE_REPO_URL", ""))
CACHE_MAX_AGE_HOURS = float(os.environ.get("CACHE_MAX_AGE_HOURS", "6"))
CACHE_MAX_REPOS = int(os.environ.get("CACHE_MAX_REPOS", "40"))  # x 300 MB per clone bounds the cache volume
# Who may set X-Forwarded-For. Loopback only by default. In deploy/compose.yaml the host's Caddy reaches the app
# through a loopback-published port, so compose sets this to the app network's gateway (#69). Unset there, every
# visitor shares one rate-limit bucket.
DEFAULT_TRUSTED_PROXIES = "127.0.0.0/8,::1/128"


def parse_networks(value: str) -> list:
    """Comma-separated CIDRs. A malformed entry raises ValueError, so a placeholder left in .env stops the server."""
    return [ipaddress.ip_network(n.strip()) for n in value.split(",") if n.strip()]


TRUSTED_PROXIES = parse_networks(os.environ.get("TRUSTED_PROXIES", DEFAULT_TRUSTED_PROXIES))
TRUSTED_PROXY_HOPS = int(os.environ.get("TRUSTED_PROXY_HOPS", "1"))  # how many proxies append to the header
GIT_LOCK = ev.GIT_LOCK
WAKE = threading.Event()
# A connection that sends nothing for REQUEST_TIMEOUT_S is closed, and so is one whose request (headers and body)
# isn't in by REQUEST_DEADLINE_S, however it trickles in. At most MAX_CONNECTIONS are served at once; the rest are
# closed on arrival, and at most MAX_CONNECTIONS_PER_CLIENT per client, so one client can't hold them all. Slow clients
# can't hold every thread (#171). Only requests with a body count toward the per-client cap.
REQUEST_TIMEOUT_S = float(os.environ.get("REQUEST_TIMEOUT_S", "30"))
REQUEST_DEADLINE_S = float(os.environ.get("REQUEST_DEADLINE_S", "60"))  # for the whole request, headers and body
MAX_CONNECTIONS = int(os.environ.get("MAX_CONNECTIONS", "64"))
MAX_CONNECTIONS_PER_CLIENT = int(os.environ.get("MAX_CONNECTIONS_PER_CLIENT", "4"))  # by client(), before the body
if min(MAX_CONNECTIONS, MAX_CONNECTIONS_PER_CLIENT) < 1 or min(REQUEST_TIMEOUT_S, REQUEST_DEADLINE_S) <= 0:
    raise SystemExit(
        "MAX_CONNECTIONS(_PER_CLIENT) must be at least 1, REQUEST_TIMEOUT_S and REQUEST_DEADLINE_S above 0"
    )
_CLIENT_LOCK = threading.Lock()
_CLIENT_CONNECTIONS: dict[str, int] = {}
SCANNER_RETRY_S = float(os.environ.get("SCANNER_RETRY_S", "30"))  # pause before handing the scanner a job again (#103)
WAITING_NOTE = "Waiting for the scanner. The scan starts as soon as it is available."


def db():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS skills(
          id TEXT PRIMARY KEY, repo TEXT, path TEXT, name TEXT, description TEXT,
          category TEXT, origin TEXT, added_at REAL);
        CREATE TABLE IF NOT EXISTS evaluations(
          id TEXT PRIMARY KEY, skill_id TEXT, mode TEXT, status TEXT, commit_sha TEXT,
          started_at TEXT, record TEXT);
        CREATE TABLE IF NOT EXISTS jobs(
          id INTEGER PRIMARY KEY AUTOINCREMENT, skill_id TEXT, status TEXT, created_at REAL,
          started_at REAL, finished_at REAL, evaluation_id TEXT, note TEXT, client TEXT);
        CREATE TABLE IF NOT EXISTS events(client TEXT, kind TEXT, at REAL);
        """)
        # A crash mid-run leaves a job "running": requeue it.
        c.execute("UPDATE jobs SET status='queued', started_at=NULL WHERE status='running'")


def inside(repo_dir, path):
    """True if path names a SKILL.md folder inside repo_dir (no escaping the clone)."""
    sd = (repo_dir / path).resolve()
    return (str(sd) + "/").startswith(str(repo_dir) + "/") and (sd / "SKILL.md").is_file()


def current_paths(repo, paths, repo_dir):
    """Listed skills whose content matches a completed result at this evaluator (#83): submitting them would only
    reuse that result, so they don't count toward the submission limit and get no job. If the evaluator version is
    unknown, every path counts as needing a run."""
    with db() as c:
        listed = [p for p in paths if c.execute("SELECT 1 FROM skills WHERE id=?", (skill_id(repo, p),)).fetchone()]
    try:
        with GIT_LOCK:  # no fetch, reset or prune may change the clone mid-hash
            return ev.up_to_date(repo, [p for p in listed if inside(repo_dir, p)], repo_dir)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return set()


def skill_id(repo, path):
    return f"{repo}:{path}"


CURATED_CATEGORIES = "skill-categories.tsv"


def catalog_categories(catalog_dir=None):
    """skill id -> category for catalog skills: the batch TSVs as they ran, then the curated file, which wins."""
    catalog_dir = catalog_dir or ROOT / "catalog"
    files = sorted(f for f in catalog_dir.glob("*.tsv") if f.name != CURATED_CATEGORIES)
    files.append(catalog_dir / CURATED_CATEGORIES)
    meta = {}
    for f in files:
        if not f.exists():
            continue
        for line in f.read_text().splitlines():
            parts = line.split("\t")
            if len(parts) >= 3 and parts[2]:
                meta[skill_id(parts[0], parts[1])] = parts[2]
    return meta


def import_results():
    """Load catalog/results.jsonl (batch runs) into the DB. A catalog skill's category follows the curated one, even
    in a DB that already holds the skill; a submitted skill keeps the category its submitter chose."""
    meta = catalog_categories()
    with db() as c:
        for rec in ev.load_results():
            sid = skill_id(rec["source"]["repo"], rec["source"]["skill_path"])
            add_record(c, rec, meta.get(sid))
            if sid in meta:
                c.execute("UPDATE skills SET category=? WHERE id=? AND origin='catalog'", (meta[sid], sid))


def read_desc(rec):
    try:
        repo_dir = ev.CACHE / rec["source"]["repo"].replace("/", "__")
        p = repo_dir / rec["source"]["skill_path"] / "SKILL.md"
        fm = disc.frontmatter(ev.read_head(p))
        return fm.get("name"), (fm.get("description") or "")[:400]
    except OSError:
        return None, ""


def add_record(c, rec, category=None):
    sid = skill_id(rec["source"]["repo"], rec["source"]["skill_path"])
    if not c.execute("SELECT 1 FROM skills WHERE id=?", (sid,)).fetchone():
        name, desc = read_desc(rec)
        c.execute(
            "INSERT INTO skills VALUES(?,?,?,?,?,?,?,?)",
            (
                sid,
                rec["source"]["repo"],
                rec["source"]["skill_path"],
                name or rec["skill_name"],
                desc,
                category or rec.get("category") or "other",
                "catalog",
                time.time(),
            ),
        )
    c.execute(
        "INSERT OR REPLACE INTO evaluations VALUES(?,?,?,?,?,?,?)",
        (rec["id"], sid, rec["mode"], rec["status"], rec["source"]["commit"], rec["started_at"], json.dumps(rec)),
    )


# ---------------------------------------------------------------- views


def engine_label(evaluator) -> str | None:
    """The exact engine a result came from, short (#105): the scanner image digest, else the source commit."""
    evaluator = evaluator or {}
    image = evaluator.get("image") or ""
    if "@sha256:" in image:
        return f"image {image.split('@sha256:', 1)[1][:8]}…"
    if evaluator.get("commit"):
        return f"commit {str(evaluator['commit'])[:7]}"
    return None


def public_eval(rec):
    """Strip local paths; keep everything a reader needs to interpret the result."""
    if not rec:
        return None
    r = dict(rec)
    r.pop("_cached", None)
    r["report_dir"] = None
    r["engine"] = engine_label(rec.get("evaluator"))
    # Only the sanitized JSON is public; full HTML/Markdown reports may quote matched secrets/PII lines.
    r["reports"] = {
        k: f"/reports/{rec['report_dir'].split('/', 1)[1]}/{fn}"
        for k, fn in (("json", "report.public.json"),)
        if (ROOT / rec["report_dir"] / fn).is_file()
    }
    return r


def mark_collisions(rows):
    """Give rows that share a name and repo a `where` label that tells them apart.

    Each row gets the shortest trailing part of its parent folder that no other row in its group shares, so
    `a/skills/x` and `b/skills/x` become `a/skills` and `b/skills`. A whole parent counts as unique unless another
    row has the same parent (`skills/x` and `a/skills/x` become `skills` and `a/skills`). If two rows share a parent,
    they get their full paths.
    Rows without a collision get no label.
    """
    groups = {}
    for r in rows:
        groups.setdefault((r["name"], r["repo"]), []).append(r)
    for group in groups.values():
        if len(group) < 2:
            continue
        parents = [[p for p in r["path"].split("/") if p not in ("", ".")][:-1] for r in group]
        for i, r in enumerate(group):
            mine, label = parents[i], None
            for k in range(1, len(mine) + 1):
                # At full length the label is the whole parent; it only clashes with an identical parent.
                if all(o[-k:] != mine[-k:] or (k == len(mine) and o != mine) for j, o in enumerate(parents) if j != i):
                    label = "/".join(mine[-k:])
                    break
            r["where"] = label or (r["path"] if r["path"] not in ("", ".") else "repo root")
    return rows


def security_level(t1):
    t1 = t1 or {}
    sec = t1.get("security") or {}
    d = sec.get("distinct") or {}
    sec_checks = [ch for ch in t1.get("checks", []) if ch["name"] in ev.SECURITY_CHECKS]
    return {
        "critical": d.get("critical", 0),
        "high": d.get("high", 0),
        "medium": d.get("medium", 0),
        "low": d.get("low", 0),
        "raw": sec.get("raw"),
        "top": sec.get("top", []),
        "checks": {ch["name"]: ch["status"] for ch in sec_checks},
        "incomplete": sorted({x for ch in sec_checks for x in ch.get("incomplete", [])}),
        "scanner_error": bool(t1.get("scanner_errors")),
    }


def skill_row(c, s, detail=False):
    evs = c.execute("SELECT record FROM evaluations WHERE skill_id=? ORDER BY started_at DESC", (s["id"],)).fetchall()
    evs = [json.loads(e["record"]) for e in evs]
    latest = evs[0] if evs else None
    job = c.execute("SELECT * FROM jobs WHERE skill_id=? ORDER BY id DESC LIMIT 1", (s["id"],)).fetchone()
    t1 = (latest or {}).get("tier1") or {}
    q = t1.get("quality") or {}
    if job and job["status"] in ("queued", "running"):
        state = job["status"]
    elif latest:
        state = latest["status"]  # completed | failed
    elif job:
        state = job["status"]
    else:
        state = "unknown"
    row = {
        "id": s["id"],
        "name": s["name"],
        "description": s["description"],
        "repo": s["repo"],
        "path": s["path"],
        "category": s["category"],
        "origin": s["origin"],
        "state": state,
        "structural": {
            "score": q.get("score"),
            "grade": q.get("grade"),
            "dimensions": q.get("dimensions"),
            "skill_type": q.get("skill_type"),
        }
        if latest and latest["status"] == "completed"
        else None,
        "security": security_level(t1) if latest and latest["status"] == "completed" else None,
        "live": {
            "status": "not_evaluated",
            "reason": "Live (Tier 3) evaluation is not available on this deployment yet.",
        },
        "commit": (latest or {}).get("source", {}).get("commit"),
        "evaluated_at": (latest or {}).get("started_at"),
        "job": dict(job) if job else None,
    }
    if row["job"]:
        row["job"].pop("client", None)
    if detail:
        row["evaluation"] = public_eval(latest)
        row["history"] = [
            {
                "id": e["id"],
                "status": e["status"],
                "commit": e["source"]["commit"],
                "started_at": e["started_at"],
                "score": ((e.get("tier1") or {}).get("quality") or {}).get("score"),
                "engine": engine_label(e.get("evaluator")),
            }
            for e in evs
        ]
    return row


# ---------------------------------------------------------------- worker


def prune_clones():
    """Bound the clone cache, sparing every repo a queued or running job still needs. Takes GIT_LOCK."""
    with db() as c:
        keep = {
            r["repo"]
            for r in c.execute(
                "SELECT DISTINCT s.repo FROM jobs j JOIN skills s ON s.id = j.skill_id "
                "WHERE j.status IN ('queued','running')"
            )
        }
    with GIT_LOCK:
        return ev.prune_cache(CACHE_MAX_AGE_HOURS * 3600, CACHE_MAX_REPOS, keep)


def worker():
    while True:
        WAKE.wait(timeout=5)
        WAKE.clear()
        try:
            prune_clones()
        except Exception:  # noqa: BLE001 -- pruning must never stop the queue
            traceback.print_exc()
        while True:
            with db() as c:
                job = c.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
                if not job:
                    break
                s = c.execute("SELECT * FROM skills WHERE id=?", (job["skill_id"],)).fetchone()

            # The job stays queued until the scan really starts: for the sidecar, when the scanner claims it (#103).
            def started(jid=job["id"]):
                with db() as c:
                    c.execute(
                        "UPDATE jobs SET status='running', started_at=?, note=NULL WHERE id=?", (time.time(), jid)
                    )

            status, eid, note = "failed", None, None
            try:
                rec = ev.evaluate(s["repo"], s["path"], s["category"], on_start=started)
                eid = rec["id"]
                status = rec["status"]
                if rec.get("_cached"):
                    note = "Unchanged since last evaluation (same skill content and evaluator); reused result."
                elif status == "failed":
                    note = rec.get("error")
                with db() as c:
                    add_record(c, rec, s["category"])
            except ev.LowDisk as e:
                # Nothing ran: no result, no score. The job says why, and a later resubmission runs normally (#168).
                note = f"Not evaluated: the server is low on disk space ({e}). Please try again later."
                print(f"evaluation refused, low disk: {e}", file=sys.stderr)
            except ev.ScannerUnavailable:
                # Not a result: the job stays queued and says why, and is offered again after a pause (#65, #103).
                with db() as c:
                    c.execute(
                        "UPDATE jobs SET status='queued', started_at=NULL, note=? WHERE id=?", (WAITING_NOTE, job["id"])
                    )
                time.sleep(SCANNER_RETRY_S)
                break
            except ValueError as e:
                status, note = "unsupported", str(e)
            except Exception as e:  # noqa: BLE001
                note = f"{type(e).__name__}: {e}"[:300]
                traceback.print_exc()
            with db() as c:
                c.execute(
                    "UPDATE jobs SET status=?, finished_at=?, evaluation_id=?, note=? WHERE id=?",
                    (status, time.time(), eid, note, job["id"]),
                )


# ---------------------------------------------------------------- HTTP


def rate_ok(c, client, kind, limit):
    since = time.time() - 3600
    n = c.execute("SELECT COUNT(*) FROM events WHERE client=? AND kind=? AND at>?", (client, kind, since)).fetchone()[0]
    if n >= limit:
        return False
    c.execute("INSERT INTO events VALUES(?,?,?)", (client, kind, time.time()))
    return True


def client_ip(peer: str, forwarded: str | None) -> str:
    """The address rate limits are keyed on. X-Forwarded-For is believed only when the direct peer is a trusted proxy,
    and then only the entry TRUSTED_PROXY_HOPS places from the right: entries further left were written by the
    client, which can put anything there."""
    try:
        trusted = any(ipaddress.ip_address(peer) in net for net in TRUSTED_PROXIES)
    except ValueError:
        trusted = False
    hops = [h.strip() for h in (forwarded or "").split(",") if h.strip()]
    if not trusted or TRUSTED_PROXY_HOPS < 1 or len(hops) < TRUSTED_PROXY_HOPS:
        return peer
    return hops[-TRUSTED_PROXY_HOPS]


class _DeadlineReader:
    """The request stream, with one deadline for all of it. Every read waits at most one socket receive, bounded by
    the per-read timeout and by what is left of the deadline, so a client that drips a byte at a time is cut off when
    the deadline passes, not when it stops sending (#171)."""

    def __init__(self, raw, sock, per_read: float, deadline: float):
        self._raw, self._sock, self._per_read, self._deadline = raw, sock, per_read, deadline

    def _arm(self) -> None:
        left = self._deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("request deadline passed")
        self._sock.settimeout(min(self._per_read, left))

    def read(self, n: int = -1) -> bytes:
        out = bytearray()
        while n < 0 or len(out) < n:
            self._arm()
            chunk = self._raw.read1(65536 if n < 0 else min(65536, n - len(out)))
            if not chunk:
                break
            out += chunk
        return bytes(out)

    def readline(self, limit: int = -1) -> bytes:
        out = bytearray()
        while limit < 0 or len(out) < limit:
            self._arm()
            buf = self._raw.peek(1)  # at most one receive, and only when nothing is buffered
            if not buf:
                break
            room = len(buf) if limit < 0 else min(len(buf), limit - len(out))
            end = buf.find(b"\n", 0, room)
            take = room if end < 0 else end + 1
            out += self._raw.read(take)  # already buffered: no receive
            if end >= 0:
                break
        return bytes(out)

    def close(self) -> None:
        self._raw.close()


class H(BaseHTTPRequestHandler):
    server_version = "evalskills"
    timeout = REQUEST_TIMEOUT_S  # per read and write: a client that sends nothing is dropped (#171)

    def setup(self):
        super().setup()
        self.rfile = _DeadlineReader(
            self.rfile, self.connection, self.timeout, time.monotonic() + REQUEST_DEADLINE_S
        )  # a request that trickles in is cut off at the deadline (#171)

    def log_message(self, fmt, *a):
        sys.stderr.write(f"{self.command} {self.path}\n")

    def parse_request(self):
        """After the headers, before any body: one client sends at most MAX_CONNECTIONS_PER_CLIENT bodies at once
        (#171). Only a request with a body counts. That is where a slow client can hold a connection past its headers
        (Caddy reads those itself), and a visitor's page load is all GETs, so it is never refused."""
        if not super().parse_request():
            return False
        if getattr(self, "_client_slot", None) is not None:  # a kept-alive connection already holds its slot
            return True
        if not self._has_body():
            return True
        key = self.client()
        with _CLIENT_LOCK:
            held = _CLIENT_CONNECTIONS.get(key, 0)
            if held >= MAX_CONNECTIONS_PER_CLIENT:
                over = True
            else:
                over = False
                _CLIENT_CONNECTIONS[key] = held + 1
                self._client_slot = key
        if over:
            self.close_connection = True
            self.send(429, {"error": "too many connections from you at once; try again shortly"})
            return False
        return True

    def _has_body(self) -> bool:
        if self.headers.get("Transfer-Encoding"):
            return True
        try:
            return int(self.headers.get("Content-Length") or 0) != 0
        except ValueError:
            return True  # unreadable: counted, and body() refuses it

    def finish(self):
        try:
            super().finish()
        finally:
            key = getattr(self, "_client_slot", None)
            if key is not None:
                self._client_slot = None
                with _CLIENT_LOCK:
                    left = _CLIENT_CONNECTIONS.get(key, 1) - 1
                    if left > 0:
                        _CLIENT_CONNECTIONS[key] = left
                    else:
                        _CLIENT_CONNECTIONS.pop(key, None)

    def client(self):
        ip = client_ip(self.client_address[0], self.headers.get("X-Forwarded-For"))
        return hashlib.sha256(ip.encode()).hexdigest()[:16]

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.connection.settimeout(self.timeout)  # the request deadline bounds reading, not the answer (#171)
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'")
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        """The request's JSON object. Raises ValueError, before reading anything, unless Content-Length is in
        0..MAX_BODY; raises ValueError for a body that is not a JSON object."""
        n = int(self.headers.get("Content-Length") or 0)
        if not 0 <= n <= MAX_BODY:
            raise ValueError("bad Content-Length")
        b = json.loads(self.rfile.read(n) or b"{}")
        if not isinstance(b, dict):
            raise ValueError("JSON body is not an object")
        return b

    def do_GET(self):
        p = self.path.split("?")[0]
        if p in ("/", "/index.html") or p.startswith("/s/"):
            return self.send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
        if p in ("/app.js", "/app.css"):
            ct = "text/javascript" if p.endswith(".js") else "text/css"
            return self.send(200, (STATIC / p[1:]).read_bytes(), ct + "; charset=utf-8")
        if p.startswith("/fonts/") and p[7:] in FONTS:
            ct = "text/plain; charset=utf-8" if p.endswith(".txt") else "font/woff2"
            return self.send(200, (STATIC / "fonts" / p[7:]).read_bytes(), ct)
        if p.startswith("/docs/") and p.endswith(".md"):
            f = (ROOT / "docs" / p[6:]).resolve()
            if f.parent == (ROOT / "docs").resolve() and f.is_file():
                return self.send(200, f.read_bytes(), "text/plain; charset=utf-8")
        if p.startswith("/reports/"):
            f = (ROOT / p[1:]).resolve()
            if (
                str(f).startswith(str((ROOT / "reports").resolve()) + "/")
                and f.is_file()
                and f.name == "report.public.json"
            ):
                ct = {
                    ".json": "application/json",
                    ".html": "text/html; charset=utf-8",
                    ".md": "text/plain; charset=utf-8",
                }[f.suffix]
                return self.send(200, f.read_bytes(), ct)
        if p == "/api/meta":
            return self.send(
                200,
                {
                    "categories": CATEGORIES,
                    "evaluator": ev.evaluator_version(),
                    "limits": {"skills_per_submission": MAX_SKILLS_PER_SUBMISSION},
                    "source_repo_url": SOURCE_REPO_URL or None,
                },
            )
        if p == "/api/skills":
            with db() as c:
                rows = mark_collisions([skill_row(c, s) for s in c.execute("SELECT * FROM skills")])
            return self.send(200, rows)
        if p.startswith("/api/skills/"):
            sid = p[len("/api/skills/") :]
            from urllib.parse import unquote

            sid = unquote(sid)
            with db() as c:
                s = c.execute("SELECT * FROM skills WHERE id=?", (sid,)).fetchone()
                if not s:
                    return self.send(404, {"error": "not found"})
                return self.send(200, skill_row(c, s, detail=True))
        if p == "/api/queue":
            with db() as c:
                q = [
                    dict(r)
                    for r in c.execute(
                        "SELECT id, skill_id, status, created_at, started_at FROM jobs "
                        "WHERE status IN ('queued','running') ORDER BY id"
                    )
                ]
            return self.send(200, q)
        return self.send(404, {"error": "not found"})

    def do_POST(self):
        # Only JSON, before anything is read: another site can't make a visitor's browser send that type without a
        # CORS preflight, which this server never answers. A form or text/plain POST from elsewhere stops here (#172).
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            self.close_connection = True
            return self.send(415, {"error": "send the request as application/json"})
        try:
            b = self.body()
        except (ValueError, json.JSONDecodeError):
            return self.send(400, {"error": "invalid JSON body"})
        client = self.client()
        if self.path == "/api/discover":
            with db() as c:
                if not rate_ok(c, client, "discover", DISCOVERIES_PER_HOUR_PER_CLIENT):
                    return self.send(429, {"error": "Too many lookups; try again later."})
            try:
                prune_clones()  # make room before this lookup adds a clone
            except Exception:  # noqa: BLE001 -- pruning must never block a lookup
                traceback.print_exc()
            try:
                with GIT_LOCK:
                    res = disc.discover(str(b.get("url", ""))[:300])
            except disc.DiscoveryError as e:
                return self.send(400, {"error": str(e)})
            except Exception:  # noqa: BLE001
                traceback.print_exc()
                return self.send(502, {"error": "Could not read repository"})
            with db() as c:
                for sk in res["skills"]:
                    s = c.execute("SELECT 1 FROM skills WHERE id=?", (skill_id(res["repo"], sk["path"]),)).fetchone()
                    sk["listed"] = bool(s)
            repo_dir = (ev.CACHE / res["repo"].replace("/", "__")).resolve()
            current = current_paths(res["repo"], [sk["path"] for sk in res["skills"] if sk["listed"]], repo_dir)
            for sk in res["skills"]:
                sk["current"] = sk["path"] in current
            return self.send(200, res)
        if self.path == "/api/submit":
            repo, paths = str(b.get("repo", "")), b.get("paths") or []
            category = b.get("category") or "other"
            if category not in [x["id"] for x in CATEGORIES]:
                category = "other"
            if not ev.REPO_RE.match(repo) or not isinstance(paths, list) or not paths:
                return self.send(400, {"error": "Choose at least one skill"})
            paths = list(dict.fromkeys(str(p).strip("/") or "." for p in paths))
            repo_dir = (ev.CACHE / repo.replace("/", "__")).resolve()
            if not repo_dir.is_dir():
                return self.send(400, {"error": "Look up the repository first"})
            if len(paths) > disc.MAX_SKILLS_LISTED:
                return self.send(400, {"error": "Too many skills in one request"})
            # Queue and rate limits come before any hashing, so a client over its limit costs no disk reads.
            with db() as c:
                if (
                    c.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]
                    >= MAX_QUEUE
                ):
                    return self.send(503, {"error": "Evaluation queue is full; try again later."})
                if not rate_ok(c, client, "submit", SUBMISSIONS_PER_HOUR_PER_CLIENT):
                    return self.send(429, {"error": "Too many submissions; try again later."})
            # The limit counts only skills that need a run; up-to-date ones are skipped below (#83).
            current = current_paths(repo, paths, repo_dir)
            if len(paths) - len(current) > MAX_SKILLS_PER_SUBMISSION:
                return self.send(
                    400, {"error": f"At most {MAX_SKILLS_PER_SUBMISSION} new or changed skills per submission"}
                )
            with db() as c:
                out = []
                for path in paths:
                    if not inside(repo_dir, path):
                        out.append({"path": path, "error": "no SKILL.md"})
                        continue
                    sid = skill_id(repo, path)
                    if path in current:
                        out.append({"path": path, "skill_id": sid, "note": "up to date"})
                        continue
                    sd = (repo_dir / path).resolve()
                    if not c.execute("SELECT 1 FROM skills WHERE id=?", (sid,)).fetchone():
                        fm = disc.frontmatter(ev.read_head(sd / "SKILL.md"))
                        c.execute(
                            "INSERT INTO skills VALUES(?,?,?,?,?,?,?,?)",
                            (
                                sid,
                                repo,
                                path,
                                fm.get("name") or sd.name,
                                (fm.get("description") or "")[:400],
                                category,
                                "submission",
                                time.time(),
                            ),
                        )
                    active = c.execute(
                        "SELECT id FROM jobs WHERE skill_id=? AND status IN ('queued','running')", (sid,)
                    ).fetchone()
                    if active:
                        out.append({"path": path, "skill_id": sid, "job": active["id"], "note": "already queued"})
                        continue
                    cur = c.execute(
                        "INSERT INTO jobs(skill_id,status,created_at,client) VALUES(?,?,?,?)",
                        (sid, "queued", time.time(), client),
                    )
                    out.append({"path": path, "skill_id": sid, "job": cur.lastrowid})
            WAKE.set()
            return self.send(202, {"submitted": out})
        return self.send(404, {"error": "not found"})


def bootstrap():
    """Seed results + sanitized public reports from the shipped catalog. A fresh deployment gets a copy; an existing
    one gets the catalog records and reports it doesn't have yet, so a catalog update (a re-scan, say, #110) reaches
    it without touching its own submissions."""
    import shutil
    import tarfile

    if ev.RESULTS != ev.SEED_RESULTS and ev.SEED_RESULTS.exists():
        ev.RESULTS.parent.mkdir(parents=True, exist_ok=True)
        ev.refuse_pilot(  # the shipped catalog never holds a pilot result (ADR-0007); refuse to start with one
            [json.loads(line) for line in ev.SEED_RESULTS.read_text().splitlines() if line.strip()],
            str(ev.SEED_RESULTS),
        )
        if not ev.RESULTS.exists():
            shutil.copy(ev.SEED_RESULTS, ev.RESULTS)
        else:
            seed = [line for line in ev.SEED_RESULTS.read_text().splitlines() if line.strip()]
            seed_ids = {json.loads(line)["id"] for line in seed}
            have, newest = set(), {}
            for r in ev.load_results():
                have.add(r["id"])
                key = (r["source"]["repo"], r["source"]["skill_path"])
                if key not in newest or r["started_at"] > newest[key]["started_at"]:
                    newest[key] = r
            # A catalog record is added only where the catalog owns what the board shows: the skill is new here, or
            # its newest record came from the catalog. It never hides a newer live submission (#111 review).
            new = []
            for line in seed:
                r = json.loads(line)
                prev = newest.get((r["source"]["repo"], r["source"]["skill_path"]))
                if r["id"] not in have and (prev is None or prev["id"] in seed_ids):
                    new.append(line)
            if new:
                with ev.RESULTS.open("a") as f:
                    f.write("".join(line + "\n" for line in new))
    bundle = ROOT / "catalog" / "public-reports.tar.gz"
    if bundle.exists():
        with tarfile.open(bundle) as t:
            members = [
                m
                for m in t.getmembers()
                if m.isfile()
                and m.name.endswith("/report.public.json")
                and not m.name.startswith("/")
                and ".." not in m.name.split("/")
                and not (ev.REPORTS / m.name).exists()
            ]
            try:
                t.extractall(ev.REPORTS, members=members, filter="data")
            except TypeError:  # Python < 3.11.4 has no filter; members are already vetted (regular files, no ..)
                t.extractall(ev.REPORTS, members=members)
    ev.resanitize_public(ev.REPORTS)


class Server(ThreadingHTTPServer):
    """At most `limit` connections at once, each on its own daemon thread. A connection beyond that is closed on
    arrival, unread: a pile of slow clients then costs one refused connection each, not the site (#171)."""

    daemon_threads = True

    def __init__(self, addr, handler, limit: int = MAX_CONNECTIONS):
        if limit < 1:
            raise ValueError("MAX_CONNECTIONS must be at least 1")
        self._slots = threading.BoundedSemaphore(limit)
        super().__init__(addr, handler)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def main():
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8080
    bootstrap()
    init_db()
    import_results()
    # Exchange entries older than two scan caps belong to no live job (#65).
    ev.sweep_exchange(2 * (ev.SCANNER_PICKUP_TIMEOUT + ev.TIER1_TIMEOUT + 60))
    threading.Thread(target=worker, daemon=True).start()
    print(f"listening on :{port}", flush=True)
    Server(("0.0.0.0", port), H).serve_forever()


if __name__ == "__main__":
    main()
