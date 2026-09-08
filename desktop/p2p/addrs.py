"""Address canonicalization and formatting — the IPv6-Ф0 groundwork.

Two invariants every address that leaves this module must satisfy:

- CANONICAL: one spelling per address. IPv6 has many textual forms
  (`2a00:0DB8::1` = `2a00:db8:0:0:0:0:0:1` = `[2a00:db8::1]`); the
  pseudonym formulas hash the STRING, so without canonicalization the
  same peer would fan out into several uuid5 tokens and quietly break the
  addr/subnet similarity axes, ban correlation and the registry. IPv4
  strings are already canonical — nothing changes for them.
- DIALABLE: an IPv6 literal inside a URL needs brackets
  (`https://[2a00:db8::1]:8801/...`); a bare `host:port` join is
  ambiguous for v6. `fmt_addr` is THE way an (ip, port) pair becomes
  text — candidate keys, URLs, log lines.

Zone ids (`fe80::1%eth0`) are stripped: they are link-local plumbing that
never belongs in a pseudonym or a peer URL.
"""

import ipaddress
from typing import Union


def canon_host(host: str) -> str:
    """One spelling per host: IPs through `ipaddress` (v6 compressed,
    brackets and zone id stripped), names lowercased. Never raises."""
    h = (host or "").strip().lower()
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    h = h.split("%", 1)[0]
    try:
        return ipaddress.ip_address(h).compressed
    except ValueError:
        return h


def is_ipv6(host: str) -> bool:
    try:
        return ipaddress.ip_address(canon_host(host)).version == 6
    except ValueError:
        return False


def fmt_host(host: str) -> str:
    """Canonical host, bracketed when a URL/`host:port` join needs it."""
    h = canon_host(host)
    return f"[{h}]" if ":" in h and is_ipv6(h) else h


def fmt_addr(host: str, port: Union[int, str]) -> str:
    """`host:port` text that is unambiguous for both families —
    `203.0.113.7:8801` / `[2a00:db8::1]:8801`."""
    return f"{fmt_host(host)}:{int(port)}"


def is_internet_vantage(host: str) -> bool:
    """Is this SOURCE address a point on the internet — one whose view of
    us says something about our reachability from outside? Loopback,
    RFC 1918, link-local, the Docker bridge gateway and carrier NAT
    (100.64/10) all answer False: a request from any of them proves only
    that we are reachable from THAT segment. The single definition behind
    both signals that become a reachability verdict — the passive inbound
    proof (sync_server) and the probe-connect call-back (both peer
    surfaces): a probe answered from a non-vantage is no verdict at all,
    because the master would dial the wrong network (its own container
    loopback, for a launcher on the same host). Never raises."""
    try:
        return ipaddress.ip_address(canon_host(host)).is_global
    except ValueError:
        return False
