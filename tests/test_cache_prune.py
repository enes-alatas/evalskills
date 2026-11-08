import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

import evaluate as ev  # noqa: E402

HOUR = 3600


def clones(tmp_path, monkeypatch, ages_h: dict[str, float]):
    monkeypatch.setattr(ev, "CACHE", tmp_path)
    now = time.time()
    for slug, age in ages_h.items():
        d = tmp_path / slug.replace("/", "__")
        d.mkdir()
        os.utime(d, (now - age * HOUR, now - age * HOUR))


def left(tmp_path):
    return sorted(d.name for d in tmp_path.iterdir())


def test_stale_clones_go_fresh_ones_stay(tmp_path, monkeypatch):
    clones(tmp_path, monkeypatch, {"a/old": 7, "b/new": 1})
    assert ev.prune_cache(6 * HOUR, 40, set()) == ["a/old"]
    assert left(tmp_path) == ["b__new"]


def test_a_clone_a_job_needs_is_kept_however_old(tmp_path, monkeypatch):
    clones(tmp_path, monkeypatch, {"a/old": 100})
    assert ev.prune_cache(6 * HOUR, 40, {"a/old"}) == []
    assert left(tmp_path) == ["a__old"]


def test_least_recently_used_go_beyond_the_count_cap(tmp_path, monkeypatch):
    clones(tmp_path, monkeypatch, {"a/r1": 0.1, "a/r2": 0.2, "a/r3": 0.3, "keep/me": 5})
    removed = ev.prune_cache(6 * HOUR, 3, {"keep/me"})  # one slot is the kept clone's, so two others stay
    assert removed == ["a/r3"]
    assert left(tmp_path) == ["a__r1", "a__r2", "keep__me"]


def test_a_missing_cache_is_fine(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "CACHE", tmp_path / "absent")
    assert ev.prune_cache(HOUR, 1, set()) == []


def test_the_server_spares_repos_with_queued_or_running_jobs(tmp_path, monkeypatch):
    import server

    monkeypatch.setattr(server, "DB_PATH", tmp_path / "inventory.db")
    server.init_db()
    cache = tmp_path / "repos"
    cache.mkdir()
    clones(cache, monkeypatch, {"q/queued": 50, "r/running": 50, "d/done": 50})
    with server.db() as c:
        for repo, status in [("q/queued", "queued"), ("r/running", "running"), ("d/done", "completed")]:
            c.execute("INSERT INTO skills (id, repo, path) VALUES (?,?,?)", (f"{repo}:.", repo, "."))
            c.execute("INSERT INTO jobs (skill_id, status) VALUES (?,?)", (f"{repo}:.", status))
    assert server.prune_clones() == ["d/done"]
    assert left(cache) == ["q__queued", "r__running"]
