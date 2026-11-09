"""Taking catalogue slices from any certified author, on a real PostgreSQL
built from the migrations — skipped without a cluster (desktop/mb_slice_client.py,
desktop/p2p/slice_trust.py, slice_rebuild.py, lb_slice_cycle.py,
mb_slice_cycle.py, the launcher's slice routes in sync_server.py):

- a slice of any certified author lands and its author is recorded; one
  without its author's certificate lands nothing; a `pow` author counts once
  its proof is evaluated, a slice whose relayed proof fails is dropped with
  nothing held against its author, and one not evaluated yet is taken but
  passed on by no one; a relayed proof never replaces the one a verification
  proved, and the certificate a node passes on is taken by the next one;
- a slice whose rows reach outside its own subtree is refused whole and
  proves its author false: what the author signed is dropped and the
  catalogue rebuilt from what is left — the bindings only its slices
  supported withdrawn —, its later slices are refused, an honest author's
  rows stay; the node that passed it on is quarantined with it unless it is
  the author itself, and is not asked again; a mailbox whose second key is
  convicted is refused for authorship;
- a penalized key's signed slice request is refused with the evidence, on
  both slice routes of the launcher's peer surface; a penalty that rolls
  back leaves no event for support;
- a slice with a row held under another MBID lands none of its rows until
  MusicBrainz's answers settle the clash, and the side MusicBrainz does not
  know loses;
- the requester records who passed each slice on, by the key its channel
  proved, in the ledger, the kept blob and the subject index;
- a signed zero-match re-opens after 30 days, and the cycle learns which
  author closed it;
- the migration runner's rebuild step puts back only what the kept slices
  carry, and the 052 delta brings a node that ran up to 051 to the slice
  record, an artist or a group one row per MBID."""

import asyncio
import hashlib
import sys
import uuid
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

psycopg2 = pytest.importorskip("psycopg2")

