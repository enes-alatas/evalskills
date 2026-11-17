"""#167: the repository lookup authenticates with a no-access token from the host's env, within a site-wide hourly
budget, and caches GitHub's answers. The token never reaches a child process (ADR-0008)."""

import io
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
import discover as disc  # noqa: E402

TOKEN = "github_pat_" + "x" * 40  # synthetic


class FakeGitHub:
    """api.github.com as far as the lookup sees it: 60 unauthenticated calls an hour, then 403 with Remaining 0."""

    def __init__(self):
        self.calls, self.unauthenticated = [], 0

    def open(self, req, timeout):
        auth = req.get_header("Authorization")
        self.calls.append((req.full_url, auth))
        if auth is None:
            self.unauthenticated += 1
            if self.unauthenticated > 60:
                raise urllib.error.HTTPError(
                    req.full_url, 403, "rate limited", {"X-RateLimit-Remaining": "0"}, io.BytesIO()
                )
        elif auth != f"Bearer {TOKEN}":
            raise urllib.error.HTTPError(req.full_url, 401, "bad credentials", {}, io.BytesIO())
        return io.BytesIO(json.dumps({"size": 1, "default_branch": "main", "private": False}).encode())


@pytest.fixture
def github(monkeypatch):
    fake = FakeGitHub()
    monkeypatch.setattr(disc._OPENER, "open", fake.open)
    monkeypatch.setattr(disc, "_META", {})
    monkeypatch.setattr(disc, "_CALLS", disc.deque())
    monkeypatch.setattr(disc, "_LOOKUP_TOKEN", "")
    return fake


def test_beyond_60_lookups_an_hour_still_succeed_with_the_token(github, monkeypatch):
    """The issue's done-when: lookups for many repositories past GitHub's anonymous 60 still get an answer."""
    monkeypatch.setattr(disc, "_LOOKUP_TOKEN", TOKEN + "\n")
    for i in range(75):
        assert disc.repo_meta(f"example/repo-{i}")["default_branch"] == "main"
    assert github.unauthenticated == 0 and len(github.calls) == 75


def test_without_a_token_the_61st_lookup_is_refused_plainly(github):
    for i in range(60):
        disc.repo_meta(f"example/repo-{i}")
    with pytest.raises(disc.DiscoveryError, match="try again later"):
        disc.repo_meta("example/repo-60")
    assert all(auth is None for _, auth in github.calls)


@pytest.mark.parametrize("content", ["", "  \n", "not a token!", "a" * 10])
def test_an_unset_empty_or_malformed_token_means_unauthenticated(github, monkeypatch, content, capsys):
    monkeypatch.setattr(disc, "_LOOKUP_TOKEN", content)
    disc.repo_meta("example/repo")
    assert github.calls == [("https://api.github.com/repos/example/repo", None)]
    err = capsys.readouterr().err
    if content and content.strip():
        assert "doesn't hold a token" in err and content.strip() not in err  # said, never echoed


def test_a_refused_token_fails_closed_and_tells_the_operator_not_the_visitor(github, monkeypatch, capsys):
    monkeypatch.setattr(disc, "_LOOKUP_TOKEN", "github_pat_" + "y" * 40)
    with pytest.raises(disc.DiscoveryError, match="unavailable right now") as e:
        disc.repo_meta("example/repo")
    err = capsys.readouterr().err
    assert "401" in err and "y" * 40 not in err and "y" * 40 not in str(e.value)


