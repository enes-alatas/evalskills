"""evalskills: tiny stdlib HTTP server + SQLite store + single evaluation worker.

Run: python3 -I pipeline/server.py [--port 8080]
"""

import hashlib
import json
import os
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
SUBMISSIONS_PER_HOUR_PER_CLIENT = int(os.environ.get("SUBMISSIONS_PER_HOUR", "20"))
DISCOVERIES_PER_HOUR_PER_CLIENT = int(os.environ.get("DISCOVERIES_PER_HOUR", "30"))

GIT_LOCK = ev.GIT_LOCK
WAKE = threading.Event()


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


def skill_id(repo, path):
    return f"{repo}:{path}"


def import_results():
    """Load catalog/results.jsonl (batch runs) into the DB."""
    meta = {}
    for f in (ROOT / "catalog").glob("*.tsv"):
        for line in f.read_text().splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                meta[skill_id(parts[0], parts[1])] = parts[2]
    with db() as c:
        for rec in ev.load_results():
            add_record(c, rec, meta.get(skill_id(rec["source"]["repo"], rec["source"]["skill_path"])))


def read_desc(rec):
    try:
        repo_dir = ev.CACHE / rec["source"]["repo"].replace("/", "__")
        p = repo_dir / rec["source"]["skill_path"] / "SKILL.md"
        fm = disc.frontmatter(p.read_text(errors="replace")[:20000])
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


def public_eval(rec):
    """Strip local paths; keep everything a reader needs to interpret the result."""
    if not rec:
        return None
    r = dict(rec)
    r.pop("_cached", None)
    r["report_dir"] = None
    # Only the sanitized JSON is public; full HTML/Markdown reports may quote matched secrets/PII lines.
    r["reports"] = {
        k: f"/reports/{rec['report_dir'].split('/', 1)[1]}/{fn}"
        for k, fn in (("json", "report.public.json"),)
        if (ROOT / rec["report_dir"] / fn).is_file()
    }
    return r


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
            }
            for e in evs
        ]
    return row


# ---------------------------------------------------------------- worker


def worker():
    while True:
        WAKE.wait(timeout=5)
        WAKE.clear()
        while True:
            with db() as c:
                job = c.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
                if not job:
                    break
                c.execute("UPDATE jobs SET status='running', started_at=? WHERE id=?", (time.time(), job["id"]))
                s = c.execute("SELECT * FROM skills WHERE id=?", (job["skill_id"],)).fetchone()
            status, eid, note = "failed", None, None
            try:
                rec = ev.evaluate(s["repo"], s["path"], s["category"])
                eid = rec["id"]
                status = rec["status"]
                if rec.get("_cached"):
                    note = "Unchanged since last evaluation (same skill content and evaluator); reused result."
                elif status == "failed":
                    note = rec.get("error")
                with db() as c:
                    add_record(c, rec, s["category"])
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


class H(BaseHTTPRequestHandler):
    server_version = "evalskills"

    def log_message(self, fmt, *a):
        sys.stderr.write(f"{self.command} {self.path}\n")

    def client(self):
        ip = self.headers.get("X-Forwarded-For", self.client_address[0]).split(",")[0].strip()
        return hashlib.sha256(ip.encode()).hexdigest()[:16]

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'")
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 20_000:
            raise ValueError("request too large")
        return json.loads(self.rfile.read(n) or b"{}")

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
                },
            )
        if p == "/api/skills":
            with db() as c:
                rows = [skill_row(c, s) for s in c.execute("SELECT * FROM skills")]
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
            return self.send(200, res)
        if self.path == "/api/submit":
            repo, paths = str(b.get("repo", "")), b.get("paths") or []
            category = b.get("category") or "other"
            if category not in [x["id"] for x in CATEGORIES]:
                category = "other"
            if not ev.REPO_RE.match(repo) or not isinstance(paths, list) or not paths:
                return self.send(400, {"error": "Choose at least one skill"})
            if len(paths) > MAX_SKILLS_PER_SUBMISSION:
                return self.send(400, {"error": f"At most {MAX_SKILLS_PER_SUBMISSION} skills per submission"})
            repo_dir = (ev.CACHE / repo.replace("/", "__")).resolve()
            if not repo_dir.is_dir():
                return self.send(400, {"error": "Look up the repository first"})
            with db() as c:
                if (
                    c.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]
                    >= MAX_QUEUE
                ):
                    return self.send(503, {"error": "Evaluation queue is full; try again later."})
                if not rate_ok(c, client, "submit", SUBMISSIONS_PER_HOUR_PER_CLIENT):
                    return self.send(429, {"error": "Too many submissions; try again later."})
                out = []
                for path in paths:
                    path = str(path).strip("/") or "."
                    sd = (repo_dir / path).resolve()
                    if not (str(sd) + "/").startswith(str(repo_dir) + "/") or not (sd / "SKILL.md").is_file():
                        out.append({"path": path, "error": "no SKILL.md"})
                        continue
                    sid = skill_id(repo, path)
                    if not c.execute("SELECT 1 FROM skills WHERE id=?", (sid,)).fetchone():
                        fm = disc.frontmatter((sd / "SKILL.md").read_text(errors="replace")[:20000])
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
    """First start in a fresh deployment: seed results + sanitized public reports from the shipped catalog."""
    import shutil
    import tarfile

    if ev.RESULTS != ev.SEED_RESULTS and not ev.RESULTS.exists() and ev.SEED_RESULTS.exists():
        ev.RESULTS.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ev.SEED_RESULTS, ev.RESULTS)
    bundle = ROOT / "catalog" / "public-reports.tar.gz"
    if bundle.exists() and not any(ev.REPORTS.glob("*/*/report.public.json")):
        with tarfile.open(bundle) as t:
            members = [
                m
                for m in t.getmembers()
                if m.isfile()
                and m.name.endswith("/report.public.json")
                and not m.name.startswith("/")
                and ".." not in m.name.split("/")
            ]
            try:
                t.extractall(ev.REPORTS, members=members, filter="data")
            except TypeError:  # Python < 3.11.4 has no filter; members are already vetted (regular files, no ..)
                t.extractall(ev.REPORTS, members=members)
    ev.resanitize_public(ev.REPORTS)


def main():
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8080
    bootstrap()
    init_db()
    import_results()
    threading.Thread(target=worker, daemon=True).start()
    print(f"listening on :{port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()


if __name__ == "__main__":
    main()
