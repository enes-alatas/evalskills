"""evalskills model gateway: the only process that holds the model-provider key (ADR-0004).

Trials and the evaluator get a per-run token instead. The gateway checks it, allows only configured models and
endpoints, enforces per-run and per-day caps and a global request rate, then forwards to the upstream with the real
key. It logs counts, never prompts or completions. Standard library only. It never follows an upstream redirect,
and never relays an upstream error body that could quote the key.

It speaks plain HTTP, whatever the port number suggests: it listens on the eval bridge only, never on a public
interface. A run's token cap is checked before each call, and a requested output limit is clamped to what the run
has left. Calls that set no limit, and concurrent calls of one run, can each pass the cap by one response; the
per-run request cap bounds the total.

Run:     python3 -I gateway/gateway.py serve --config /etc/evalskills/gateway.json
Issue:   python3 -I gateway/gateway.py issue --config ... --run RUN_ID --out /path/token   (prints nothing secret)
Usage:   python3 -I gateway/gateway.py usage --config ... [--run RUN_ID]
Revoke:  python3 -I gateway/gateway.py revoke --config ... --run RUN_ID
"""

import argparse
import datetime as dt
import hashlib
import json
import os
import secrets
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

FORWARDED = ("/v1/chat/completions", "/v1/embeddings")  # everything else is refused, except the local model list
MAX_BODY = 4_000_000  # bytes per request body
TOKEN_PREFIX = "egw-"

DEFAULTS = {
    "listen_host": "127.0.0.1",
    "listen_port": 8443,
    "upstream_base_url": "https://integrate.api.nvidia.com/v1",
    "key_file": "/run/secrets/model_key",
    "state_db": "/var/lib/evalskills-gateway/state.db",
    "allowed_models": [],
    "per_run_requests": 400,
    "per_run_tokens": 2_000_000,
    "per_day_requests": 2000,
    "requests_per_minute": 30,
    "max_rate_wait_s": 120,
    "upstream_timeout_s": 300,
    # A chat request without an output limit gets this one (or what the run has left, if less), so every call's
    # spend is bounded before it starts. It stays within what the allowed models accept.
    "max_output_tokens": 4096,
    # Calls whose usage never arrived (no usage chunk, a failed read) can't be counted against per_run_tokens; after
    # this many, the run is refused, so the token cap fails closed (ADR-0004).
    "per_run_unmetered": 3,
    # After a client hangs up mid-stream, reading on to the usage chunk stops after this long overall.
    "drain_timeout_s": 120,
}


class ConfigError(ValueError):
    pass


