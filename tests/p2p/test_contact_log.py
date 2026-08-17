"""Pure-logic slice of the contact log; the DB path is exercised live
(the middlewares record into the running Postgres, `--report` reads it)."""

import json

from desktop.p2p import contact_log as cl


def test_endpoint_family():
    assert cl.endpoint_family("/health") == "health"
    assert cl.endpoint_family("/api/sync/inventory") == "sync.inventory"
    assert cl.endpoint_family("/api/sync/pull/track-stats") == "sync.pull"
    assert cl.endpoint_family("/api/mb/search?q=x&limit=1") == "mb.search"
    assert cl.endpoint_family("/api/chat/key-rotation") == "chat.key_rotation"
    assert cl.endpoint_family("/api/relay/wake-stream?pubkey=ab") == "relay.wake_stream"
    assert cl.endpoint_family("/whatever") == "other"


def test_addr_ids_are_the_shared_formula_plus_a_subnet_sibling():
    a1, s1 = cl.addr_ids("203.0.113.7")
    a2, s2 = cl.addr_ids("203.0.113.200")
    a3, s3 = cl.addr_ids("203.0.114.7")
    assert a1 != a2 and s1 == s2 and s1 != s3
    from desktop.p2p.identity_registry import addr_uuid
    assert a1 == addr_uuid("203.0.113.7")
    a6, s6 = cl.addr_ids("2001:db8:1:2::9")
    _, s6b = cl.addr_ids("2001:db8:1:ffff::1")
    assert s6 == s6b
    assert cl.addr_ids(None) == (None, None)
    assert cl.addr_ids("not-an-ip") == (cl.addr_ids("not-an-ip")[0], None)


def test_extract_request_shape():
    assert cl.extract_request_shape("/api/mb/search?q=Tangerine%20Dream&limit=5", b"") == (1, ["tangerine dream"])
    assert cl.extract_request_shape("/api/mb/search?limit=5", b"") == (None, None)
    body = json.dumps({"names": ["Tangerine Dream", " Klaus Schulze ", ""] + [f"a{i}" for i in range(20)]}).encode()
    items, targets = cl.extract_request_shape("/api/mb/slice", body)
    assert items == 23 and targets == ["tangerine dream", "klaus schulze"] + [f"a{i}" for i in range(6)]
    assert cl.extract_request_shape("/api/sync/inventory", json.dumps({"track_uuids": ["a", "b"]}).encode()) == (2, None)
    assert cl.extract_request_shape("/api/sync/pull/track-stats", json.dumps({"uuids": ["a"]}).encode()) == (1, None)
    assert cl.extract_request_shape("/api/sync/offer", json.dumps({"recordings": []}).encode()) == (0, None)
    assert cl.extract_request_shape("/api/sync/inventory", b"{not json") == (None, None)
    assert cl.extract_request_shape("/api/mb/slice", b"[]") == (None, None)


def test_ema_cost_tracking_without_a_database():
    log = cl.ContactLog(conn_factory=None)
    for cpu in (100.0, 100.0, 100.0):
        log.record(endpoint="mb.slice", status=200, wall_ms=200.0, cpu_ms=cpu, addr="203.0.113.7")
    c = log._costs["mb.slice"]
    assert c["n"] == 3 and abs(c["cpu"] - 100.0) < 1e-9 and c["dirty"]
    log.record(endpoint="mb.slice", status=200, wall_ms=200.0, cpu_ms=200.0)
    assert 100.0 < log._costs["mb.slice"]["cpu"] < 110.0        # EMA_ALPHA = 0.05
    assert len(log._queue) == 4
    row = log._queue[0]
    assert row[4] == "mb.slice" and row[6] == 200 and row[2] is not None and row[3] is not None
