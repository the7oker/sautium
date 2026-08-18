"""Similarity — conjunctions price, single axes do not; anchoring; relay
diversity. Pure cases over registry-shaped rows (the DB path is the
module's --selftest against the live database)."""

import os
from datetime import datetime, timedelta, timezone

from desktop.p2p import similarity as sim

T0 = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


def row(pubkey=None, issued=T0, token=None, klass=None, domain=None, addr=None, subnet=None,
        difficulty=32, status="verified", banned=False):
    return {"pubkey": pubkey or os.urandom(32).hex(), "issued_at": issued, "email_token": token,
            "email_class": klass, "email_domain_token": domain, "difficulty": difficulty,
            "first_addr": addr, "last_addr": addr, "first_subnet": subnet, "last_subnet": subnet,
            "status": status, "banned": banned}


def test_single_axes_are_worth_nothing_and_conjunctions_add_up():
    a = row(issued=T0, subnet="s1", domain="d1", klass="other")
    assert sim.pair_score(a, row(issued=T0 + timedelta(seconds=30)))[0] == 0.0          # birth alone
    assert sim.pair_score(a, row(issued=T0 - timedelta(days=90), subnet="s1"))[0] == 0.0  # subnet alone
    assert sim.pair_score(a, row(issued=T0 - timedelta(days=90), domain="d1", klass="other"))[0] == 0.0
    score, hits = sim.pair_score(a, row(issued=T0 + timedelta(seconds=30), subnet="s1"))
    assert hits == ["birth", "subnet"] and score == sim.W_BIRTH_10MIN + sim.W_SUBNET
    score, hits = sim.pair_score(a, row(issued=T0 + timedelta(hours=3), subnet="s1", domain="d1", klass="other"))
    assert hits == ["birthday", "subnet", "domain"] and score == sim.W_BIRTH_DAY + sim.W_SUBNET + sim.W_DOMAIN
    # an exact address beats the subnet (not both), gmail-class domains carry nothing
    b = row(issued=T0 + timedelta(seconds=5), addr="a1", subnet="s1", domain="gmail", klass="major")
    a2 = dict(a, first_addr="a1", last_addr="a1", email_domain_token="gmail", email_class="major")
    score, hits = sim.pair_score(a2, b)
    assert hits == ["birth", "addr"] and score == sim.W_BIRTH_10MIN + sim.W_ADDR
    # symmetric
    assert sim.pair_score(b, a2) == sim.pair_score(a2, b)
    # the node-side domain table decides informativeness: a populous provider's real
    # token says nothing even if the Worker's label drifted; a disposable one is a fleet marker
    from desktop.p2p import email_domains as ed
    gmail, mailinator = ed.EMAIL_DOMAINS["gmail.com"][2], ed.EMAIL_DOMAINS["mailinator.com"][2]
    x = row(issued=T0, subnet="s9", domain=gmail, klass="other")
    assert sim.pair_score(x, row(issued=T0 + timedelta(seconds=9), subnet="s9", domain=gmail, klass="other"))[1] == ["birth", "subnet"]
    y = row(issued=T0, subnet="s9", domain=mailinator, klass="disposable")
    assert sim.pair_score(y, row(issued=T0 + timedelta(seconds=9), subnet="s9", domain=mailinator, klass="disposable"))[1] == ["birth", "subnet", "domain"]


def test_mailbox_is_a_hard_link_and_the_wave_hint_is_an_axis():
    a = row(token="t1", issued=T0)
    score, hits = sim.pair_score(a, row(token="t1", issued=T0 - timedelta(days=400)))
    assert hits == ["mailbox"] and score == sim.W_MAILBOX                                # alone, still counts
    a = row(issued=T0, difficulty=64, subnet="s1")
    b = row(issued=T0 + timedelta(seconds=40), difficulty=96, subnet="s1")
    score, hits = sim.pair_score(a, b)
    assert hits == ["birth", "wave", "subnet"] and score == sim.W_BIRTH_10MIN + sim.W_WAVE + sim.W_SUBNET
    c = row(issued=T0 + timedelta(seconds=40), difficulty=32, subnet="s1")               # paid the base: no wave
    assert sim.pair_score(a, c)[1] == ["birth", "subnet"]