class NoRedirects(urllib.request.HTTPRedirectHandler):
    """urllib's default handler re-sends our headers, the key included, to any Location, http:// too. Refuse: a 3xx
    comes back as an HTTPError, and its body is never relayed (see forward)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


UPSTREAM = urllib.request.build_opener(NoRedirects)


def load_config(path: str) -> dict:
    """Config is a JSON file; unknown keys are refused so a typo cannot silently drop a cap."""
    raw = json.loads(Path(path).read_text())
    unknown = set(raw) - set(DEFAULTS)
    if unknown:
        raise ConfigError(f"unknown config keys: {', '.join(sorted(unknown))}")
    cfg = {**DEFAULTS, **raw}
    if not cfg["upstream_base_url"].startswith("https://"):
        raise ConfigError("upstream_base_url must be https")
    if not cfg["allowed_models"] or not all(isinstance(m, str) and m for m in cfg["allowed_models"]):
        raise ConfigError("allowed_models must list at least one model ID")
    for k in (
        "per_run_requests",
        "per_run_tokens",
        "per_day_requests",
        "requests_per_minute",
        "max_output_tokens",
        "per_run_unmetered",
        "drain_timeout_s",
    ):
        if not isinstance(cfg[k], int) or cfg[k] <= 0:
            raise ConfigError(f"{k} must be a positive integer")
    cfg["upstream_base_url"] = cfg["upstream_base_url"].rstrip("/")
    return cfg


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def leaks_key(payload: bytes, key: str, window: int = 8) -> bool:
    """True if the payload holds the key or any 8-character run of it (providers quote keys partly, too)."""
    k = key.encode()
    if len(k) <= window:
        return k in payload
    return any(k[i : i + window] in payload for i in range(len(k) - window + 1))


def today() -> str:
    return dt.datetime.now(dt.UTC).date().isoformat()


class State:
    """Runs, tokens (hashed) and counters in SQLite, so caps survive restarts."""

    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.lock = threading.Lock()
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
              run_id TEXT PRIMARY KEY, token_sha256 TEXT UNIQUE NOT NULL, issued_at TEXT NOT NULL,
              revoked INTEGER NOT NULL DEFAULT 0, requests INTEGER NOT NULL DEFAULT 0,
              prompt_tokens INTEGER NOT NULL DEFAULT 0, completion_tokens INTEGER NOT NULL DEFAULT 0,
              unmetered INTEGER NOT NULL DEFAULT 0, refused INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS days (day TEXT PRIMARY KEY, requests INTEGER NOT NULL DEFAULT 0);
            """
        )

    def issue(self, run_id: str) -> str:
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        with self.lock:
            self.db.execute(
                "INSERT INTO runs (run_id, token_sha256, issued_at) VALUES (?, ?, ?)",
                (run_id, token_hash(token), dt.datetime.now(dt.UTC).isoformat(timespec="seconds")),
            )
        return token

    def revoke(self, run_id: str) -> bool:
        with self.lock:
            return self.db.execute("UPDATE runs SET revoked = 1 WHERE run_id = ?", (run_id,)).rowcount == 1

    def run_for(self, token: str):
        digest = token_hash(token)
        with self.lock:
            row = self.db.execute(
                "SELECT run_id, token_sha256, revoked, requests, prompt_tokens + completion_tokens FROM runs "
                "WHERE token_sha256 = ?",
                (digest,),
            ).fetchone()
        if not row:
            return None
        return {"run_id": row[0], "revoked": bool(row[2]), "requests": row[3], "tokens": row[4]}

    def admit(self, run_id: str, cfg: dict) -> str | None:
        """Count one request against the run and the day, or return why it is refused. Atomic under the lock."""
        day = today()
        with self.lock:
            run = self.db.execute(
                "SELECT revoked, requests, prompt_tokens + completion_tokens, unmetered FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            daily = self.db.execute("SELECT requests FROM days WHERE day = ?", (day,)).fetchone()
            why = None
            if run is None or run[0]:
                why = "run revoked"
            elif run[1] >= cfg["per_run_requests"]:
                why = "run request cap reached"
            elif run[2] >= cfg["per_run_tokens"]:
                why = "run token cap reached"
            elif run[3] >= cfg["per_run_unmetered"]:
                why = "run unmetered cap reached"
            elif daily and daily[0] >= cfg["per_day_requests"]:
                why = "daily request cap reached"
            if why:
                self.db.execute("UPDATE runs SET refused = refused + 1 WHERE run_id = ?", (run_id,))
                return why
            self.db.execute("UPDATE runs SET requests = requests + 1 WHERE run_id = ?", (run_id,))
            self.db.execute(
                "INSERT INTO days (day, requests) VALUES (?, 1) ON CONFLICT(day) DO UPDATE SET requests = requests + 1",
                (day,),
            )
        return None

    def meter(self, run_id: str, usage: dict | None) -> None:
        with self.lock:
            if not usage:
                self.db.execute("UPDATE runs SET unmetered = unmetered + 1 WHERE run_id = ?", (run_id,))
                return
            self.db.execute(
                "UPDATE runs SET prompt_tokens = prompt_tokens + ?, completion_tokens = completion_tokens + ? "
                "WHERE run_id = ?",
                (int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0), run_id),
            )

    def usage(self, run_id: str | None = None) -> list[dict]:
        cols = "run_id, issued_at, revoked, requests, prompt_tokens, completion_tokens, unmetered, refused"
        with self.lock:
            q = f"SELECT {cols} FROM runs" + (" WHERE run_id = ?" if run_id else "") + " ORDER BY issued_at"
            rows = self.db.execute(q, (run_id,) if run_id else ()).fetchall()
        return [dict(zip(cols.split(", "), r, strict=True)) for r in rows]


class RateLimiter:
    """A global sliding window: at most N upstream requests in any 60 s. Callers wait, up to a limit."""

    def __init__(self, per_minute: int, clock=time.monotonic, sleep=time.sleep):
        self.per_minute, self.clock, self.sleep = per_minute, clock, sleep
        self.stamps: list[float] = []
        self.lock = threading.Lock()

    def acquire(self, max_wait: float) -> bool:
        deadline = self.clock() + max_wait
        while True:
            with self.lock:
                now = self.clock()
                self.stamps = [t for t in self.stamps if now - t < 60]
                if len(self.stamps) < self.per_minute:
                    self.stamps.append(now)
                    return True
                wait = 60 - (now - self.stamps[0])
            if self.clock() + wait > deadline:
                return False
            self.sleep(min(wait, 1.0))


