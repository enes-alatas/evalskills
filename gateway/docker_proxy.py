"""evalskills Docker socket proxy: the eval runner's only way to the host's main Docker daemon (#137).

Trials run on the host's main daemon, every container under runsc (gVisor). The runner never gets the daemon's socket;
it gets this proxy's socket, and the proxy (root) forwards only what a SkillEvaluator/Harbor trial needs:
- **Allowlist, not denylist.** Every route is listed; anything else is refused with 403. Container, exec and network
  create bodies are checked field by field against a list of known fields; an unknown field is refused and its name is
  logged, so a Docker or Compose upgrade fails closed until the list is reviewed.
- **runsc always.** Container create gets ``HostConfig.Runtime = runsc`` whatever was asked (only "" or runsc are
  accepted). No privileged containers or execs, no added capabilities, devices, host namespaces, sysctls, unmasked
  paths, security-option overrides or published ports.
- **Own objects only.** Everything the proxy creates gets the owner label. Inspecting, starting, stopping, exec-ing
  into, copying to and from, and deleting work only on objects that carry it; lists and events are filtered to it.
  Images must carry it too (GvisorEnvironment builds them with the label; the proxy itself refuses every build and
  pull, because Compose v2 builds through BuildKit's /session and /grpc, which a /build check would never see).
- **Binds** come only from under one root (/srv/evals/run), at least two levels down (one fresh folder per trial).
  The proxy refuses a source if it, or any folder on its path, is a symlink; folders at or below the root must be
  owned by the eval user, folders above it by root. A bind onto /etc/resolv.conf must be read-only and hold exactly
  ``nameserver 127.0.0.1``. The check runs before the daemon mounts, so a window remains (documented, #137).
- **Networks** are bridge networks, forced ``internal`` with masquerading off and no address on the host
  (``inhibit_ipv4``), each given a /28 from the trial pool. A trial on one reaches only the other containers on it.
  On a trial's container create the proxy attaches the model gateway and the egress proxy (``services``, containers
  on a normal egress network) to that network and names them in the trial's /etc/hosts (``evals-gateway``,
  ``evals-proxy``); before a network delete it detaches them. A container joins only these networks, or none.
  Never the default bridge: docker0 has NAT egress and an address on the host.
- **No smuggling.** Every request goes upstream on its own connection with ``Connection: close``, with a head the
  proxy rebuilds itself. Client bytes after the request are forwarded only after the daemon has switched an attach
  or exec start to a raw stream (101).
- ``GET /_evalskills/isolation`` reports whether every trial network still has its flags and both services still
  forward nothing and sit on no one else's network; the runner refuses to start trials when it isn't ok. There is no
  host firewall for trials: the isolation is Docker's (ADR-0003, amended 2026-11-14).

It logs methods, routes and decisions, never bodies or environment values. Standard library only.

Run (as root; evalskills-docker-proxy.service): python3 -I gateway/docker_proxy.py --config /etc/evals/docker-proxy.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import grp
import http.client
import ipaddress
import json
import os
import re
import select
import socket
import socketserver
import stat
import sys
import tempfile
import threading
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent))  # run with -I: our own folder only (root-owned on the host)
import docker_build  # noqa: E402

BUILD_DEFAULTS = {
    "allowed_dockerfiles": None,  # deploy/gvisor/build/allowed-dockerfiles.txt as installed
    "base_image": None,  # the trial base's tag, as in every final Dockerfile's FROM line
    "base_image_id": None,  # its image ID once loaded on the host (sha256:…); the build refuses any other
    "max_bytes": 256 * 1024 * 1024,
    "max_files": 20000,
    "timeout_s": 900,
}
BUILD_HEADERS = {
    "host",
    "content-length",
    "transfer-encoding",
    "content-type",
    "user-agent",
    "accept",
    "connection",
    "accept-encoding",
}

DEFAULTS = {
    "listen": "/run/evalskills/docker.sock",
    "listen_group": "evalskills",
    "upstream": "/var/run/docker.sock",
    "bind_root": "/srv/evals/run",
    "eval_uid": None,
    "network_pool": "172.31.240.0/21",
    "network_prefix": 28,
    "egress_network": "evalskills-egress",  # the services' own network (deploy/live/compose.yaml)
    "idle_timeout_s": 1800,  # longer than a trial (900 s cap, #118): an agent may print nothing until it ends
    "builds": None,  # see BUILD_DEFAULTS; null = the build endpoint answers 503
    # The name a trial uses for each service -> the service's container (deploy/live/compose.yaml). The proxy attaches
    # these containers to every trial network and points the names at their addresses there (#137).
    "services": {"evals-gateway": "evalskills-gateway", "evals-proxy": "evalskills-egress-proxy"},
}
OWNER_LABEL = "io.evalskills.owned"
RUNTIME = "runsc"
# Each trial network is internal with no address on the host (inhibit_ipv4), so a trial reaches nothing but the
# containers on its own network (#137). The model gateway and the egress proxy are containers on a normal egress
# network; the proxy attaches them to every trial network and names them in each trial's /etc/hosts (cfg services).
NO_DNS_RESOLV = b"nameserver 127.0.0.1\n"
MAX_HEAD = 64 * 1024
MAX_JSON_BODY = 1024 * 1024
ID = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}"
HOSTNAME = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
IMAGE_REF = r"[A-Za-z0-9][A-Za-z0-9_.:/@-]{0,255}"
VERSION = re.compile(r"^/v1\.\d{1,3}(/.*)$")
MIN_IDLE_S = 900 + 300  # the trial cap (#118) plus a margin
ABOVE_ROOT_UIDS = {0}  # who may own the folders above bind_root: root (tests add their own uid)


class Refused(Exception):
    """A request the proxy won't forward; the message is safe to return and log (names, never values)."""

    def __init__(self, why: str, code: int = 403):
        super().__init__(why)
        self.code = code


class ConfigError(ValueError):
    pass


