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
