"""evalskills egress proxy: trials reach package hosts through this, and nothing else (#123, item 6).

Harbor installs the agent inside every trial at setup (system packages, Node through nvm, `npm i -g opencode-ai`),
so trials need a few download hosts. Their networks let them reach only the gateway and this proxy. The proxy allows
HTTPS tunnels (CONNECT, port 443) and plain HTTP GET/HEAD (port 80, for package mirrors) to allowlisted hostnames
only, and only when every address a name resolves to is public, so an allowlisted name can never point a trial at
the host or the LAN. It holds no secret. It logs hostnames, decisions and byte counts, never paths or bodies.
Standard library only.

Run: python3 -I gateway/egress_proxy.py --config /etc/evalskills/egress-proxy.json --deny <the host's addresses>
"""

import argparse
import datetime as dt
import ipaddress
import json
import re
import select
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

DEFAULTS = {
    "listen_host": "127.0.0.1",
    "listen_port": 3128,
    "allowed_hosts": [],
    "allowed_clients": ["127.0.0.0/8"],
    "idle_timeout_s": 120,
    # Pilot only (#236, ADR-0003 amended 2026-11-20): any bare hostname passes the host check. Every other rule holds.
    "open_egress": False,
}
HOSTNAME = re.compile(r"(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", re.IGNORECASE)
HOP = {"proxy-connection", "proxy-authorization", "connection", "keep-alive", "te", "trailer", "upgrade"}


class ConfigError(ValueError):
    pass


def load_config(path: str) -> dict:
    raw = json.loads(Path(path).read_text())
    unknown = set(raw) - set(DEFAULTS)
    if unknown:
        raise ConfigError(f"unknown config keys: {', '.join(sorted(unknown))}")
    cfg = {**DEFAULTS, **raw}
    hosts = cfg["allowed_hosts"]
    if not hosts or not all(isinstance(h, str) and HOSTNAME.fullmatch(h) for h in hosts):
        raise ConfigError("allowed_hosts must list bare hostnames")
    cfg["allowed_hosts"] = {h.lower().rstrip(".") for h in hosts}
    try:  # who may use the proxy: the trial range in deploy, never the app's network or the internet
        cfg["allowed_clients"] = [ipaddress.ip_network(c) for c in cfg["allowed_clients"]]
    except (TypeError, ValueError) as e:
        raise ConfigError(f"allowed_clients must list networks: {e}") from e
    if not cfg["allowed_clients"]:
        raise ConfigError("allowed_clients must list at least one network")
    if not isinstance(cfg["open_egress"], bool):
        raise ConfigError("open_egress must be true or false")
    return cfg