def log(event: str, **fields) -> None:
    print(
        json.dumps({"t": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"), "event": event, **fields}),
        file=sys.stderr,
        flush=True,
    )


def load_config(raw: dict) -> dict:
    unknown = set(raw) - set(DEFAULTS)
    if unknown:
        raise ConfigError(f"unknown config keys: {', '.join(sorted(unknown))}")
    cfg = {**DEFAULTS, **raw}
    if not isinstance(cfg["eval_uid"], int) or cfg["eval_uid"] <= 0:
        raise ConfigError("eval_uid must be the eval user's uid (not root)")
    root = cfg["bind_root"]
    if not isinstance(root, str) or not root.startswith("/") or os.path.normpath(root) != root or root == "/":
        raise ConfigError("bind_root must be a normalized absolute path other than /")
    try:
        cfg["network_pool"] = ipaddress.ip_network(cfg["network_pool"])
    except ValueError as e:
        raise ConfigError(f"bad network: {e}") from e
    if not isinstance(cfg["idle_timeout_s"], int) or cfg["idle_timeout_s"] < MIN_IDLE_S:
        raise ConfigError(f"idle_timeout_s must be at least {MIN_IDLE_S} (the trial cap plus a margin)")
    if not isinstance(cfg["egress_network"], str) or not re.fullmatch(ID, cfg["egress_network"]):
        raise ConfigError("egress_network must name a network")
    if not cfg["network_pool"].prefixlen <= cfg["network_prefix"] <= 30:
        raise ConfigError("network_prefix must split network_pool into networks of at least 4 addresses")
    svc = cfg["services"]
    if (
        not isinstance(svc, dict)
        or not svc
        or not all(isinstance(k, str) and re.fullmatch(HOSTNAME, k) for k in svc)
        or not all(isinstance(v, str) and re.fullmatch(ID, v) for v in svc.values())
        or len(set(svc.values())) != len(svc)
    ):
        raise ConfigError("services must map host names to distinct container names")
    if cfg["builds"] is not None:
        cfg["builds"] = load_build_config(cfg["builds"])
    return cfg


def load_build_config(raw: dict) -> dict:
    if not isinstance(raw, dict) or set(raw) - set(BUILD_DEFAULTS):
        raise ConfigError(f"builds may only set: {', '.join(BUILD_DEFAULTS)}")
    b = {**BUILD_DEFAULTS, **raw}
    if not isinstance(b["base_image"], str) or not re.fullmatch(
        r"[a-z0-9][a-z0-9._/-]*:[A-Za-z0-9._-]+", b["base_image"]
    ):
        raise ConfigError("builds.base_image must be a repo:tag")
    if not isinstance(b["base_image_id"], str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", b["base_image_id"]):
        raise ConfigError("builds.base_image_id must be the loaded image's sha256 ID")
    for k in ("max_bytes", "max_files", "timeout_s"):
        if not isinstance(b[k], int) or b[k] <= 0:
            raise ConfigError(f"builds.{k} must be a positive integer")
    try:
        b["allowlist"] = docker_build.load_allowlist(Path(b["allowed_dockerfiles"]), b["base_image"])
    except (OSError, TypeError, ValueError) as e:
        raise ConfigError(f"builds.allowed_dockerfiles: {e}") from e
    return b


# ---------------------------------------------------------------- upstream


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float = 30):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


class Daemon:
    """The proxy's own small calls to the daemon (inspect, list), never on behalf of the client's raw bytes."""

    def __init__(self, path: str):
        self.path = path

    def get(self, url: str):
        conn = _UnixHTTPConnection(self.path)
        try:
            conn.request("GET", url, headers={"Host": "docker"})
            resp = conn.getresponse()
            body = resp.read(8 * MAX_JSON_BODY)
            if resp.status == 404:
                return None
            if resp.status != 200:
                raise Refused(f"daemon answered {resp.status} to the proxy's check", 502)
            return json.loads(body)
        except (OSError, ValueError) as e:
            raise Refused(f"the proxy's check failed ({type(e).__name__})", 502) from e
        finally:
            conn.close()

    def labels(self, kind: str, ident: str) -> dict:
        url = {
            "container": f"/containers/{ident}/json",
            "network": f"/networks/{ident}",
            "image": f"/images/{ident}/json",
        }
        doc = self.get(url[kind])
        if doc is None:
            raise Refused(f"no such {kind}", 404)
        labels = (doc.get("Config") or {}).get("Labels") if kind != "network" else doc.get("Labels")
        return labels or {}

    def owned(self, kind: str, ident: str) -> bool:
        return self.labels(kind, ident).get(OWNER_LABEL) == "1"

    def exec_container(self, exec_id: str) -> str:
        doc = self.get(f"/exec/{exec_id}/json")
        if doc is None:
            raise Refused("no such exec instance", 404)
        return str(doc.get("ContainerID") or "")

    def post(self, url: str, doc: dict) -> int:
        """One of the proxy's own small writes (attach or detach a service); returns the daemon's status."""
        conn = _UnixHTTPConnection(self.path)
        try:
            conn.request(
                "POST", url, body=json.dumps(doc), headers={"Host": "docker", "Content-Type": "application/json"}
            )
            resp = conn.getresponse()
            resp.read(MAX_JSON_BODY)
            return resp.status
        except OSError as e:
            raise Refused(f"the proxy's call failed ({type(e).__name__})", 502) from e
        finally:
            conn.close()

    def service_address(self, network: str, container: str) -> str | None:
        """The IPv4 address a service container has on one of the proxy's networks, or None when it isn't attached."""
        doc = self.get(f"/containers/{container}/json")
        if doc is None or not (doc.get("State") or {}).get("Running"):
            raise Refused(f"the service {container} is not running", 503)
        if ((doc.get("Config") or {}).get("Labels") or {}).get(OWNER_LABEL):
            raise Refused(f"{container} is a trial's container, not the service", 502)  # a trial took its name
        net = self.get(f"/networks/{network}") or {}
        for ep in ((doc.get("NetworkSettings") or {}).get("Networks") or {}).values():
            if net.get("Id") and ep.get("NetworkID") == net["Id"] and ep.get("IPAddress"):
                return str(ipaddress.IPv4Address(ep["IPAddress"]))
        return None

    def attach_services(self, network: str, services: dict) -> dict:
        """Attach each service to the trial network if it isn't yet; → {host name: its address there}."""
        out = {}
        for host, container in services.items():
            ip = self.service_address(network, container)
            if ip is None:
                status = self.post(f"/networks/{network}/connect", {"Container": container})
                ip = self.service_address(network, container)  # also covers a concurrent create attaching it first
                if ip is None:
                    raise Refused(f"could not attach {container} to the trial network ({status})", 502)
                log("attached", service=container)
            out[host] = ip
        return out

    def detach_services(self, network: str, services: dict) -> None:
        """Before a trial network is removed: Docker refuses to remove a network a container is still on."""
        for container in services.values():
            if self.post(f"/networks/{network}/disconnect", {"Container": container, "Force": True}) == 200:
                log("detached", service=container)

    def owned_subnets(self) -> list:
        nets = self.get("/networks?" + urlencode({"filters": json.dumps({"label": [f"{OWNER_LABEL}=1"]})})) or []
        out = []
        for n in nets:
            for c in (n.get("IPAM") or {}).get("Config") or []:
                try:
                    out.append(ipaddress.ip_network(c.get("Subnet", ""), strict=False))
                except ValueError:
                    continue
        return out


# ---------------------------------------------------------------- body policy


def _empty(v) -> bool:
    return v in (None, "", [], {}, 0, False)


def _only(doc: dict, allowed: dict, where: str) -> None:
    """Every key must be known; each known key's rule must accept its value. Rules: None = any value, "empty" = must
    be empty/zero/false, a set = one of these values, a callable = custom check (raises Refused)."""
    if not isinstance(doc, dict):
        raise Refused(f"{where} must be an object")
    for key, value in doc.items():
        if key not in allowed:
            raise Refused(f"unknown field {where}.{key}")
        rule = allowed[key]
        if rule is None:
            continue
        if rule == "empty":
            if not _empty(value):
                raise Refused(f"{where}.{key} must be empty")
        elif isinstance(rule, (set, frozenset)):
            if not any(type(value) is type(r) and value == r for r in rule):  # exact type: 0 is not False
                raise Refused(f"{where}.{key} has a value the proxy doesn't allow")
        else:
            rule(value, f"{where}.{key}")


def _bools_false(value, where):
    if value not in (None, False):
        raise Refused(f"{where} must be false")


def _non_negative(value, where):
    if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
        raise Refused(f"{where} must be a non-negative integer")


def _strings(value, where):
    if value is not None and not (isinstance(value, list) and all(isinstance(s, str) for s in value)):
        raise Refused(f"{where} must be a list of strings")


def _no_new_privileges(value, where):
    _strings(value, where)
    for s in value or []:
        if s not in ("no-new-privileges", "no-new-privileges:true", "no-new-privileges=true"):
            raise Refused(f"{where} may only set no-new-privileges")


CONFIG_FIELDS = {
    "Hostname": None,
    "Domainname": None,
    "User": None,
    "AttachStdin": None,
    "AttachStdout": None,
    "AttachStderr": None,
    "ExposedPorts": None,  # exposing is metadata; publishing (PortBindings) is refused
    "Tty": None,
    "OpenStdin": None,
    "StdinOnce": None,
    "Env": _strings,
    "Cmd": None,
    "Healthcheck": None,
    "ArgsEscaped": None,
    "Image": None,  # checked separately: must be an owned image
    "Volumes": None,  # anonymous local volumes only; HostConfig.VolumeDriver must be empty
    "WorkingDir": None,
    "Entrypoint": None,
    "NetworkDisabled": None,
    "MacAddress": "empty",
    "OnBuild": "empty",
    "Labels": None,  # the owner label is set by the proxy
    "StopSignal": None,
    "StopTimeout": None,
    "Shell": None,
    "HostConfig": None,  # checked separately
    "NetworkingConfig": None,  # checked separately
}

HOST_CONFIG_FIELDS = {
    "Binds": None,  # checked separately
    "ContainerIDFile": "empty",
    "LogConfig": lambda v, w: _only(v or {}, {"Type": {"", "json-file", "local"}, "Config": "empty"}, w),
    "NetworkMode": None,  # checked separately
    "PortBindings": "empty",
    "RestartPolicy": lambda v, w: _only(v or {}, {"Name": {"", "no"}, "MaximumRetryCount": "empty"}, w),
    "AutoRemove": None,
    "VolumeDriver": "empty",
    "VolumesFrom": "empty",
    "ConsoleSize": None,
    "Annotations": "empty",
    "CapAdd": "empty",
    "CapDrop": _strings,
    "CgroupnsMode": {"", "private"},
    "Dns": _strings,
    "DnsOptions": _strings,
    "DnsSearch": _strings,
    "ExtraHosts": _strings,
    "GroupAdd": _strings,
    "IpcMode": {"", "private", "none", "shareable"},
    "Cgroup": "empty",
    "Links": _strings,
    "OomScoreAdj": _non_negative,
    "PidMode": "empty",
    "Privileged": _bools_false,
    "PublishAllPorts": _bools_false,
    "ReadonlyRootfs": None,
    "SecurityOpt": _no_new_privileges,
    "StorageOpt": "empty",
    "Tmpfs": None,  # in-memory mounts inside the sandbox
    "UTSMode": "empty",
    "UsernsMode": "empty",
    "ShmSize": _non_negative,
    "Sysctls": "empty",
    "Runtime": {None, "", RUNTIME},  # then forced to runsc
    "Isolation": {None, "", "default"},
    # Resources (embedded): limits are fine, host devices and cgroup placement are not
    "CpuShares": _non_negative,
    "Memory": _non_negative,
    "NanoCpus": _non_negative,
    "CgroupParent": "empty",
    "BlkioWeight": _non_negative,
    "BlkioWeightDevice": "empty",
    "BlkioDeviceReadBps": "empty",
    "BlkioDeviceWriteBps": "empty",
    "BlkioDeviceReadIOps": "empty",
    "BlkioDeviceWriteIOps": "empty",
    "CpuPeriod": _non_negative,
    "CpuQuota": None,
    "CpuRealtimePeriod": "empty",
    "CpuRealtimeRuntime": "empty",
    "CpusetCpus": None,
    "CpusetMems": None,
    "Devices": "empty",
    "DeviceCgroupRules": "empty",
    "DeviceRequests": "empty",
    "KernelMemory": _non_negative,
    "KernelMemoryTCP": _non_negative,
    "MemoryReservation": _non_negative,
    "MemorySwap": None,
    "MemorySwappiness": None,
    "OomKillDisable": _bools_false,
    "PidsLimit": None,
    "Ulimits": None,
    "CpuCount": _non_negative,
    "CpuPercent": _non_negative,
    "IOMaximumIOps": _non_negative,
    "IOMaximumBandwidth": _non_negative,
    "Mounts": None,  # checked separately
    "MaskedPaths": {None},  # [] would unmask /proc paths
    "ReadonlyPaths": {None},
    "Init": None,
}

ENDPOINT_FIELDS = {
    "IPAMConfig": "empty",
    "Links": _strings,
    "Aliases": _strings,
    "MacAddress": "empty",
    "DriverOpts": "empty",
    "GwPriority": None,
    "NetworkID": None,
    "EndpointID": "empty",
    "Gateway": "empty",
    "IPAddress": "empty",
    "IPPrefixLen": "empty",
    "IPv6Gateway": "empty",
    "GlobalIPv6Address": "empty",
    "GlobalIPv6PrefixLen": "empty",
    "DNSNames": _strings,
}

EXEC_FIELDS = {
    "AttachStdin": None,
    "AttachStdout": None,
    "AttachStderr": None,
    "ConsoleSize": None,
    "DetachKeys": None,
    "Tty": None,
    "Env": _strings,
    "Cmd": _strings,
    "Privileged": _bools_false,
    "User": None,
    "WorkingDir": None,
}

EXEC_START_FIELDS = {"Detach": None, "Tty": None, "ConsoleSize": None}

NETWORK_FIELDS = {
    "Name": None,
    "CheckDuplicate": None,
    "Driver": {None, "", "bridge"},
    "Scope": {None, "", "local"},
    "EnableIPv4": {None, True},
    "EnableIPv6": _bools_false,
    "IPAM": lambda v, w: _only(v or {}, {"Driver": {None, "", "default"}, "Config": "empty", "Options": "empty"}, w),
    "Internal": None,  # forced true
    "Attachable": None,
    "Ingress": _bools_false,
    "ConfigOnly": _bools_false,
    "ConfigFrom": "empty",
    "Options": "empty",  # the proxy sets its own
    "Labels": None,
}

MOUNT_FIELDS = {
    "Type": {"bind", "tmpfs"},
    "Source": None,
    "Target": None,
    "ReadOnly": None,
    "Consistency": {None, "", "default"},
    "BindOptions": lambda v, w: _only(v or {}, {"CreateMountpoint": None}, w),
    "TmpfsOptions": lambda v, w: _only(v or {}, {"SizeBytes": _non_negative, "Mode": None}, w),
}


class Policy:
    def __init__(self, cfg: dict, daemon: Daemon):
        self.cfg = cfg
        self.daemon = daemon
        self._net_lock = threading.Lock()
        self._reserved: set = set()  # subnets handed out whose network create hasn't returned yet

    # binds -------------------------------------------------------------

    def check_bind_source(self, src: str) -> None:
        root = self.cfg["bind_root"]
        if not isinstance(src, str) or not src.startswith("/") or os.path.normpath(src) != src:
            raise Refused("bind source must be a normalized absolute path")
        if not src.startswith(root + "/"):
            raise Refused("bind source is outside the trial root")
        below = src[len(root) + 1 :].split("/")
        if len(below) < 2:
            raise Refused("bind source must be inside one trial's folder")
        path = ""
        for part in src.strip("/").split("/"):
            path += "/" + part
            try:
                st = os.lstat(path)
            except OSError as e:
                raise Refused("bind source does not exist") from e
            if stat.S_ISLNK(st.st_mode):
                raise Refused("bind source has a symlink on its path")
            inside = path == root or path.startswith(root + "/")
            if st.st_uid not in ({self.cfg["eval_uid"]} if inside else ABOVE_ROOT_UIDS):
                raise Refused("bind source has a folder with the wrong owner on its path")

    def check_bind(self, src: str, target: str, read_only: bool) -> None:
        self.check_bind_source(src)
        if os.path.normpath(target or "/") == "/etc/resolv.conf":
            if not read_only:
                raise Refused("the resolv.conf bind must be read-only")
            try:
                with open(os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as f:
                    if not stat.S_ISREG(os.fstat(f.fileno()).st_mode) or f.read(64) != NO_DNS_RESOLV:
                        raise Refused("the resolv.conf bind must name only 127.0.0.1")
            except OSError as e:
                raise Refused("the resolv.conf bind can't be read") from e

    def check_binds(self, binds) -> None:
        _strings(binds, "HostConfig.Binds")
        for b in binds or []:
            parts = b.split(":")
            if len(parts) not in (2, 3):
                raise Refused("HostConfig.Binds entries must be source:target[:options]")
            opts = set(parts[2].split(",")) if len(parts) == 3 else set()
            if not opts <= {"ro", "rw", "z"}:
                raise Refused("HostConfig.Binds options may only be ro, rw and z")
            self.check_bind(parts[0], parts[1], "ro" in opts)

    def check_mounts(self, mounts) -> None:
        if mounts is None:
            return
        if not isinstance(mounts, list):
            raise Refused("HostConfig.Mounts must be a list")
        for m in mounts:
            _only(m, MOUNT_FIELDS, "HostConfig.Mounts[]")
            if m.get("Type") == "bind":
                self.check_bind(m.get("Source"), m.get("Target"), bool(m.get("ReadOnly")))
            elif m.get("Source"):
                raise Refused("tmpfs mounts take no source")

    # networks ----------------------------------------------------------

    def check_network_ref(self, name: str, where: str) -> None:
        """An owned network, by name or id. "", "default" and "bridge" mean docker0, which has NAT egress and an
        address on the host; "host" and "container:…" share someone else's network."""
        if name in ("", "default", "bridge", "host", "none") or not re.fullmatch(ID, name or ""):
            raise Refused(f"{where} must name a network")
        if not self.daemon.owned("network", name):
            raise Refused(f"{where} is not one of the proxy's networks")

    def rewrite_container_create(self, body: dict) -> dict:
        _only(body, CONFIG_FIELDS, "Config")
        hc = body.get("HostConfig") or {}
        _only(hc, HOST_CONFIG_FIELDS, "HostConfig")
        self.check_binds(hc.get("Binds"))
        self.check_mounts(hc.get("Mounts"))
        mode = hc.get("NetworkMode")
        if mode != "none":  # no network at all, or one of ours; never the default bridge
            self.check_network_ref(mode if isinstance(mode, str) else "", "HostConfig.NetworkMode")
        nc = body.get("NetworkingConfig") or {}
        _only(nc, {"EndpointsConfig": None}, "NetworkingConfig")
        endpoints = nc.get("EndpointsConfig") or {}
        if not isinstance(endpoints, dict):
            raise Refused("NetworkingConfig.EndpointsConfig must be an object")
        for name, ep in endpoints.items():
            self.check_network_ref(name, "NetworkingConfig.EndpointsConfig")
            _only(ep or {}, ENDPOINT_FIELDS, "NetworkingConfig.EndpointsConfig[]")
        hosts = hc.get("ExtraHosts") or []
        if not isinstance(hosts, list) or not all(isinstance(h, str) for h in hosts):
            raise Refused("HostConfig.ExtraHosts must be a list of strings")
        # Docker takes "name:ip" and "name=ip"; refuse the services' names under either separator
        names = set(self.cfg["services"])
        if any(re.split("[:=]", h, maxsplit=1)[0].strip().lower() in names for h in hosts):
            raise Refused(f"HostConfig.ExtraHosts may not set {', '.join(sorted(names))}: the proxy sets them")
        if mode != "none":  # a trial on one of our networks: attach the services to it and name them
            addrs = self.daemon.attach_services(mode, self.cfg["services"])
            # first, because a resolver reading /etc/hosts takes the first line that names the host
            hc["ExtraHosts"] = [*(f"{h}:{ip}" for h, ip in addrs.items()), *hosts]
        image = body.get("Image")
        if not isinstance(image, str) or not re.fullmatch(IMAGE_REF, image):
            raise Refused("Config.Image must name an image")
        if not self.daemon.owned("image", image):
            raise Refused("Config.Image is not an image built for a trial")
        hc["Runtime"] = RUNTIME
        body["HostConfig"] = hc
        body["Labels"] = {**(body.get("Labels") or {}), OWNER_LABEL: "1"}
        return body

    def rewrite_network_create(self, body: dict) -> dict:
        _only(body, NETWORK_FIELDS, "Network")
        if not re.fullmatch(ID, body.get("Name") or ""):
            raise Refused("Network.Name must be a plain name")
        with self._net_lock:  # two trials starting together must not get the same subnet: see release_subnet
            subnet = self._free_subnet()
            self._reserved.add(subnet)
            body.update(
                {
                    "Driver": "bridge",
                    "Internal": True,
                    "EnableIPv6": False,
                    "IPAM": {"Driver": "default", "Config": [{"Subnet": str(subnet), "Gateway": str(subnet[1])}]},
                    # the gateway address is reserved but never put on the host's bridge: no route to the host
                    "Options": {
                        "com.docker.network.bridge.enable_ip_masquerade": "false",
                        "com.docker.network.bridge.inhibit_ipv4": "true",
                    },
                    "Labels": {**(body.get("Labels") or {}), OWNER_LABEL: "1"},
                }
            )
        return body

    def release_subnet(self, body: dict) -> None:
        """Called once the daemon has answered the network create: the subnet is now either in use or free again."""
        with self._net_lock:
            for c in body["IPAM"]["Config"]:
                self._reserved.discard(ipaddress.ip_network(c["Subnet"]))

    def _free_subnet(self):
        used = [*self.daemon.owned_subnets(), *self._reserved]
        for candidate in self.cfg["network_pool"].subnets(new_prefix=self.cfg["network_prefix"]):
            if not any(candidate.overlaps(u) for u in used):
                return candidate
        raise Refused("the trial network pool is full", 503)

    def rewrite_exec_create(self, body: dict) -> dict:
        _only(body, EXEC_FIELDS, "Exec")
        return body

    def rewrite_exec_start(self, body: dict) -> dict:
        _only(body, EXEC_START_FIELDS, "ExecStart")
        return body

    # queries -----------------------------------------------------------

    @staticmethod
    def owner_filtered(query: str) -> str:
        """Add the owner label to a list or events query's filters, so other workloads stay invisible.

        The daemon reads filters either all in the map form {"key": {"value": true}} or all in the list form
        {"key": ["value"]}, and answers "invalid filter" to a mix. Compose sends the map form (its image check before
        `up`), the docker CLI the list form. So every key is rewritten to the map form, which the daemon has always
        accepted (#206)."""
        pairs = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True) if k != "filters"]
        filters = {}
        for k, v in parse_qsl(query, keep_blank_values=True):
            if k == "filters" and v:
                try:
                    filters = json.loads(v)
                except ValueError as e:
                    raise Refused("filters must be JSON") from e
        if not isinstance(filters, dict):
            raise Refused("filters must be an object")
        as_map = {}
        for key, values in filters.items():
            if isinstance(values, dict):
                if not all(isinstance(on, bool) for on in values.values()):
                    raise Refused(f"filters.{key} must map values to true or false")
                as_map[key] = dict(values)  # as sent: the daemon applies an entry whether it says true or false
            elif isinstance(values, list) and all(isinstance(v, str) for v in values):
                as_map[key] = dict.fromkeys(values, True)
            else:
                raise Refused(f"filters.{key} must be a list or an object")
        as_map.setdefault("label", {})[f"{OWNER_LABEL}=1"] = True
        return urlencode([*pairs, ("filters", json.dumps(as_map))])


# ---------------------------------------------------------------- routing

C = rf"/containers/({ID})"
E = rf"/exec/({ID})"
N = rf"/networks/({ID})"
ROUTES = [
    # (method, path regex, kind, action); kind names whose ownership the captured id needs
    ("GET", r"/_ping", None, "pass"),
    ("HEAD", r"/_ping", None, "pass"),
    ("GET", r"/version", None, "pass"),
    ("GET", r"/info", None, "pass"),
    ("GET", r"/containers/json", None, "filter"),
    ("POST", r"/containers/create", None, "container_create"),
    ("GET", C + r"/json", "container", "pass"),
    ("GET", C + r"/logs", "container", "pass"),
    ("GET", C + r"/top", "container", "pass"),
    ("GET", C + r"/stats", "container", "pass"),
    ("POST", C + r"/(?:start|stop|restart|kill|wait|resize)", "container", "pass"),
    ("POST", C + r"/attach", "container", "hijack"),
    ("GET", C + r"/archive", "container", "pass"),
    ("HEAD", C + r"/archive", "container", "pass"),
    ("PUT", C + r"/archive", "container", "stream"),
    ("POST", C + r"/exec", "container", "exec_create"),
    ("DELETE", C, "container", "pass"),
    ("GET", E + r"/json", "exec", "pass"),
    ("POST", E + r"/resize", "exec", "pass"),
    ("POST", E + r"/start", "exec", "exec_start"),
    ("GET", r"/networks", None, "filter"),
    ("POST", r"/networks/create", None, "network_create"),
    ("GET", N, "network", "pass"),
    ("DELETE", N, "network", "network_delete"),
    ("POST", N + r"/(?:connect|disconnect)", "network", "network_attach"),
    ("GET", r"/images/json", None, "filter"),
    ("GET", rf"/images/({IMAGE_REF})/json", "image", "pass"),  # other images' Config.Env may hold secrets
    ("DELETE", rf"/images/({IMAGE_REF})", "image", "pass"),
    ("GET", r"/events", None, "filter"),
    # Compose 2.3x+ lists the project's volumes before `up` and stops if refused (#227). Filtered to the owner label,
    # and volumes can't be created through the proxy, so the answer is always empty: nothing of anyone else's shows.
    ("GET", r"/volumes", None, "filter"),
]
ROUTES = [(m, re.compile(p + r"\Z"), k, a) for m, p, k, a in ROUTES]


def route(method: str, path: str):
    """→ (action, kind, captured id, route path). Versioned paths (/v1.47/...) match like unversioned ones."""
    if "%" in path or "/../" in path + "/" or "/./" in path + "/" or "//" in path:
        raise Refused("path must be plain")
    m = VERSION.match(path)
    bare = m.group(1) if m else path
    for meth, rx, kind, action in ROUTES:
        hit = rx.match(bare)
        if hit and meth == method:
            return action, kind, (hit.group(1) if hit.groups() else None), bare
    raise Refused(f"{method} {re.sub(ID, '…', bare)[:80]} is not allowed")


# ---------------------------------------------------------------- HTTP plumbing


class Head:
    def __init__(self, method: str, target: str, headers: list):
        self.method, self.target, self.headers = method, target, headers

    def get(self, name: str):
        name = name.lower()
        vals = [v for k, v in self.headers if k.lower() == name]
        if len(vals) > 1:
            raise Refused(f"duplicate {name} header", 400)
        return vals[0] if vals else None


def read_head(sock: socket.socket, buf: bytearray) -> Head | None:
    while b"\r\n\r\n" not in buf:
        if len(buf) > MAX_HEAD:
            raise Refused("request head too large", 431)
        chunk = sock.recv(65536)
        if not chunk:
            if buf:
                raise Refused("incomplete request", 400)
            return None
        buf += chunk
    raw, _, rest = bytes(buf).partition(b"\r\n\r\n")
    buf[:] = rest
    try:
        lines = raw.decode("latin-1").split("\r\n")
        method, target, version = lines[0].split(" ")
    except ValueError as e:
        raise Refused("bad request line", 400) from e
    if version not in ("HTTP/1.1", "HTTP/1.0") or not re.fullmatch(r"[A-Z]{3,7}", method):
        raise Refused("bad request line", 400)
    headers = []
    for line in lines[1:]:
        k, sep, v = line.partition(":")
        if not sep or not re.fullmatch(r"[A-Za-z0-9-]+", k):
            raise Refused("bad header", 400)
        headers.append((k, v.strip()))
    return Head(method, target, headers)


def _need(sock, buf: bytearray, n: int) -> None:
    while len(buf) < n:
        chunk = sock.recv(65536)
        if not chunk:
            raise Refused("body ended early", 400)
        buf += chunk


def _line(sock, buf: bytearray) -> bytes:
    while b"\r\n" not in buf:
        if len(buf) > 4096:
            raise Refused("bad chunked body", 400)
        chunk = sock.recv(65536)
        if not chunk:
            raise Refused("body ended early", 400)
        buf += chunk
    line, _, rest = bytes(buf).partition(b"\r\n")
    buf[:] = rest
    return line


def body_chunks(sock, buf: bytearray, head: Head):
    """Yield the request body as decoded pieces, consuming exactly the body from the socket and nothing after it."""
    te, cl = head.get("transfer-encoding"), head.get("content-length")
    if te is not None and cl is not None:
        raise Refused("both Content-Length and Transfer-Encoding", 400)
    if te is not None:
        if te.lower() != "chunked":
            raise Refused("only chunked transfer encoding", 400)
        while True:
            size_line = _line(sock, buf).split(b";", 1)[0].strip()
            if not re.fullmatch(rb"[0-9A-Fa-f]{1,8}", size_line):
                raise Refused("bad chunked body", 400)
            size = int(size_line, 16)
            if size == 0:
                while _line(sock, buf):  # trailers, ignored
                    pass
                return
            _need(sock, buf, size + 2)
            if buf[size : size + 2] != b"\r\n":
                raise Refused("bad chunked body", 400)
            yield bytes(buf[:size])
            del buf[: size + 2]
    if cl is None:
        return
    if not re.fullmatch(r"\d{1,12}", cl):
        raise Refused("bad Content-Length", 400)
    left = int(cl)
    while left:
        if not buf:
            _need(sock, buf, 1)
        piece = bytes(buf[:left])
        del buf[: len(piece)]
        left -= len(piece)
        yield piece


def read_json_body(sock, buf, head) -> dict:
    data = bytearray()
    for piece in body_chunks(sock, buf, head):
        data += piece
        if len(data) > MAX_JSON_BODY:
            raise Refused("body too large", 413)
    if not data:
        return {}
    try:
        doc = json.loads(data)
    except ValueError as e:
        raise Refused("body must be JSON", 400) from e
    if not isinstance(doc, dict):
        raise Refused("body must be a JSON object", 400)
    return doc


PASS_HEADERS = {"content-type", "user-agent", "accept"}


def upstream_head(method: str, target: str, head: Head, *, hijack: bool, length: int | None, chunked: bool) -> bytes:
    lines = [f"{method} {target} HTTP/1.1", "Host: docker"]
    lines += [f"{k}: {v}" for k, v in head.headers if k.lower() in PASS_HEADERS]
    lines += ["Connection: Upgrade", "Upgrade: tcp"] if hijack else ["Connection: close"]
    if length is not None:
        lines.append(f"Content-Length: {length}")
    elif chunked:
        lines.append("Transfer-Encoding: chunked")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


def send_error(sock, code: int, why: str) -> None:
    body = json.dumps({"message": f"evalskills docker proxy: {why}"}).encode()
    reason = http.client.responses.get(code, "Error")
    head = f"HTTP/1.1 {code} {reason}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n"
    try:
        sock.sendall((head + "Connection: close\r\n\r\n").encode() + body)
    except OSError:
        pass


def pipe(a: socket.socket, b: socket.socket, idle: float, both: bool) -> None:
    """Copy b→a (and a→b when ``both``) until either side closes or goes quiet for ``idle`` seconds."""
    socks = [a, b] if both else [b]
    while True:
        ready, _, _ = select.select(socks, [], [], idle)
        if not ready:
            return
        for s in ready:
            data = s.recv(65536)
            if not data:
                if s is a:
                    try:
                        b.shutdown(socket.SHUT_WR)  # half-close: stdin ended, output may still come
                    except OSError:
                        return
                    socks = [b]
                    break
                return
            (b if s is a else a).sendall(data)


# ---------------------------------------------------------------- isolation

MASQUERADE = "com.docker.network.bridge.enable_ip_masquerade"
INHIBIT = "com.docker.network.bridge.inhibit_ipv4"


def isolation_status(cfg: dict, daemon: Daemon) -> tuple[bool, str]:
    """What keeps a trial in (#137, ADR-0003 amended 2026-11-14), read back from the daemon before every pilot:
    - every trial network is internal, with masquerading off and no address on the host;
    - each service runs with ip_forward=0, publishes nothing, and is on its egress network and trial networks only."""
    try:
        nets = daemon.get("/networks?" + urlencode({"filters": json.dumps({"label": [f"{OWNER_LABEL}=1"]})})) or []
        owned = set()
        for n in nets:
            doc = daemon.get(f"/networks/{n.get('Id', '')}") or {}
            opts = doc.get("Options") or {}
            if doc.get("Internal") is not True or opts.get(INHIBIT) != "true" or opts.get(MASQUERADE) != "false":
                return False, f"trial network {str(doc.get('Name'))[:64]} lost its isolation flags"
            owned.add(doc.get("Name"))
        for container in cfg["services"].values():
            doc = daemon.get(f"/containers/{container}/json")
            if doc is None or not (doc.get("State") or {}).get("Running"):
                return False, f"{container} is not running"
            hc = doc.get("HostConfig") or {}
            if (hc.get("Sysctls") or {}).get("net.ipv4.ip_forward") != "0":
                return False, f"{container} may forward (net.ipv4.ip_forward is not 0)"
            if hc.get("NetworkMode") == "host" or any(hc.get("PortBindings") or {}):
                return False, f"{container} is on the host network or publishes a port"
            on = set((doc.get("NetworkSettings") or {}).get("Networks") or {})
            if cfg["egress_network"] not in on or on - owned - {cfg["egress_network"]}:
                return False, f"{container} is on a network that is neither its egress network nor a trial's"
    except Refused as e:
        return False, f"could not read the daemon ({e})"
    return True, "ok"


# ---------------------------------------------------------------- server


class Handler(socketserver.BaseRequestHandler):
    cfg: dict
    policy: Policy

    def handle(self):
        client = self.request
        client.settimeout(60)
        buf = bytearray()
        try:
            head = read_head(client, buf)
            if head is None:
                return
            self.serve(client, buf, head)
        except Refused as r:
            log("refused", why=str(r), code=r.code)
            send_error(client, r.code, str(r))
        except OSError as e:
            log("error", why=type(e).__name__)
        except Exception as e:  # a bug must fail closed, never forward
            log("error", why=type(e).__name__)
            send_error(client, 500, "internal error")

    def serve(self, client, buf, head: Head):
        path, _, query = head.target.partition("?")
        if head.method == "GET" and path == "/_evalskills/isolation":
            ok, why = isolation_status(self.cfg, self.policy.daemon)
            body = json.dumps({"ok": ok, "why": why}).encode()
            code = 200 if ok else 503
            client.sendall(
                f"HTTP/1.1 {code} {http.client.responses[code]}\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                + body
            )
            return
        if path == "/_evalskills/build":
            return self.serve_build(client, buf, head, query)
        action, kind, ident, bare = route(head.method, path)
        if kind == "exec":
            if not self.policy.daemon.owned("container", self.policy.daemon.exec_container(ident)):
                raise Refused("exec instance is not in one of the proxy's containers")
        elif kind and not self.policy.daemon.owned(kind, ident):
            raise Refused(f"{kind} is not one of the proxy's")
        if action == "filter":
            query = self.policy.owner_filtered(query)
        asked = {v.strip("/") for k, v in parse_qsl(query) if k == "name"}
        if action == "container_create" and asked & set(self.cfg["services"].values()):
            raise Refused("a trial container may not take a service's name")
        if action == "network_delete":
            self.policy.daemon.detach_services(ident, self.cfg["services"])
            action = "pass"
        target = path + ("?" + query if query else "")

        body, stream = None, False
        rewrite = {
            "container_create": self.policy.rewrite_container_create,
            "network_create": self.policy.rewrite_network_create,
            "exec_create": self.policy.rewrite_exec_create,
            "exec_start": self.policy.rewrite_exec_start,
            "network_attach": self.check_network_attach,
        }.get(action)
        if rewrite:
            doc = rewrite(read_json_body(client, buf, head))
            if action == "network_create":
                try:
                    return self.forward(client, buf, head, action, bare, target, json.dumps(doc).encode(), False)
                finally:
                    self.policy.release_subnet(doc)
            body = json.dumps(doc).encode()
        elif action == "stream":
            stream = True
        elif action in ("pass", "filter", "hijack"):
            if head.get("content-length") not in (None, "0") or head.get("transfer-encoding"):
                raise Refused("this route takes no body", 400)
        self.forward(client, buf, head, action, bare, target, body, stream)

    def forward(self, client, buf, head: Head, action: str, bare: str, target: str, body, stream: bool) -> None:
        hijack = action == "hijack" or (action == "exec_start" and not json.loads(body).get("Detach"))

        up = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            up.settimeout(60)
            up.connect(self.cfg["upstream"])
            log("forward", method=head.method, route=re.sub(ID, "…", bare)[:80])
            length = len(body) if body is not None else (None if stream else 0)
            up.sendall(upstream_head(head.method, target, head, hijack=hijack, length=length, chunked=stream))
            if body:
                up.sendall(body)
            elif stream:
                for piece in body_chunks(client, buf, head):
                    up.sendall(b"%x\r\n%s\r\n" % (len(piece), piece))
                up.sendall(b"0\r\n\r\n")
            client.settimeout(None)
            up.settimeout(None)
            if not hijack:
                pipe(client, up, self.cfg["idle_timeout_s"], both=False)
                return
            # Forward the daemon's answer; only once it has switched to a raw stream does client input flow up.
            resp = bytearray()
            while b"\r\n\r\n" not in resp:
                chunk = up.recv(65536)
                if not chunk:
                    client.sendall(bytes(resp))
                    return
                resp += chunk
                if len(resp) > MAX_HEAD:
                    raise Refused("daemon response head too large", 502)
            client.sendall(bytes(resp))
            status = bytes(resp).split(b" ", 2)[1:2]
            if status == [b"101"] or (status == [b"200"] and action == "hijack"):
                if buf:
                    up.sendall(bytes(buf))
                    buf.clear()
                pipe(client, up, self.cfg["idle_timeout_s"], both=True)
            else:
                pipe(client, up, self.cfg["idle_timeout_s"], both=False)
        finally:
            up.close()

    def serve_build(self, client, buf, head: Head, query: str) -> None:
        """The client sends only the context. Anything else it adds is refused, not dropped (#137)."""
        b = self.cfg["builds"]
        if head.method != "POST":
            raise Refused("builds are POST only", 405)
        if query or "?" in head.target:
            raise Refused("builds take no query parameters", 400)
        extra = sorted({k.lower() for k, _ in head.headers} - BUILD_HEADERS)
        if extra:
            raise Refused(f"builds take no {extra[0]} header", 400)
        # http.client sends ``Accept-Encoding: identity`` by itself; any other value asks for a compressed reply
        if any(k.lower() == "accept-encoding" and v.strip().lower() != "identity" for k, v in head.headers):
            raise Refused("builds take no accept-encoding header other than identity", 400)
        if (head.get("content-type") or "application/x-tar") != "application/x-tar":
            raise Refused("the build context must be application/x-tar", 400)
        if b is None:
            raise Refused("builds are not configured on this proxy", 503)
        try:
            with docker_build.spool(body_chunks(client, buf, head), b["max_bytes"]) as src:
                with tempfile.NamedTemporaryFile(prefix="evalskills-build-", suffix=".tar") as ctx:
                    sha = docker_build.prepare_context(src, ctx, b["allowlist"], max_files=b["max_files"])
                    ctx.flush()
                    log("build", dockerfile=sha[:12])
                    result = docker_build.build(
                        Path(ctx.name),
                        upstream=self.cfg["upstream"],
                        base_image=b["base_image"],
                        base_image_id=b["base_image_id"],
                        timeout=b["timeout_s"],
                    )
        except docker_build.BuildError as e:
            raise Refused(str(e), e.code) from e
        log("built", dockerfile=sha[:12], tag=result["tag"])
        body = json.dumps(result).encode()
        client.sendall(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )

    def check_network_attach(self, body: dict) -> dict:
        _only(body, {"Container": None, "EndpointConfig": None, "Force": _bools_false}, "NetworkConnect")
        if not re.fullmatch(ID, str(body.get("Container") or "")):
            raise Refused("NetworkConnect.Container must name a container")
        if not self.policy.daemon.owned("container", body["Container"]):
            raise Refused("container is not one of the proxy's")
        _only(body.get("EndpointConfig") or {}, ENDPOINT_FIELDS, "NetworkConnect.EndpointConfig")
        return body


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def make_server(cfg: dict) -> Server:
    """Bind the listen socket as rw for its owner and group only. Its folder must belong to us and be writable by
    nobody else, so nobody can swap the socket for a symlink, and the chown never follows one anyway."""
    path = Path(cfg["listen"])
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    st = os.lstat(path.parent)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o022:
        raise ConfigError("the listen socket's folder must be ours and writable by nobody else")
    if path.exists() or path.is_symlink():
        path.unlink()
    handler = type("BoundHandler", (Handler,), {"cfg": cfg, "policy": Policy(cfg, Daemon(cfg["upstream"]))})
    old = os.umask(0o117)  # the socket is born 0660: no chmod afterwards
    try:
        server = Server(str(path), handler)
    finally:
        os.umask(old)
    if cfg["listen_group"]:
        os.chown(path, -1, grp.getgrnam(cfg["listen_group"]).gr_gid, follow_symlinks=False)
    return server


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", required=True)
    args = ap.parse_args(argv)
    try:
        cfg = load_config(json.loads(Path(args.config).read_text()))
    except (OSError, ValueError) as e:
        print(f"config: {e}", file=sys.stderr)
        return 2
    server = make_server(cfg)
    log("listening", socket=cfg["listen"])
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
