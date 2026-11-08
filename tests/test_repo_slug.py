import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))

import discover as disc  # noqa: E402
import evaluate as ev  # noqa: E402

REFUSED = ["../etc", "./x", "-x/y", "a/..", "a/.", "a/-b", "a/b\n", "a/b/c", "a", "", "x" * 40 + "/y", "a/" + "b" * 101]


@pytest.mark.parametrize("slug", REFUSED)
def test_slugs_github_never_issues_are_refused(slug):
    assert not ev.REPO_RE.match(slug)
    with pytest.raises(ValueError):
        ev.checkout(slug)


@pytest.mark.parametrize("slug", ["a/b", "a-b/c.d_e", "O1/.github", "x" * 39 + "/y", "a/" + "b" * 100])
def test_real_shapes_pass(slug):
    assert ev.REPO_RE.match(slug)


def test_every_catalogued_repo_still_passes():
    lines = (ROOT / "catalog" / "results.jsonl").read_text().splitlines()
    repos = {json.loads(line)["source"]["repo"] for line in lines if line.strip()}
    assert repos and all(ev.REPO_RE.match(r) for r in repos)


@pytest.mark.parametrize("url", ["https://github.com/-x/y", "https://github.com/a/..", "https://github.com/../etc"])
def test_discovery_refuses_them_too(url):
    with pytest.raises(disc.DiscoveryError):
        disc.parse_repo_url(url)
