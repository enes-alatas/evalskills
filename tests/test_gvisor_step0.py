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
    for parent, target in (("INPUT", "EVALS-INPUT"), ("FORWARD", "EVALS-EGRESS"), ("DOCKER-USER", "EVALS-EGRESS")):
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


@pytest.mark.parametrize("script", ["firewall.sh", "install.sh", "uninstall.sh", "preflight.sh"])
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
    assert f"-I FORWARD 1 -s {SCOPE} -j EVALS-EGRESS" in calls  # #136: not only via the main daemon's DOCKER-USER
    assert not [c for c in calls if c.startswith(("-A INPUT", "-A FORWARD", "-A DOCKER-USER"))]


def jumps(rules):
    s = SCOPE.replace("/", "_")
    return rules[f"INPUT_{s}_EVALS-INPUT"], rules[f"FORWARD_{s}_EVALS-EGRESS"], rules[f"DOCKER-USER_{s}_EVALS-EGRESS"]


def test_a_rerun_replaces_old_jumps_with_exactly_one(tmp_path):
    calls, rules = run_firewall(tmp_path, existing=2)
    assert jumps(rules) == (1, 1, 1)
    assert sum(c == f"-D INPUT -s {SCOPE} -j EVALS-INPUT" for c in calls) == 3  # two removed, then the miss


def test_a_rerun_holds_trial_traffic_closed_while_rebuilding(tmp_path):
    calls, rules = run_firewall(tmp_path, existing=1)
    s = SCOPE.replace("/", "_")
    assert [rules[f"{c}_{s}_DROP"] for c in ("INPUT", "FORWARD", "DOCKER-USER")] == [0, 0, 0]  # the hold is gone
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
    # preflight first (it changes nothing), then the rules, before the bridge and dockerd: never run without them
    assert len(pre) >= 2 and "preflight.sh" in pre[0] and "firewall.sh" in pre[1]
    assert "EnvironmentFile=-/etc/docker-evals/firewall.env" in unit


ROUTES_CLASH = "default via 192.0.2.1 dev eth0\n172.31.0.0/16 dev br-main proto kernel\n172.31.248.0/24 dev br-evals\n"
ROUTES_OK = "default via 192.0.2.1 dev eth0\n172.17.0.0/16 dev docker0\n172.31.248.0/24 dev br-evals proto kernel\n"


@pytest.mark.parametrize(("routes", "code"), [(ROUTES_CLASH, 1), (ROUTES_OK, 0)])
def test_preflight_refuses_a_range_that_overlaps_host_routes(tmp_path, routes, code):
    """Step-0 incident: the eval daemon must not start where its range overlaps another network on the host."""
    fake = {"ip": f"cat <<'R'\n{routes}R", "id": "echo 0", "runsc": "true", "pgrep": "exit 1"}
    for name, body in fake.items():
        (tmp_path / name).write_text(f"#!/bin/sh\n{body}\n")
        (tmp_path / name).chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "MAIN_DOCKER_SOCK": str(tmp_path / "none.sock")}
    p = subprocess.run(["sh", str(GVISOR / "preflight.sh")], env=env, capture_output=True, text=True)
    assert p.returncode == code, p.stderr
    if code:
        assert "172.31.0.0/16" in p.stderr


def test_install_stops_when_apt_fails():
    """Under set -e, a failure inside `a && b` does not stop the script: update and install must be separate."""
    text = (GVISOR / "install.sh").read_text()
    assert "apt-get update &&" not in text
    assert 'id -u)" = 0' in text and "preflight.sh" in text


def test_eval_daemon_writes_no_iptables_rules():
    """Step-0 incident: the second dockerd rewrote shared chains and cut the main daemon's egress."""
    cfg = json.loads((GVISOR / "daemon-evals.json").read_text())
    assert cfg["iptables"] is False and cfg["ip6tables"] is False and cfg["ipv6"] is False
    assert "icc" not in cfg  # enforced through Docker's iptables rules, so a no-op here; nobody should count on it


# Anything that would change a machine: if a guard regresses, the script fails loudly here and touches nothing.
DANGEROUS = (
    "curl",
    "gpg",
    "apt-get",
    "systemctl",
    "groupadd",
    "groupdel",
    "userdel",
    "install",
    "iptables",
    "iptables-save",
    "rm",
    "sed",
    "docker",
    "dpkg",
)


