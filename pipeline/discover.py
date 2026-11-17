"""Discover skills (folders containing SKILL.md) in a public GitHub repository."""

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque

from evaluate import (  # noqa: F401
    CACHE,
    MAX_REPO_KB,
    REPO_RE,
    CloneTimeout,
    RepoTooLarge,
    checkout,
    git_env,
    read_head,
)

MAX_SKILLS_LISTED = 1000  # names and paths only; NVIDIA/skills had 450 on 2026-11-10 (#87)

# The repository lookup authenticates with a fine-grained GitHub token that has no repository access, so it reads only
# public data, with its owner account's 5,000 requests an hour instead of 60 per server address (#167; ADR-0002,
# amended 2026-11-17). The host sets it as GITHUB_LOOKUP_TOKEN (ADR-0008). It is taken out of this process's
# environment at import, so no child process can inherit it. Unset or empty, as in development and CI, lookups go out
# unauthenticated.
TOKEN_RE = re.compile(r"[A-Za-z0-9_]{20,255}")
_LOOKUP_TOKEN = os.environ.pop("GITHUB_LOOKUP_TOKEN", "")
# The site's own budget of GitHub calls an hour, well under the owner account's 5,000, so a flood of lookups from many
# clients can't spend the quota that account's other work uses. A cached answer costs nothing.
LOOKUPS_PER_HOUR = int(os.environ.get("LOOKUPS_PER_HOUR", "2000"))
_CALLS: deque[float] = deque()
_CALLS_LOCK = threading.Lock()
# A repository's answer is reused for this long, so a burst of lookups for one repository costs GitHub one call.
REPO_META_TTL_S = float(os.environ.get("REPO_META_TTL_S", "300"))
REPO_META_CACHE_MAX = 512
_META: dict[str, tuple[float, dict]] = {}
_META_LOCK = threading.Lock()

URL_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?"
    r"(?:/tree/[^/]+/(.*))?/?$"
)


class DiscoveryError(Exception):
    pass


def parse_repo_url(url: str) -> tuple[str, str | None]:
    """Return ('owner/repo', optional subpath) or raise."""
    m = URL_RE.match(url.strip())
    if not m:
        raise DiscoveryError("Only public github.com repository URLs are supported, e.g. https://github.com/owner/repo")
    repo = f"{m.group(1)}/{m.group(2)}"
    if not REPO_RE.match(repo):
        raise DiscoveryError("Unrecognised repository name")
    return repo, (m.group(3) or None)


def lookup_token(raw: str | None = None) -> str | None:
    """The lookup token, or None to go unauthenticated. Never logged; a malformed value counts as none, said so."""
    raw = (_LOOKUP_TOKEN if raw is None else raw).strip()
    if not raw:
        return None
    if not TOKEN_RE.fullmatch(raw):
        print("repo lookup: GITHUB_LOOKUP_TOKEN doesn't hold a token; looking up unauthenticated", file=sys.stderr)
        return None
    return raw


def _spend_lookup() -> None:
    """Take one GitHub call from the site's hourly budget, or refuse before calling GitHub."""
    now = time.monotonic()
    with _CALLS_LOCK:
        while _CALLS and now - _CALLS[0] >= 3600:
            _CALLS.popleft()
        if len(_CALLS) >= LOOKUPS_PER_HOUR:
            raise DiscoveryError("Repository lookups are busy right now; please try again later")
        _CALLS.append(now)


def repo_meta(repo: str) -> dict:
    """GitHub's description of a public repository (size, default branch, visibility), cached for REPO_META_TTL_S."""
    key = repo.lower()
    now = time.monotonic()
    with _META_LOCK:
        hit = _META.get(key)
        if hit and now - hit[0] < REPO_META_TTL_S:
            return hit[1]
    meta = _fetch_repo_meta(repo)
    with _META_LOCK:
        if len(_META) >= REPO_META_CACHE_MAX:
            for k, (t, _) in list(_META.items()):
                if now - t >= REPO_META_TTL_S:
                    del _META[k]
            while len(_META) >= REPO_META_CACHE_MAX:
                del _META[next(iter(_META))]  # the oldest insert
        _META[key] = (now, meta)
    return meta