def test_fleet_prices_only_when_anchored_and_the_cgnat_yard_never_does():
    fleet = [row(issued=T0 + timedelta(seconds=10 * k), subnet="s-fleet", domain="odd", klass="disposable")
             for k in range(8)]
    me = fleet[0]
    members = sim.cluster_from_rows(me, fleet)
    assert len(members) == 7 and all(m["score"] >= sim.CLUSTER_THRESHOLD for m in members)
    assert sim.sim_mult_from_cluster(members) == 1.0                                     # nobody caught yet
    fleet[7]["banned"] = True
    members = sim.cluster_from_rows(me, fleet)
    mult = sim.sim_mult_from_cluster(members)
    assert 1.0 < mult <= sim.SIM_MULT_MAX
    expected = 1.0 + sim.ANCHOR_GAIN * min(sim.SCORE_CAP, sim.W_BIRTH_10MIN + sim.W_SUBNET + sim.W_DOMAIN)
    assert abs(mult - expected) < 1e-9
    fleet[6]["status"] = "failed"                                                        # a second anchor
    assert sim.sim_mult_from_cluster(sim.cluster_from_rows(me, fleet)) == min(sim.SIM_MULT_MAX, 1.0 + 2 * (expected - 1.0))
    # honest CGNAT neighbours: one subnet, births months apart, mailboxes on gmail
    yard = [row(issued=T0 - timedelta(days=30 * k), subnet="s-yard", domain="gmail", klass="major") for k in range(8)]
    yard[7]["banned"] = True
    assert sim.cluster_from_rows(yard[0], yard) == []
    assert sim.sim_mult_from_cluster(sim.cluster_from_rows(yard[0], yard)) == 1.0
    # a launch wave: same day, everything else different → 0.5 alone → nothing
    wave = [row(issued=T0 + timedelta(minutes=30 * k), subnet=f"s{k}") for k in range(8)]
    wave[3]["banned"] = True
    assert sim.cluster_from_rows(wave[0], wave) == []


def test_relay_order_prefers_distinct_subnets_then_known_relays():
    held = [{"ip": "198.51.100.7", "pubkey": "h1"}]
    candidates = [
        {"ip": "198.51.100.9", "pubkey": "c1", "known": True},        # same /24 as the held relay
        {"ip": "203.0.113.5", "pubkey": "c2", "known": False},
        {"ip": "192.0.2.10", "pubkey": "c3", "known": True},
        {"ip": "203.0.113.77", "pubkey": "c4", "known": False},       # same /24 as c2
    ]
    ordered = [c["pubkey"] for c in sim.relay_order(candidates, held)]
    assert ordered[0] == "c3"                                          # known + distinct subnet
    assert ordered[1] == "c2"                                          # distinct subnet, unknown
    assert ordered[-1] in ("c1", "c4") and set(ordered[2:]) == {"c1", "c4"}
    # a registry pair score can outrank the address view
    scores = {frozenset({"c3", "h1"}): 5.0}
    ordered = [c["pubkey"] for c in sim.relay_order(candidates, held, lambda a, b: scores.get(frozenset({a, b}), 0.0))]
    assert ordered[0] == "c2" and ordered[1] in ("c1", "c4", "c3")
    assert sim.subnet_of("198.51.100.7") == "198.51.100.0/24" and sim.subnet_of("nope") is None
    assert sim.relay_order([], held) == []


def test_index_serves_cached_values_without_blocking():
    calls = []

    class Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def factory():
        calls.append(1)
        return Conn()

    clock = [1000.0]
    idx = sim.SimilarityIndex(factory, ttl=60, cap=3, clock=lambda: clock[0], background=False)
    sim.cluster = lambda conn, pubkey: {"pubkey": pubkey, "members": [], "sim_mult": 2.5}   # stub the DB part
    try:
        assert idx.sim_mult(None) == 1.0 and idx.sim_mult("") == 1.0 and calls == []
        assert idx.sim_mult("AB" * 32) == 1.0                                              # unknown → 1.0, queued
        assert idx.stats()["pending"] == 1
        assert idx.drain() == 1 and idx.sim_mult("ab" * 32) == 2.5 and idx.stats()["priced"] == 1
        assert idx.drain() == 0                                                              # fresh: no re-query
        clock[0] += 61
        assert idx.sim_mult("ab" * 32) == 2.5 and idx.stats()["pending"] == 1                # stale value served, refresh queued
        for k in ("01", "02", "03"):
            idx.sim_mult(k * 32)
        idx.drain()
        assert idx.stats()["cached"] == 3                                                    # capped
    finally:
        del sim.cluster
        import importlib
        importlib.reload(sim)
