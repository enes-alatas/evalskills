"""ADR-0007: no live pilot record reaches the public build or the board."""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))
import evaluate as ev  # noqa: E402
import export_catalog  # noqa: E402

PILOT_REC = {"id": "pilot-x", "mode": "tier3", "pilot": True, "source": {"repo": "a/b", "skill_path": "s"}}


def test_the_shipped_catalog_holds_no_pilot_record():
    rows = [json.loads(line) for line in ev.SEED_RESULTS.read_text().splitlines() if line.strip()]
    assert rows and not [r["id"] for r in rows if ev.is_pilot(r)]


@pytest.mark.parametrize("rec", [{"mode": "tier3"}, {"mode": "tier1-static", "pilot": True}])
def test_any_live_or_pilot_record_is_refused(rec):
    with pytest.raises(ev.PilotRecord):
        ev.refuse_pilot([{"id": "ok", "mode": "tier1-static"}, rec], "here")
    ev.refuse_pilot([{"id": "ok", "mode": "tier1-static"}], "here")  # static records pass


def test_export_refuses_before_writing_anything(monkeypatch):
    bundle = ROOT / "catalog" / "public-reports.tar.gz"
    before = bundle.read_bytes() if bundle.exists() else None
    monkeypatch.setattr(ev, "load_results", lambda: [PILOT_REC])
    with pytest.raises(ev.PilotRecord):
        export_catalog.main()
    assert (bundle.read_bytes() if bundle.exists() else None) == before


def test_bootstrap_refuses_a_seed_with_a_pilot_record(tmp_path, monkeypatch):
    import server

    seed, live = tmp_path / "seed.jsonl", tmp_path / "data" / "results.jsonl"
    seed.write_text(json.dumps(PILOT_REC) + "\n")
    monkeypatch.setattr(ev, "SEED_RESULTS", seed)
    monkeypatch.setattr(ev, "RESULTS", live)
    with pytest.raises(ev.PilotRecord):
        server.bootstrap()
    assert not live.exists()
