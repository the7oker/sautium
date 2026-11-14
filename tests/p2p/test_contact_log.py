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


def test_a_request_s_items_are_counted_never_named():
    body = json.dumps({"names": ["Tangerine Dream", " Klaus Schulze ", ""] + [f"a{i}" for i in range(20)]}).encode()
    assert cl.request_items("/api/mb/slice", body) == 23
    assert cl.request_items("/api/lb/slice", json.dumps({"artist_mbids": ["x", "y"]}).encode()) == 2
    assert cl.request_items("/api/mb/search?q=Tangerine%20Dream&limit=5", b"") is None
    assert cl.request_items("/api/sync/inventory", json.dumps({"track_uuids": ["a", "b"]}).encode()) == 2
    assert cl.request_items("/api/sync/pull/track-stats", json.dumps({"uuids": ["a"]}).encode()) == 1
    assert cl.request_items("/api/sync/offer", json.dumps({"recordings": []}).encode()) == 0
    assert cl.request_items("/api/sync/inventory", b"{not json") is None
    assert cl.request_items("/api/mb/slice", b"[]") is None


def test_a_request_about_artists_leaves_no_row_only_its_cost():
    """The names a request carried and the moment a key or an address asked
    together tell which artists a node asked for: a slice or a catalogue
    search keeps neither, and its cost still prices the lane."""
    log = cl.ContactLog(conn_factory=None)
    for endpoint in ("mb.slice", "lb.slice", "mb.search"):
        log.record(endpoint=endpoint, status=200, wall_ms=5.0, cpu_ms=4.0, pubkey="ab" * 32,
                   addr="203.0.113.7", lane="identity", items=3)
        log.record(endpoint=endpoint, status=429, wall_ms=1.0, cpu_ms=0.5, addr="203.0.113.7")
    assert len(log._queue) == 0
    assert {e: c["n"] for e, c in log._costs.items()} == {"mb.slice": 1, "lb.slice": 1, "mb.search": 1}
    log.record(endpoint="sync.inventory", status=200, wall_ms=5.0, cpu_ms=4.0, pubkey="ab" * 32,
               addr="203.0.113.7", items=2)
    (row,) = log._queue
    assert row[4] == "sync.inventory" and row[11] == 2 and len(row) == 14


def test_ema_cost_tracking_without_a_database():
    log = cl.ContactLog(conn_factory=None)
    for cpu in (100.0, 100.0, 100.0):
        log.record(endpoint="sync.pull", status=200, wall_ms=200.0, cpu_ms=cpu, addr="203.0.113.7")
    c = log._costs["sync.pull"]
    assert c["n"] == 3 and abs(c["cpu"] - 100.0) < 1e-9 and c["dirty"]
    log.record(endpoint="sync.pull", status=200, wall_ms=200.0, cpu_ms=200.0)
    assert 100.0 < log._costs["sync.pull"]["cpu"] < 110.0        # EMA_ALPHA = 0.05
    ema = log._costs["sync.pull"]["cpu"]
    for status in (402, 403, 429, 503):                         # refusals are logged, never priced into base
        log.record(endpoint="sync.pull", status=status, wall_ms=1.0, cpu_ms=0.5)
    assert log._costs["sync.pull"]["cpu"] == ema and log._costs["sync.pull"]["n"] == 4
    assert "gate.quote" not in log._costs
    log.record(endpoint="gate.quote", status=402, wall_ms=1.0, cpu_ms=0.5)
    assert "gate.quote" not in log._costs
    assert len(log._queue) == 9
    row = log._queue[0]
    assert row[4] == "sync.pull" and row[6] == 200 and row[2] is not None and row[3] is not None
