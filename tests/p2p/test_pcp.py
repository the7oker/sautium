"""PCP v2 packet contracts (RFC 6887): both address families, the nonce
echo, and result-code parsing. The live half ran against the Netgear
RS200 (MAP SUCCESS; deletion without the creation nonce → NOT_AUTHORIZED)."""

import struct

import pytest

from desktop.p2p import pcp


def _fake_response(req: bytes, result=0, lifetime=120, external_ip="91.227.181.179",
                   external_port=None):
    nonce = req[24:36]
    protocol, internal_port, req_ext = struct.unpack("!B3xHH", req[36:44])
    header = struct.pack("!BBBBII", pcp.PCP_VERSION, pcp.OP_MAP | 0x80, 0, result,
                         lifetime, 12345) + b"\x00" * 12
    payload = nonce + struct.pack("!B3xHH", protocol, internal_port,
                                  external_port if external_port is not None else req_ext)
    return header + payload + pcp._pack_ip(external_ip)


def test_v4_request_uses_mapped_address_and_round_trips():
    req = pcp.build_map_request("192.168.1.188", 8801, external_port=8801,
                                lifetime=7200, nonce=b"n" * 12)
    assert len(req) == 60
    assert req[0] == 2 and req[1] == pcp.OP_MAP
    assert struct.unpack("!I", req[4:8])[0] == 7200
    assert req[8:24] == b"\x00" * 10 + b"\xff\xff" + bytes([192, 168, 1, 188])
    assert req[24:36] == b"n" * 12
    res = pcp.parse_map_response(_fake_response(req))
    assert res.success and res.result_name == "SUCCESS"
    assert res.external_ip == "91.227.181.179" and res.external_port == 8801
    assert res.internal_port == 8801 and res.nonce == b"n" * 12 and res.lifetime == 120


def test_v6_request_is_a_pinhole_for_the_own_address():
    req = pcp.build_map_request("2a00:db8::1", 8801, nonce=b"m" * 12)
    import ipaddress
    assert req[8:24] == ipaddress.ip_address("2a00:db8::1").packed
    # the suggested external address IS the client's address — no NAT in v6
    assert req[44:60] == ipaddress.ip_address("2a00:db8::1").packed
    res = pcp.parse_map_response(_fake_response(req, external_ip="2a00:db8::1"))
    assert res.success and res.external_ip == "2a00:db8::1"


def test_result_codes_and_malformed_responses():
    req = pcp.build_map_request("192.168.1.188", 8801)
    denied = pcp.parse_map_response(_fake_response(req, result=2, lifetime=0, external_port=0))
    assert not denied.success and denied.result_name == "NOT_AUTHORIZED"
    with pytest.raises(ValueError):
        pcp.parse_map_response(b"\x02" + b"\x00" * 10)                 # short
    bad = bytearray(_fake_response(req)); bad[0] = 1                    # wrong version
    with pytest.raises(ValueError):
        pcp.parse_map_response(bytes(bad))
    bad = bytearray(_fake_response(req)); bad[1] = pcp.OP_ANNOUNCE | 0x80
    with pytest.raises(ValueError):
        pcp.parse_map_response(bytes(bad))
