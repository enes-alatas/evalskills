import re
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"


@pytest.mark.parametrize("name", ["env.example", "compose.yaml"])
def test_zone_a_config_names_no_model_credential(name):
    # ADR-0002 / ADR-0004: the app container's env file and compose service hold no model keys or provider settings.
    text = (DEPLOY / name).read_text()
    assert not re.search(r"API_KEY|SKILL_EVAL_LLM_|TOKEN\s*=", text)


def test_app_network_is_outside_the_eval_firewall_scope():
    # #99: firewall.sh sends everything from 172.31.240.0/20 to EVALS-EGRESS (model gateway only) and drops it at
    # INPUT. The app's own bridge must not fall inside that range, or its clones and the host's proxy break.
    import ipaddress

    scope = re.search(r"-s (\d+\.\d+\.\d+\.\d+/\d+) -j EVALS-EGRESS", (DEPLOY / "gvisor" / "firewall.sh").read_text())
    assert scope, "firewall.sh no longer names its scope in the expected form"
    scope = ipaddress.ip_network(scope.group(1))
    compose = (DEPLOY / "compose.yaml").read_text()
    subnet = ipaddress.ip_network(re.search(r"EVALSKILLS_SUBNET:-([\d./]+)", compose).group(1))
    gateway = ipaddress.ip_address(re.search(r"gateway: \"\$\{EVALSKILLS_GATEWAY:-([\d.]+)\}", compose).group(1))
    assert not subnet.overlaps(scope)
    assert gateway in subnet
    assert f"EVALSKILLS_GATEWAY:-{gateway}}}/32" in compose  # TRUSTED_PROXIES follows the gateway