def log(event: str, **fields) -> None:
    print(
        json.dumps({"t": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"), "event": event, **fields}),
        file=sys.stderr,
        flush=True,
    )


def allowed(host: str, allowlist: set, open_egress: bool = False) -> bool:
    """Exact names only: no wildcards, no IP literals. With open_egress, any bare hostname, still no IP literals."""
    host = host.lower().rstrip(".")
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    if open_egress:  # the length check first: HOSTNAME's own lookahead bounds it too, but never rely on that alone
        return len(host) <= 253 and bool(HOSTNAME.fullmatch(host))
    return host in allowlist


def is_public(ip) -> bool:
    return ip.is_global


def resolve_public(host: str, port: int, resolver=socket.getaddrinfo, public=is_public, denied=()) -> list:
    """Every address must be public and outside `denied`, or the name is refused outright (no picking the safe one).
    `denied` holds the host's own addresses (#236): a public one passes is_public, and the host would see the
    connection come from this container's private address."""
    infos = resolver(host, port, type=socket.SOCK_STREAM)
    addrs = [i[4] for i in infos]
    ips = [ipaddress.ip_address(a[0].split("%")[0]) for a in addrs]
    if not ips or not all(public(ip) and not any(ip in net for net in denied) for ip in ips):
        return []
    return addrs


def parse_denied(value: str) -> list:
    """The --deny list: comma-separated addresses or networks. Empty is refused, so the proxy never starts without
    the host's own addresses (9-live.sh passes them)."""
    try:
        nets = [ipaddress.ip_network(v.strip(), strict=False) for v in value.split(",") if v.strip()]
    except ValueError as e:
        raise ConfigError(f"--deny must list addresses or networks: {e}") from e
    if not nets:
        raise ConfigError("--deny is empty: start the proxy through deploy/gvisor/host/9-live.sh")
    return nets


def make_handler(cfg: dict, resolver=socket.getaddrinfo, public=is_public):
    allowlist, open_egress = cfg["allowed_hosts"], cfg["open_egress"]

    class Handler(BaseHTTPRequestHandler):
        server_version = "evalskills-egress"
        sys_version = ""
        timeout = 60  # a client that never sends a request line cannot hold a thread forever

        def parse_request(self):
            if not super().parse_request():
                return False
            peer = ipaddress.ip_address(self.client_address[0])
            if not any(peer in net for net in cfg["allowed_clients"]):
                log("denied", host="", why="client not allowed")
                self.send_error(403, "client not allowed")
                return False
            return True

        def log_message(self, *a):
            pass

        def deny(self, code: int, host: str, why: str) -> None:
            log("denied", host=host[:255], why=why)
            self.send_error(code, why)

        def connect_upstream(self, host: str, port: int):
            try:
                addrs = resolve_public(host, port, resolver, public, cfg.get("denied_networks", ()))
            except OSError:
                return None, "does not resolve"
            if not addrs:
                return None, "resolves to a non-public or denied address"
            for addr in addrs:
                try:
                    return socket.create_connection(addr[:2], timeout=30), None
                except OSError:
                    continue
            return None, "unreachable"

        def do_CONNECT(self):
            host, _, port = self.path.rpartition(":")
            if not host or port != "443":
                return self.deny(403, self.path, "only host:443 tunnels")
            if not allowed(host, allowlist, open_egress):
                return self.deny(403, host, "host not allowed")
            up, why = self.connect_upstream(host, 443)
            if not up:
                return self.deny(502, host, why)
            self.send_response(200, "Connection established")
            self.end_headers()
            sent, got = self.pipe(self.connection, up)
            log("tunnel", host=host, sent=sent, received=got)

        def pipe(self, a, b):
            counts = {a: 0, b: 0}
            try:
                while True:
                    ready, _, _ = select.select([a, b], [], [], cfg["idle_timeout_s"])
                    if not ready:
                        break
                    for s in ready:
                        data = s.recv(65536)
                        if not data:
                            return counts[a], counts[b]
                        (b if s is a else a).sendall(data)
                        counts[s] += len(data)
            except OSError:
                pass
            finally:
                b.close()
                self.close_connection = True
            return counts[a], counts[b]

        def plain(self):
            u = urlsplit(self.path)
            host = u.hostname or ""
            try:
                port = u.port or 80
            except ValueError:
                return self.deny(400, host, "bad port")
            if u.scheme != "http" or port != 80:
                return self.deny(403, host, "only http://host/ on port 80; use CONNECT for https")
            if not allowed(host, allowlist, open_egress):
                return self.deny(403, host, "host not allowed")
            up, why = self.connect_upstream(host, 80)
            if not up:
                return self.deny(502, host, why)
            target = (u.path or "/") + (f"?{u.query}" if u.query else "")
            head = [f"{self.command} {target} HTTP/1.1", f"Host: {host}", "Connection: close"]
            head += [f"{k}: {v}" for k, v in self.headers.items() if k.lower() not in HOP | {"host"}]
            try:
                up.sendall(("\r\n".join(head) + "\r\n\r\n").encode("latin-1"))
                got = 0
                while data := up.recv(65536):
                    self.wfile.write(data)
                    got += len(data)
            except OSError:
                got = -1
            finally:
                up.close()
                self.close_connection = True
            log("http", host=host, received=got)

        do_GET = plain
        do_HEAD = plain

        def refuse_method(self):
            self.deny(405, urlsplit(self.path).hostname or "", "method not allowed")

        do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = refuse_method

    return Handler


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="egress_proxy")
    ap.add_argument("--config", required=True)
    ap.add_argument("--deny", required=True, help="the host's own addresses, comma-separated (9-live.sh)")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    cfg["denied_networks"] = parse_denied(args.deny)
    httpd = ThreadingHTTPServer((cfg["listen_host"], cfg["listen_port"]), make_handler(cfg))
    httpd.daemon_threads = True
    log(
        "listening",
        host=cfg["listen_host"],
        port=cfg["listen_port"],
        allowed="any public hostname" if cfg["open_egress"] else sorted(cfg["allowed_hosts"]),
        denied=len(cfg["denied_networks"]),  # a count: the host's addresses stay out of the log
    )
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
