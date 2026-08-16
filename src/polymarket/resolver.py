"""DNS-over-HTTPS fallback for when the system resolver refuses the API hostnames.

Some ISP resolvers answer NXDOMAIN for polymarket.com. Only the name lookup is
affected -- the API itself answers normally once you have an address -- so the
whole problem can be solved inside this process by asking a public resolver over
HTTPS and pinning the answer, instead of changing DNS for the whole machine.

The DoH endpoints are addressed by IP literal (1.1.1.1, 8.8.8.8), whose TLS
certificates carry those IPs in their SANs. That means bootstrapping needs no
working DNS at all, and certificate verification stays on throughout.
"""

from __future__ import annotations

import logging
import socket
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)

DOH_ENDPOINTS = (
    ("https://1.1.1.1/dns-query", "cloudflare-dns.com"),
    ("https://8.8.8.8/resolve", "dns.google"),
)

CACHE_TTL = 300.0

_cache: dict[str, tuple[float, list[str]]] = {}
_original_getaddrinfo = None
_patched_hosts: set[str] = set()


def system_resolves(host: str) -> bool:
    getaddrinfo = _original_getaddrinfo or socket.getaddrinfo
    try:
        getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return False
    return True


def doh_resolve(host: str, timeout: float = 10.0) -> list[str]:
    """Look `host` up over HTTPS. Returns A/AAAA addresses, best-effort."""
    for url, sni in DOH_ENDPOINTS:
        try:
            with httpx.Client(timeout=timeout) as client:
                # sni_hostname makes TLS negotiate (and verify) against the
                # resolver's real name even though the URL is a bare IP.
                response = client.get(
                    url,
                    params={"name": host, "type": "A"},
                    headers={"accept": "application/dns-json"},
                    extensions={"sni_hostname": sni},
                )
            response.raise_for_status()
            payload: dict[str, Any] = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.debug("DoH lookup via %s failed: %s", url, exc)
            continue

        addresses = [
            str(answer.get("data"))
            for answer in payload.get("Answer") or []
            if answer.get("type") in (1, 28) and answer.get("data")
        ]
        if addresses:
            log.debug("DoH %s -> %s (via %s)", host, addresses, sni)
            return addresses
    return []


def _cached(host: str) -> list[str]:
    hit = _cache.get(host)
    if hit and time.monotonic() - hit[0] < CACHE_TTL:
        return hit[1]
    addresses = doh_resolve(host)
    if addresses:
        _cache[host] = (time.monotonic(), addresses)
    elif hit:
        return hit[1]  # stale beats nothing
    return addresses


def install(hosts: list[str]) -> bool:
    """Route `hosts` through DoH by substituting the address in getaddrinfo.

    Returns True if at least one host was resolved and the shim is active. Only
    the named hosts are affected; everything else takes the normal path.
    """
    global _original_getaddrinfo

    resolved = {host: _cached(host) for host in hosts}
    usable = {host: ips for host, ips in resolved.items() if ips}
    if not usable:
        return False

    _patched_hosts.update(usable)

    if _original_getaddrinfo is None:
        _original_getaddrinfo = socket.getaddrinfo

        def getaddrinfo(host, port, *args, **kwargs):  # type: ignore[no-untyped-def]
            if host in _patched_hosts:
                addresses = _cached(host)
                if addresses:
                    return _original_getaddrinfo(addresses[0], port, *args, **kwargs)
            return _original_getaddrinfo(host, port, *args, **kwargs)

        socket.getaddrinfo = getaddrinfo

    for host, ips in usable.items():
        log.info("resolving %s via DNS-over-HTTPS -> %s", host, ips[0])
    return True


def ensure(hosts: list[str], mode: str = "auto") -> bool:
    """Apply the DoH shim according to `mode`: auto | always | never.

    "auto" only steps in when the system resolver cannot answer, so a machine with
    working DNS behaves exactly as if this module did not exist.
    """
    if mode == "never":
        return False
    if mode == "auto" and all(system_resolves(host) for host in hosts):
        return False
    return install(hosts)
