import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

import evaluate as ev  # noqa: E402

EMAIL = "someone@example.invalid"
TOKEN = "sk-test-0000-not-a-real-key"
IP = "203.0.113.77"  # TEST-NET-3
WINPATH = "C:\\Users\\fixtureuser\\file.txt"


def synthetic_report():
    """A report shaped like SkillEvaluator's, with one fake match copied into every place it copies messages."""
    pii = f"Non-placeholder email address: {EMAIL}"
    secret = f"Possible API key: {TOKEN}"
    return {
        "finding_groups": [
            {"category": "PII", "message": pii, "occurrences": [{"validator": "PII Scan", "message": pii}]},
            {"category": "SECURITY", "message": secret, "validators": ["Security Scan"]},
            {"category": "SCHEMA", "message": "Author not specified in metadata", "suggestion": f"Use <{EMAIL}>"},
        ],
        "results": [
            {
                "validator": "PII Scan",
                "findings": [{"message": pii, "line_content": f"mail {EMAIL}", "file_path": "SKILL.md"}],
                "occurrences": [
                    {"message": pii, "matched_value": EMAIL},
                    {"message": f"Non-RFC1918 IP address: {IP}", "matched_value": IP, "file_path": "SKILL.md"},
                    {"message": "Personal Windows user path", "matched_value": WINPATH, "new_field": WINPATH},
                    {"check": "pii_detection", "future_list": [IP]},
                ],
                "occurrences_grouped": {"emails": [{"message": pii}]},
                "legacy": {
                    "errors": [f"[PII-HIGH] {pii} in SKILL.md:59"],
                    "warnings": [f"[PII-LOW] {pii}"],
                    "messages": [f"[OK] pii_scan_start: contact {EMAIL}", f"[OK] pii_detection: saw {IP}"],
                },
            },
            {
                "validator": "Secrets Detection",
                "findings": [{"message": secret, "match": TOKEN}],
                "legacy": {"errors": [f"[SECRET-CRITICAL] {secret} in run.sh:3"]},
            },
            {"validator": "Security Scan", "legacy": {"errors": [f"[SECURITY-HIGH] Hardcoded token: {TOKEN}"]}},
        ],
    }


def test_no_match_survives_anywhere():
    out = json.dumps(ev.sanitize_report(synthetic_report()))
    for match in (EMAIL, TOKEN, IP, "fixtureuser"):
        assert match not in out


def test_unknown_scanner_fields_fail_closed():
    occ = ev.sanitize_report(synthetic_report())["results"][0]["occurrences"]
    assert occ[1]["matched_value"] == "[omitted]" and occ[1]["file_path"] == "SKILL.md"
    assert occ[2]["new_field"] == "[omitted]"
    assert occ[3] == {"check": "pii_detection", "future_list": ["[omitted]"]}


def test_an_unreadable_public_report_is_removed_not_fatal(tmp_path):
    good, bad = tmp_path / "a" / "r1", tmp_path / "b" / "r2"
    good.mkdir(parents=True)
    bad.mkdir(parents=True)
    (good / "report.public.json").write_text(json.dumps(synthetic_report()))
    (bad / "report.public.json").write_text('{"truncated": ')
    assert ev.resanitize_public(tmp_path) == 1
    assert not (bad / "report.public.json").exists()
    assert IP not in (good / "report.public.json").read_text()


def test_titles_rules_and_locations_are_kept():
    pub = ev.sanitize_report(synthetic_report())
    pii = pub["results"][0]
    assert pii["findings"][0]["message"] == "Non-placeholder email address: [matched text omitted]"
    assert pii["findings"][0]["file_path"] == "SKILL.md"
    assert pii["legacy"]["errors"][0] == "[PII-HIGH] Non-placeholder email address: [matched text omitted]"
    assert pub["finding_groups"][2]["message"] == "Author not specified in metadata"


def test_sanitizing_twice_changes_nothing():
    once = ev.sanitize_report(synthetic_report())
    assert ev.sanitize_report(once) == once
