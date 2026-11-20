"""#137 (PR C2): the host step scripts in deploy/gvisor/host/, run against stub binaries and a temporary prefix.

Every command a step could change a machine with is a stub that logs its arguments and keeps a little state (users,
groups, files). Paths go under $EVALS_PREFIX. So these tests never touch the real system, whoever runs them.
"""

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOST = ROOT / "deploy" / "gvisor" / "host"
STEPS = sorted(p.name for p in HOST.glob("[1-9]-*.sh") if not p.name.endswith("-undo.sh"))
READ_ONLY = {"4-check.sh"}  # changes nothing, so it has no undo
UNDOS = [s.replace(".sh", "-undo.sh") for s in STEPS if s not in READ_ONLY]
IMAGE_ID = "sha256:" + "ab" * 32  # synthetic
LIVE_CONFIGS = [("evalskills-gateway", "gateway.json"), ("evalskills-egress-proxy", "egress-proxy.json")]

# One stub for every command; its behaviour is picked by the name it was called as.
STUB = r"""#!/bin/sh
n=$(basename "$0")
echo "$n $*" >> "$STUB_LOG"
S="$STUB_STATE"
case "$n" in
  id)
    case "$1" in
      -u) echo "${STUB_UID:-0}" ;;
      -nG) cat "$S/groups_of_$2" 2>/dev/null || echo "$2" ;;
      *) echo "uid=10010($1)" ;;
    esac ;;
  getent)
    grep -E "^$2:|^[^:]*:[^:]*:$2:" "$S/$1" 2>/dev/null || exit 2 ;;
  groupadd) echo "$2:x:999:" >> "$S/group" ;;
  useradd)
    uid=""; groups=""; name=""
    while [ $# -gt 0 ]; do
      case "$1" in --uid) uid=$2; shift ;; --groups) groups=$2; shift ;;
        --home-dir|--shell|--comment) shift ;; --*) ;; *) name=$1 ;; esac
      shift
    done
    echo "$name:x:$uid:$uid::/var/lib/evals:/usr/sbin/nologin" >> "$S/passwd"
    echo "$name:x:$uid:" >> "$S/group"
    echo "$name $groups" > "$S/groups_of_$name"
    sed -i "s/^$groups:x:999:$/$groups:x:999:$name/" "$S/group" ;;
  userdel)
    sed -i "/^$1:/d" "$S/passwd"; sed -i "/^$1:/d" "$S/group"; sed -i "s/:$1$/:/" "$S/group" ;;
  groupdel) sed -i "/^$1:/d" "$S/group" ;;
  install)
    dir=0; last=""
    for a in "$@"; do case "$a" in -d) dir=1 ;; esac; done
    while [ $# -gt 0 ]; do
      case "$1" in -o|-g|-m) shift 2; continue ;; -d) shift; continue ;; esac
      if [ "$dir" = 1 ]; then mkdir -p "$1"; else set -- "$@"; break; fi
      shift
    done
    if [ "$dir" = 0 ]; then
      for last in "$@"; do :; done
      while [ $# -gt 1 ]; do
        if [ -d "$last" ]; then cp "$1" "$last/"; else cp "$1" "$last"; fi
        shift
      done
    fi ;;
  fallocate) : > "$3" ;;
  mountpoint) [ -e "$S/mounted" ]; exit ;;
  systemctl)
    case "$1" in
      is-active) [ "$2" = -q ] && shift; [ -e "$S/active_$2" ]; exit ;;
      enable) [ "$2" = --now ] && touch "$S/mounted" ;;
      disable) rm -f "$S/mounted" ;;
    esac ;;
  iptables-save) cat "$S/rules_$(cat "$S/saves" 2>/dev/null || echo 0)" 2>/dev/null
    echo $(( $(cat "$S/saves" 2>/dev/null || echo 0) + 1 )) > "$S/saves" ;;
  iptables) t=; [ "$1" = -t ] && { t="$2_"; shift 2; }
    [ "$1" = -D ] && { f="$S/jumps_$t$2"; n=$(cat "$f" 2>/dev/null || echo 0); [ "$n" -gt 0 ] || exit 1;
    echo $((n - 1)) > "$f"; } ; exit 0 ;;
  docker)
    case "$1" in
      ps) cat "$S/ps_$(cat "$S/pss" 2>/dev/null || echo 0)" 2>/dev/null
        echo $(( $(cat "$S/pss" 2>/dev/null || echo 0) + 1 )) > "$S/pss" ;;
      image) [ "$2" = inspect ] && { [ -e "$S/image" ] || exit 1; echo "$STUB_IMAGE_ID"; } ;;
      load) cat > /dev/null; touch "$S/image" ;;
      version) echo "${STUB_DOCKER_VERSION:-29.6.1}" ;;
      exec) cat "$S/exec_$2" ;;
      compose) echo "compose env EVALSKILLS_HOST_ADDRS=${EVALSKILLS_HOST_ADDRS:-}" >> "$STUB_LOG" ;;
      info) [ -e "$S/no_runsc" ] && echo '{"runc":{}}' || echo '{"runc":{},"runsc":{}}' ;;
      network)
        case "$2" in
          ls) ls "$S/nets" 2>/dev/null ;;
          inspect) cat "$S/nets/$5" ;;
        esac ;;
    esac ;;
  ip) [ "$*" = "-o addr show scope global" ] && cat "$S/ip_addrs" 2>/dev/null ;;
  pgrep) exit 1 ;;
  git) case "$*" in *"rev-parse HEAD"*) echo "${STUB_GIT_HEAD:-}" ;; esac ;;
  uv) echo "uv env UV_PYTHON_INSTALL_DIR=$UV_PYTHON_INSTALL_DIR UV_CACHE_DIR=$UV_CACHE_DIR" >> "$STUB_LOG"
    [ "$1" = sync ] && mkdir -p .venv "$UV_PYTHON_INSTALL_DIR" ;;
  runuser) exit "${STUB_RUNUSER_EXIT:-0}" ;;
esac
exit 0
"""
STUBBED = (
    "id getent groupadd useradd userdel groupdel install fallocate mkfs.ext4 mountpoint mount umount systemctl "
    "networkctl ip iptables iptables-save docker chown pgrep git uv sleep df runuser"
).split()


