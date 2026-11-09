import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

import server  # noqa: E402

CATALOG = Path(__file__).resolve().parents[1] / "catalog"


def record(repo, path, category=""):
    return {
        "id": f"{repo}:{path}:tier1-static:t",
        "skill_name": path.rsplit("/", 1)[-1],
        "category": category,
        "mode": "tier1-static",
        "status": "completed",
        "started_at": "t",
        "source": {"repo": repo, "skill_path": path, "commit": "0" * 40},
    }


def write(path, rows):
    path.write_text("".join("\t".join(r) + "\n" for r in rows))


def setup(tmp_path, monkeypatch, records):
    cat = tmp_path / "catalog"
    cat.mkdir()
    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.setattr(server, "DB_PATH", tmp_path / "inventory.db")
    monkeypatch.setattr(server.ev, "load_results", lambda: records)
    monkeypatch.setattr(server, "read_desc", lambda rec: (None, ""))
    server.init_db()
    return cat


def categories():
    with server.db() as c:
        return {r["id"]: r["category"] for r in c.execute("SELECT id, category FROM skills")}


def test_curated_file_overrides_batch_tsv_whatever_the_file_order(tmp_path, monkeypatch):
    cat = setup(tmp_path, monkeypatch, [record("example/a", "s/one"), record("example/a", "s/two")])
    write(cat / "zz-batch.tsv", [("example/a", "s/one", "other"), ("example/a", "s/two", "documents")])
    write(cat / server.CURATED_CATEGORIES, [("example/a", "s/one", "writing")])
    server.import_results()
    assert categories() == {"example/a:s/one": "writing", "example/a:s/two": "documents"}


def test_reimport_updates_an_existing_catalog_skill(tmp_path, monkeypatch):
    cat = setup(tmp_path, monkeypatch, [record("example/a", "s/one", "other")])
    server.import_results()
    assert categories() == {"example/a:s/one": "other"}
    write(cat / server.CURATED_CATEGORIES, [("example/a", "s/one", "planning")])
    server.import_results()
    assert categories() == {"example/a:s/one": "planning"}


def test_reimport_leaves_a_submitted_skill_alone(tmp_path, monkeypatch):
    cat = setup(tmp_path, monkeypatch, [record("example/a", "s/one")])
    with server.db() as c:
        c.execute(
            "INSERT INTO skills VALUES (?,?,?,?,?,?,?,?)",
            ("example/a:s/one", "example/a", "s/one", "one", "", "security", "submission", 0),
        )
    write(cat / server.CURATED_CATEGORIES, [("example/a", "s/one", "planning")])
    server.import_results()
    assert categories() == {"example/a:s/one": "security"}


def test_every_catalog_skill_gets_a_known_category():
    known = {c["id"] for c in json.loads((CATALOG / "categories.json").read_text())}
    meta = server.catalog_categories(CATALOG)
    assert set(meta.values()) <= known
    for line in (CATALOG / "results.jsonl").read_text().splitlines():
        rec = json.loads(line)
        sid = server.skill_id(rec["source"]["repo"], rec["source"]["skill_path"])
        assert (meta.get(sid) or rec.get("category")) in known, sid
