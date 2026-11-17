import io
import os
import subprocess
import sys
import time
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

import discover as disc  # noqa: E402
import evaluate as ev  # noqa: E402


@pytest.mark.parametrize(
    "code, headers, message",
    [
        (429, {}, "try again later"),
        (403, {"X-RateLimit-Remaining": "0"}, "try again later"),
        (403, {"X-RateLimit-Remaining": "42", "Retry-After": "60"}, "try again later"),  # secondary rate limit
        (403, {}, "refused to describe"),  # blocked repo or other 403: still no clone, but not called rate limiting
    ],
)
def test_a_size_lookup_without_an_answer_refuses_instead_of_cloning(monkeypatch, code, headers, message):
    def urlopen(req, timeout):
        raise urllib.error.HTTPError(req.full_url, code, "refused", headers, io.BytesIO())

    def checkout(_repo):
        raise AssertionError("must not clone when the size is unknown")

    monkeypatch.setattr(disc.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(disc, "checkout", checkout)
    with pytest.raises(disc.DiscoveryError, match=message):
        disc.discover("https://github.com/example/repo")


# A stand-in for git on PATH (checkout passes git only an allowlisted environment, so its mode is a file beside it).
# `git clone … DIR` follows the mode: "write:N" writes N bytes and exits, "grow" keeps
# writing (a repository larger than GitHub said), "hang" never finishes, "fail" exits 128 after a partial write.
# `git -C DIR rev-parse HEAD` prints a fixed commit, fetch follows the mode too, and reset succeeds unless a
# reset_mode file beside it names a mode for it.
FAKE_GIT = r"""#!/bin/sh
if [ "$1" = clone ]; then for a; do d=$a; done; mkdir -p "$d/.git"
elif [ "$1" = -C ]; then d=$2; [ "$3" = rev-parse ] && { echo cccccccccccccccccccccccccccccccccccccccc; exit 0; }
  if [ "$3" = reset ]; then [ -f "$(dirname "$0")/reset_mode" ] || exit 0
    FAKE_GIT=$(cat "$(dirname "$0")/reset_mode"); fi
fi
[ -n "$FAKE_GIT" ] || FAKE_GIT=$(cat "$(dirname "$0")/mode")
case "$FAKE_GIT" in
  write:*) head -c "${FAKE_GIT#write:}" /dev/zero > "$d/blob.bin" ;;
  grow) i=0; while :; do i=$((i+1)); head -c 65536 /dev/zero > "$d/.git/pack-$i"; sleep 0.02; done ;;
  hang) head -c 10 /dev/zero > "$d/.git/partial"; sleep 600 ;;
  fail) head -c 10 /dev/zero > "$d/.git/partial"; exit 128 ;;
esac
"""


@pytest.fixture
def git(tmp_path, monkeypatch):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "git").write_text(FAKE_GIT)
    (bin_ / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_}:{os.environ['PATH']}")
    monkeypatch.setattr(ev, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(ev, "MAX_REPO_KB", 64)
    monkeypatch.setattr(ev, "CLONE_POLL_S", 0.05)
    monkeypatch.setattr(ev, "CLONE_TIMEOUT_S", 1)

    def mode(m, reset=None):
        (bin_ / "mode").write_text(m)
        if reset:
            (bin_ / "reset_mode").write_text(reset)

    return mode


def test_a_checkout_over_the_limit_is_deleted_and_refused(tmp_path, git):
    git("write:131072")
    with pytest.raises(ev.RepoTooLarge):
        ev.checkout("example/repo")
    assert not (tmp_path / "cache" / "example__repo").exists()


def test_a_checkout_under_the_limit_is_kept(tmp_path, git):
    git("write:1024")
    d, commit = ev.checkout("example/repo")
    assert d.exists() and commit == "c" * 40


def test_a_clone_growing_past_the_cap_is_stopped_before_it_finishes(tmp_path, git):
    """#169: GitHub's size can lag a push. The cap holds while the clone runs, not only after it returns."""
    git("grow")  # never finishes on its own
    t = time.monotonic()
    with pytest.raises(ev.RepoTooLarge, match="stopped at"):
        ev.checkout("example/repo")
    assert time.monotonic() - t < 1  # stopped by the cap, well before the 1 s timeout
    assert not (tmp_path / "cache" / "example__repo").exists()


def test_a_clone_killed_at_its_timeout_leaves_nothing_and_the_next_starts_clean(tmp_path, git):
    git("hang")
    with pytest.raises(ev.CloneTimeout):
        ev.checkout("example/repo")
    assert not (tmp_path / "cache" / "example__repo").exists()
    git("write:1024")
    d, _ = ev.checkout("example/repo")  # a fresh clone, not a fetch into the partial one
    assert (d / "blob.bin").exists() and not (d / ".git" / "partial").exists()


def test_a_failed_clone_or_fetch_removes_its_folder(tmp_path, git):
    git("fail")
    with pytest.raises(subprocess.CalledProcessError):
        ev.checkout("example/repo")
    assert not (tmp_path / "cache" / "example__repo").exists()
    git("write:1024")
    ev.checkout("example/repo")
    git("fail")  # now a fetch into the existing clone
    with pytest.raises(subprocess.CalledProcessError):
        ev.checkout("example/repo")
    assert not (tmp_path / "cache" / "example__repo").exists()


def test_a_refresh_whose_reset_grows_past_the_cap_is_stopped_and_removed(tmp_path, git):
    """#169: a cached repo's new push can be a tiny pack that inflates on reset. The cap holds while reset writes."""
    git("write:1024")
    ev.checkout("example/repo")
    git("write:0", reset="grow")  # the fetch is small; reset keeps writing
    t = time.monotonic()
    with pytest.raises(ev.RepoTooLarge, match="stopped at"):
        ev.checkout("example/repo")
    assert time.monotonic() - t < 1
    assert not (tmp_path / "cache" / "example__repo").exists()


def test_a_refresh_whose_reset_hangs_raises_clone_timeout(tmp_path, git):
    git("write:1024")
    ev.checkout("example/repo")
    git("write:0", reset="hang")
    with pytest.raises(ev.CloneTimeout):
        ev.checkout("example/repo")
    assert not (tmp_path / "cache" / "example__repo").exists()


def test_a_timed_out_lookup_tells_the_visitor_plainly(tmp_path, git, monkeypatch):
    monkeypatch.setattr(disc, "repo_meta", lambda repo: {"size": 1, "default_branch": "main"})
    git("hang")
    with pytest.raises(disc.DiscoveryError, match="took too long"):
        disc.discover("https://github.com/example/repo")