@pytest.fixture
def host(tmp_path):
    bin_dir, state, prefix = tmp_path / "bin", tmp_path / "state", tmp_path / "root"
    for d in (bin_dir, state, prefix):
        d.mkdir()
    for name in STUBBED:
        (bin_dir / name).write_text(STUB)
        (bin_dir / name).chmod(0o755)
    (bin_dir / "python3").symlink_to(sys.executable)
    (state / "passwd").write_text("root:x:0:0::/root:/bin/sh\n")
    (state / "group").write_text("root:x:0:\n")
    log = tmp_path / "calls.log"
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "EVALS_PREFIX": str(prefix),
        "STUB_LOG": str(log),
        "STUB_STATE": str(state),
        "STUB_IMAGE_ID": IMAGE_ID,
    }

    def run(script, *args, check=True, **extra):
        log.write_text("")
        p = subprocess.run(
            ["sh", str(HOST / script), *args], env={**env, **extra}, capture_output=True, text=True, timeout=60
        )
        if check:
            assert p.returncode == 0, p.stdout + p.stderr
        return p, log.read_text().splitlines()

    return {"run": run, "state": state, "prefix": prefix, "tmp": tmp_path}


def test_every_step_has_an_undo_and_parses():
    assert [s.split("-", 1)[0] for s in STEPS] == [str(i) for i in range(1, 10)]
    for step in STEPS:
        undo = HOST / step.replace(".sh", "-undo.sh")
        assert undo.exists() != (step in READ_ONLY), undo.name
        for script in [HOST / step] + ([undo] if undo.exists() else []):
            subprocess.run(["sh", "-n", str(script)], check=True, timeout=30)
            assert script.read_text().startswith("#!/bin/sh\n") and "\nset -eu\n" in script.read_text()


