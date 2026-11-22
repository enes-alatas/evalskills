"""#161: deploy-smoke skips, never passes, when GitHub rate-limits CI's lookup, and fails as before on anything else."""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="the smoke script needs jq, as CI's runner has")

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "scripts" / "deploy-smoke.sh").read_text()


def helper() -> str:
    """The script's RATE_LIMITED line and skip_if_rate_limited function, as written there."""
    line = re.search(r"^RATE_LIMITED=.*$", SCRIPT, re.M).group(0)
    func = re.search(r"^skip_if_rate_limited\(\) \{\n.*?^\}$", SCRIPT, re.M | re.S).group(0)
    return line + "\n" + func + "\n"


def run(code: str, body: str, tmp_path: Path):
    summary = tmp_path / "summary.md"
    script = helper() + f"CODE={code!r}\nBODY={body!r}\nskip_if_rate_limited discover\necho continued\n"
    p = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={**os.environ, "GITHUB_STEP_SUMMARY": str(summary)},
    )
    return p, (summary.read_text() if summary.exists() else "")


def test_the_message_matches_what_discover_says():
    discover = (ROOT / "pipeline" / "discover.py").read_text()
    msg = re.search(r'^RATE_LIMITED="(.*)"$', SCRIPT, re.M).group(1)
    assert f'DiscoveryError("{msg}")' in discover


def test_a_rate_limit_answer_is_a_named_skip(tmp_path):
    p, summary = run("400", '{"error": "GitHub is rate-limiting this server; please try again later"}', tmp_path)
    assert p.returncode == 0 and "continued" not in p.stdout  # stops here: nothing after it runs
    assert "::warning::deploy smoke skipped: rate-limited" in p.stdout
    assert "were not tested" in summary and "passed:" not in summary


@pytest.mark.parametrize(
    ("code", "body"),
    [
        ("400", '{"error": "Repository not found or not public"}'),
        ("200", '{"skills": []}'),
        ("502", '{"error": "GitHub is rate-limiting this server; please try again later"}'),
        ("400", "not json"),
    ],
)
def test_any_other_answer_goes_on_to_the_checks(tmp_path, code, body):
    p, summary = run(code, body, tmp_path)
    assert p.returncode == 0 and p.stdout.strip() == "continued" and summary == ""
