"""The issue forms (#145): the outside reporters' form comes first and applies only `community` and `triage`."""

import json
import re
from pathlib import Path

FORMS = Path(__file__).resolve().parents[1] / ".github" / "ISSUE_TEMPLATE"
TYPES = {"bug", "new feature", "improvement", "chore", "documentation", "security", "question"}


def labels(form: Path) -> list[str]:
    found = re.findall(r"^labels:\s*(\[.*\])\s*$", form.read_text(), flags=re.M)
    assert len(found) == 1, f"{form.name}: expected one top-level labels line"
    return json.loads(found[0])


def forms() -> list[Path]:
    return sorted(p for p in FORMS.glob("*.yml") if p.name != "config.yml")


def test_report_form_is_listed_first():
    assert forms()[0].name == "0-report.yml"


def test_report_form_presets_only_community_and_triage():
    assert labels(FORMS / "0-report.yml") == ["community", "triage"]


def test_every_team_form_carries_exactly_one_type_and_no_community_label():
    for form in forms()[1:]:
        got = labels(form)
        assert len(TYPES.intersection(got)) == 1, form.name
        assert "community" not in got and "triage" not in got, form.name


def test_report_form_points_to_private_reporting_and_warns_about_credentials():
    text = (FORMS / "0-report.yml").read_text()
    assert "SECURITY.md" in text
    assert "Never paste a real credential" in text