def test_every_step_and_undo_is_committed_executable():
    """#199: the README runs each step by its path, so a script committed without its x bit fails on the host with
    Permission denied. The tests run them through sh, which hides that, so this checks the mode git records."""
    names = [f"deploy/gvisor/host/{n}" for n in STEPS + UNDOS]
    out = subprocess.run(
        ["git", "ls-files", "-s", "--", *names], cwd=ROOT, capture_output=True, text=True, check=True, timeout=30
    ).stdout
    modes = {line.split("\t", 1)[1]: line.split()[0] for line in out.splitlines()}
    assert sorted(modes) == sorted(names)  # every one is tracked
    assert {n: m for n, m in modes.items() if m != "100755"} == {}


@pytest.mark.parametrize("script", STEPS + UNDOS)
def test_without_yes_nothing_changes(host, script):
    p, calls = host["run"](script)
    assert "Dry run: nothing changed" in p.stdout
    assert calls == ["id -u"], calls  # the root check, and nothing else
    assert not any(host["prefix"].iterdir())


@pytest.mark.parametrize("script", ["1-user.sh", "4-check.sh", "7-docker-proxy-undo.sh"])
def test_needs_root(host, script):
    p, _ = host["run"](script, "--yes", check=False, STUB_UID="1000")
    assert p.returncode == 1 and "run as root" in p.stderr


def test_user_step_is_idempotent_and_keeps_evals_out_of_docker(host):
    host["run"]("1-user.sh", "--yes")
    _, calls = host["run"]("1-user.sh", "--yes")
    assert not [c for c in calls if c.startswith(("useradd", "groupadd"))]  # second run: nothing new
    passwd = (host["state"] / "passwd").read_text()
    assert "evals:x:10010:" in passwd
    assert (host["state"] / "groups_of_evals").read_text().split() == ["evals", "evalskills"]
    (host["state"] / "groups_of_evals").write_text("evals evalskills docker\n")
    p, _ = host["run"]("1-user.sh", "--yes", check=False)
    assert p.returncode == 1 and "also in group docker" in p.stderr


def test_user_step_refuses_a_name_with_another_uid(host):
    (host["state"] / "passwd").write_text("evals:x:1234:1234::/home/evals:/bin/sh\n")
    p, calls = host["run"]("1-user.sh", "--yes", check=False)
    assert p.returncode == 1 and "another uid" in p.stderr
    assert not [c for c in calls if c.startswith("useradd")]
    p, calls = host["run"]("1-user-undo.sh", "--yes", check=False)
    assert p.returncode == 1 and not [c for c in calls if c.startswith("userdel")]


def test_user_undo_removes_only_its_user_and_group(host):
    host["run"]("1-user.sh", "--yes")
    _, calls = host["run"]("1-user-undo.sh", "--yes")
    assert "userdel evals" in calls and "groupdel evalskills" in calls
    assert (host["state"] / "passwd").read_text() == "root:x:0:0::/root:/bin/sh\n"
    assert (host["state"] / "group").read_text() == "root:x:0:\n"


def test_run_disk_is_preallocated_once_and_mounted_nodev_nosuid(host):
    host["run"]("1-user.sh", "--yes")
    _, calls = host["run"]("2-run-disk.sh", "--yes")
    assert any(c.startswith("fallocate -l 5G ") for c in calls)
    assert [c for c in calls if c.startswith("mkfs.ext4")][0].count(" -E nodiscard ") == 1  # no holes punched (#179)
    _, calls = host["run"]("2-run-disk.sh", "--yes")
    assert not [c for c in calls if c.startswith(("fallocate", "mkfs.ext4"))]  # never reformats
    unit = (host["prefix"] / "etc/systemd/system/srv-evals-run.mount").read_text()
    assert "Options=loop,nodev,nosuid\n" in unit and "Where=/srv/evals/run\n" in unit
    assert "chown evals:evals " + str(host["prefix"] / "srv/evals/run") in calls


