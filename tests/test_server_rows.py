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


def test_engine_label_names_the_image_digest_or_the_commit():
    image = "ghcr.io/nvidia/skillevaluator-tier1@sha256:" + "9d548324" + "0" * 56
    assert server.engine_label({"version": "0.8.0", "commit": None, "image": image}) == "image 9d548324…"
    assert server.engine_label({"version": "0.8.0", "commit": "d136a597c54784ad"}) == "commit d136a59"
    assert server.engine_label({"version": "0.8.0"}) is None
    assert server.engine_label(None) is None


def test_detail_rows_carry_the_engine(tmp_path, monkeypatch):
    import json

    monkeypatch.setattr(server, "DB_PATH", tmp_path / "inventory.db")
    server.init_db()
    rec = {
        "id": "r1",
        "status": "completed",
        "started_at": "20261111T120000Z",
        "source": {"commit": "a" * 40},
        "evaluator": {"version": "0.8.0", "commit": "d136a597c54784ad"},
        "report_dir": "reports/x/y",
    }
    with server.db() as c:
        c.execute(
            "INSERT INTO skills VALUES (?,?,?,?,?,?,?,?)",
            ("example/repo:skills/demo", "example/repo", "skills/demo", "demo", "Fixture", "docs", "fixture", 0),
        )
        c.execute(
            "INSERT INTO evaluations VALUES (?,?,?,?,?,?,?)",
            (
                "r1",
                "example/repo:skills/demo",
                "tier1-static",
                "completed",
                "a" * 40,
                rec["started_at"],
                json.dumps(rec),
            ),
        )
        row = server.skill_row(c, c.execute("SELECT * FROM skills").fetchone(), detail=True)
    assert row["evaluation"]["engine"] == "commit d136a59"
    assert row["history"][0]["engine"] == "commit d136a59"
