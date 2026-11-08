import io
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

import discover as disc  # noqa: E402
import evaluate as ev  # noqa: E402


@pytest.mark.parametrize("code", [403, 429])
def test_a_rate_limited_size_lookup_refuses_instead_of_cloning(monkeypatch, code):
    def urlopen(req, timeout):
        raise urllib.error.HTTPError(req.full_url, code, "rate limited", {}, io.BytesIO())

    def checkout(_repo):
        raise AssertionError("must not clone when the size is unknown")

    monkeypatch.setattr(disc.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(disc, "checkout", checkout)
    with pytest.raises(disc.DiscoveryError, match="try again later"):
        disc.discover("https://github.com/example/repo")


def fake_clone(size_bytes):
    def sh(cmd, **_kw):
        if cmd[:2] == ["git", "clone"]:
            d = Path(cmd[-1])
            d.mkdir(parents=True)
            (d / "blob.bin").write_bytes(b"\0" * size_bytes)
            return ""
        return "c" * 40  # rev-parse

    return sh


def test_a_checkout_over_the_limit_is_deleted_and_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "CACHE", tmp_path)
    monkeypatch.setattr(ev, "MAX_REPO_KB", 4)
    monkeypatch.setattr(ev, "sh", fake_clone(8 * 1024))
    with pytest.raises(ev.RepoTooLarge):
        ev.checkout("example/repo")
    assert not (tmp_path / "example__repo").exists()


def test_a_checkout_under_the_limit_is_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "CACHE", tmp_path)
    monkeypatch.setattr(ev, "MAX_REPO_KB", 4)
    monkeypatch.setattr(ev, "sh", fake_clone(1024))
    d, commit = ev.checkout("example/repo")
    assert d.exists() and commit == "c" * 40