def test_undos_refuse_while_the_socket_proxy_runs(host):
    (host["state"] / "active_evalskills-docker-proxy").touch()
    for script in ("2-run-disk-undo.sh", "5-code-undo.sh", "9-live-undo.sh"):
        p, calls = host["run"](script, "--yes", check=False)
        assert p.returncode == 1 and "socket proxy is running" in p.stderr, script
        assert not [c for c in calls if c.startswith(("rm ", "docker compose", "systemctl disable"))]


def test_hosts_step_names_the_gateway_once_and_its_undo_removes_only_that(host):
    hosts = host["prefix"] / "etc/hosts"
    hosts.parent.mkdir(parents=True, exist_ok=True)
    hosts.write_text("127.0.0.1 localhost\n10.0.0.5 app\n")
    _, calls = host["run"]("3-hosts.sh", "--yes")
    host["run"]("3-hosts.sh", "--yes")
    line = "172.31.248.2 evals-gateway # evalskills #137"
    assert hosts.read_text() == f"127.0.0.1 localhost\n10.0.0.5 app\n{line}\n"
    assert not [c for c in calls if c.split()[0] in ("ip", "networkctl", "iptables", "systemctl")]  # nothing else
    host["run"]("3-hosts-undo.sh", "--yes")
    assert hosts.read_text() == "127.0.0.1 localhost\n10.0.0.5 app\n"


def nets(host, **found):
    d = host["state"] / "nets"
    d.mkdir(exist_ok=True)
    for name, line in found.items():
        (d / name).write_text(line + "\n")


def run_image(host, sparse=False) -> Path:
    img = host["prefix"] / "var/lib/evals/run.img"
    img.parent.mkdir(parents=True, exist_ok=True)
    if sparse:
        with img.open("wb") as f:
            f.truncate(1 << 20)  # 1 MiB apparent, no blocks: what mkfs.ext4's discard left on the host (#179)
    else:
        subprocess.run(["fallocate", "-l", "1M", str(img)], check=True)
    return img


def test_check_step_passes_on_a_ready_host_and_changes_nothing(host):
    run_image(host)
    nets(host, bridge="bridge||172.17.0.0/16 ", app="evalskills_default||172.30.250.0/24 ")
    nets(host, mine="trial-1|1|172.31.240.0/28 ", egress="evalskills-egress||172.31.248.0/24 ")
    p, calls = host["run"]("4-check.sh", "--yes")
    assert "ok: Docker 29.6.1" in p.stdout
    assert {c.split()[0] for c in calls} <= {"id", "docker"}
    assert not [c for c in calls if c.startswith("docker ") and c.split()[1] not in ("version", "info", "network")]


@pytest.mark.parametrize(
    ("setup", "why"),
    [
        ({"STUB_DOCKER_VERSION": "27.5.1"}, "28 or later"),
        ({"no_runsc": True}, "runsc is not a registered runtime"),
        ({"net": "other||172.31.240.64/26 "}, "already uses our ranges: other 172.31.240.64/26"),
        ({"net": "other||172.31.0.0/16 "}, "already uses our ranges"),
        ({"net": "other||10.0.0.0/24 172.31.248.128/25 "}, "already uses our ranges: other 172.31.248.128/25"),
        ({"image": None}, "no run image"),
        ({"image": "sparse"}, "run.img is sparse: 0 of 1048576 bytes allocated"),  # thinned by fstrim, say
    ],
)
def test_check_step_refuses_a_host_that_is_not_ready(host, setup, why):
    if setup.get("image", "full"):
        run_image(host, sparse=setup.get("image") == "sparse")
    env = {k: v for k, v in setup.items() if k.startswith("STUB_")}
    if setup.get("no_runsc"):
        (host["state"] / "no_runsc").touch()
    if "net" in setup:
        nets(host, other=setup["net"])
    p, _ = host["run"]("4-check.sh", "--yes", check=False, **env)
    assert p.returncode == 1 and why in p.stderr


