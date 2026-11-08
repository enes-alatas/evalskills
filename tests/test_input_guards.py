"""The guards that keep untrusted input contained (#33). Fixture data and synthetic repos only: no network, no
SkillEvaluator. Each test fails if its guard is removed."""

import http.client
import io
import json
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

import discover as disc  # noqa: E402
import evaluate as ev  # noqa: E402
import server  # noqa: E402


def call(addr, method, path, body=None, headers=None):
    c = http.client.HTTPConnection(*addr, timeout=5)
    c.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers or {})
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, data


@pytest.fixture
def root(tmp_path, monkeypatch):
    """A fake ROOT: reports/, docs/ and a cache with one synthetic clone holding one skill."""
    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.setattr(ev, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(server.WAKE, "set", lambda: None)  # no worker in tests
    rep = tmp_path / "reports" / "o__r__s" / "run"
    rep.mkdir(parents=True)
    (rep / "report.public.json").write_text('{"ok": true}')
    (rep / "skillevaluator-tier1.json").write_text('{"raw": true}')
    (tmp_path / "secret").mkdir()
    (tmp_path / "secret" / "report.public.json").write_text('{"outside": true}')
    (tmp_path / "docs" / "sub").mkdir(parents=True)
    (tmp_path / "docs" / "METHODOLOGY.md").write_text("# methodology")
    (tmp_path / "docs" / "sub" / "deep.md").write_text("# deep")
    (tmp_path / "README.md").write_text("# readme")
    clone = tmp_path / "cache" / "example__repo"
    (clone / "skills" / "demo").mkdir(parents=True)
    (clone / "skills" / "demo" / "SKILL.md").write_text("---\nname: demo\ndescription: fixture\n---\n")
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "SKILL.md").write_text("---\nname: outside\n---\n")
    return tmp_path


# ---------------------------------------------------------------- /api/submit paths


@pytest.mark.parametrize("path", ["../../elsewhere", "skills/../../../elsewhere", "/etc", "../../.."])
def test_submit_refuses_a_path_outside_the_clone(app, root, path):
    code, data = call(app, "POST", "/api/submit", {"repo": "example/repo", "paths": [path]})
    assert code == 202
    (res,) = json.loads(data)["submitted"]
    assert res.get("error") == "no SKILL.md" and "skill_id" not in res


def test_submit_accepts_a_skill_inside_the_clone(app, root):
    code, data = call(app, "POST", "/api/submit", {"repo": "example/repo", "paths": ["skills/demo"]})
    assert code == 202 and json.loads(data)["submitted"][0]["skill_id"]


def test_submit_refuses_a_bad_repo_slug(app, root):
    code, data = call(app, "POST", "/api/submit", {"repo": "example/repo/x", "paths": ["skills/demo"]})
    assert code == 400 and json.loads(data)["error"] == "Choose at least one skill"


# ---------------------------------------------------------------- static files


def test_reports_serves_a_public_report(app, root):
    code, data = call(app, "GET", "/reports/o__r__s/run/report.public.json")
    assert code == 200 and json.loads(data) == {"ok": True}


@pytest.mark.parametrize(
    "path",
    [
        "/reports/o__r__s/run/skillevaluator-tier1.json",  # the raw report
        "/reports/../secret/report.public.json",  # outside reports/
        "/reports/%2e%2e/secret/report.public.json",
    ],
)
def test_reports_serves_nothing_else(app, root, path):
    assert call(app, "GET", path)[0] == 404


def test_docs_serves_a_top_level_markdown_file(app, root):
    assert call(app, "GET", "/docs/METHODOLOGY.md") == (200, b"# methodology")


@pytest.mark.parametrize("path", ["/docs/sub/deep.md", "/docs/../README.md", "/docs/METHODOLOGY.txt"])
def test_docs_serves_nothing_else(app, root, path):
    assert call(app, "GET", path)[0] == 404


# ---------------------------------------------------------------- slugs and URLs


@pytest.mark.parametrize("slug", ["a/b/c", "a", "a/b;rm -rf", "a/b c", "a/b\nc", ""])
def test_checkout_rejects_a_bad_slug(slug):
    with pytest.raises(ValueError):
        ev.checkout(slug)


@pytest.mark.parametrize(
    "url",
    [
        "https://gitlab.com/o/r",
        "https://github.com.evil.example/o/r",
        "file:///etc/passwd",
        "ssh://git@github.com/o/r",
        "https://github.com/o",
        "",
    ],
)
def test_parse_repo_url_refuses_what_is_not_a_github_repo(url):
    with pytest.raises(disc.DiscoveryError):
        disc.parse_repo_url(url)


def test_parse_repo_url_accepts_a_github_repo_and_subpath():
    assert disc.parse_repo_url("https://github.com/o/r/tree/main/skills/x") == ("o/r", "skills/x")


# ---------------------------------------------------------------- limits


def test_lookups_over_the_hourly_limit_get_429(app, root, monkeypatch):
    monkeypatch.setattr(server, "DISCOVERIES_PER_HOUR_PER_CLIENT", 2)
    monkeypatch.setattr(disc, "discover", lambda url: {"repo": "example/repo", "commit": "c" * 40, "skills": []})
    codes = [call(app, "POST", "/api/discover", {"url": "https://github.com/example/repo"})[0] for _ in range(3)]
    assert codes == [200, 200, 429]


def test_submissions_over_the_hourly_limit_get_429(app, root, monkeypatch):
    monkeypatch.setattr(server, "SUBMISSIONS_PER_HOUR_PER_CLIENT", 1)
    body = {"repo": "example/repo", "paths": ["skills/demo"]}
    assert [call(app, "POST", "/api/submit", body)[0] for _ in range(2)] == [202, 429]


def test_a_full_queue_gets_503(app, root, monkeypatch):
    monkeypatch.setattr(server, "MAX_QUEUE", 1)
    with server.db() as c:
        c.execute("INSERT INTO jobs(skill_id, status) VALUES ('x', 'queued')")
    code, _ = call(app, "POST", "/api/submit", {"repo": "example/repo", "paths": ["skills/demo"]})
    assert code == 503


# ---------------------------------------------------------------- bootstrap extraction


def test_bootstrap_extracts_only_regular_public_reports(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.setattr(ev, "REPORTS", tmp_path / "reports")
    monkeypatch.setattr(ev, "RESULTS", ev.SEED_RESULTS)  # skip the results seeding step
    (tmp_path / "catalog").mkdir()

    def add(t, name, data=b"{}", kind=tarfile.REGTYPE, link=""):
        info = tarfile.TarInfo(name)
        info.type, info.linkname, info.size = kind, link, len(data) if kind == tarfile.REGTYPE else 0
        t.addfile(info, io.BytesIO(data) if kind == tarfile.REGTYPE else None)

    with tarfile.open(tmp_path / "catalog" / "public-reports.tar.gz", "w:gz") as t:
        add(t, "good/run/report.public.json", b'{"ok": true}')
        add(t, "good/run/skillevaluator-tier1.json")  # a raw report
        add(t, "../escape/report.public.json")
        add(t, "/abs/report.public.json")
        add(t, "link/run/report.public.json", kind=tarfile.SYMTYPE, link="/etc/passwd")
    server.bootstrap()
    got = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file() or p.is_symlink())
    assert got == ["catalog/public-reports.tar.gz", "reports/good/run/report.public.json"]
