import re
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"


@pytest.mark.parametrize("name", ["env.example", "compose.yaml"])
def test_zone_a_config_names_no_model_credential(name):
    # ADR-0002 / ADR-0004: the app container's env file and compose service hold no model keys or provider settings.
    text = (DEPLOY / name).read_text()
    assert not re.search(r"API_KEY|SKILL_EVAL_LLM_|TOKEN\s*=", text)