def artifact(tmp: Path, tags=("evalskills/trial-base:d136a597",), **over) -> Path:
    d = tmp / "artifact"
    d.mkdir(exist_ok=True)
    tar = d / "trial-base.tar.gz"
    manifest = json.dumps([{"Config": "c.json", "RepoTags": list(tags), "Layers": []}]).encode()
    with tarfile.open(tar, "w:gz") as t:
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest)
        t.addfile(info, io.BytesIO(manifest))
    meta = {
        "image": "evalskills/trial-base:d136a597",
        "loadable": True,
        "pinned": "committed",
        "tar_sha256": hashlib.sha256(tar.read_bytes()).hexdigest(),
        "run_id": "1",
        "commit": "c" * 40,
        **over,
    }
    (d / "trial-base.json").write_text(json.dumps(meta))
    return d


@pytest.mark.parametrize(
    ("over", "why"),
    [
        ({"loadable": False}, "not loadable"),
        ({"pinned": "new"}, "uncommitted digest"),
        ({"tar_sha256": "0" * 64}, "does not match"),
        ({"image": "evalskills/trial-base:other"}, "is not evalskills/trial-base"),
    ],
)
def test_base_image_step_refuses_an_artifact_that_fails_a_check(host, over, why):
    p, calls = host["run"]("6-base-image.sh", "--yes", str(artifact(host["tmp"], **over)), check=False)
    assert p.returncode == 1 and why in p.stderr
    assert not [c for c in calls if c.startswith("docker load")]


@pytest.mark.parametrize("tags", [("evalskills/trial-base:d136a597", "caddy:latest"), ("caddy:latest",), ()])
def test_base_image_step_refuses_a_tarball_tagging_anything_else(host, tags):
    p, calls = host["run"]("6-base-image.sh", "--yes", str(artifact(host["tmp"], tags=tags)), check=False)
    assert p.returncode == 1 and "not exactly evalskills/trial-base:d136a597" in p.stderr
    assert not [c for c in calls if c.startswith("docker load")]


def test_base_image_step_loads_and_prints_the_hosts_id(host):
    p, calls = host["run"]("6-base-image.sh", "--yes", str(artifact(host["tmp"])))
    assert "docker load" in calls and IMAGE_ID in p.stdout


def test_proxy_step_pins_the_hosts_image_id_and_never_enables_the_unit(host):
    sys.path.insert(0, str(ROOT / "gateway"))
    import docker_proxy as dp

    host["run"]("1-user.sh", "--yes")
    code = host["prefix"] / "opt/evalskills/gateway"
    code.mkdir(parents=True)
    (code / "docker_proxy.py").write_text("")
    (host["state"] / "image").touch()
    _, calls = host["run"]("7-docker-proxy.sh", "--yes")
    assert not [c for c in calls if c.startswith(("systemctl enable", "systemctl start"))]
    raw = json.loads((host["prefix"] / "etc/evals/docker-proxy.json").read_text())
    assert raw["builds"]["base_image_id"] == IMAGE_ID
    raw["builds"]["allowed_dockerfiles"] = str(ROOT / "deploy/gvisor/build/allowed-dockerfiles.txt")
    cfg = dp.load_config(raw)
    assert cfg["eval_uid"] == 10010 and cfg["listen_group"] == "evalskills" and cfg["bind_root"] == "/srv/evals/run"
    assert os.stat(host["prefix"] / "etc/evals/docker-proxy.json").st_mode & 0o777 == 0o640


def test_proxy_unit_needs_the_run_disk():
    unit = (HOST / "evalskills-docker-proxy.service").read_text()
    assert "Requires=docker.service srv-evals-run.mount\n" in unit
    assert "--config /etc/evals/docker-proxy.json" in unit and "[Install]" not in unit  # can't be enabled


def test_docker_client_config_points_at_the_egress_proxy_and_is_roots(host):
    host["run"]("1-user.sh", "--yes")
    _, calls = host["run"]("8-docker-client.sh", "--yes")
    cfg = json.loads((host["prefix"] / "var/lib/evals/.docker/config.json").read_text())
    assert cfg["proxies"]["default"]["httpsProxy"] == "http://evals-proxy:3128"
    assert any(c.startswith("install -o root -g root -m 0644 ") for c in calls)


