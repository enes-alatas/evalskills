"""Gate over SkillEvaluator Tier 1 reports for the skills in this repo (issue #10).

SkillEvaluator's own verdict fails every skill here for reasons about format, not safety: the Schema & Repository
Governance rules, and SkillSpector's `incomplete` analysis or pattern hits on skills that teach git and security
procedures. So the container's exit code is not the gate. This script is.

Rule: every validator in each skill's report must be `passed`, except the ADVISORY ones. Their status and their
high/critical findings go to the summary for the reviewer to read. An advisory validator must still have run, with a
verdict of passed, failed or incomplete. A scanner that crashed (SkillEvaluator records "unexpected exit code")
fails. A validator not on the advisory list gates, including one a later image adds, so an image bump fails closed.
A run that reported fewer skills than the tree holds fails. Structured fields only: the text of a finding is
never matched.

Usage: python3 -I scripts/skill_gate.py <reports-dir> <skills-dir> [summary-file]
Exit 0 when every gated validator passed, 1 when one did not, 2 when the reports are missing or malformed.
"""

import json
import sys
from pathlib import Path

ADVISORY = ("Schema & Repository Governance", "Security Scan")
ADVISORY_VERDICTS = ("passed", "failed", "incomplete")


def first_line(text):
    return (str(text or "").splitlines() or [""])[0][:200]


class Broken(Exception):
    """The reports cannot be judged."""


def judge(report):
    """Return (name, score, gate problems, advisory statuses, notable advisory findings) for one report."""
    results = report.get("results")
    if not isinstance(results, list) or not results:
        raise Broken("no results")
    skills = report.get("skills") or [{}]
    name = str(skills[0].get("name", "?"))
    quality = report.get("quality_summary") or [{}]
    score = quality[0].get("overall_score", "?") if isinstance(quality, list) else "?"
    problems, advisory = [], []
    by_name = {}
    for result in results:
        by_name.setdefault(result.get("validator"), []).append(result)
        validator, status = result.get("validator"), result.get("status")
        crashed = [
            error for error in (result.get("legacy") or {}).get("errors") or [] if "unexpected exit code" in str(error)
        ]
        if crashed:
            problems.append(f"{validator}: scanner crashed")
        if validator in ADVISORY:
            advisory.append(f"{validator}: {status}")
            if status not in ADVISORY_VERDICTS:
                problems.append(f"{validator}: {status}")
        elif status != "passed":
            problems.append(f"{validator}: {status}")
    problems += [f"{validator}: missing" for validator in ADVISORY if validator not in by_name]
    notable = [
        f"{name}: {group.get('check_name') or group.get('category')}: {first_line(group.get('message'))}"
        for group in report.get("finding_groups") or []
        if group.get("severity") in ("critical", "high") and set(group.get("validators") or []) & set(ADVISORY)
    ]
    return name, score, problems, advisory, notable


def gate(reports_dir, skills_dir):
    """Return (exit code, summary markdown, log lines)."""
    want = len(list(Path(skills_dir).glob("*/SKILL.md")))
    files = sorted(Path(reports_dir).glob("skill-*/skillevaluator-tier1.json"))
    if len(files) != want:
        raise Broken(f"the tree holds {want} skills but tier 1 left {len(files)} reports")
    rows, notes, log, failed = [], [], [], False
    for path in files:
        try:
            name, score, problems, advisory, notable = judge(json.loads(path.read_text()))
        except (json.JSONDecodeError, AttributeError, TypeError, Broken) as error:
            raise Broken(f"{path} is not a tier 1 report ({error})") from error
        verdict = "passed" if not problems else "; ".join(problems)
        rows.append(f"| {name} | {score} | {verdict} | {'; '.join(advisory)} |")
        notes += notable
        if problems:
            failed = True
            log.append(f"::error title=skill tier 1::{name}: {verdict}")
        else:
            log.append(f"{name}: passed (quality {score}; advisory: {'; '.join(advisory)})")
    summary = "\n".join(
        [
            "### Skills: SkillEvaluator Tier 1",
            "",
            "| skill | quality | gate | advisory |",
            "| --- | --- | --- | --- |",
            *rows,
            "",
            "High and critical advisory findings, for the reviewer to read (not gated):",
            "",
            *(["- " + note for note in notes] or ["none"]),
            "",
        ]
    )
    return (1 if failed else 0), summary, log


def main(argv):
    if len(argv) not in (3, 4):
        print(__doc__.strip().splitlines()[-2], file=sys.stderr)
        return 2
    try:
        code, summary, log = gate(argv[1], argv[2])
    except Broken as error:
        print(f"::error title=skill tier 1::{error}")
        return 2
    print("\n".join(log))
    if len(argv) == 4:
        with open(argv[3], "a") as handle:
            handle.write(summary)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
