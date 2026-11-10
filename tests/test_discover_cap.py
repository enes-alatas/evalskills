"""Discovery lists up to MAX_SKILLS_LISTED skills and says when it cut the list (#87). Synthetic clone, no network."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

import discover as disc  # noqa: E402


def fake_repo(tmp_path, monkeypatch, n):
    root = tmp_path / "clone"
    for i in range(n):
        d = root / "skills" / f"s{i:02d}"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"---\nname: s{i:02d}\n---\n")
    monkeypatch.setattr(disc, "repo_meta", lambda repo: {"size": 1, "default_branch": "main"})
    monkeypatch.setattr(disc, "checkout", lambda repo: (root, "c" * 40))


def test_a_list_over_the_cap_is_cut_and_says_so(tmp_path, monkeypatch):
    fake_repo(tmp_path, monkeypatch, 5)
    monkeypatch.setattr(disc, "MAX_SKILLS_LISTED", 3)
    res = disc.discover("https://github.com/example/many")
    assert [s["name"] for s in res["skills"]] == ["s00", "s01", "s02"]
    assert res["total"] == 5 and res["truncated"] is True


def test_a_list_under_the_cap_is_complete(tmp_path, monkeypatch):
    fake_repo(tmp_path, monkeypatch, 5)
    res = disc.discover("https://github.com/example/many")
    assert len(res["skills"]) == 5 and res["total"] == 5 and res["truncated"] is False


def test_the_cap_covers_a_large_real_repository():
    assert disc.MAX_SKILLS_LISTED >= 450  # NVIDIA/skills on 2026-11-10