def live_ready(host, ps: str) -> None:
    (host["prefix"] / "etc/evalskills/secrets").mkdir(parents=True)
    (host["prefix"] / "etc/evalskills/secrets/model_key").touch()
    (host["prefix"] / "opt/evalskills/deploy/live").mkdir(parents=True)
    (host["prefix"] / "opt/evalskills/deploy/live/compose.yaml").touch()
    for container, name in LIVE_CONFIGS:
        (host["prefix"] / "opt/evalskills/deploy/live" / name).write_text(f'{{"{name}": 2}}\n')
        (host["state"] / f"exec_{container}").write_text(f'{{"{name}": 2}}\n')  # what the container reads
    (host["state"] / "ip_addrs").write_text(
        "2: eth0    inet 203.0.113.7/24 brd 203.0.113.255 scope global eth0\\       valid_lft forever\n"
        "2: eth0    inet6 2001:db8::7/64 scope global \\       valid_lft forever\n"
    )  # documentation ranges, synthetic
    (host["state"] / "ps_0").write_text(ps)


def test_live_step_refuses_without_the_model_key(host):
    live_ready(host, "")
    (host["prefix"] / "etc/evalskills/secrets/model_key").unlink()
    p, calls = host["run"]("9-live.sh", "--yes", check=False)
    assert p.returncode == 1 and "no model key file" in p.stderr
    assert not [c for c in calls if c.startswith("docker compose")]


def test_live_undo_refuses_while_the_socket_proxy_runs(host):
    live_ready(host, "")
    (host["state"] / "active_evalskills-docker-proxy").touch()
    p, calls = host["run"]("9-live-undo.sh", "--yes", check=False)
    assert p.returncode == 1 and "socket proxy is running" in p.stderr
    assert not [c for c in calls if c.startswith("docker compose")]


def test_live_step_recreates_the_gateway_and_proxy(host):
    live_ready(host, "web 0.0.0.0:8080->443/tcp, 127.0.0.1:18443->8443/tcp\n")
    _, calls = host["run"]("9-live.sh", "--yes")
    up = [c for c in calls if c.startswith("docker compose")]
    assert len(up) == 1 and up[0].endswith(" up -d --build --force-recreate")  # a changed mounted file applies (#234)
    assert {c for c in calls if c.startswith("docker exec")} == {
        f"docker exec {c} cat /etc/evalskills/{n}" for c, n in LIVE_CONFIGS
    }


@pytest.mark.parametrize("container,name", [*LIVE_CONFIGS])
def test_live_step_fails_when_a_container_reads_an_old_config(host, container, name):
    live_ready(host, "")
    (host["state"] / f"exec_{container}").write_text(f'{{"{name}": 1}}\n')  # the inode from before the checkout
    p, _ = host["run"]("9-live.sh", "--yes", check=False)
    assert p.returncode == 1 and f"{container} doesn't read the checked-out {name}" in p.stderr


def test_live_step_gives_the_egress_proxy_the_hosts_own_addresses(host):
    live_ready(host, "")
    p, calls = host["run"]("9-live.sh", "--yes", EVALSKILLS_DENY_EXTRA="198.51.100.1")
    assert "compose env EVALSKILLS_HOST_ADDRS=203.0.113.7,2001:db8::7,198.51.100.1" in calls  # #236
    assert "denies this host's addresses: 203.0.113.7,2001:db8::7,198.51.100.1" in p.stdout  # a NAT'd one shows missing


def test_live_step_refuses_without_the_hosts_addresses(host):
    live_ready(host, "")
    (host["state"] / "ip_addrs").write_text("")
    p, calls = host["run"]("9-live.sh", "--yes", check=False)
    assert p.returncode == 1 and "could not list this host's addresses" in p.stderr
    assert not [c for c in calls if c.startswith("docker compose")]


def test_live_step_refuses_while_the_socket_proxy_runs(host):
    live_ready(host, "")
    (host["state"] / "active_evalskills-docker-proxy").touch()
    p, calls = host["run"]("9-live.sh", "--yes", check=False)
    assert p.returncode == 1 and "socket proxy is running" in p.stderr
    assert not [c for c in calls if c.startswith("docker compose")]


