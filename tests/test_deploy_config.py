import re
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"


@pytest.mark.parametrize("name", ["env.example", "compose.yaml"])
def test_zone_a_config_names_no_model_credential(name):
    # ADR-0002 / ADR-0004: the app container's env file and compose service hold no model keys or provider settings.
    text = (DEPLOY / name).read_text()
    assert not re.search(r"API_KEY|SKILL_EVAL_LLM_|TOKEN\s*=", text)


def test_app_network_is_outside_the_trial_and_egress_ranges():
    # #99, #137: the app's own bridge must not fall inside the trial pool or the services' egress network, or the
    # step 4 check refuses the host and the socket proxy could hand a trial the app's range.
    import ipaddress

    common = (DEPLOY / "gvisor" / "host" / "common.sh").read_text()
    ranges = [ipaddress.ip_network(re.search(rf"^{k}=([\d./]+)", common, re.M).group(1)) for k in ("POOL", "EGRESS")]
    compose = (DEPLOY / "compose.yaml").read_text()
    subnet = ipaddress.ip_network(re.search(r"EVALSKILLS_SUBNET:-([\d./]+)", compose).group(1))
    gateway = ipaddress.ip_address(re.search(r"gateway: \"\$\{EVALSKILLS_GATEWAY:-([\d.]+)\}", compose).group(1))
    assert not any(subnet.overlaps(r) for r in ranges)
    assert gateway in subnet
    assert f"EVALSKILLS_GATEWAY:-{gateway}}}/32" in compose  # TRUSTED_PROXIES follows the gateway
