"""Pure-logic slice of the identity registry. Everything that touches the
database runs as `python -m desktop.p2p.identity_registry --selftest`
against the live Postgres (throwaway keys, cleaned up)."""

from datetime import datetime, timedelta, timezone

from desktop.p2p import identity_registry as reg


def test_ripening_is_computed_from_the_authority_anchor():
    now = datetime(2026, 8, 17, 12, 0, 0, tzinfo=timezone.utc)
    fresh = {"issued_at": "2026-08-17T11:30:00Z"}
    ripe = {"issued_at": "2026-08-17T10:59:59Z"}
    assert not reg.is_ripe(fresh, now)
    assert reg.is_ripe(ripe, now)
    edge = {"issued_at": (now - timedelta(seconds=reg.RIPENING_SECONDS)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    assert reg.is_ripe(edge, now)


def test_parse_issued_at_is_utc_seconds():
    dt = reg.parse_issued_at("2026-07-05T10:06:23Z")
    assert dt == datetime(2026, 7, 5, 10, 6, 23, tzinfo=timezone.utc)


def test_admission_maps_to_http():
    assert reg.Admission("verified").http_status == 200
    assert reg.Admission("busy", "x", 30).http_status == 503
    assert reg.Admission("rate_limited", "x", 3600).http_status == 429
    for s in ("invalid", "proof_required", "failed", "banned"):
        assert reg.Admission(s).http_status == 403


def test_addr_uuid_is_the_shared_node_addr_formula():
    assert reg.addr_uuid(None) is None
    assert reg.addr_uuid("203.0.113.7") == reg.addr_uuid("HTTPS://203.0.113.7:8801/x")
    assert reg.addr_uuid("203.0.113.7") != reg.addr_uuid("203.0.113.8")