def test_run_disk_refuses_a_sparse_image(host):
    host["run"]("1-user.sh", "--yes")
    run_image(host, sparse=True)
    p, _ = host["run"]("2-run-disk.sh", "--yes", check=False)
    assert p.returncode != 0 and "is sparse" in p.stderr and "fallocate -l 1048576 " in p.stderr
    assert not (host["state"] / "mounted").exists()  # refused before it is mounted


def test_run_disk_accepts_a_fully_allocated_image(host):
    host["run"]("1-user.sh", "--yes")
    run_image(host)
    p, _ = host["run"]("2-run-disk.sh", "--yes")
    assert "not reformatting" in p.stdout


SE_PIN = "d136a597c54784ad8a0ce28542c208602b397a5c"


def code_host(host, with_uv=True):
    host["run"]("1-user.sh", "--yes")
    for d in ("opt/evalskills/.git", "opt/skillevaluator/.git"):
        (host["prefix"] / d).mkdir(parents=True)  # the clones (git is a stub)
    if with_uv:
        uv = host["prefix"] / "opt/uv/bin/uv"
        uv.parent.mkdir(parents=True)
        uv.write_text(STUB)
        uv.chmod(0o755)


def test_code_step_refuses_without_uv_in_opt_before_cloning(host):
    code_host(host, with_uv=False)  # uv on PATH (a stub) is not enough: root's PATH isn't what runs on the host
    p, calls = host["run"]("5-code.sh", "--yes", "c" * 40, check=False, STUB_GIT_HEAD=SE_PIN)
    assert p.returncode == 1 and "no uv at /opt/uv/bin/uv" in p.stderr
    assert not [c for c in calls if c.startswith(("git ", "uv "))]  # nothing cloned or synced (#180)


def test_code_step_keeps_python_and_the_cache_under_opt_and_checks_the_trial_user(host):
    code_host(host)
    p, calls = host["run"]("5-code.sh", "--yes", "c" * 40, STUB_GIT_HEAD=SE_PIN)
    opt = host["prefix"] / "opt/uv"
    uv_calls = [c for c in calls if c.startswith("uv env ")]
    assert len(uv_calls) == 2 and all(
        c == f"uv env UV_PYTHON_INSTALL_DIR={opt}/python UV_CACHE_DIR={opt}/cache" for c in uv_calls
    )
    assert f"chown -R root:root {opt}/python" in calls  # never /root, which evals can't read (#180)
    venv_python = f"{host['prefix']}/opt/skillevaluator/.venv/bin/python"
    assert f"runuser -u evals -- {venv_python} -I -c import skillevaluator, evalskills_gvisor" in calls
    assert "done: evalskills" in p.stdout


def test_code_step_refuses_while_the_socket_proxy_runs(host):
    code_host(host)
    (host["state"] / "active_evalskills-docker-proxy").touch()
    p, calls = host["run"]("5-code.sh", "--yes", "c" * 40, check=False, STUB_GIT_HEAD=SE_PIN)
    assert p.returncode == 1 and "socket proxy is running" in p.stderr
    assert not [c for c in calls if c.startswith(("git ", "uv "))]  # the running proxy would keep its old code (#230)


def test_code_step_fails_when_the_trial_user_cannot_run_the_venv(host):
    code_host(host)
    p, _ = host["run"]("5-code.sh", "--yes", "c" * 40, check=False, STUB_GIT_HEAD=SE_PIN, STUB_RUNUSER_EXIT="126")
    assert p.returncode == 1 and "evals can't run /opt/skillevaluator/.venv/bin/python" in p.stderr


def test_code_undo_removes_the_python_and_cache_it_made_but_not_uv(host):
    for d in ("opt/evalskills", "opt/skillevaluator", "opt/uv/python", "opt/uv/cache", "opt/uv/bin"):
        (host["prefix"] / d).mkdir(parents=True)
    host["run"]("5-code-undo.sh", "--yes")
    assert sorted(p.name for p in (host["prefix"] / "opt").rglob("*")) == ["bin", "uv"]
