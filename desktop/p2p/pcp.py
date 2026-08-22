"""PCP v2 client (RFC 6887) — port mappings and IPv6 firewall pinholes.

Why next to the UPnP code: UPnP-IGD only maps IPv4 behind NAT. For IPv6
there is no NAT to map — the router's stateful firewall must be asked to
open a PINHOLE, and PCP's MAP opcode is the modern way to ask (its v1
ancestor is NAT-PMP). The SAME request shape serves both families: sent
from a v4 socket it creates a v4 port mapping (an alternative to UPnP
that needs no SSDP), sent from a v6 socket it opens a v6 pinhole for the
sender's own address. Measured 2026-08-22: the Netgear RS200 answers PCP
v2 on 5351.

MAP is self-addressed by design: the internal address is the packet's
source, so a client can only open its own ports — no third-party
poisoning surface. Lifetimes are finite and refreshed by the caller
(portmap --keep, the future dual-stack reachability loop); lifetime 0
revokes.
"""

import ipaddress
import logging
import os
import socket
import struct
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

PCP_PORT = 5351
PCP_VERSION = 2
OP_ANNOUNCE = 0
OP_MAP = 1
PROTO_TCP = 6
PROTO_UDP = 17
DEFAULT_LIFETIME = 2 * 3600
TIMEOUT_S = 3.0

RESULT_CODES = {
    0: "SUCCESS", 1: "UNSUPP_VERSION", 2: "NOT_AUTHORIZED", 3: "MALFORMED_REQUEST",
    4: "UNSUPP_OPCODE", 5: "UNSUPP_OPTION", 6: "MALFORMED_OPTION", 7: "NETWORK_FAILURE",
    8: "NO_RESOURCES", 9: "UNSUPP_PROTOCOL", 10: "USER_EX_QUOTA", 11: "CANNOT_PROVIDE_EXTERNAL",
    12: "ADDRESS_MISMATCH", 13: "EXCESSIVE_REMOTE_PEERS",
}


@dataclass
class MapResult:
    success: bool
    result: int
    result_name: str
    lifetime: int
    external_ip: str
    external_port: int
    internal_port: int
    nonce: bytes = b""     # keep it: deletion must present the SAME nonce


def _pack_ip(ip: str) -> bytes:
    """A PCP address field is always 16 bytes; IPv4 rides v4-mapped."""
    a = ipaddress.ip_address(ip)
    return a.packed if a.version == 6 else b"\x00" * 10 + b"\xff\xff" + a.packed


def _unpack_ip(raw: bytes) -> str:
    if raw[:12] == b"\x00" * 10 + b"\xff\xff":
        return str(ipaddress.IPv4Address(raw[12:]))
    return str(ipaddress.IPv6Address(raw))


def default_gateway_v4() -> Optional[str]:
    try:
        if sys.platform == "win32":
            out = subprocess.run(["route", "print", "0.0.0.0"], capture_output=True,
                                 text=True, timeout=10).stdout
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[0] == "0.0.0.0" and parts[1] == "0.0.0.0":
                    return parts[2]
        else:
            out = subprocess.run(["ip", "route", "show", "default"], capture_output=True,
                                 text=True, timeout=10).stdout
            parts = out.split()
            if "via" in parts:
                return parts[parts.index("via") + 1]
    except Exception as e:
        logger.debug("gateway discovery failed: %s", e)
    return None


def build_map_request(client_ip: str, internal_port: int, *, protocol: int = PROTO_TCP,
                      external_port: int = 0, lifetime: int = DEFAULT_LIFETIME,
                      nonce: Optional[bytes] = None) -> bytes:
    """24-byte common header + 36-byte MAP payload. external_port 0 lets the
    server pick; for a v6 pinhole the suggested external ip is the client's
    own address (there is no NAT to translate through)."""
    nonce = nonce or os.urandom(12)
    assert len(nonce) == 12
    header = struct.pack("!BBHI", PCP_VERSION, OP_MAP, 0, int(lifetime)) + _pack_ip(client_ip)
    suggested = client_ip if ipaddress.ip_address(client_ip).version == 6 else "0.0.0.0"
    payload = (nonce + struct.pack("!B3xHH", protocol, int(internal_port),
                                   int(external_port)) + _pack_ip(suggested))
    return header + payload


def parse_map_response(data: bytes) -> MapResult:
    if len(data) < 60:
        raise ValueError(f"short PCP response ({len(data)}B)")
    version, op, result = data[0], data[1], data[3]
    if version != PCP_VERSION or op != (OP_MAP | 0x80):
        raise ValueError(f"not a PCP v2 MAP response (ver={version} op={op})")
    lifetime = struct.unpack("!I", data[4:8])[0]
    nonce = data[24:36]
    protocol, internal_port, external_port = struct.unpack("!B3xHH", data[36:44])
    return MapResult(success=result == 0, result=result,
                     result_name=RESULT_CODES.get(result, str(result)),
                     lifetime=lifetime, external_ip=_unpack_ip(data[44:60]),
                     external_port=external_port, internal_port=internal_port,
                     nonce=nonce)


def map_port(gateway: str, internal_port: int, *, protocol: int = PROTO_TCP,
             external_port: int = 0, lifetime: int = DEFAULT_LIFETIME,
             nonce: Optional[bytes] = None,
             timeout: float = TIMEOUT_S) -> Optional[MapResult]:
    """One MAP round-trip. The family of `gateway` decides the family of
    the mapping: a v6 gateway address opens a PINHOLE for this host's own
    v6 address; a v4 one creates a NAT mapping. Refresh and DELETE must
    present the creation nonce (the server answers NOT_AUTHORIZED
    otherwise — that is the off-path-deletion guard, measured on the
    RS200). None on timeout (no PCP server) — the caller falls back to
    UPnP or gives up."""
    fam = socket.AF_INET6 if ipaddress.ip_address(gateway).version == 6 else socket.AF_INET
    s = socket.socket(fam, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.connect((gateway, PCP_PORT))
        client_ip = s.getsockname()[0].split("%", 1)[0]
        nonce = nonce or os.urandom(12)
        req = build_map_request(client_ip, internal_port, protocol=protocol,
                                external_port=external_port, lifetime=lifetime,
                                nonce=nonce)
        s.send(req)
        for _ in range(3):
            data = s.recv(1100)
            if len(data) >= 4 and data[1] == (OP_MAP | 0x80):
                res = parse_map_response(data)
                (logger.info if res.success else logger.warning)(
                    "PCP MAP %s:%d → %s:%d — %s (lifetime %ds)",
                    client_ip, internal_port, res.external_ip, res.external_port,
                    res.result_name, res.lifetime)
                return res
        return None
    except (socket.timeout, OSError) as e:
        logger.debug("PCP MAP via %s failed: %s", gateway, e)
        return None
    finally:
        s.close()


def revoke(gateway: str, internal_port: int, nonce: bytes, *,
           protocol: int = PROTO_TCP) -> Optional[MapResult]:
    """Lifetime-0 MAP with the CREATION nonce deletes the mapping."""
    return map_port(gateway, internal_port, protocol=protocol, lifetime=0, nonce=nonce)