class _SameHostRedirects(urllib.request.HTTPRedirectHandler):
    """A renamed repository answers with a redirect. urllib would resend every header, the token included, wherever
    that points, so only a redirect within https://api.github.com is followed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith("https://api.github.com/"):
            raise DiscoveryError("GitHub answered with an unexpected redirect, so the repository can't be checked")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_SameHostRedirects)


def _fetch_repo_meta(repo: str) -> dict:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "evalskills"}
    _spend_lookup()
    token = lookup_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}", headers=headers)
    try:
        with _OPENER.open(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise DiscoveryError("Repository not found or not public") from e
        if e.code == 401:  # the token is wrong or expired: fail closed, and say so to the operator, not the visitor
            print("repo lookup: GitHub refused the lookup token (401); replace it", file=sys.stderr)
            raise DiscoveryError("Repository lookups are unavailable right now; please try again later") from e
        if e.code in (403, 429):  # without the size we must not clone (fail closed)
            h = e.headers or {}
            # Primary limit: Remaining is 0. Secondary limit: a 403 with Retry-After while Remaining is non-zero.
            if e.code == 429 or h.get("X-RateLimit-Remaining") == "0" or h.get("Retry-After"):
                raise DiscoveryError("GitHub is rate-limiting this server; please try again later") from e
            raise DiscoveryError("GitHub refused to describe this repository, so it can't be checked") from e
        raise


def frontmatter(text: str) -> dict:
    """Minimal YAML frontmatter reader for name/description (handles folded/literal blocks)."""
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
    if not m:
        return {}
    out, key, buf = {}, None, []
    for line in m.group(1).splitlines():
        km = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if km and not line.startswith(" "):
            if key and buf:
                out[key] = " ".join(buf).strip()
            key, val = km.group(1), km.group(2).strip()
            buf = [] if val in (">", "|", ">-", "|-", "") else [val.strip("'\"")]
        elif key and line.startswith(" "):
            buf.append(line.strip())
    if key and buf:
        out[key] = " ".join(buf).strip()
    return out


def discover(url: str) -> dict:
    repo, sub = parse_repo_url(url)
    meta = repo_meta(repo)
    if meta.get("private"):
        raise DiscoveryError("Repository is private")
    if meta.get("size", 0) > MAX_REPO_KB:
        raise DiscoveryError(f"Repository too large ({meta['size'] // 1024} MB > {MAX_REPO_KB // 1024} MB)")
    try:
        d, commit = checkout(repo)
    except subprocess.CalledProcessError as e:
        raise DiscoveryError("Could not clone repository") from e
    except CloneTimeout as e:
        raise DiscoveryError("Cloning the repository took too long; please try again later") from e
    except RepoTooLarge as e:
        raise DiscoveryError(str(e)) from e
    root = d.resolve()
    skills, total = [], 0
    for p in sorted(root.rglob("SKILL.md")):
        if ".git" in p.parts or p.is_symlink():
            continue
        rel = str(p.parent.relative_to(root)) if p.parent != root else "."
        if sub and not (rel == sub.strip("/") or rel.startswith(sub.strip("/") + "/")):
            continue
        total += 1
        if len(skills) >= MAX_SKILLS_LISTED:
            continue  # keep counting, so the form can say how many were left out
        fm = frontmatter(read_head(p))
        skills.append(
            {"path": rel, "name": fm.get("name") or p.parent.name, "description": (fm.get("description") or "")[:300]}
        )
    return {
        "repo": repo,
        "commit": commit,
        "default_branch": meta.get("default_branch"),
        "skills": skills,
        "total": total,
        "truncated": total > len(skills),
    }


if __name__ == "__main__":
    import sys

    print(json.dumps(discover(sys.argv[1]), indent=1)[:3000])
