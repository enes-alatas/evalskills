"""The catalog re-scan (#110) picks each skill's newest static run and writes records in the catalog's schema."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import catalog_rescan as cr  # noqa: E402


def rec(path: str, stamp: str, mode: str = "tier1-static") -> dict:
    return {
        "id": f"x@{stamp}",
        "skill_name": Path(path).name,
        "category": "testing",
        "mode": mode,
        "started_at": stamp,
        "duration_s": 5.0,
        "source": {"repo": "o/r", "skill_path": path, "commit": "a" * 40, "tree_digest": "sha256:00"},
    }


def test_latest_per_skill_keeps_newest_static_run():
    rows = [rec("s/a", "20261107T000000Z"), rec("s/a", "20261108T000000Z"), rec("s/b", "20261107T000000Z")]
    rows.append(rec("s/b", "20261109T000000Z", mode="tier1-llm"))
    got = cr.latest_per_skill(rows)
    assert [(r["source"]["skill_path"], r["started_at"]) for r in got] == [
        ("s/a", "20261108T000000Z"),
        ("s/b", "20261107T000000Z"),
    ]


def test_new_record_keeps_source_and_names_the_image():
    evaluator = {"version": "0.8.0", "commit": None, "image": "img@sha256:9d54"}
    attempts = [{"exit_code": 1, "duration_s": 3.0, "scanner_crash": "x"}, {"exit_code": 0, "duration_s": 4.0}]
    report = {"quality_summary": [{"score": 80}], "results": []}
    r = cr.new_record(rec("s/a", "1"), evaluator, "20261111T000000Z", attempts, report)
    assert r["source"]["commit"] == "a" * 40 and r["evaluator"] == evaluator
    assert r["status"] == "completed" and r["duration_s"] == 7.0 and r["exit_code"] == 0
    assert r["report_dir"] == "reports/o__r__s__a/aaaaaaaaaaaa-tier1-static-20261111T000000Z"


def test_missing_report_is_a_failure_not_a_score():
    r = cr.new_record(rec("s/a", "1"), {"image": "i"}, "2", [{"exit_code": 2, "duration_s": 1.0}], None)
    assert r["status"] == "failed" and "tier1" not in r


def test_unpack_reads_the_log_and_skips_unsafe_members(tmp_path):
    import base64
    import io
    import subprocess
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name in ("results.jsonl", "reports/a/report.public.json", "../escape", "/abs"):
            info = tarfile.TarInfo(name)
            info.size = 2
            t.addfile(info, io.BytesIO(b"{}"))
        link = tarfile.TarInfo("link")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
        t.addfile(link)
    b64 = base64.encodebytes(buf.getvalue()).decode().splitlines()
    pre = "rescan\tprint the output\t2026-11-11T00:00:00.1Z "
    log = "".join(pre + x + "\n" for x in ["BEGIN catalog-rescan.tar.gz", *b64, "END catalog-rescan.tar.gz"])
    script = Path(__file__).resolve().parents[1] / "scripts" / "rescan_unpack.py"
    out = tmp_path / "out"
    subprocess.run([sys.executable, "-I", str(script), str(out)], input=log, text=True, check=True)
    got = sorted(str(p.relative_to(out)) for p in out.rglob("*") if p.is_file() or p.is_symlink())
    assert got == ["reports/a/report.public.json", "results.jsonl"]
    assert not (tmp_path / "escape").exists()


def test_a_renamed_scanner_validator_fails_the_rescan(tmp_path, monkeypatch):
    """#243: a report with secret findings under an unlisted validator gets no record, and the job exits non-zero."""
    import json

    report = {
        "quality_summary": [{"overall_score": 80}],
        "results": [{"validator": "Credential Scan", "legacy": {"errors": ["[SECRET-HIGH] key: x in a.sh:1"]}}],
    }
    repo = tmp_path / "repo"
    (repo / "s" / "a").mkdir(parents=True)
    (repo / "s" / "a" / "SKILL.md").write_text("---\nname: a\n---\n")

    def fake_scan(image, skill, reports):
        reports.mkdir(parents=True)
        (reports / "skillevaluator-tier1.json").write_text(json.dumps(report))
        return 0, 1.0

    old = rec("s/a", "1")
    monkeypatch.setattr(cr, "image_version", lambda image: "9.9.9")
    monkeypatch.setattr(cr.ev, "load_results", lambda: [old])
    monkeypatch.setattr(cr.ev, "checkout", lambda repo_name, commit: (repo, None))
    monkeypatch.setattr(cr.ev, "tree_digest", lambda path: old["source"]["tree_digest"])
    monkeypatch.setattr(cr.par, "scan", fake_scan)
    out = tmp_path / "out"
    assert cr.main("img@sha256:00", out) == 1
    assert (out / "results.jsonl").read_text() == ""
    assert not (out / "reports").exists()
    row = json.loads((out / "rescan.json").read_text())["rows"][0]
    assert row["status"] == f"{cr.UNKNOWN_VALIDATOR}: Credential Scan"


def test_a_pr_sample_is_fixed_and_spans_repositories():
    """#233: n skills from n different repositories, the same ones every time, whatever order the records come in."""
    import random

    rows = []
    for repo in ("a/one", "b/two", "c/three", "d/four", "e/five", "f/six", "g/seven"):
        for path in ("skills/z", "skills/b", "skills/m"):
            rows.append({**rec(path, "1"), "source": {**rec(path, "1")["source"], "repo": repo}})
    got = cr.sample(rows, 5)
    assert [(r["source"]["repo"], r["source"]["skill_path"]) for r in got] == [
        ("a/one", "skills/b"),
        ("b/two", "skills/b"),
        ("c/three", "skills/b"),
        ("e/five", "skills/b"),
        ("f/six", "skills/b"),
    ]
    shuffled = rows[:]
    random.Random(7).shuffle(shuffled)
    assert cr.sample(shuffled, 5) == got
    assert len(cr.sample(rows, 50)) == 7  # never more than one per repository
    assert cr.sample([], 5) == []


def test_the_shipped_catalog_sample_is_five_repositories():
    import json

    lines = (Path(__file__).resolve().parents[1] / "catalog" / "results.jsonl").read_text().splitlines()
    got = cr.sample(cr.latest_per_skill([json.loads(x) for x in lines if x.strip()]), 5)
    assert len({r["source"]["repo"] for r in got}) == 5
