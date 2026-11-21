"""#175: the skills list is built once and reused, a finished job shows at once, and old rate-limit rows go."""

import json
import sqlite3
import time
import urllib.request

import pytest
import server


def fetch(app, path="/api/skills"):
    with urllib.request.urlopen(f"http://{app[0]}:{app[1]}{path}", timeout=5) as r:
        return json.loads(r.read())


@pytest.fixture
def statements(monkeypatch):
    """Every SQL statement the server runs, from here on."""
    seen, real = [], server.db

    def counted():
        c = real()
        c.set_trace_callback(seen.append)
        return c

    monkeypatch.setattr(server, "db", counted)
    return seen


def add_skill(sid="example/repo:skills/demo"):
    with server.db() as c:
        c.execute(
            "INSERT INTO skills VALUES (?,?,?,?,?,?,?,?)",
            (sid, "example/repo", "skills/demo", "demo", "Fixture", "docs", "fixture", 0),
        )
    server.LISTING.invalidate()  # as every write in the server does


def test_a_second_request_in_the_window_does_no_database_work(app, statements):
    add_skill()
    first = fetch(app)
    before = len(statements)
    assert fetch(app) == first and len(first) == 1
    assert len(statements) == before  # served from the cache: not one statement


def test_the_list_is_rebuilt_after_the_window(app, monkeypatch):
    add_skill()
    fetch(app)
    builds = server.LISTING.builds
    monkeypatch.setattr(server, "LIST_TTL_S", 0)
    fetch(app)
    assert server.LISTING.builds == builds + 1


def test_a_burst_costs_one_build(app):
    import concurrent.futures

    add_skill()
    builds = server.LISTING.builds
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        got = list(pool.map(lambda _: fetch(app), range(16)))
    assert all(g == got[0] for g in got)
    assert server.LISTING.builds == builds + 1


def test_a_finished_job_shows_in_the_next_list(app, monkeypatch):
    add_skill()
    with server.db() as c:
        c.execute(
            "INSERT INTO jobs(skill_id,status,created_at,client) VALUES(?,?,?,?)",
            ("example/repo:skills/demo", "queued", time.time(), "t"),
        )
    server.LISTING.invalidate()
    assert fetch(app)[0]["state"] == "queued"  # cached now, within its window

    def evaluate(repo, path, category, on_start=None):
        on_start()
        return {
            "id": "r1",
            "skill_name": "demo",
            "mode": "tier1-static",
            "status": "completed",
            "started_at": "20261121T000000Z",
            "source": {"repo": repo, "skill_path": path, "commit": "a" * 40},
            "evaluator": {"version": "1.0.1", "commit": None, "image": "img@sha256:" + "0" * 64},
            "tier1": {"quality": {"score": 88.0, "grade": "B"}},
        }

    monkeypatch.setattr(server.ev, "evaluate", evaluate)
    assert server.process_next() is True
    row = fetch(app)[0]
    assert row["state"] == "completed"
    assert server.process_next() is False  # nothing left


def test_rate_checks_drop_rows_past_the_window(app):
    now = time.time()
    with server.db() as c:
        c.executemany(
            "INSERT INTO events VALUES(?,?,?)",
            [
                ("old", "submit", now - server.RATE_WINDOW_S - 60),
                ("old", "discover", now - 7200),
                ("new", "submit", now),
            ],
        )
        assert server.rate_ok(c, "someone", "submit", 5)
        left = sorted(r["client"] for r in c.execute("SELECT client FROM events"))
    assert left == ["new", "someone"]


def test_the_events_table_is_indexed(app):
    c = sqlite3.connect(server.DB_PATH)
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='events'")}
    plan = " ".join(
        str(r[-1])
        for r in c.execute(
            "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM events WHERE client=? AND kind=? AND at>?", ("a", "b", 0)
        )
    )
    c.close()
    assert {"events_by_client", "events_by_time"} <= names
    assert "events_by_client" in plan
