"""The skills gate still refuses what it claims to (scripts/skill_gate.py), on synthetic reports."""

import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("skill_gate", Path(__file__).parent.parent / "scripts" / "skill_gate.py")
skill_gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(skill_gate)

GATED = ["QUALITY", "Secrets Detection", "PII Scan", "Unicode Smuggling Detection"]


def report(name, **overrides):
    statuses = {validator: "passed" for validator in GATED}
    statuses.update({"Schema & Repository Governance": "failed", "Security Scan": "incomplete"})
    statuses.update(overrides)
    return {
        "skills": [{"name": name}],
        "quality_summary": [{"overall_score": 88.0}],
        "results": [{"validator": validator, "status": status} for validator, status in statuses.items() if status],
        "finding_groups": [
            {"severity": "high", "validators": ["Security Scan"], "check_name": "tool_misuse", "message": "x\ny"}
        ],
    }


def layout(tmp_path, reports, skills=None):
    skills_dir, reports_dir = tmp_path / "skills", tmp_path / "reports"
    for name in skills if skills is not None else [r["skills"][0]["name"] for r in reports]:
        (skills_dir / name).mkdir(parents=True)
        (skills_dir / name / "SKILL.md").write_text("---\nname: x\n---\n")
    for index, body in enumerate(reports):
        (reports_dir / f"skill-{index}").mkdir(parents=True)
        (reports_dir / f"skill-{index}" / "skillevaluator-tier1.json").write_text(json.dumps(body))
    return reports_dir, skills_dir


def test_passes_when_only_advisory_validators_fail(tmp_path):
    code, summary, _ = skill_gate.gate(*layout(tmp_path, [report("a"), report("b")]))
    assert code == 0
    assert "| a | 88.0 | passed |" in summary
    assert "a: tool_misuse: x" in summary


@pytest.mark.parametrize(
    ("overrides", "problem"),
    [
        ({"Secrets Detection": "failed"}, "Secrets Detection: failed"),
        ({"QUALITY": "failed"}, "QUALITY: failed"),
        ({"PII Scan": "skipped"}, "PII Scan: skipped"),
        ({"Unicode Smuggling Detection": "error"}, "Unicode Smuggling Detection: error"),
        ({"A Validator Added Later": "failed"}, "A Validator Added Later: failed"),
        ({"Security Scan": None}, "Security Scan: missing"),
        ({"Security Scan": "error"}, "Security Scan: error"),
    ],
)
def test_refuses(tmp_path, overrides, problem):
    code, summary, log = skill_gate.gate(*layout(tmp_path, [report("a", **overrides)]))
    assert code == 1
    assert problem in summary
    assert any(line.startswith("::error") for line in log)


def test_refuses_a_crashed_scanner_even_under_an_advisory_verdict(tmp_path):
    body = report("a")
    body["results"][-1]["legacy"] = {"errors": ["skillspector: unexpected exit code 1"]}
    code, summary, _ = skill_gate.gate(*layout(tmp_path, [body]))
    assert code == 1
    assert "scanner crashed" in summary


def test_broken_when_a_skill_has_no_report(tmp_path):
    with pytest.raises(skill_gate.Broken, match="2 skills but tier 1 left 1"):
        skill_gate.gate(*layout(tmp_path, [report("a")], skills=["a", "b"]))


def test_broken_when_a_report_is_malformed(tmp_path):
    reports_dir, skills_dir = layout(tmp_path, [{"results": []}], skills=["a"])
    with pytest.raises(skill_gate.Broken, match="not a tier 1 report"):
        skill_gate.gate(reports_dir, skills_dir)


def test_main_exit_codes(tmp_path):
    reports_dir, skills_dir = layout(tmp_path, [report("a")])
    summary = tmp_path / "summary.md"
    assert skill_gate.main(["gate", str(reports_dir), str(skills_dir), str(summary)]) == 0
    assert "### Skills" in summary.read_text()
    assert skill_gate.main(["gate", str(tmp_path / "nothing"), str(skills_dir)]) == 2
