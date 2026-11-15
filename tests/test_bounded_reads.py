"""Files from a cloned repository are read only up to a bound, never whole (#170). Synthetic clones, no network."""

import hashlib
import os
import re
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

import discover as disc  # noqa: E402
import evaluate as ev  # noqa: E402

PIPELINE = Path(__file__).resolve().parents[1] / "pipeline"
FRONT = b"---\nname: big\ndescription: a skill with a very large body\n---\n"


def huge(path: Path, size: int = 1 << 30) -> Path:
    """A SKILL.md of `size` bytes: real frontmatter, then a sparse tail, so the test costs no disk or memory."""
    with path.open("wb") as f:
        f.write(FRONT)
        f.truncate(size)
    return path


def test_only_the_bound_is_read_from_a_huge_skill_md(tmp_path, monkeypatch):
    p = huge(tmp_path / "SKILL.md")
    asked = []
    real = os.fdopen

    def counting(fd, mode):
        f = real(fd, mode)
        read = f.read

        def bounded(n=-1):
            asked.append(n)
            return read(n)

        f.read = bounded
        return f

    monkeypatch.setattr(ev.os, "fdopen", counting)
    text = ev.read_head(p)
    assert asked == [ev.HEAD_BYTES] and len(text) == ev.HEAD_BYTES
    assert disc.frontmatter(text)["name"] == "big"


def test_discover_lists_a_skill_whose_skill_md_is_a_gigabyte(tmp_path, monkeypatch):
    d = tmp_path / "clone" / "skills" / "big"
    d.mkdir(parents=True)
    huge(d / "SKILL.md")
    monkeypatch.setattr(disc, "repo_meta", lambda repo: {"size": 1, "default_branch": "main"})
    monkeypatch.setattr(disc, "checkout", lambda repo: (tmp_path / "clone", "c" * 40))
    res = disc.discover("https://github.com/example/big")
    assert res["skills"] == [{"path": "skills/big", "name": "big", "description": "a skill with a very large body"}]


def test_a_link_a_fifo_or_a_folder_reads_as_empty(tmp_path):
    (tmp_path / "zero").symlink_to("/dev/zero")  # endless if followed
    (tmp_path / "passwd").symlink_to("/etc/passwd")  # outside the clone
    os.mkfifo(tmp_path / "fifo")  # a plain open would block until a writer comes
    (tmp_path / "dir").mkdir()
    got = {}

    def read_all():
        for name in ("zero", "passwd", "fifo", "dir"):
            got[name] = ev.read_head(tmp_path / name)

    t = threading.Thread(target=read_all, daemon=True)
    t.start()
    t.join(10)
    assert not t.is_alive() and got == {"zero": "", "passwd": "", "fifo": "", "dir": ""}


def test_tree_digest_is_unchanged_by_hashing_in_chunks(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "SKILL.md").write_bytes(FRONT)
    (tmp_path / "a" / "data.bin").write_bytes(os.urandom(3 * 1024 * 1024 + 7))  # past one read buffer
    old = hashlib.sha256()  # the whole-file algorithm it replaced, so stored digests still match
    for p in sorted(tmp_path.rglob("*")):
        if p.is_file():
            old.update(str(p.relative_to(tmp_path)).encode())
            old.update(hashlib.sha256(p.read_bytes()).digest())
    assert ev.tree_digest(tmp_path) == "sha256:" + old.hexdigest()


def test_no_skill_md_is_read_whole_anywhere_in_the_pipeline():
    for f in PIPELINE.glob("*.py"):
        for n, line in enumerate(f.read_text().splitlines(), 1):
            assert not re.search(r"SKILL\.md.*read_(text|bytes)\(|read_text\(errors=", line), f"{f.name}:{n}: {line}"
    assert not re.search(r"read_(text|bytes)\(", (PIPELINE / "discover.py").read_text())