def log(event: str, **fields) -> None:
    """Counts and identifiers only. Never a body, a header or a token."""
    print(
        json.dumps({"t": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"), "event": event, **fields}),
        file=sys.stderr,
        flush=True,
    )


def make_handler(cfg: dict, state: State, limiter: RateLimiter, key: str):
    allowed = set(cfg["allowed_models"])

    class Handler(BaseHTTPRequestHandler):
        server_version = "evalskills-gateway"
        sys_version = ""
        timeout = 60  # a client that opens a connection and sends nothing cannot hold a thread forever

        def log_message(self, *args):  # the default logs request lines; we log our own counts
            pass

        def reply(self, code: int, obj: dict) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def refuse(self, code: int, why: str, run_id: str | None = None) -> None:
            log("refused", run=run_id, code=code, why=why, path=self.path if self.path in FORWARDED else "other")
            self.reply(code, {"error": {"message": why, "type": "gateway_refused"}})

        def authed_run(self):
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                return None
            return state.run_for(auth[len("Bearer ") :].strip())

        def do_GET(self):
            run = self.authed_run()
            if not run or run["revoked"]:
                return self.refuse(401, "unknown or revoked token")
            if self.path.rstrip("/") != "/v1/models":
                return self.refuse(404, "not served")
            self.reply(200, {"object": "list", "data": [{"id": m, "object": "model"} for m in sorted(allowed)]})

        def do_POST(self):
            run = self.authed_run()
            if not run or run["revoked"]:
                return self.refuse(401, "unknown or revoked token")
            run_id = run["run_id"]
            if self.path not in FORWARDED:
                return self.refuse(404, "endpoint not allowed", run_id)
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self.refuse(400, "bad Content-Length", run_id)
            if length <= 0 or length > MAX_BODY:
                return self.refuse(413, "body too large or empty", run_id)
            try:
                body = json.loads(self.rfile.read(length))
            except (ValueError, UnicodeDecodeError):
                return self.refuse(400, "body is not JSON", run_id)
            if not isinstance(body, dict) or body.get("model") not in allowed:
                return self.refuse(403, "model not allowed", run_id)
            stream = bool(body.get("stream")) and self.path == "/v1/chat/completions"
            if stream:  # ask for the usage chunk, so streamed calls are metered too
                body["stream_options"] = {**(body.get("stream_options") or {}), "include_usage": True}
            if not limiter.acquire(cfg["max_rate_wait_s"]):  # before admit(), so a refused wait costs no request
                return self.refuse(429, "gateway rate limit", run_id)
            why = state.admit(run_id, cfg)
            if why:
                return self.refuse(429, why, run_id)
            left = cfg["per_run_tokens"] - run["tokens"]  # a requested output limit never exceeds what the run has left
            limits = [k for k in ("max_tokens", "max_completion_tokens") if k in body]
            if self.path == "/v1/chat/completions" and not limits:  # no limit asked for: set one
                body["max_tokens"] = min(left, cfg["max_output_tokens"])
                limits = ["max_tokens"]
            for k in limits:
                if not isinstance(body[k], int) or body[k] > left:
                    body[k] = max(min(left, cfg["max_output_tokens"]), 1)
            self.forward(run_id, body, stream)

        def forward(self, run_id: str, body: dict, stream: bool) -> None:
            req = urllib.request.Request(
                cfg["upstream_base_url"] + self.path[len("/v1") :],
                data=json.dumps(body).encode(),
                method="POST",
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream" if stream else "application/json",
                },
            )
            started = time.monotonic()
            try:
                resp = UPSTREAM.open(req, timeout=cfg["upstream_timeout_s"])  # noqa: S310 (https only)
            except urllib.error.HTTPError as e:  # the request was already counted by admit(); no tokens to meter
                log("upstream_error", run=run_id, code=e.code, model=body.get("model"))
                payload = e.read(MAX_BODY)
                if e.code < 400 or e.code in (401, 403) or leaks_key(payload, key):
                    # a redirect, the gateway's own key refused, or a body quoting the key: never relayed
                    return self.reply(
                        502, {"error": {"message": "upstream refused", "type": "gateway_upstream", "code": e.code}}
                    )
                self.send_response(e.code)
                self.send_header("Content-Type", e.headers.get("Content-Type", "application/json"))
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                log("upstream_unreachable", run=run_id, why=type(e).__name__)
                return self.reply(502, {"error": {"message": "upstream unreachable", "type": "gateway_upstream"}})
            # Usage is metered before the client sees the end of the reply, so the run's next request is admitted
            # against counted tokens. A client that hangs up is still metered: see relay_stream.
            seen = {"usage": None, "metered": False}

            def meter():
                if not seen["metered"]:
                    seen["metered"] = True
                    usage = seen["usage"] if isinstance(seen["usage"], dict) else None
                    state.meter(run_id, usage)  # no usage seen: counted as unmetered

            try:
                with resp:
                    self.send_response(resp.status)
                    self.send_header("Content-Type", resp.headers.get("Content-Type", "application/json"))
                    self.relay_stream(resp, seen, meter, run_id) if stream else self.relay_body(resp, seen, meter)
            finally:
                meter()  # the relay failed first: whatever usage it saw still counts
            usage = seen["usage"] if isinstance(seen["usage"], dict) else None
            log(
                "forwarded",
                run=run_id,
                model=body.get("model"),
                path=self.path,
                stream=stream,
                seconds=round(time.monotonic() - started, 2),
                prompt_tokens=(usage or {}).get("prompt_tokens"),
                completion_tokens=(usage or {}).get("completion_tokens"),
            )

        def relay_body(self, resp, seen, meter) -> None:
            payload = resp.read()
            try:
                seen["usage"] = json.loads(payload).get("usage")
            except (ValueError, AttributeError):
                pass
            meter()
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def relay_stream(self, resp, seen, meter, run_id: str) -> None:
            """Pass server-sent events through line by line, keeping the last usage object seen. It is metered just
            before `data: [DONE]` is written, or when upstream ends.

            If the client goes away, even before the headers are written, keep reading upstream with writes off: those
            tokens are billed either way, and the usage chunk comes last. Each call's output is capped (max_tokens is
            always set for chat), and the read after a hang-up stops at drain_timeout_s overall.
            """
            self.send_header("Cache-Control", "no-cache")
            self.close_connection = True  # no Content-Length: the stream ends when we close
            client = True
            hung_up_at = None
            try:
                self.end_headers()  # the first write to the client
            except OSError:
                client, hung_up_at = False, time.monotonic()
            for line in resp:
                if hung_up_at is not None and time.monotonic() - hung_up_at > cfg["drain_timeout_s"]:
                    log("drain_timeout", run=run_id)
                    break
                if line.startswith(b"data: {"):
                    try:
                        seen["usage"] = json.loads(line[6:]).get("usage") or seen["usage"]
                    except (ValueError, AttributeError):
                        pass
                elif line.startswith(b"data: [DONE]"):
                    meter()
                if client:
                    try:
                        self.wfile.write(line)
                        self.wfile.flush()
                    except OSError:  # the client hung up
                        client, hung_up_at = False, time.monotonic()
            meter()

    return Handler


def serve(cfg: dict) -> None:
    key = Path(cfg["key_file"]).read_text().strip()
    if not key:
        raise ConfigError("key_file is empty")
    state = State(cfg["state_db"])
    limiter = RateLimiter(cfg["requests_per_minute"])
    httpd = ThreadingHTTPServer((cfg["listen_host"], cfg["listen_port"]), make_handler(cfg, state, limiter, key))
    log("listening", host=cfg["listen_host"], port=cfg["listen_port"], models=sorted(cfg["allowed_models"]))
    httpd.serve_forever()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="gateway")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("serve", "issue", "usage", "revoke"):
        p = sub.add_parser(name)
        p.add_argument("--config", required=True)
        if name in ("issue", "revoke"):
            p.add_argument("--run", required=True)
        if name == "usage":
            p.add_argument("--run")
        if name == "issue":
            p.add_argument("--out", required=True, help="file the token is written to (mode 0600)")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    if args.cmd == "serve":
        serve(cfg)
        return 0
    state = State(cfg["state_db"])
    if args.cmd == "issue":
        fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # before issuing: no orphan run
        try:
            token = state.issue(args.run)
        except sqlite3.IntegrityError:
            os.close(fd)
            os.unlink(args.out)
            print(f"run {args.run} already has a token; revoke it and use a new run ID", file=sys.stderr)
            return 1
        with os.fdopen(fd, "w") as f:
            f.write(token + "\n")
        print(json.dumps({"run": args.run, "token_file": args.out}))
    elif args.cmd == "revoke":
        print(json.dumps({"run": args.run, "revoked": state.revoke(args.run)}))
    else:
        print(json.dumps(state.usage(args.run), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
