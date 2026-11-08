import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

import server  # noqa: E402


def test_skill_rows_carry_no_install_hint(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DB_PATH", tmp_path / "inventory.db")
    server.init_db()
    with server.db() as c:
        c.execute(
            "INSERT INTO skills VALUES (?,?,?,?,?,?,?,?)",
            ("example/repo:skills/demo", "example/repo", "skills/demo", "demo", "Fixture", "docs", "fixture", 0),
        )
        s = c.execute("SELECT * FROM skills").fetchone()
        for detail in (False, True):
            assert "install" not in server.skill_row(c, s, detail=detail)
    assert not hasattr(server, "install_hint")
