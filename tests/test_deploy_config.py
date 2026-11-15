import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"


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


# Base images pinned by digest, each watched by Dependabot (#178). deploy/live joins with #183.
PINNED_DOCKERFILES = ["deploy/Dockerfile", "deploy/live/Dockerfile"]


@pytest.mark.parametrize("path", PINNED_DOCKERFILES)
def test_base_images_are_pinned_by_digest_and_watched(path):
    froms = [line for line in (ROOT / path).read_text().splitlines() if line.startswith("FROM ")]
    assert froms and all(re.fullmatch(r"FROM \S+@sha256:[0-9a-f]{64}( AS \S+)?", f) for f in froms), froms
    dependabot = (ROOT / ".github/dependabot.yml").read_text()
    assert re.search(r'directories: \[[^\]]*"/' + re.escape(str(Path(path).parent)) + r'"', dependabot)


def test_dependabot_never_proposes_a_new_python_minor_or_major():
    dependabot = (ROOT / ".github/dependabot.yml").read_text()
    docker = dependabot[dependabot.index("package-ecosystem: docker") :]
    rule = re.search(r"- dependency-name: python\n\s+update-types: \[([^\]]*)\]", docker)
    assert rule and {"version-update:semver-major", "version-update:semver-minor"} <= set(
        re.findall(r'"([^"]+)"', rule[1])
    )
