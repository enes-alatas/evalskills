"""#121/#123: the live pilot's gateway and egress proxy deploy files agree with each other and with the firewall."""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT / "deploy" / "live"
sys.path.insert(0, str(ROOT / "gateway"))
import egress_proxy as ep  # noqa: E402

import gateway as gw  # noqa: E402

COMPOSE = (LIVE / "compose.yaml").read_text()


def service(name: str) -> str:
    """The text of one top-level service block in compose.yaml."""
    m = re.search(rf"^  {name}:\n((?:    .*\n|\s*\n)+)", COMPOSE, re.M)
    assert m, f"no service {name}"
    return m.group(1)


def test_configs_load_and_listen_on_the_eval_bridge():
    g = gw.load_config(str(LIVE / "gateway.json"))
    p = ep.load_config(str(LIVE / "egress-proxy.json"))
    assert g["listen_host"] == p["listen_host"] == "172.31.248.1"  # br-evals, from docker-evals.service
    assert "172.31.248.1/24" in (ROOT / "deploy" / "gvisor" / "docker-evals.service").read_text()
    env = (ROOT / "deploy" / "gvisor" / "firewall.env").read_text()
    assert f"#GATEWAY_PORT={g['listen_port']}" in env and f"#PROXY_PORT={p['listen_port']}" in env


def test_only_the_gateway_gets_the_key():
    assert "secrets: [model_key]" in service("gateway")
    assert "secret" not in service("egress-proxy")
    assert gw.load_config(str(LIVE / "gateway.json"))["key_file"] == "/run/secrets/model_key"
    assert not re.search(r"API_KEY|nvapi-|environment:", COMPOSE)  # the key is a file, never an env var (ADR-0004)


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
    assert not [h for h in hosts if "github.com" == h or h.endswith(".github.com")]  # raw content only, no API


def test_proxy_serves_the_trial_range_only():
    import ipaddress

    clients = ep.load_config(str(LIVE / "egress-proxy.json"))["allowed_clients"]
    assert clients == [ipaddress.ip_network("172.31.240.0/20")]  # firewall.sh's scope: trials and the host's own bridge
    app = re.search(r"EVALSKILLS_SUBNET:-([\d./]+)", (ROOT / "deploy" / "compose.yaml").read_text()).group(1)
    assert not any(ipaddress.ip_network(app).overlaps(c) for c in clients)  # the public app never gets the proxy
