"""#121/#123: the live pilot's gateway and egress proxy deploy files agree with each other and with the socket proxy."""

import ipaddress
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT / "deploy" / "live"
sys.path.insert(0, str(ROOT / "gateway"))
import docker_proxy as dp  # noqa: E402
import egress_proxy as ep  # noqa: E402

import gateway as gw  # noqa: E402

COMPOSE = (LIVE / "compose.yaml").read_text()


def service(name: str) -> str:
    """The text of one top-level service block in compose.yaml."""
    m = re.search(rf"^  {name}:\n((?:    .*\n|\s*\n)+)", COMPOSE, re.M)
    assert m, f"no service {name}"
    return m.group(1)


def test_services_sit_where_the_socket_proxy_and_the_host_look_for_them():
    g = gw.load_config(str(LIVE / "gateway.json"))
    p = ep.load_config(str(LIVE / "egress-proxy.json"))
    # they listen on their own interfaces: the egress network and the trial networks the socket proxy attaches
    assert g["listen_host"] == p["listen_host"] == "0.0.0.0"
    assert "network_mode" not in COMPOSE and "ports:" not in COMPOSE  # off host networking, nothing published
    assert "ipv4_address: 172.31.248.2" in service("gateway")
    assert "ipv4_address: 172.31.248.3" in service("egress-proxy")
    assert "subnet: 172.31.248.0/24" in COMPOSE
    # on the egress network and every trial network: neither may route between them (the anchor applies to both)
    assert 'sysctls: { net.ipv4.ip_forward: "0" }' in COMPOSE.split("services:")[0]
    assert "<<: *hardening" in service("gateway") and "<<: *hardening" in service("egress-proxy")
    proxy_cfg = json.loads((ROOT / "deploy/gvisor/host/docker-proxy.json").read_text())
    services = {**dp.DEFAULTS["services"], **proxy_cfg.get("services", {})}
    assert services == {"evals-gateway": "evalskills-gateway", "evals-proxy": "evalskills-egress-proxy"}
    assert "container_name: evalskills-gateway " in service("gateway")
    assert "container_name: evalskills-egress-proxy\n" in service("egress-proxy")
    host = ROOT / "deploy" / "gvisor" / "host"
    client = (host / "docker-client-config.json").read_text()  # trials reach the proxy by the name the proxy sets
    assert f"http://evals-proxy:{p['listen_port']}" in client
    common = (host / "common.sh").read_text()  # step 3 names the gateway's egress address for the host side
    assert "GATEWAY_ADDR=172.31.248.2 " in common and "GATEWAY_HOST=evals-gateway " in common
    pool = ipaddress.ip_network(dp.DEFAULTS["network_pool"])
    assert any(pool.subnet_of(n) for n in p["allowed_clients"])  # the proxy serves trials
    assert not any(ipaddress.ip_network("172.31.248.0/24").overlaps(n) for n in p["allowed_clients"])  # not the host


def test_only_the_gateway_gets_the_key():
    # ADR-0008 (#218): a host-set env var, required by compose, for the gateway alone; no mounted file any more
    assert re.search(
        r"\n    environment:\n      GATEWAY_MODEL_KEY: \$\{GATEWAY_MODEL_KEY\?[^}]+\}\n", service("gateway")
    )
    assert COMPOSE.count("GATEWAY_MODEL_KEY: ") == 1
    assert "environment:" not in service("egress-proxy") and "secret" not in service("egress-proxy")
    assert "secrets:" not in COMPOSE and "/run/secrets" not in COMPOSE
    assert "key_file" not in gw.load_config(str(LIVE / "gateway.json"))
    assert not re.search(r"API_KEY|nvapi-", COMPOSE)  # the value itself is never in the repo


def test_both_services_are_hardened():
    for name in ("x-hardening",):
        block = COMPOSE.split(f"{name}:", 1)[1].split("\nservices:", 1)[0]
        for line in ("read_only: true", "cap_drop: [ALL]", 'security_opt: ["no-new-privileges:true"]', "pids_limit:"):
            assert line in block
    for name in ("gateway", "egress-proxy"):
        block = service(name)
        assert "<<: *hardening" in block
        assert re.search(r'user: "1000[23]:1000[23]"', block)
    assert "docker.sock" not in COMPOSE


def test_gateway_state_dir_is_owned_by_the_gateway_user():
    dockerfile = (LIVE / "Dockerfile").read_text()
    assert "install -d -o 10002 -g 10002 -m 0700 /var/lib/evalskills-gateway" in dockerfile
    assert gw.load_config(str(LIVE / "gateway.json"))["state_db"].startswith("/var/lib/evalskills-gateway/")


def test_proxy_allowlist_is_download_hosts_only():
    hosts = ep.load_config(str(LIVE / "egress-proxy.json"))["allowed_hosts"]
    assert "integrate.api.nvidia.com" not in hosts  # model calls go through the gateway, with a token
    assert "github.com" in hosts  # nvm's install.sh git-clones nvm from it (#231)
    assert not [h for h in hosts if h.endswith(".github.com")]  # no API, no other subdomain


def test_open_egress_is_the_pilots_alone():
    # On for the #121 pilot only (#236, ADR-0003 amended 2026-11-20). Turning it off, or keeping it for anything
    # beyond the pilot, changes this test and the ADR together.
    assert ep.load_config(str(LIVE / "egress-proxy.json"))["open_egress"] is True
    assert "open egress for the #121 pilot only" in (ROOT / "docs/adr/0003-gvisor-eval-daemon.md").read_text()


def test_proxy_serves_the_trial_range_only():
    clients = ep.load_config(str(LIVE / "egress-proxy.json"))["allowed_clients"]
    assert clients == [ipaddress.ip_network(dp.DEFAULTS["network_pool"])]  # the trial networks, not the egress one
    app = re.search(r"EVALSKILLS_SUBNET:-([\d./]+)", (ROOT / "deploy" / "compose.yaml").read_text()).group(1)
    assert not any(ipaddress.ip_network(app).overlaps(c) for c in clients)  # the public app never gets the proxy


def test_the_documented_pilot_model_is_the_one_the_gateway_allows():
    """#252: NVIDIA retired the first pilot model; every place that names the model must move together."""
    allowed = gw.load_config(str(LIVE / "gateway.json"))["allowed_models"]
    assert gw.load_config(str(ROOT / "gateway" / "config.example.json"))["allowed_models"] == allowed
    for doc in (LIVE / "README.md", ROOT / "pipeline" / "live_pilot.py"):
        named = set(re.findall(r"(?:TIER3_AGENT_MODEL|SKILL_EVAL_LLM_MODEL)=(\S+)", doc.read_text()))
        assert named and named <= set(allowed), doc