def stub_bin(tmp_path, **bodies):
    for name in DANGEROUS:
        (tmp_path / name).write_text("#!/bin/sh\nexit 99\n")
        (tmp_path / name).chmod(0o755)
    for name, body in bodies.items():
        (tmp_path / name).write_text(f"#!/bin/sh\n{body}\n")
        (tmp_path / name).chmod(0o755)
    return {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}


@pytest.mark.parametrize("script", ["install.sh", "preflight.sh"])
def test_refuses_next_to_another_dockerd(tmp_path, script):
    """#137: a second dockerd broke the main daemon's networks twice. Refuse whenever one is already running."""
    env = stub_bin(tmp_path, pgrep="exit 0", id="echo 0", runsc="true", ip="true")
    p = subprocess.run(["sh", str(GVISOR / script)], env=env, capture_output=True, text=True, cwd=tmp_path)
    assert p.returncode == 1 and "another dockerd" in p.stderr


def test_uninstall_is_a_dry_run_without_yes(tmp_path):
    """#138: it says what it touches first, and acts only with --yes."""
    env = stub_bin(tmp_path, id="echo 0", ip="exit 99")
    p = subprocess.run(["sh", str(GVISOR / "uninstall.sh")], env=env, capture_output=True, text=True, cwd=tmp_path)
    assert p.returncode == 0, p.stderr
    assert "Dry run" in p.stdout and "br-evals" in p.stdout and "EVALS-INPUT" in p.stdout


def test_uninstall_needs_root(tmp_path):
    env = stub_bin(tmp_path, id="echo 1000", ip="exit 99")
    p = subprocess.run(["sh", str(GVISOR / "uninstall.sh"), "--yes"], env=env, capture_output=True, text=True)
    assert p.returncode == 1 and "root" in p.stderr


def test_uninstall_only_touches_the_eval_range():
    text = (GVISOR / "uninstall.sh").read_text()
    deletes = [line for line in text.splitlines() if "iptables -D" in line]
    assert deletes and all('-s "$SCOPE"' in line for line in deletes)
    assert "SCOPE=172.31.240.0/20" in text
    assert "iptables-save" in text and "diff" in text


def test_uninstall_leaves_dockremap_and_a_running_daemon_alone(tmp_path):
    """#138 review: dockremap isn't uniquely ours, and stopping a running eval daemon is its own announced step."""
    text = (GVISOR / "uninstall.sh").read_text()
    assert "userdel" not in text.replace("Then: userdel", "")
    env = stub_bin(tmp_path, id="echo 0", ip="exit 99", systemctl='[ "$1" = is-active ] && exit 0; exit 99')
    p = subprocess.run(["sh", str(GVISOR / "uninstall.sh"), "--yes"], env=env, capture_output=True, text=True)
    assert p.returncode == 1 and "own announced step" in p.stderr


@pytest.mark.parametrize(
    ("mounts", "refused"),
    [
        ("overlay /var/lib/docker-evals/overlay2/abc/merged overlay rw 0 0\n", True),
        ("tmpfs /var/lib/docker-evals tmpfs rw 0 0\n", True),
        ("overlay /var/lib/docker-evals2/x overlay rw 0 0\n/dev/sda1 / ext4 rw 0 0\n", False),
    ],
)
def test_uninstall_refuses_with_mounts_under_the_data_root(tmp_path, mounts, refused):
    """#138 review: a leftover overlay mount below the data root must stop rm -rf, not only a mount on it."""
    (tmp_path / "mounts").write_text(mounts)
    env = stub_bin(tmp_path, id="echo 0", ip="exit 99", systemctl='[ "$1" = is-active ] && exit 3; exit 99')
    env["MOUNTS_FILE"] = str(tmp_path / "mounts")
    p = subprocess.run(["sh", str(GVISOR / "uninstall.sh"), "--yes"], env=env, capture_output=True, text=True)
    if refused:
        assert p.returncode == 1 and "still mounted" in p.stderr
    else:
        assert "still mounted" not in p.stderr  # it goes on, into the stubbed docker call, and stops there