def test_one_repository_costs_github_one_call_per_ttl(github, monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(disc.time, "monotonic", lambda: t[0])
    for name in ("Example/Repo", "example/repo", "example/repo"):
        disc.repo_meta(name)
    assert len(github.calls) == 1
    t[0] += disc.REPO_META_TTL_S + 1
    disc.repo_meta("example/repo")
    assert len(github.calls) == 2


def test_a_failed_lookup_is_not_cached(github, monkeypatch):
    calls = []

    def missing(req, timeout):
        calls.append(req.full_url)
        raise urllib.error.HTTPError(req.full_url, 404, "not found", {}, io.BytesIO())

    monkeypatch.setattr(disc._OPENER, "open", missing)
    for _ in range(2):
        with pytest.raises(disc.DiscoveryError, match="not found"):
            disc.repo_meta("example/gone")
    assert len(calls) == 2


def test_the_cache_stays_bounded(github, monkeypatch):
    monkeypatch.setattr(disc, "REPO_META_CACHE_MAX", 5)
    for i in range(12):
        disc.repo_meta(f"example/repo-{i}")
    assert len(disc._META) <= 5 and "example/repo-11" in disc._META


def test_a_redirect_off_api_github_com_is_not_followed_with_the_token():
    h = disc._SameHostRedirects()
    req = urllib.request.Request("https://api.github.com/repos/a/b", headers={"Authorization": f"Bearer {TOKEN}"})
    with pytest.raises(disc.DiscoveryError, match="unexpected redirect"):
        h.redirect_request(req, None, 301, "moved", {}, "https://evil.example/repos/a/b")
    same = h.redirect_request(req, None, 301, "moved", {}, "https://api.github.com/repositories/1")
    assert same.full_url == "https://api.github.com/repositories/1"


def test_past_the_site_budget_the_app_refuses_without_calling_github(github, monkeypatch):
    """Keen on #211: the token's 5,000 an hour are its owner account's, so the site spends at most LOOKUPS_PER_HOUR."""
    monkeypatch.setattr(disc, "_LOOKUP_TOKEN", TOKEN)
    monkeypatch.setattr(disc, "LOOKUPS_PER_HOUR", 2000)
    t = [1000.0]
    monkeypatch.setattr(disc.time, "monotonic", lambda: t[0])
    for i in range(2000):
        disc.repo_meta(f"example/repo-{i}")
    with pytest.raises(disc.DiscoveryError, match="busy right now"):
        disc.repo_meta("example/repo-2000")
    assert len(github.calls) == 2000  # lookup 2,001 never reached GitHub
    disc.repo_meta("example/repo-1999")  # a cached answer still costs nothing
    t[0] += 3600
    disc.repo_meta("example/repo-2000")  # an hour on, the budget is back
    assert len(github.calls) == 2001


def test_the_default_budget_is_well_under_the_accounts_quota():
    assert disc.LOOKUPS_PER_HOUR <= 2000


def test_the_token_is_taken_out_of_the_environment_so_no_child_inherits_it():
    """ADR-0008: importing discover removes GITHUB_LOOKUP_TOKEN from os.environ; sh() and git_env() pass the allowlist
    only, so neither a git call nor the evaluator ever sees it."""
    probe = (
        "import os, sys; sys.path.insert(0, 'pipeline'); import discover as d, evaluate as ev; "
        "assert d.lookup_token() == os.environ.get('WANT'), 'token not read'; "
        "assert 'GITHUB_LOOKUP_TOKEN' not in os.environ; "
        "assert 'GITHUB_LOOKUP_TOKEN' not in ev.git_env() and 'GITHUB_LOOKUP_TOKEN' not in ev.ENV_ALLOW; "
        "out = ev.sh(['sh', '-c', 'env']); "
        "assert 'GITHUB_LOOKUP_TOKEN' not in out and os.environ['WANT'] not in out, 'a child inherited it'"
    )
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "GITHUB_LOOKUP_TOKEN": TOKEN, "WANT": TOKEN}
    subprocess.run([sys.executable, "-c", probe], cwd=root, env=env, check=True, capture_output=True, timeout=60)


def test_sh_keeps_an_env_a_caller_passes():
    import evaluate as ev

    assert ev.sh(["sh", "-c", "echo $ONLY"], env={"ONLY": "this", "PATH": os.environ["PATH"]}) == "this"
