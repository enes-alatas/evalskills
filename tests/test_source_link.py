"""The header's GitHub link comes only from a well-formed SOURCE_REPO_URL (#77)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

import server  # noqa: E402


@pytest.mark.parametrize("url", ["https://github.com/enes-alatas/evalskills", " https://github.com/a/b.c_d "])
def test_github_repo_urls_are_accepted(url):
    assert server.source_repo_url(url) == url.strip()


@pytest.mark.parametrize(
    "url",
    [
        "",
        "http://github.com/a/b",
        "https://github.com/a/b/tree/main",
        "https://evil.example/a/b",
        "javascript:alert(1)",
        "https://github.com/a/b?x=1",
        "https://github.com.evil.example/a/b",
    ],
)
def test_anything_else_hides_the_link(url):
    assert server.source_repo_url(url) == ""
