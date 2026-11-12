"""#123: the gVisor eval daemon's drafts, checked without a host. firewall.sh runs against a stub iptables."""

import json
import os
import subprocess
from pathlib import Path

import pytest

GVISOR = Path(__file__).resolve().parents[1] / "deploy" / "gvisor"
SCOPE = "172.31.240.0/20"

# A stub iptables that keeps a count of each rule inserted at the top of INPUT or DOCKER-USER, so reruns and
# removals are checked against state, not just against the calls made.
STUB = """#!/bin/sh
echo "$*" >> "$IPT_LOG"
key() { echo "$IPT_STATE/$(echo "$2_$4_$6" | tr '/ ' '__')"; }
case "$1" in
  -N) [ "$2" = DOCKER-USER ] && exit 1; exit 0 ;;  # DOCKER-USER already exists, as dockerd made it
  -I) [ "$3" = 1 ] && set -- "$1" "$2" "$4" "$5" "$6" "$7"
      f=$(key "$@"); n=$(cat "$f" 2>/dev/null || echo 0); echo $((n + 1)) > "$f" ;;
  -D) f=$(key "$@"); n=$(cat "$f" 2>/dev/null || echo 0); [ "$n" -gt 0 ] || exit 1; echo $((n - 1)) > "$f" ;;
esac
exit 0
"""


def run_firewall(tmp_path, existing=0, **env):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "iptables"
    stub.write_text(STUB)
    stub.chmod(0o755)
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    for parent, target in (("INPUT", "EVALS-INPUT"), ("DOCKER-USER", "EVALS-EGRESS")):
        (state / f"{parent}_{SCOPE.replace('/', '_')}_{target}").write_text(str(existing))  # jumps left by earlier runs
    log = tmp_path / "iptables.log"
    log.write_text("")
    clean = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "IPT_LOG": str(log), "IPT_STATE": str(state), **env}
    subprocess.run(["sh", str(GVISOR / "firewall.sh")], env=clean, check=True, timeout=30)
    rules = {p.name: int(p.read_text()) for p in state.iterdir()}
    return log.read_text().splitlines(), rules


def chain(calls, name):
    return [c for c in calls if c.startswith(f"-A {name} ")]


def test_daemon_uses_its_own_containerd_namespaces():
    # Without these the second dockerd shares the system containerd's "moby" namespace with the main daemon.
    cfg = json.loads((GVISOR / "daemon-evals.json").read_text())
    assert cfg["containerd-namespace"] not in ("", "moby")
    assert cfg["containerd-plugins-namespace"] not in ("", "plugins.moby")
    assert cfg["containerd-namespace"] != cfg["containerd-plugins-namespace"]
    assert cfg["default-runtime"] == "runsc"


@pytest.mark.parametrize("script", ["firewall.sh", "install.sh"])
def test_scripts_parse(script):
    subprocess.run(["sh", "-n", str(GVISOR / script)], check=True, timeout=30)


def test_no_addresses_means_deny_all(tmp_path):
    calls, _ = run_firewall(tmp_path)
    for name in ("EVALS-EGRESS", "EVALS-INPUT"):
        rules = chain(calls, name)
        assert rules[0].endswith("--ctstate ESTABLISHED,RELATED -j ACCEPT")
        assert rules[-1] == f"-A {name} -j DROP"
        assert len(rules) == 2, rules  # nothing else is allowed


def test_jumps_come_first_so_no_earlier_accept_wins(tmp_path):
    calls, _ = run_firewall(tmp_path)
    assert f"-I INPUT 1 -s {SCOPE} -j EVALS-INPUT" in calls
    assert f"-I DOCKER-USER 1 -s {SCOPE} -j EVALS-EGRESS" in calls
    assert not [c for c in calls if c.startswith("-A INPUT") or c.startswith("-A DOCKER-USER")]


def jumps(rules):
    s = SCOPE.replace("/", "_")
    return rules[f"INPUT_{s}_EVALS-INPUT"], rules[f"DOCKER-USER_{s}_EVALS-EGRESS"]


def test_a_rerun_replaces_old_jumps_with_exactly_one(tmp_path):
    calls, rules = run_firewall(tmp_path, existing=2)
    assert jumps(rules) == (1, 1)
    assert sum(c == f"-D INPUT -s {SCOPE} -j EVALS-INPUT" for c in calls) == 3  # two removed, then the miss


def test_a_rerun_holds_trial_traffic_closed_while_rebuilding(tmp_path):
    calls, rules = run_firewall(tmp_path, existing=1)
    s = SCOPE.replace("/", "_")
    assert rules[f"INPUT_{s}_DROP"] == rules[f"DOCKER-USER_{s}_DROP"] == 0  # the hold is gone afterwards
    hold = calls.index(f"-I INPUT 1 -s {SCOPE} -j DROP")
    flush = next(i for i, c in enumerate(calls) if c.startswith("-F EVALS-") or c.startswith("-N EVALS-"))
    release = calls.index(f"-D INPUT -s {SCOPE} -j DROP")
    jump_in = calls.index(f"-I INPUT 1 -s {SCOPE} -j EVALS-INPUT")
    assert hold < flush < jump_in < release  # dropped, never open, until the new chains are in place


def test_only_configured_addresses_are_allowed(tmp_path):
    calls, _ = run_firewall(tmp_path, GATEWAY_IP="172.31.248.1", GATEWAY_PORT="8443")
    for name in ("EVALS-EGRESS", "EVALS-INPUT"):
        rules = chain(calls, name)
        assert f"-A {name} -p tcp -d 172.31.248.1 --dport 8443 -j ACCEPT" in rules
        assert len(rules) == 3  # replies, the gateway, drop; no proxy rule when PROXY_IP is unset
        assert rules[-1] == f"-A {name} -j DROP"


def test_no_dns_rule(tmp_path):
    calls, _ = run_firewall(tmp_path, GATEWAY_IP="172.31.248.1", PROXY_IP="172.31.248.1")
    assert not [c for c in calls if "--dport 53" in c or "-p udp" in c]


def test_unit_applies_the_rules_before_every_start():
    unit = (GVISOR / "docker-evals.service").read_text()
    pre = [line for line in unit.splitlines() if line.startswith("ExecStartPre=")]
    assert pre and "firewall.sh" in pre[0]  # first, so the daemon never runs without the rules
    assert "EnvironmentFile=-/etc/docker-evals/firewall.env" in unit
