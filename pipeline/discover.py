"""Discover skills (folders containing SKILL.md) in a public GitHub repository."""

import json
import re
import subprocess
import urllib.request

from evaluate import CACHE, REPO_RE, checkout, git_env  # noqa: F401

MAX_REPO_KB = 300_000  # refuse repos over ~300 MB
MAX_SKILLS_LISTED = 200

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


def repo_meta(repo: str) -> dict:
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "evalskills"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise DiscoveryError("Repository not found or not public") from e
        if e.code == 403:  # rate limited: fall back to cloning without the size check
            return {}
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
    root = d.resolve()
    skills = []
    for p in sorted(root.rglob("SKILL.md")):
        if ".git" in p.parts or p.is_symlink():
            continue
        rel = str(p.parent.relative_to(root)) if p.parent != root else "."
        if sub and not (rel == sub.strip("/") or rel.startswith(sub.strip("/") + "/")):
            continue
        fm = frontmatter(p.read_text(errors="replace")[:20000])
        skills.append(
            {"path": rel, "name": fm.get("name") or p.parent.name, "description": (fm.get("description") or "")[:300]}
        )
        if len(skills) >= MAX_SKILLS_LISTED:
            break
    return {"repo": repo, "commit": commit, "default_branch": meta.get("default_branch"), "skills": skills}


if __name__ == "__main__":
    import sys

    print(json.dumps(discover(sys.argv[1]), indent=1)[:3000])