REPO = Path(__file__).resolve().parents[2]
for _p in (REPO, REPO / "backend"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from desktop import mb_slice_client  # noqa: E402
from desktop.mb_slice_client import MBSliceClient  # noqa: E402
from desktop.p2p import (birth_cert, identity_pow, identity_proof, identity_registry, lb_slice_cycle,  # noqa: E402
                         lb_slice_queries, mb_slice_cycle, mb_slice_queries, slice_checks,
                         slice_rebuild, slice_trust)
from desktop.p2p.sync_walk import DeadAddresses  # noqa: E402
from tests.slice_certs import (Peer, Subtree, certificate, certified, lb_entry, mb_entry,  # noqa: E402
                               proof, pub, trust, uncertified)

OWN = pub(Ed25519PrivateKey.generate())
_STATE = ("mb_slice_blobs", "mb_slice_fetches", "lb_slice_blobs", "lb_slice_fetches", "slice_index",
          "slice_evidence", "slice_tally", "slice_penalties", "slice_check_plans", "slice_clashes",
          "slice_clash_wins", "slice_burned_mailboxes", "p2p_identities", "p2p_node_bans",
          "mbws_queue", "mbws_answers", "mbws_artists", "mbws_artist_aliases", "mbws_release_groups", "mbws_artist_release_groups", "mbws_releases",
          "mbws_recordings", "mbws_credits", "lb_recording", "lb_artist", "artists",
          "pending_scrobble_artists", "pending_scrobbles") + tuple(mb_slice_queries.SLICE_TABLES)
_SETTINGS = ["musicbrainz.db_version", "listenbrainz.db_version", "mb_slice.published", "lb_slice.published"]


@pytest.fixture(autouse=True)
def _authority(monkeypatch):
    trust(monkeypatch)


@pytest.fixture
def conn(scratch_dsn):
    """A node without either dump, holding no slice yet; in UTC, as the peer
    surface's connections are."""
    c = psycopg2.connect(scratch_dsn, options="-c timezone=UTC")
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute("TRUNCATE " + ", ".join(_STATE) + " CASCADE")
        cur.execute("DELETE FROM user_settings WHERE key = ANY(%s)", (_SETTINGS,))
    yield c
    c.close()


def _key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def _take(dsn, peer, names, source_node=""):
    """One exchange with `peer` through the requester."""
    client = MBSliceClient(peer, db_dsn=dsn, source_node=source_node)
    try:
        return client.run(names)
    finally:
        client.close()


def _offer(dsn, key, subtree, relay=None):
    """`key`'s slice of `subtree`, passed on by `relay` (its author itself by default)."""
    peer = Peer({subtree.name: mb_entry(key, subtree.name, subtree.one())}, pub(relay or key))
    return _take(dsn, peer, [subtree.name])


def _outside(name):
    """A slice with one row its own walk does not reach."""
    s = Subtree(name)
    s.group()
    s.outside()
    return s


def _honest(name):
    s = Subtree(name)
    s.group()
    return s


def _rows(conn, sql, *params):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _count(conn, table):
    return _rows(conn, f"SELECT count(*) FROM {table}")[0][0]


def _gids(conn, table):
    return {r[0] for r in _rows(conn, f"SELECT gid::text FROM {table}")}


def _penalties(conn):
    return dict(_rows(conn, "SELECT btrim(pubkey), party::text FROM slice_penalties"))


def _answer(conn, kind, mbid, status="found", name=None):
    """MusicBrainz's answer for (kind, mbid), as backend/mb_answers.py keeps it
    — with the artist's name or the group's title when given."""
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO mbws_answers (kind, mbid, status, target) VALUES (%s, %s, %s, %s)""",
                    (kind, mbid, status, None if status == "unknown" else mbid))
        if name and kind == "artist":
            cur.execute("INSERT INTO mbws_artists (mbid, name) VALUES (%s, %s)", (mbid, name))
        elif name:
            cur.execute("INSERT INTO mbws_release_groups (mbid, title) VALUES (%s, %s)", (mbid, name))


# --------------------------------------------------------------------------
# Certified authors
# --------------------------------------------------------------------------

def test_a_slice_of_any_certified_author_lands_and_its_author_is_recorded(conn, scratch_dsn):
    authors = [_key(), _key()]
    taken = []
    for i, author in enumerate(authors):
        s = Subtree(f"Artist {i}")
        group, _ = s.group()
        stats = _offer(scratch_dsn, author, s)
        assert (stats["matched"], stats["missing"]) == (1, []) and stats["rows_inserted"] > 0
        taken.append((s, group))
    assert _gids(conn, "mb_artist") == {s.gid for s, _ in taken}
    assert _gids(conn, "mb_release_group") == {g for _, g in taken}
    assert dict(_rows(conn, "SELECT pubkey, status::text FROM p2p_identities")) == {
        pub(a): "verified" for a in authors}
    assert sorted(_rows(conn, "SELECT name_key, source_pubkey FROM mb_slice_fetches")) == [
        ("artist 0", pub(authors[0])), ("artist 1", pub(authors[1]))]


@pytest.mark.parametrize("certify", [
    uncertified,
    lambda e: certified(e, certificate(pub(_key()))),
    lambda e: certified(e, certificate(e["author_pubkey"], authority=_key())),
], ids=["none", "another-keys", "unknown-authority"])
def test_a_slice_without_its_authors_certificate_lands_nothing(conn, scratch_dsn, certify):
    author, s = _key(), _honest("Uncertified")
    entry = certify(mb_entry(author, s.name, s.one()))
    assert _take(scratch_dsn, Peer({s.name: entry}, pub(author)), [s.name]) == {
        "error": "all slices failed verification"}
    for table in ("mb_artist", "mb_release_group", "mb_slice_fetches", "mb_slice_blobs", "slice_index",
                  "p2p_identities"):
        assert _count(conn, table) == 0, table


@pytest.fixture
def evaluator(monkeypatch):
    """The memory-hard evaluation of a proof, its outcome set per key
    (`outcome[pubkey]`: True, False); a key with no outcome finds the
    evaluator taken, as while another evaluation runs."""
    outcome: dict = {}
    real = identity_registry.verify_relayed

    def verify_relayed(c, cert, bound, relay):
        if cert["pubkey"] not in outcome:
            return "unverified"
        return real(c, cert, bound, relay)

    monkeypatch.setattr(identity_proof, "verify_proof", lambda bound, cert: outcome[cert["pubkey"]])
    monkeypatch.setattr(identity_pow, "mem_available_kib", lambda: None)
    monkeypatch.setattr(identity_registry, "_REJECTED", set())
    monkeypatch.setattr(identity_registry, "_RELAYED_FAILED", {})
    monkeypatch.setattr(identity_registry, "_RELAY_FAILED", {})
    monkeypatch.setattr(identity_registry, "verify_relayed", verify_relayed)
    return outcome


def _nonce(c) -> dict:
    return dict(_rows(c, "SELECT pubkey, proof_nonce FROM p2p_identities"))


def test_a_pow_author_counts_once_its_proof_is_evaluated(conn, scratch_dsn, evaluator):
    evaluated, failing, pending = _key(), _key(), _key()
    evaluator.update({pub(evaluated): True, pub(failing): False})
    subtree = {}
    entries = {}
    for key in (evaluated, failing, pending):
        s = _honest(f"By {pub(key)[:12]}")
        cert = certificate(pub(key), method="pow")
        entries[s.name] = mb_entry(key, s.name, s.one(), cert, proof(cert))
        subtree[key] = s
    stats = _take(scratch_dsn, Peer(entries, pub(_key())), list(entries))
    assert stats["matched"] == 2
    assert _gids(conn, "mb_artist") == {subtree[evaluated].gid, subtree[pending].gid}
    # The proof that failed came from the relay, not from its author: the
    # slice is dropped, the author recorded from its certificate alone —
    # not failed, not banned, no nonce of the relay's kept.
    assert dict(_rows(conn, "SELECT pubkey, status::text FROM p2p_identities")) == {
        pub(evaluated): "verified", pub(failing): "unverified", pub(pending): "unverified"}
    assert _rows(conn, "SELECT pubkey FROM p2p_node_bans") == []
    assert _nonce(conn)[pub(failing)] is None and _nonce(conn)[pub(pending)] is None
    # The evaluated author is passed on under its certificate and proof; the
    # one not evaluated yet by no one, however its slice checks out.
    forwarded = slice_trust.author_certificate(conn, pub(evaluated))
    assert birth_cert.verify_certificate(forwarded[0]) and identity_proof.proof_binds(*forwarded[::-1])
    assert slice_trust.author_certificate(conn, pub(pending)) is None
    assert slice_checks.servable(conn, "mb", subtree[pending].name.lower(), OWN) is False
    # The same failed proof is not evaluated twice; the author's own proof,
    # passed on later, verifies it and its slice lands.
    later = _honest("Later Name")
    cert = certificate(pub(failing), method="pow")
    out = _take(scratch_dsn, Peer({later.name: mb_entry(failing, later.name, later.one(), cert, proof(cert))},
                                  pub(_key())), [later.name])
    assert out == {"error": "all slices failed verification"} and later.gid not in _gids(conn, "mb_artist")
    evaluator[pub(failing)] = True
    good = {**proof(cert), "nonce": "11" * identity_pow.NONCE_LEN}
    assert "error" not in _take(scratch_dsn, Peer({later.name: mb_entry(failing, later.name, later.one(),
                                                                         cert, good)}, pub(_key())),
                                [later.name])
    assert later.gid in _gids(conn, "mb_artist")
    assert dict(_rows(conn, "SELECT pubkey, status::text FROM p2p_identities"))[pub(failing)] == "verified"
    assert _nonce(conn)[pub(failing)] == good["nonce"]


def test_a_relayed_proof_never_replaces_the_one_a_verification_proved(conn, scratch_dsn, evaluator):
    author = _key()
    cert = certificate(pub(author), method="pow")
    proved = {**proof(cert), "nonce": "11" * identity_pow.NONCE_LEN}
    identity_registry.observe(conn, cert, proof=proved)
    identity_registry.mark_verified(conn, pub(author), proved["nonce"])
    s = _honest("Proved Author")
    relayed = {**proof(cert), "nonce": "22" * identity_pow.NONCE_LEN}
    assert "error" not in _take(scratch_dsn, Peer({s.name: mb_entry(author, s.name, s.one(), cert, relayed)},
                                                  pub(_key())), [s.name])
    assert s.gid in _gids(conn, "mb_artist")
    assert _nonce(conn)[pub(author)] == proved["nonce"]
    assert slice_trust.author_certificate(conn, pub(author))[1]["nonce"] == proved["nonce"]
    # Nor does the author's own word replace it, once a verification proved one.
    identity_registry.observe(conn, cert, proof=relayed)
    assert _nonce(conn)[pub(author)] == proved["nonce"]


def _signed_anew(cert: dict, **changed) -> dict:
    """`cert` as the authority signs it again, the same anchor and new fields."""
    from tests.slice_certs import AUTHORITY
    again = {**cert, **changed}
    again["sig"] = AUTHORITY.sign(birth_cert.canonical_payload(again)).hex()
    return again


def _status(c, key):
    return _rows(c, "SELECT status::text, proof_nonce, verified_at IS NOT NULL FROM p2p_identities "
                    "WHERE pubkey = %s", key)[0]


def test_a_pow_certificate_signed_anew_is_unproven_until_its_proof_is_shown_again(
        conn, scratch_dsn, evaluator):
    """A pow proof is work over its certificate's signature: once the
    authority signs the key's certificate anew, the nonce the earlier one
    was proved with fails beside it, so the row is unverified with no nonce
    — no relay forwards the old proof beside the new certificate — until a
    proof of the new one verifies."""
    author = _key()
    first = certificate(pub(author), method="pow")
    proved = {**proof(first), "nonce": "11" * identity_pow.NONCE_LEN}
    identity_registry.observe(conn, first, proof=proved)
    identity_registry.mark_verified(conn, pub(author), proved["nonce"])
    # The same certificate, its signature spelled otherwise, is no new one.
    identity_registry.observe(conn, {**first, "sig": first["sig"].upper()})
    assert _status(conn, pub(author)) == ("verified", proved["nonce"], True)
    again = _signed_anew(first, difficulty=first["difficulty"] + 1)
    identity_registry.observe(conn, again, proof=proved)            # the old proof binds no more
    assert _status(conn, pub(author)) == ("unverified", None, False)
    assert slice_trust.author_certificate(conn, pub(author)) is None
    assert slice_trust.author_entries(conn, [pub(author)]) == {}
    shown = {**proof(again), "nonce": "22" * identity_pow.NONCE_LEN}
    evaluator[pub(author)] = True
    assert slice_trust.observe_author(conn, again, shown, pub(author)) == "verified"
    assert _status(conn, pub(author)) == ("verified", shown["nonce"], True)
    forwarded = slice_trust.author_certificate(conn, pub(author))
    assert forwarded[0]["sig"] == again["sig"] and identity_proof.proof_binds(forwarded[1], forwarded[0])
    # Signed anew as an email certificate: verified by its mailbox, no proof.
    by_mail = _signed_anew(again, method="email", email_token="ab" * 32, email_class="major")
    identity_registry.observe(conn, by_mail)
    assert _status(conn, pub(author))[0] == "verified"
    # And signed anew for a changed mailbox: still verified by its mailbox.
    identity_registry.observe(conn, _signed_anew(by_mail, email_token="cd" * 32))
    assert _status(conn, pub(author))[0] == "verified"
    # A key whose proof failed here stays failed whatever it presents next.
    liar = _key()
    lie = certificate(pub(liar), method="pow")
    identity_registry.observe(conn, lie)
    identity_registry.mark_failed(conn, pub(liar), None, "pow_failed")
    identity_registry.observe(conn, _signed_anew(lie, difficulty=lie["difficulty"] + 1))
    assert _status(conn, pub(liar))[0] == "failed"


def test_the_certificate_a_node_passes_on_is_taken_by_the_next_one(conn, scratch_dsn, evaluator):
    author = _key()
    cert = certificate(pub(author), method="pow")
    proved = {**proof(cert), "nonce": "11" * identity_pow.NONCE_LEN}
    identity_registry.observe(conn, cert, proof=proved)
    identity_registry.mark_verified(conn, pub(author), proved["nonce"])
    forwarded, forwarded_proof = slice_trust.author_certificate(conn, pub(author))
    assert forwarded["email_class"] is None and birth_cert.verify_certificate(forwarded)
    # The next node, which never met the author: its registry takes the
    # certificate as passed on, and the proof as the relay's word.
    with conn.cursor() as cur:
        cur.execute("DELETE FROM p2p_identities WHERE pubkey = %s", (pub(author),))
    evaluator[pub(author)] = True
    assert slice_trust.observe_author(conn, forwarded, forwarded_proof, pub(_key())) == "verified"
    assert _rows(conn, "SELECT email_class FROM p2p_identities WHERE pubkey = %s", pub(author)) == [(None,)]


# --------------------------------------------------------------------------
# P2: a slice is its own subtree
# --------------------------------------------------------------------------

def test_a_slice_reaching_outside_its_subtree_is_refused_whole_and_its_authors_slices_dropped(conn, scratch_dsn):
    author, honest = _key(), _key()
    earlier, kept = _honest("Earlier Name"), Subtree("Honest Name")
    kept_group, _ = kept.group()
    assert "error" not in _offer(scratch_dsn, author, earlier)
    assert "error" not in _offer(scratch_dsn, honest, kept)
    with conn.cursor() as cur:                       # what the canon bound on each
        for s in (earlier, kept):
            artist = str(uuid.uuid4())
            cur.execute("INSERT INTO artists (id, name, last_mb_sync) VALUES (%s, %s, now())",
                        (artist, s.name))
            cur.execute("INSERT INTO artist_mbids (mbid, artist_id, confidence) VALUES (%s, %s, 'phantom')",
                        (s.gid, artist))
    # The P2 convicts its author, the answer's own source: refused whole.
    assert _offer(scratch_dsn, author, _outside("Outside Name")) == {"error": "source carries a penalty"}
    # Refused whole; the conviction and the drop asked for the rebuild, which
    # the backend's rebuilder runs — the author's earlier rows go in it.
    assert _rows(conn, "SELECT 1 FROM user_settings WHERE key = %s", slice_rebuild.PENDING_KEY) == [(1,)]
    assert slice_rebuild.run_pending(conn)["bindings_withdrawn"] == 1
    assert _rows(conn, "SELECT 1 FROM user_settings WHERE key = %s", slice_rebuild.PENDING_KEY) == []
    assert slice_rebuild.run_pending(conn) is None
    assert _gids(conn, "mb_artist") == {kept.gid} and _gids(conn, "mb_release_group") == {kept_group}
    assert _penalties(conn) == {pub(author): "author"}
    assert _rows(conn, """SELECT e.kind::text, e.entity::text, e.key, btrim(e.author), btrim(e.relay)
                            FROM slice_penalties p JOIN slice_evidence e ON e.id = p.evidence""") == [
        ("P2", "subtree", "outside name", pub(author), pub(author))]
    for table, col in (("mb_slice_blobs", "name_key"), ("mb_slice_fetches", "name_key"), ("slice_index", "key")):
        assert _rows(conn, f"SELECT {col} FROM {table}") == [("honest name",)], table
    assert _rows(conn, "SELECT mbid::text FROM artist_mbids") == [(kept.gid,)]
    # The artist whose binding went is taken by the canon again.
    assert _rows(conn, "SELECT name FROM artists WHERE last_mb_sync IS NULL") == [("Earlier Name",)]
    # Its later slices are refused: their source carries the penalty.
    later = _honest("Later Name")
    assert _offer(scratch_dsn, author, later) == {"error": "source carries a penalty"}
    assert _gids(conn, "mb_artist") == {kept.gid}


def test_a_node_that_passes_a_falsification_on_is_quarantined_with_its_author(conn, scratch_dsn):
    author, relay, honest = _key(), _key(), _key()
    passed = _honest("Passed On")
    assert "error" not in _offer(scratch_dsn, honest, passed, relay=relay)
    # The P2 quarantines the node that passed it on, the answer's source: refused whole.
    assert _offer(scratch_dsn, author, _outside("Outside Name"), relay=relay) == {
        "error": "source carries a penalty"}
    assert _penalties(conn) == {pub(author): "author", pub(relay): "relay"}
    assert len(_rows(conn, "SELECT DISTINCT evidence FROM slice_penalties")) == 1   # one event, both keys
    # What the quarantined node passed on goes with it; its author is not blamed.
    slice_rebuild.run_pending(conn)
    assert _count(conn, "mb_slice_blobs") == 0 and _gids(conn, "mb_artist") == set()
    assert slice_trust.penalty(conn, pub(honest)) is None
    # It is not asked again, and its answers are refused.
    assert {pub(author), pub(relay)} <= lb_slice_cycle.load_bans(scratch_dsn)[0]
    again = _honest("Asked Again")
    assert _offer(scratch_dsn, honest, again, relay=relay) == {"error": "source carries a penalty"}
    assert "error" not in _offer(scratch_dsn, honest, again, relay=_key())
    assert _gids(conn, "mb_artist") == {again.gid}


def test_an_author_answering_with_its_own_falsification_is_convicted_as_its_author(conn, scratch_dsn):
    author = _key()
    # The P2 convicts its author, the answer's own source: refused whole.
    assert _offer(scratch_dsn, author, _outside("Outside Name")) == {"error": "source carries a penalty"}
    assert _penalties(conn) == {pub(author): "author"}
    assert _rows(conn, "SELECT btrim(author), btrim(relay) FROM slice_evidence") == [(pub(author), pub(author))]


def test_a_mailbox_whose_second_key_is_convicted_is_refused_for_authorship(conn, scratch_dsn):
    mailbox = "e" * 64
    first, second, third = _key(), _key(), _key()

    def offer(key, subtree):
        entry = mb_entry(key, subtree.name, subtree.one(), certificate(pub(key), email_token=mailbox))
        return _take(scratch_dsn, Peer({subtree.name: entry}, pub(key)), [subtree.name])

    offer(first, _outside("First Outside"))
    assert _count(conn, "slice_burned_mailboxes") == 0             # one key's conviction burns nothing
    offer(second, _outside("Second Outside"))
    assert _rows(conn, "SELECT email_token FROM slice_burned_mailboxes") == [(mailbox,)]
    assert offer(third, _honest("Third Try")) == {"error": "all slices failed verification"}
    assert _gids(conn, "mb_artist") == set()
    another = _honest("Another Mailbox")
    assert "error" not in _offer(scratch_dsn, _key(), another)       # a key of its own mailbox
    assert _gids(conn, "mb_artist") == {another.gid}


# --------------------------------------------------------------------------
# What a penalized key is told
# --------------------------------------------------------------------------

def _launcher_answers(dsn, signers):
    """What the launcher's slice routes answer requests signed by each of
    `signers` (None: unsigned) — the peer gate's verdict stood in for."""
    from aiohttp import ClientSession, web
    from aiohttp.test_utils import TestServer
    from desktop.p2p.sync_server import SyncServer
    server = SyncServer(dsn, node_id=OWN)
    server._sharing_enabled = lambda: True
    server._own_slice_cert = lambda: None

    @web.middleware
    async def signed(request, handler):
        request["peer_pubkey"] = request.headers.get("X-Test-Signer")
        return await handler(request)

    app = web.Application(middlewares=[signed])
    app.router.add_post("/api/mb/slice", server.handle_mb_slice)
    app.router.add_post("/api/lb/slice", server.handle_lb_slice)

    async def exchange():
        out = {}
        async with TestServer(app) as ts, ClientSession() as session:
            for path, body in (("/api/mb/slice", {"names": ["Sade"]}),
                               ("/api/lb/slice", {"artist_mbids": [str(uuid.UUID(int=5))]})):
                for signer in signers:
                    headers = {"X-Test-Signer": signer} if signer else {}
                    async with session.post(ts.make_url(path), json=body, headers=headers) as r:
                        out[(path, signer)] = (r.status, await r.json())
        return out

    try:
        return asyncio.run(exchange())
    finally:
        if server._pool is not None:
            server._pool.closeall()


def test_a_penalized_keys_signed_slice_request_is_refused_with_the_evidence(conn, scratch_dsn):
    author, relay, other = _key(), _key(), _key()
    outside = _outside("Outside Name")
    _offer(scratch_dsn, author, outside, relay=relay)
    refusal = slice_trust.refusal(conn, pub(relay))
    assert {k: v for k, v in refusal.items() if k != "seen"} == {
        "error": "slice_refused", "party": "relay", "kind": "P2", "entity": "subtree", "mbid": None,
        "name": "outside name", "author": pub(author), "dump_version": outside.version}
    assert refusal["seen"]
    assert slice_trust.refusal(conn, pub(author))["party"] == "author"
    assert slice_trust.refusal(conn, pub(other)) is None and slice_trust.refusal(conn, None) is None
    answers = _launcher_answers(scratch_dsn, [pub(relay), pub(other), None])
    for path, asked in (("/api/mb/slice", "Sade"), ("/api/lb/slice", str(uuid.UUID(int=5)))):
        assert answers[(path, pub(relay))] == (403, refusal)
        for signer in (pub(other), None):
            status, body = answers[(path, signer)]
            assert status == 200 and body["missing"] == [asked]


def test_a_penalty_that_rolls_back_leaves_no_event_for_support(conn, scratch_dsn):
    """The event for support stands or falls with the penalty it reports: it
    never commits the caller's transaction."""
    key = pub(_key())
    with conn.cursor() as cur:
        cur.execute("DELETE FROM user_settings WHERE key = 'support.diagnostics_enabled'")  # on
    events = "SELECT count(*) FROM diag_events WHERE kind = 'slice.penalized'"
    before = _rows(conn, events)[0][0]
    tx = psycopg2.connect(scratch_dsn, options="-c timezone=UTC")
    try:
        assert slice_trust._punish(tx, key, "author", None)
        tx.rollback()
    finally:
        tx.close()
    assert key not in _penalties(conn)
    assert _rows(conn, events)[0][0] == before


# --------------------------------------------------------------------------
# Clashes
# --------------------------------------------------------------------------

def _payload(conn, name_key):
    blob = _rows(conn, "SELECT blob_gz FROM mb_slice_blobs WHERE name_key = %s", name_key)[0][0]
    return hashlib.sha256(bytes(blob)).hexdigest()


def test_a_slice_that_clashes_with_a_held_row_lands_nothing_until_musicbrainz_settles_it(conn, scratch_dsn):
    held_author, incoming_author, relay = _key(), _key(), _key()
    held = Subtree("Held Name")
    held_group, _ = held.group()
    _offer(scratch_dsn, held_author, held)
    incoming = Subtree("Incoming Name", artist_id=held.artist_id)    # the held artist's id, another MBID
    incoming.group()
    stats = _offer(scratch_dsn, incoming_author, incoming, relay=relay)
    assert (stats["contested"], stats["rows_inserted"]) == (1, 0)
    assert _gids(conn, "mb_artist") == {held.gid} and _gids(conn, "mb_release_group") == {held_group}
    assert _rows(conn, "SELECT contested FROM mb_slice_blobs WHERE name_key = 'incoming name'") == [(True,)]
    assert _rows(conn, "SELECT entity::text, incoming::text, held::text FROM slice_clashes") == [
        ("mb_artist", incoming.gid, held.gid)]
    # Asked at the check's priority — a forger's clashes are no person's act —
    # and the contested name is not closed: asked again of another author.
    assert dict(_rows(conn, "SELECT mbid::text, priority FROM mbws_queue WHERE kind = 'artist'")) == {
        incoming.gid: slice_checks.PRIORITY, held.gid: slice_checks.PRIORITY}
    assert _rows(conn, "SELECT void FROM mb_slice_fetches WHERE name_key = 'incoming name'") == [(True,)]
    assert {r[0] for r in _rows(conn, """SELECT mbid::text FROM mbws_waiters
                                          WHERE subject = 'slice_check' AND subject_key = 'mb:incoming name'""")
            } == {incoming.gid, held.gid}
    assert slice_checks.servable(conn, "mb", "incoming name", OWN) is False
    # MusicBrainz knows the held MBID and not the incoming one: the incoming
    # blob lost — which rows land, not who lied: no event, no penalty.
    _answer(conn, "artist", incoming.gid, "unknown")
    _answer(conn, "artist", held.gid)
    slice_checks.evaluate(conn, "mb", "incoming name")
    assert _count(conn, "slice_evidence") == 0 and _penalties(conn) == {}
    assert _count(conn, "slice_clashes") == 0
    assert _rows(conn, "SELECT suspect, contested FROM mb_slice_blobs WHERE name_key = 'incoming name'") == [
        (True, True)]
    assert _rows(conn, "SELECT void FROM mb_slice_fetches WHERE name_key = 'incoming name'") == [(True,)]
    assert slice_checks.servable(conn, "mb", "incoming name", OWN) is False
    slice_rebuild.rebuild(conn)                      # a rebuild leaves the losing blob out
    assert _gids(conn, "mb_artist") == {held.gid} and _gids(conn, "mb_release_group") == {held_group}


def test_when_the_held_side_loses_the_blob_that_carried_it_stays_out_and_the_rows_land(conn, scratch_dsn):
    held_author, relay, incoming_author = _key(), _key(), _key()
    held = _honest("Held Name")
    _offer(scratch_dsn, held_author, held, relay=relay)
    incoming = Subtree("Incoming Name", artist_id=held.artist_id)
    incoming_group, _ = incoming.group()
    assert _offer(scratch_dsn, incoming_author, incoming)["contested"] == 1
    _answer(conn, "artist", incoming.gid)
    _answer(conn, "artist", held.gid, "unknown")
    slice_checks.evaluate(conn, "mb", "incoming name")
    assert _count(conn, "slice_evidence") == 0 and _penalties(conn) == {}
    # The blob that carried the held row stays out of every rebuild, its name
    # asked again; the rebuild the settlement asked for lands the winner.
    assert dict(_rows(conn, "SELECT name_key, suspect AND contested FROM mb_slice_blobs")) == {
        "held name": True, "incoming name": False}
    assert _rows(conn, "SELECT void FROM mb_slice_fetches WHERE name_key = 'held name'") == [(True,)]
    assert _rows(conn, "SELECT void FROM mb_slice_fetches WHERE name_key = 'incoming name'") == [(False,)]
    assert slice_rebuild.run_pending(conn)["landed"] == 1
    assert _rows(conn, "SELECT contested FROM mb_slice_blobs WHERE name_key = 'incoming name'") == [(False,)]
    assert _gids(conn, "mb_artist") == {incoming.gid} and _gids(conn, "mb_release_group") == {incoming_group}


def _reusing(held, name, releases):
    """A slice whose artist and first `releases` releases take the held
    slice's row ids under MBIDs of their own."""
    s = Subtree(name, artist_id=held.artist_id)
    s.group(releases=releases)
    rel, med = mb_slice_queries.SLICE_TABLES["mb_release"], mb_slice_queries.SLICE_TABLES["mb_medium"]
    held_ids = [r[rel.index("id")] for r in held.tables["mb_release"]]
    for row, rid in zip(s.tables["mb_release"], held_ids):
        old, row[rel.index("id")] = row[rel.index("id")], rid
        for m in s.tables["mb_medium"]:
            if m[med.index("release")] == old:
                m[med.index("release")] = rid
    return s


def test_a_blob_with_more_clashes_than_are_asked_loses_without_a_question(conn, scratch_dsn):
    """An honest dump never clashes: four clashes are a forger's, and cost
    MusicBrainz no question and the node no rebuild."""
    held = Subtree("Held Name")
    held.group(releases=3)
    _offer(scratch_dsn, _key(), held)
    incoming = _reusing(held, "Incoming Name", releases=3)          # the artist and three releases
    assert _offer(scratch_dsn, _key(), incoming)["contested"] == 1
    assert _count(conn, "slice_clashes") == 0 and _count(conn, "mbws_queue") == 0
    assert _rows(conn, "SELECT suspect, contested FROM mb_slice_blobs WHERE name_key = 'incoming name'") == [
        (True, True)]
    assert _rows(conn, "SELECT void FROM mb_slice_fetches WHERE name_key = 'incoming name'") == [(True,)]
    assert _rows(conn, "SELECT 1 FROM user_settings WHERE key = %s", slice_rebuild.PENDING_KEY) == []


def test_clashes_are_asked_one_at_a_time_and_the_first_loss_ends_them(conn, scratch_dsn):
    held = Subtree("Held Name")
    held.group(releases=1)
    _offer(scratch_dsn, _key(), held)
    incoming = _reusing(held, "Incoming Name", releases=1)          # the artist and one release
    _offer(scratch_dsn, _key(), incoming)
    rel = mb_slice_queries.SLICE_TABLES["mb_release"].index("gid")
    held_release, incoming_release = held.tables["mb_release"][0][rel], incoming.tables["mb_release"][0][rel]
    queued = lambda: {r[0] for r in _rows(conn, "SELECT mbid::text FROM mbws_queue")}  # noqa: E731
    assert queued() == {incoming.gid, held.gid}                     # the first clash only
    _answer(conn, "artist", incoming.gid)
    _answer(conn, "artist", held.gid, "unknown")                    # the first clash won
    assert slice_checks.evaluate(conn, "mb", "incoming name") is None
    assert queued() >= {incoming_release, held_release}             # now the second one
    _answer(conn, "release", incoming_release, "unknown")
    _answer(conn, "release", held_release)                          # lost: it ends there
    slice_checks.evaluate(conn, "mb", "incoming name")
    assert dict(_rows(conn, "SELECT name_key, suspect FROM mb_slice_blobs")) == {
        "held name": False, "incoming name": True}                   # nothing else lost
    assert _rows(conn, "SELECT 1 FROM user_settings WHERE key = %s", slice_rebuild.PENDING_KEY) == []


def test_an_author_s_clash_wins_are_bounded_a_day(conn):
    """A forger pairing removed MBIDs with real ones takes held blobs out a
    few times a day, not once per row."""
    author, other = pub(_key()), pub(_key())
    with slice_rebuild.atomic(conn):
        wins = [slice_checks._win_allowed(conn, author) for _ in range(slice_checks.CLASH_WINS_A_DAY + 1)]
        assert slice_checks._win_allowed(conn, other)
    assert wins == [True] * slice_checks.CLASH_WINS_A_DAY + [False]


def test_an_mbid_held_under_another_id_is_a_clash_told_by_musicbrainz_names(conn, scratch_dsn):
    """One MBID has one row: a slice's artist row under an MBID held here
    under another id lands nothing, and the name stays open; MusicBrainz's
    answer for the MBID tells the rows apart by their names."""
    held = _honest("Kept Name")
    _offer(scratch_dsn, _key(), held)
    twin = Subtree("Twin Name", gid=held.gid)                       # another id, the same MBID
    twin_group, _ = twin.group()
    assert _offer(scratch_dsn, _key(), twin)["contested"] == 1
    assert _rows(conn, "SELECT entity::text, incoming::text, held::text, incoming_name, held_name "
                       "FROM slice_clashes") == [("mb_artist", held.gid, held.gid, "Twin Name", "Kept Name")]
    assert _rows(conn, "SELECT void FROM mb_slice_fetches WHERE name_key = 'twin name'") == [(True,)]
    assert twin_group not in _gids(conn, "mb_release_group")
    # MusicBrainz names the incoming row: it wins, the blob that carried the
    # held row loses, and the rebuild lands the winner.
    _answer(conn, "artist", held.gid, name="Twin Name")
    slice_checks.evaluate(conn, "mb", "twin name")
    assert dict(_rows(conn, "SELECT name_key, suspect AND contested FROM mb_slice_blobs")) == {
        "kept name": True, "twin name": False}
    slice_rebuild.run_pending(conn)
    assert twin_group in _gids(conn, "mb_release_group")


def test_an_mbid_held_under_another_id_keeps_the_held_row_when_musicbrainz_names_it(conn, scratch_dsn):
    held = _honest("Kept Name")
    _offer(scratch_dsn, _key(), held)
    twin = Subtree("Twin Name", gid=held.gid)
    twin.group()
    _offer(scratch_dsn, _key(), twin)
    _answer(conn, "artist", held.gid, name="Kept Name")
    slice_checks.evaluate(conn, "mb", "twin name")
    assert dict(_rows(conn, "SELECT name_key, suspect FROM mb_slice_blobs")) == {
        "kept name": False, "twin name": True}
    assert _count(conn, "slice_evidence") == 0 and _penalties(conn) == {}


# --------------------------------------------------------------------------
# Who passed it on
# --------------------------------------------------------------------------

def test_the_requester_records_who_passed_each_slice_on(conn, scratch_dsn):
    author, relay, unpinned = _key(), _key(), _key()
    s = Subtree("Passed Along")
    group, _ = s.group()
    peer = Peer({s.name: mb_entry(author, s.name, s.one())}, pub(relay), base_url="https://203.0.113.7:21001")
    _take(scratch_dsn, peer, [s.name], source_node=pub(unpinned))
    addr = mb_slice_queries.addr_uuid(peer.base_url)
    assert _rows(conn, """SELECT source_node, source_pubkey, btrim(relay), source_addr::text
                            FROM mb_slice_fetches""") == [(pub(unpinned), pub(author), pub(relay), addr)]
    assert _rows(conn, "SELECT btrim(author_pubkey), btrim(relay), checked_at FROM mb_slice_blobs") == [
        (pub(author), pub(relay), None)]
    assert _rows(conn, """SELECT family::text, key, btrim(author), btrim(relay), relay_addr::text,
                                 btrim(payload_sha256), dump_version, subject_artists::text[],
                                 subject_groups::text[] FROM slice_index""") == [
        ("mb", "passed along", pub(author), pub(relay), addr, _payload(conn, "passed along"), s.version,
         [s.gid], [group])]
    # A channel that proved no key: the node it answered as; the author itself: its own relay.
    other = _honest("Unpinned")
    _take(scratch_dsn, Peer({other.name: mb_entry(author, other.name, other.one())}), [other.name],
          source_node=pub(unpinned))
    _offer(scratch_dsn, author, _honest("Straight From Its Author"))
    assert dict(_rows(conn, "SELECT key, btrim(relay) FROM slice_index")) == {
        "passed along": pub(relay), "unpinned": pub(unpinned), "straight from its author": pub(author)}


TRUE_NONCE = "77" * identity_pow.NONCE_LEN


@pytest.fixture
def by_nonce(monkeypatch):
    """The relayed-proof verifier as it runs, its memory-hard evaluation
    faked: a proof verifies when its nonce is TRUE_NONCE. Returns the nonces
    evaluated."""
    evaluated = []
    monkeypatch.setattr(identity_proof, "verify_proof",
                        lambda bound, cert: evaluated.append(bound["nonce"]) or bound["nonce"] == TRUE_NONCE)
    monkeypatch.setattr(identity_pow, "mem_available_kib", lambda: None)
    for name, empty in (("_REJECTED", set()), ("_RELAYED_FAILED", {}), ("_RELAY_FAILED", {})):
        monkeypatch.setattr(identity_registry, name, empty)
    return evaluated


def _nonced(cert, nonce):
    return {**proof(cert), "nonce": nonce}


def _false_nonce(n):
    return f"{n:02x}" * identity_pow.NONCE_LEN


def _status_of(c, key):
    return _rows(c, "SELECT status::text FROM p2p_identities WHERE pubkey = %s", key)[0][0]


def test_false_proofs_a_relay_brings_never_keep_an_author_s_own_slice_from_verifying_it(
        conn, scratch_dsn, by_nonce):
    """Anyone can serve an honest author's slices beside a false nonce. The
    requester counts each under the key the relay's channel proved: three
    of them stop other relays' proofs of the author for the day, never the
    author's own — and a source that only claims the author's key, its
    channel proving none, is no author."""
    author, forger, honest = _key(), _key(), _key()
    cert = certificate(pub(author), method="pow")

    def offer(name, nonce, relay=None, source_node=""):
        s = _honest(name)
        peer = Peer({s.name: mb_entry(author, s.name, s.one(), cert, _nonced(cert, nonce))},
                    pub(relay) if relay else None)
        return _take(scratch_dsn, peer, [s.name], source_node=source_node)
    for n in range(1, 4):
        assert offer(f"Forged {n}", _false_nonce(n), forger) == {"error": "all slices failed verification"}
    assert "error" not in offer("Honest Relay", TRUE_NONCE, honest)
    assert _status_of(conn, pub(author)) == "unverified"
    assert "error" not in offer("Unpinned Claim", TRUE_NONCE, source_node=pub(author))
    assert _status_of(conn, pub(author)) == "unverified"
    assert by_nonce == [_false_nonce(1), _false_nonce(2), _false_nonce(3)]
    assert "error" not in offer("From Itself", TRUE_NONCE, author)
    assert _status_of(conn, pub(author)) == "verified" and by_nonce[-1] == TRUE_NONCE


def test_the_lb_requester_counts_a_false_proof_under_its_relay_and_asks_a_refused_source_for_none(
        conn, scratch_dsn, by_nonce):
    author, forger, refused = _key(), _key(), _key()
    cert = certificate(pub(author), method="pow")
    cycle = lb_slice_cycle.LbSliceCycle(scratch_dsn, connect=None, dead=DeadAddresses())

    def offer(nonce, relay=None, node="a-node"):
        artist = str(uuid.uuid4())
        one = {"dump_version": "20261101-001500", "artist": [10, 3],
               "recordings": [[str(uuid.uuid4()), 5, 2, [artist]]], "truncated": False}
        peer = Peer({artist: lb_entry(author, artist, one, cert, _nonced(cert, nonce))},
                    pub(relay) if relay else None)
        return asyncio.run(cycle._exchange(peer, node, None, [artist], None))
    with conn.cursor() as cur:
        cur.execute("INSERT INTO slice_penalties (pubkey, party) VALUES (%s, 'relay')", (pub(refused),))
    assert offer(_false_nonce(9), refused) == {"error": "source carries a penalty"}
    assert by_nonce == []                                   # nothing evaluated for a refused source
    for n in range(1, 4):
        assert offer(_false_nonce(n), forger)["error"] == "all slices failed verification"
    # A source whose channel proved no key, its node id claiming the author's: no author.
    assert offer(TRUE_NONCE, node=pub(author))["imported"] == 1
    assert _status_of(conn, pub(author)) == "unverified"
    assert offer(TRUE_NONCE, author)["imported"] == 1
    assert _status_of(conn, pub(author)) == "verified"
    assert by_nonce == [_false_nonce(1), _false_nonce(2), _false_nonce(3), TRUE_NONCE]


def test_the_lb_requester_records_who_passed_each_slice_on(conn, scratch_dsn):
    author, relay = _key(), _key()
    artist, recording = str(uuid.uuid4()), str(uuid.uuid4())
    one = {"dump_version": "20261101-001500", "artist": [10, 3], "recordings": [[recording, 5, 2, [artist]]],
           "truncated": False}
    peer = Peer({artist: lb_entry(author, artist, one)}, pub(relay), base_url="https://203.0.113.8:21002")
    addr = lb_slice_queries.addr_uuid(peer.base_url)
    cycle = lb_slice_cycle.LbSliceCycle(scratch_dsn, connect=None, dead=DeadAddresses())
    out = asyncio.run(cycle._exchange(peer, "a-node", addr, [artist], None))
    assert (out["imported"], out["recordings"], out["missing"]) == (1, 1, [])
    assert _rows(conn, """SELECT artist_mbid::text, source_node, source_pubkey, btrim(relay), source_addr::text
                            FROM lb_slice_fetches""") == [(artist, "a-node", pub(author), pub(relay), addr)]
    assert _rows(conn, "SELECT btrim(author_pubkey), btrim(relay) FROM lb_slice_blobs") == [
        (pub(author), pub(relay))]
    blob = bytes(_rows(conn, "SELECT blob_gz FROM lb_slice_blobs")[0][0])
    assert _rows(conn, """SELECT family::text, key, btrim(author), btrim(relay), relay_addr::text,
                                 btrim(payload_sha256), subject_artists::text[] FROM slice_index""") == [
        ("lb", artist, pub(author), pub(relay), addr, hashlib.sha256(blob).hexdigest(), [artist])]
    assert _rows(conn, "SELECT pubkey, status::text FROM p2p_identities") == [(pub(author), "verified")]


# --------------------------------------------------------------------------
# A zero-match ages out
# --------------------------------------------------------------------------

def test_a_signed_zero_match_reopens_after_thirty_days(conn, scratch_dsn):
    closer, other = _key(), _key()
    with conn.cursor() as cur:
        for name in ("gone quiet", "still closed", "matched long ago"):
            cur.execute("INSERT INTO pending_scrobble_artists (name_key) VALUES (%s)", (name,))
            cur.execute("""INSERT INTO pending_scrobbles (played_at, artist, title, name_key)
                           VALUES (now(), %s, 'a song', %s)""", (name, name))
        cur.execute("""INSERT INTO mb_slice_fetches (name_key, matched_ids, source_pubkey, fetched_at) VALUES
                       ('gone quiet', 0, %s, now() - interval '31 days'),
                       ('still closed', 0, %s, now() - interval '29 days'),
                       ('matched long ago', 3, %s, now() - interval '400 days')""",
                    (pub(closer), pub(other), pub(other)))
    assert mb_slice_queries.pending_slice_names(conn) == [("gone quiet", 1)]
    assert mb_slice_cycle.reopened_authors(scratch_dsn, ["Gone Quiet", "Never Asked"]) == {
        "Gone Quiet": pub(closer)}


# --------------------------------------------------------------------------
# The migration runner's rebuild step
# --------------------------------------------------------------------------

def test_the_rebuild_step_puts_back_only_what_the_kept_slices_carry(conn):
    """The upgrade's step asks for the rebuild — the start never waits on the
    loader's lock — and the rebuilder runs it."""
    import db_migrate
    with conn.cursor() as cur:                       # a row no kept slice carries
        cur.execute("INSERT INTO mb_artist (id, gid, name) VALUES (8000001, %s, 'Left Behind')",
                    (str(uuid.uuid4()),))
    assert db_migrate._rebuild_slices(conn) == {} and _count(conn, "mb_artist") == 1   # no slice kept
    author = _key()
    kept, reaching = _honest("Kept Name"), _outside("Reaching Out")
    twin = Subtree("Twin Name", gid=kept.gid)        # another id under the kept artist's MBID
    twin.group()
    for age, s in enumerate((kept, reaching, twin)):
        entry = mb_entry(author, s.name, s.one())
        core, blob_gz, *_ = mb_slice_queries.verify_slice_entry(s.name, entry)
        mb_slice_queries.store_slice_blob(conn, s.name.lower(), s.version, pub(author), entry["sig"], blob_gz,
                                          relay=pub(author))
        with conn.cursor() as cur:
            cur.execute("UPDATE mb_slice_blobs SET created_at = now() - make_interval(mins => %s) "
                        "WHERE name_key = %s", (10 - age, s.name.lower()))
        if s is not twin:                            # its rows as they landed before the import's rules
            mb_slice_client.insert_rows(conn, s.name.lower(), mb_slice_client._rows(core))
    before = _count(conn, "mb_artist")
    assert db_migrate._rebuild_slices(conn) == {"requested": True}
    assert _count(conn, "mb_artist") == before       # nothing rebuilt in the start itself
    out = slice_rebuild.run_pending(conn)
    # Another id under the kept artist's MBID is a clash too: the twin's rows
    # wait for MusicBrainz's answer, by the rows' names.
    assert (out["rebuilt"], out["refused"], out["contested"]) == (1, 1, 1)
    assert _gids(conn, "mb_artist") == {kept.gid}
    assert dict(_rows(conn, "SELECT name_key, contested FROM mb_slice_blobs")) == {
        "kept name": False, "reaching out": False, "twin name": True}
    assert dict(_rows(conn, "SELECT name_key, suspect FROM mb_slice_blobs")) == {
        "kept name": False, "reaching out": True, "twin name": False}
    assert _rows(conn, "SELECT entity::text, incoming::text, held::text, incoming_name, held_name "
                       "FROM slice_clashes") == [("mb_artist", kept.gid, kept.gid, "Twin Name",
                                                  "Kept Name")]
    # The blobs kept before the subject index existed are indexed, their carried MBIDs noted.
    assert {r[0] for r in _rows(conn, "SELECT key FROM slice_index")} == {"kept name", "twin name"}
    assert _rows(conn, "SELECT mbids::text[] @> ARRAY[%s] FROM mb_slice_blobs WHERE name_key = 'kept name'",
                 kept.gid) == [(True,)]


def test_the_delta_brings_a_node_at_051_to_the_slice_record_and_one_row_per_mbid(scratch_dsn):
    from desktop import db_init
    c = psycopg2.connect(scratch_dsn)
    twice, once = str(uuid.uuid4()), str(uuid.uuid4())
    try:
        with c.cursor() as cur:
            cur.execute("SELECT 1 FROM _schema_migrations WHERE filename = '052_slice_provenance.sql'")
            assert cur.fetchone() == (1,)                # a no-op after 001 on a fresh node
            # The node that ran everything up to 051, with an MBID two rows share.
            cur.execute("""DROP TABLE slice_index, slice_evidence, slice_tally, slice_penalties,
                                      slice_check_plans, slice_clashes, slice_clash_wins,
                                      slice_burned_mailboxes;
                           DROP TYPE slice_family, slice_evidence_kind, slice_entity, slice_party,
                                     mb_slice_table;
                           ALTER TABLE mb_slice_blobs DROP COLUMN relay, DROP COLUMN checked_at,
                                                      DROP COLUMN suspect, DROP COLUMN contested,
                                                      DROP COLUMN payload_sha256, DROP COLUMN mbids;
                           ALTER TABLE lb_slice_blobs DROP COLUMN relay, DROP COLUMN checked_at,
                                                      DROP COLUMN suspect;
                           ALTER TABLE mb_slice_fetches DROP COLUMN relay;
                           ALTER TABLE lb_slice_fetches DROP COLUMN relay;
                           DROP INDEX uq_mb_artist_gid, uq_mb_release_group_gid;
                           CREATE INDEX IF NOT EXISTS idx_mb_artist_gid ON mb_artist (gid);
                           CREATE INDEX IF NOT EXISTS idx_mb_rg_gid ON mb_release_group (gid);
                           TRUNCATE mb_artist, mb_release_group;
                           DELETE FROM _schema_migrations WHERE filename = '052_slice_provenance.sql'""")
            cur.execute("INSERT INTO mb_artist (id, gid, name) VALUES (1, %s, 'One'), (2, %s, 'Two'), "
                        "(3, %s, 'Three')", (twice, twice, once))
            cur.execute("INSERT INTO mb_release_group (id, gid, name) VALUES (1, %s, 'A'), (2, %s, 'B'), "
                        "(3, %s, 'C')", (twice, twice, once))
        c.commit()
        assert db_init.apply_migrations(c) == 1
        c.commit()
        with c.cursor() as cur:
            for table in ("mb_artist", "mb_release_group"):
                cur.execute(f"SELECT id FROM {table}")
                assert cur.fetchall() == [(3,)], table      # a shared MBID's rows go, for the rebuild
            cur.execute("""SELECT indexname FROM pg_indexes
                            WHERE tablename IN ('mb_artist', 'mb_release_group') AND indexname IN
                                  ('uq_mb_artist_gid', 'uq_mb_release_group_gid', 'idx_mb_artist_gid',
                                   'idx_mb_rg_gid') ORDER BY 1""")
            assert [r[0] for r in cur.fetchall()] == ["uq_mb_artist_gid", "uq_mb_release_group_gid"]
            cur.execute("""SELECT relay, checked_at, suspect, contested FROM mb_slice_blobs
                           UNION ALL SELECT relay, checked_at, suspect, FALSE FROM lb_slice_blobs""")
            cur.execute("SELECT payload_sha256, mbids FROM mb_slice_blobs")
            cur.execute("SELECT covered FROM slice_evidence")
            cur.execute("SELECT payload_sha256 FROM slice_clashes")
            cur.execute("SELECT relay FROM mb_slice_fetches UNION ALL SELECT relay FROM lb_slice_fetches")
            cur.execute("""SELECT count(*) FROM slice_index, slice_evidence, slice_tally, slice_penalties,
                                                slice_check_plans, slice_clashes, slice_burned_mailboxes""")
            assert cur.fetchone() == (0,)
        assert db_init.apply_migrations(c) == 0
    finally:
        c.close()
