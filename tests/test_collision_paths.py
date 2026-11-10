import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

import server  # noqa: E402


def rows(*paths, name="x", repo="o/r"):
    return [{"name": name, "repo": repo, "path": p} for p in paths]


def where(rs):
    return [r.get("where") for r in server.mark_collisions(rs)]


def test_unique_rows_get_no_label():
    assert where(rows("a/x") + rows("b/y", name="y") + rows("a/x", repo="o/other")) == [None, None, None]


def test_label_is_the_differing_parent():
    assert where(rows(".agents/skills/x", ".mastracode/skills/x")) == [".agents/skills", ".mastracode/skills"]


def test_shared_last_folders_need_a_longer_suffix():
    # A fixed-depth parent would show "skills" twice.
    assert where(rows("a/skills/x", "b/skills/x")) == ["a/skills", "b/skills"]


def test_shortest_unique_suffix_per_row():
    assert where(rows("a/skills/x", "b/skills/x", "c/x")) == ["a/skills", "b/skills", "c"]


def test_full_path_when_no_parent_suffix_is_unique():
    # Same parent folder, different skill folders: only the full path tells them apart.
    assert where(rows("skills/x", "skills/x-v2")) == ["skills/x", "skills/x-v2"]


def test_whole_parent_is_unique_unless_identical():
    assert where(rows("a/skills/x", "skills/x")) == ["a/skills", "skills"]


def test_repo_root():
    assert where(rows(".", "skills/x")) == ["repo root", "skills"]
