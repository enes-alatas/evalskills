import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

import server  # noqa: E402

CADDY = "172.18.0.2"  # the proxy, trusted once TRUSTED_PROXIES names it
NEIGHBOUR = "172.18.0.9"  # another container on the same network
OUTSIDER = "203.0.113.9"  # TEST-NET-3: a direct, untrusted peer


@pytest.fixture(autouse=True)
def caddy_pinned(monkeypatch):
    monkeypatch.setattr(server, "TRUSTED_PROXIES", [server.ipaddress.ip_network(CADDY + "/32")])


@pytest.mark.parametrize(
    "peer, forwarded, expected",
    [
        (CADDY, "198.51.100.7", "198.51.100.7"),  # the proxy's own entry
        (CADDY, "1.2.3.4, 198.51.100.7", "198.51.100.7"),  # a client-written entry on the left is ignored
        (CADDY, None, CADDY),  # no header: the peer itself
        (OUTSIDER, "1.2.3.4", OUTSIDER),  # an untrusted peer cannot claim an address
        (NEIGHBOUR, "1.2.3.4", NEIGHBOUR),  # nor can a neighbour on the proxy's network
        ("not-an-ip", "1.2.3.4", "not-an-ip"),
    ],
)
def test_client_ip(peer, forwarded, expected):
    assert server.client_ip(peer, forwarded) == expected


def test_two_hops_take_the_second_entry_from_the_right(monkeypatch):
    monkeypatch.setattr(server, "TRUSTED_PROXY_HOPS", 2)
    assert server.client_ip(CADDY, "1.2.3.4, 198.51.100.7, 10.0.0.5") == "198.51.100.7"
    assert server.client_ip(CADDY, "198.51.100.7") == CADDY  # fewer entries than hops: don't trust a short chain


def test_hops_zero_ignores_the_header(monkeypatch):
    monkeypatch.setattr(server, "TRUSTED_PROXY_HOPS", 0)
    assert server.client_ip(CADDY, "198.51.100.7") == CADDY


def test_by_default_only_loopback_is_trusted(monkeypatch):
    monkeypatch.setattr(server, "TRUSTED_PROXIES", server.parse_networks(server.DEFAULT_TRUSTED_PROXIES))
    assert server.client_ip("127.0.0.1", "198.51.100.7") == "198.51.100.7"
    assert server.client_ip(CADDY, "198.51.100.7") == CADDY  # a private-range peer is not trusted unless named


def test_the_env_example_placeholder_refuses_to_parse():
    example = (Path(__file__).resolve().parents[1] / "deploy" / "env.example").read_text()
    (value,) = [line.split("=", 1)[1] for line in example.splitlines() if line.startswith("TRUSTED_PROXIES=")]
    with pytest.raises(ValueError):
        server.parse_networks(value)
