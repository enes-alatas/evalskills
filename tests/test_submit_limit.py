"""The submission limit counts only skills that need a run (#83). Synthetic clones and fixture results only: no network,
no SkillEvaluator."""

import http.client
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

import evaluate as ev  # noqa: E402
import server  # noqa: E402

REPO = "example/many"
EVC = "e" * 40


def call(addr, method, path, body=None):
    c = http.client.HTTPConnection(*addr, timeout=5)
    headers = {"Content-Type": "application/json"} if body is not None else {}
    c.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
    r = c.getresponse()
    data = json.loads(r.read() or b"{}")
    c.close()
    return r.status, data


@pytest.fixture
def many(app, tmp_path, monkeypatch):
    """A clone with 14 skills; s00 and s01 are listed with a completed result for their current content."""
    monkeypatch.setattr(ev, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(ev, "RESULTS", tmp_path / "results.jsonl")
    monkeypatch.setattr(ev, "evaluator_version", lambda: {"version": "0", "commit": EVC})
    monkeypatch.setattr(server.WAKE, "set", lambda: None)
    monkeypatch.setattr(server, "MAX_SKILLS_PER_SUBMISSION", 10)
    clone = tmp_path / "cache" / "example__many"
    for i in range(14):
        d = clone / "skills" / f"s{i:02d}"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"---\nname: s{i:02d}\ndescription: fixture\n---\n")
    recs = []
    with server.db() as c:
        for i in (0, 1):
            path = f"skills/s{i:02d}"
            c.execute(
                "INSERT INTO skills VALUES(?,?,?,?,?,?,?,?)",
                (server.skill_id(REPO, path), REPO, path, f"s{i:02d}", "", "other", "catalog", 0),
            )
            recs.append(
                {
                    "status": "completed",
                    "mode": "tier1-static",
                    "source": {"repo": REPO, "skill_path": path, "tree_digest": ev.tree_digest(clone / path)},
                    "evaluator": {"commit": EVC},
                }
            )
    ev.RESULTS.write_text("".join(json.dumps(r) + "\n" for r in recs))
    return clone


def paths(*idx):
    return [f"skills/s{i:02d}" for i in idx]


def test_up_to_date_skills_do_not_count_and_get_no_job(app, many):
    code, data = call(app, "POST", "/api/submit", {"repo": REPO, "paths": paths(0, 1, *range(2, 12))})
    assert code == 202
    by = {x["path"]: x for x in data["submitted"]}
    assert by["skills/s00"]["note"] == "up to date" and "job" not in by["skills/s00"]
    assert by["skills/s01"]["note"] == "up to date" and "job" not in by["skills/s01"]
    assert sum("job" in x for x in data["submitted"]) == 10


def test_eleven_skills_needing_a_run_are_refused(app, many):
    code, data = call(app, "POST", "/api/submit", {"repo": REPO, "paths": paths(*range(2, 13))})
    assert code == 400 and "new or changed" in data["error"]


def test_a_changed_skill_counts(app, many):
    (many / "skills" / "s00" / "SKILL.md").write_text("---\nname: s00\ndescription: edited\n---\n")
    code, _ = call(app, "POST", "/api/submit", {"repo": REPO, "paths": paths(0, *range(2, 12))})
    assert code == 400


def test_a_new_evaluator_makes_listed_skills_count(app, many, monkeypatch):
    monkeypatch.setattr(ev, "evaluator_version", lambda: {"version": "1", "commit": "f" * 40})
    code, _ = call(app, "POST", "/api/submit", {"repo": REPO, "paths": paths(0, 1, *range(2, 11))})
    assert code == 400


def test_an_unknown_evaluator_version_counts_every_skill(app, many, monkeypatch):
    def boom():
        raise RuntimeError("no evaluator")

    monkeypatch.setattr(ev, "evaluator_version", boom)
    code, _ = call(app, "POST", "/api/submit", {"repo": REPO, "paths": paths(0, 1, *range(2, 11))})
    assert code == 400


def test_duplicate_paths_count_once(app, many):
    code, data = call(app, "POST", "/api/submit", {"repo": REPO, "paths": paths(*range(2, 12)) * 2})
    assert code == 202 and len(data["submitted"]) == 10


def test_discover_marks_up_to_date_skills(app, many, monkeypatch):
    skills = [{"name": p.rsplit("/", 1)[-1], "path": p} for p in paths(0, 1, 2)]
    found = {"repo": REPO, "commit": "c" * 40, "skills": skills}
    monkeypatch.setattr(server.disc, "discover", lambda url: json.loads(json.dumps(found)))
    code, data = call(app, "POST", "/api/discover", {"url": "https://github.com/example/many"})
    assert code == 200
    assert [(s["listed"], s["current"]) for s in data["skills"]] == [(True, True), (True, True), (False, False)]


def no_hashing(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("hashed before the limits")

    monkeypatch.setattr(server, "current_paths", boom)  # current_paths itself would swallow an error from up_to_date


def test_a_client_over_its_rate_limit_gets_429_before_any_hashing(app, many, monkeypatch):
    monkeypatch.setattr(server, "SUBMISSIONS_PER_HOUR_PER_CLIENT", 0)
    no_hashing(monkeypatch)
    code, _ = call(app, "POST", "/api/submit", {"repo": REPO, "paths": paths(0, 1)})
    assert code == 429


def test_a_full_queue_gets_503_before_any_hashing(app, many, monkeypatch):
    monkeypatch.setattr(server, "MAX_QUEUE", 0)
    no_hashing(monkeypatch)
    code, _ = call(app, "POST", "/api/submit", {"repo": REPO, "paths": paths(0, 1)})
    assert code == 503


def test_more_paths_than_discovery_lists_are_refused(app, many, monkeypatch):
    monkeypatch.setattr(server.disc, "MAX_SKILLS_LISTED", 3)
    no_hashing(monkeypatch)
    code, data = call(app, "POST", "/api/submit", {"repo": REPO, "paths": paths(0, 1, 2, 3)})
    assert code == 400 and data["error"] == "Too many skills in one request"
