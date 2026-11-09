"""Carried certificates (BIRTH-SYNC slice 2) against a real PostgreSQL built
from the migrations: the `authors` map a sealed payload carries
(slice_trust.author_entries — this node's own pair, an author verified here
with its proof in the authority's spelling, an author only witnessed with
the certificate alone, never a banned key's, the newest certificate in the
current format), the pull handlers that build it, a push cut into parts
with the authors of each part's items, and the import gate's `carried`
sighting — only for an author with a record whose seal verified, delivered
by the peer that pulled or pushed it, never the deliverer itself, nothing
from a share file or an unsigned push, a certificate checked as on every
channel and no proof read. The sync writes no identity-registry row and
evaluates no proof. Skipped without a cluster."""

import uuid
from contextlib import contextmanager

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

psycopg2 = pytest.importorskip("psycopg2")
import psycopg2.pool  # noqa: E402

from desktop import sync_client  # noqa: E402
from desktop.p2p import (birth_cert, identity_proof, identity_registry, peer_auth,  # noqa: E402
                         slice_trust, sync_queries, sync_walk, witness)
from tests.p2p.sealed_records import pub, seal_track  # noqa: E402
from tests.p2p.test_witness import ISSUER, certificate  # noqa: E402

MODEL = str(uuid.UUID(int=0xB2))
OWN_KEY = Ed25519PrivateKey.generate()
OWN = pub(OWN_KEY)


@pytest.fixture(autouse=True)
def trusted(monkeypatch):
    for mirror in (birth_cert, witness, sync_client):
        monkeypatch.setattr(mirror, "TRUSTED_AUTHORITIES", [ISSUER])


@pytest.fixture(autouse=True)
def no_evaluation(monkeypatch):
    """Nothing in the sync evaluates a proof: the 2 GiB call raises."""
    def evaluate(*_a, **_k):
        raise AssertionError("the sync evaluated a proof")
    monkeypatch.setattr(identity_proof, "verify_proof", evaluate)
    monkeypatch.setattr(identity_registry, "verify_relayed", evaluate)


@pytest.fixture(scope="module")
def pool(scratch_dsn):
    p = psycopg2.pool.ThreadedConnectionPool(1, 4, scratch_dsn)

    @contextmanager
    def connect():
        conn = p.getconn()
        try:
            conn.autocommit = True
            yield conn
        finally:
            p.putconn(conn)
    yield connect
    p.closeall()


@pytest.fixture
def conn(pool):
    with pool() as c:
        with c.cursor() as cur:
            cur.execute("INSERT INTO embedding_models (id, name, dimension) VALUES (%s, 'clap', 512) "
                        "ON CONFLICT DO NOTHING", (MODEL,))
        yield c


@pytest.fixture
def recorder(pool):
    return witness.Recorder(pool, own_keys=lambda: frozenset({OWN}))


def _key():
    return Ed25519PrivateKey.generate()


def _proof(cert: dict, nonce="ab" * 16) -> dict:
    return {"v": identity_proof.PROOF_VERSION, "pubkey": cert["pubkey"], "cert_sig": cert["sig"],
            "nonce": nonce, "difficulty": cert["difficulty"], "params_version": cert["params_version"]}


def _carried(conn, subject):
    with conn.cursor() as cur:
        cur.execute("SELECT via FROM p2p_witness WHERE subject = %s AND channel = 'carried'",
                    (subject,))
        return [r[0] for r in cur.fetchall()]


def _registered(conn, subject) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM p2p_identities WHERE pubkey = %s", (subject,))
        return cur.fetchone() is not None


def _track(conn, recording=None) -> tuple:
    track, recording = str(uuid.uuid4()), recording or str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute("INSERT INTO tracks (id, title) VALUES (%s, 'carried?')", (track,))
    return track, recording


def _forget(conn, track) -> None:
    """The analysis of `track` gone from this node, so an import lands it again."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM embeddings WHERE track_id = %s", (track,))
        cur.execute("DELETE FROM audio_features WHERE track_id = %s", (track,))
        cur.execute("DELETE FROM track_mbids WHERE track_id = %s", (track,))
        cur.execute("DELETE FROM analysis_sources WHERE track_id = %s", (track,))


def _served(conn, author_key, category="segments", own=None) -> tuple:
    """`author_key`'s sealed analysis of a new track as a peer serves it,
    then forgotten here: (track, recording, the payload)."""
    track, recording = _track(conn)
    with conn.cursor() as cur:
        seal_track(cur, author_key, track, MODEL, recording=recording)
    payload = sync_queries.PULL_HANDLERS[category](conn, [track], own=own)
    _forget(conn, track)
    return track, recording, payload


class _Peer:
    """A pulled peer as the import gate sees it: the key its TLS channel is
    bound to, and this node's identity with its `carried` sink."""

    def __init__(self, key: str, recorder):
        self.peer_pubkey = key
        self.peer = peer_auth.PeerIdentity(
            pubkey=OWN, sign=lambda _b: b"", cert_bundle=lambda: None,
            carried=lambda cert, author, via: recorder.note(cert, "carried", author, via))


def _pull(scratch_dsn, peer, category, track, payload) -> int:
    client = sync_client.SyncClient(peer, scratch_dsn)
    try:
        return client._import_items(category, sync_client.answer_to(payload, {track}))
    finally:
        client._close_conn()


def _witnessed(recorder, key, **over) -> dict:
    cert = certificate(pub(key), **over)
    assert recorder.record(cert, "introduced", pub(key))
    return cert


# ---------------------------------------------------------------------------
# The map
# ---------------------------------------------------------------------------

def test_the_map_names_each_author_by_what_this_node_holds(conn, recorder):
    own = (certificate(OWN), _proof(certificate(OWN)))
    verified, mailbox, seen, banned, unknown = (pub(_key()) for _ in range(5))
    vcert = certificate(verified)
    identity_registry.observe(conn, vcert, proof=_proof(vcert))
    identity_registry.mark_verified(conn, verified, "ab" * 16)
    mcert = certificate(mailbox, method="email", email_token="cd" * 32, email_class="major")
    identity_registry.observe(conn, mcert)                       # an email certificate: verified at once
    scert = certificate(seen)
    assert recorder.record(scert, "probe", seen)
    identity_registry.observe(conn, scert, proof=_proof(scert))   # registered, never evaluated
    assert recorder.record(certificate(banned), "probe", banned)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO p2p_node_bans (pubkey, reason) VALUES (%s, 'manual')", (banned,))

    entries = slice_trust.author_entries(
        conn, [OWN, verified.upper(), mailbox, seen, banned, unknown, None], own)
    assert set(entries) == {OWN, verified, mailbox, seen}
    assert entries[OWN] == {"author_cert": own[0], "author_proof": own[1]}
    assert entries[verified]["author_cert"] == vcert
    assert identity_proof.proof_binds(entries[verified]["author_proof"], vcert)
    assert entries[verified]["author_proof"]["nonce"] == "ab" * 16
    assert entries[mailbox] == {"author_cert": mcert, "author_proof": None}
    # Witnessed, not verified here: the certificate alone, never the nonce its
    # introduction left — a proof this node never checked.
    assert entries[seen] == {"author_cert": scert, "author_proof": None}
    for entry in entries.values():
        assert birth_cert.verify_certificate(entry["author_cert"])
    assert slice_trust.author_entries(conn, [], own) == {}


def test_a_respelled_signature_is_forwarded_as_the_authority_writes_it(conn, pool):
    key = pub(_key())
    cert = certificate(key)
    spelled = dict(cert, sig=cert["sig"].upper())
    assert birth_cert.verify_certificate(spelled)               # the mirror takes either spelling
    identity_registry.observe(conn, spelled, proof=_proof(spelled))
    identity_registry.mark_verified(conn, key, "ab" * 16)
    assert identity_registry.get(conn, key)["cert_sig"] == cert["sig"].upper()
    entry = slice_trust.author_entries(conn, [key])[key]
    assert entry["author_cert"] == cert
    assert entry["author_proof"]["cert_sig"] == cert["sig"]
    assert identity_proof.proof_binds(entry["author_proof"], entry["author_cert"])
    downstream = witness.Recorder(pool, own_keys=frozenset)
    assert downstream.record(entry["author_cert"], "carried", key, pub(_key()))


def test_the_newest_certificate_witnessed_travels_an_anomaly_too(conn, recorder, monkeypatch):
    key = _key()
    first = _witnessed(recorder, key)
    entries = slice_trust.author_entries(conn, [pub(key)])
    assert entries[pub(key)]["author_cert"] == first
    # Another issued_at for the same key — an authority's misbehaviour the
    # record keeps (BIRTH-SYNC §2.5) — travels as the newest sighting.
    anomaly = certificate(pub(key), issued_at="2026-11-03T09:00:00Z")
    assert recorder.record(anomaly, "probe", pub(key))
    assert slice_trust.author_entries(conn, [pub(key)])[pub(key)]["author_cert"] == anomaly
    # After a format bump the old format is no certificate anyone takes.
    monkeypatch.setattr(birth_cert, "CERT_VERSION", birth_cert.CERT_VERSION + 1)
    assert slice_trust.author_entries(conn, [pub(key)]) == {}


def test_the_pull_handlers_carry_the_map_of_their_authors(conn, recorder):
    stranger = _key()
    cert = _witnessed(recorder, stranger)
    own = (certificate(OWN), _proof(certificate(OWN)))
    theirs, mine = _track(conn), _track(conn)
    with conn.cursor() as cur:
        seal_track(cur, stranger, theirs[0], MODEL, recording=theirs[1])
        seal_track(cur, OWN_KEY, mine[0], MODEL, recording=mine[1], first_hand=True)
    for category in ("segments", "audio_features", "track_mbids"):
        payload = sync_queries.PULL_HANDLERS[category](conn, [theirs[0], mine[0]], own=own)
        assert payload["authors"] == {
            pub(stranger): {"author_cert": cert, "author_proof": None},
            OWN: {"author_cert": own[0], "author_proof": own[1]}}, category
        # Without its own pair (a push, a share export) the node names only others.
        assert set(sync_queries.PULL_HANDLERS[category](conn, [mine[0]])["authors"]) == set()
        assert sync_queries.PULL_HANDLERS[category](conn, [])["authors"] == {}


def test_a_push_part_names_the_authors_of_its_items(conn, recorder, monkeypatch):
    one, two = _key(), _key()
    _witnessed(recorder, one)
    _witnessed(recorder, two)
    a, b = _track(conn), _track(conn)
    with conn.cursor() as cur:
        seal_track(cur, one, a[0], MODEL, recording=a[1])
        seal_track(cur, two, b[0], MODEL, recording=b[1])
    payload = sync_queries.pull_audio_features(conn, [a[0], b[0]])
    assert set(payload["authors"]) == {pub(one), pub(two)}
    monkeypatch.setattr(sync_walk, "PUSH_MAX_BYTES", 1)             # one item a part
    parts = sync_walk.push_parts(payload)
    assert [{it["author_pubkey"] for it in p["items"]} for p in parts] == [
        {it["author_pubkey"]} for it in payload["items"]]
    for part in parts:
        named = {it["author_pubkey"] for it in part["items"]}
        assert set(part["authors"]) == named
        assert set(part["batches"]) == {it["batch_root"] for it in part["items"]}


# ---------------------------------------------------------------------------
# The import gate
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("category", ["segments", "audio_features"])
def test_an_author_whose_record_verified_is_carried_by_the_peer_that_delivered_it(
        conn, recorder, scratch_dsn, category):
    author, peer = _key(), pub(_key())
    _witnessed(recorder, author)                     # a pow author, never mined: no proof travels
    track, _rec, payload = _served(conn, author, category)
    assert payload["authors"][pub(author)]["author_proof"] is None
    assert _pull(scratch_dsn, _Peer(peer, recorder), category, track, payload) == 1
    assert _carried(conn, pub(author)) == [peer]
    assert not _registered(conn, pub(author))         # the sync writes no identity row
    _forget(conn, track)
    assert _pull(scratch_dsn, _Peer(pub(_key()), recorder), category, track, payload) == 1
    assert _carried(conn, pub(author)) == [peer]      # the first deliverer stays the record's


def test_the_deliverer_is_no_carried_author(conn, recorder, scratch_dsn):
    peer, author = _key(), _key()
    _witnessed(recorder, peer)
    _witnessed(recorder, author)
    peers_track, _r1, payload = _served(conn, peer)
    authors_track, _r2, other = _served(conn, author)
    payload["items"] += other["items"]
    payload["batches"].update(other["batches"])
    payload["authors"].update(other["authors"])
    client = sync_client.SyncClient(_Peer(pub(peer), recorder), scratch_dsn)
    try:
        assert client._import_items(
            "segments", sync_client.answer_to(payload, {peers_track, authors_track})) == 2
    finally:
        client._close_conn()
    assert _carried(conn, pub(peer)) == []
    assert _carried(conn, pub(author)) == [pub(peer)]               # the same payload's other author


def test_a_certificate_that_does_not_hold_records_nothing_and_the_payload_imports(
        conn, recorder, scratch_dsn):
    author, other = _key(), _key()
    _witnessed(recorder, author)
    track, _rec, payload = _served(conn, author)
    entry = payload["authors"][pub(author)]
    payload["authors"][pub(author)] = {"author_cert": certificate(pub(other)), "author_proof": None}
    assert _pull(scratch_dsn, _Peer(pub(_key()), recorder), "segments", track, payload) == 1
    forged = dict(entry["author_cert"], issued_at="2026-01-01T00:00:00Z")     # the sig no longer holds
    payload["authors"][pub(author)] = {"author_cert": forged, "author_proof": None}
    _forget(conn, track)
    assert _pull(scratch_dsn, _Peer(pub(_key()), recorder), "segments", track, payload) == 1
    payload["authors"] = {pub(author): "not an entry"}
    _forget(conn, track)
    assert _pull(scratch_dsn, _Peer(pub(_key()), recorder), "segments", track, payload) == 1
    assert _carried(conn, pub(author)) == [] and _carried(conn, pub(other)) == []
    payload["authors"] = {pub(author): entry}                     # the certificate as served
    _forget(conn, track)
    peer = pub(_key())
    assert _pull(scratch_dsn, _Peer(peer, recorder), "segments", track, payload) == 1
    assert _carried(conn, pub(author)) == [peer]


def test_an_older_peers_payload_imports_as_before(conn, recorder, scratch_dsn):
    author = _key()
    _witnessed(recorder, author)
    track, _rec, payload = _served(conn, author)
    entries = payload.pop("authors")
    assert _pull(scratch_dsn, _Peer(pub(_key()), recorder), "segments", track, payload) == 1
    assert _carried(conn, pub(author)) == []
    _forget(conn, track)
    peer = pub(_key())
    assert _pull(scratch_dsn, _Peer(peer, recorder), "segments", track,
                 dict(payload, authors=entries)) == 1
    assert _carried(conn, pub(author)) == [peer]


def test_a_certificate_beside_records_that_did_not_verify_is_no_sighting(conn, recorder, scratch_dsn):
    author = _key()
    _witnessed(recorder, author)
    track, _rec, payload = _served(conn, author, "audio_features")
    signed_bpm = payload["items"][0]["bpm"]
    payload["items"][0]["bpm"] = 121.5                        # no longer what the author signed
    assert _pull(scratch_dsn, _Peer(pub(_key()), recorder), "audio_features", track, payload) == 0
    assert _carried(conn, pub(author)) == []
    payload["items"][0]["bpm"] = signed_bpm
    peer = pub(_key())
    assert _pull(scratch_dsn, _Peer(peer, recorder), "audio_features", track, payload) == 1
    assert _carried(conn, pub(author)) == [peer]


def _on_the_tracklist(conn, track, recording) -> None:
    """A phantom slot of this node's own catalogue for `track`: what a
    carrier's offer answer, and a push's admission, ask for."""
    album = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute("INSERT INTO albums (id, title) VALUES (%s, 'LP')", (album,))
        cur.execute("INSERT INTO album_tracks (album_id, track_id, position, recording_mbid) "
                    "VALUES (%s, %s, 1, %s)", (album, track, recording))


@pytest.mark.parametrize("category", ["segments", "audio_features", "track_mbids"])
def test_a_push_is_carried_as_delivered_by_the_key_that_signed_it(conn, recorder, scratch_dsn, category):
    author, pusher = _key(), pub(_key())
    _witnessed(recorder, author)
    track, recording, payload = _served(conn, author, category)
    _on_the_tracklist(conn, track, recording)

    def sink(cert, subject, via):
        recorder.note(cert, "carried", subject, via)
    # Unsigned, it is no sighting; signed, its pusher delivered it.
    assert sync_client.import_pushed(scratch_dsn, category, payload, 100, deliverer=None,
                                     carried=sink) == 1
    assert _carried(conn, pub(author)) == []
    _forget(conn, track)
    assert sync_client.import_pushed(scratch_dsn, category, payload, 100,
                                     deliverer=pusher.upper(), carried=sink) == 1
    assert _carried(conn, pub(author)) == [pusher]
    assert not _registered(conn, pub(author))


def test_a_share_file_is_no_sighting(conn, recorder, scratch_dsn):
    author = _key()
    _witnessed(recorder, author)
    track, _rec, payload = _served(conn, author)
    client = sync_client.SyncClient(None, scratch_dsn)             # a share import's client
    try:
        assert client._import_items("segments", payload) == 1
    finally:
        client._close_conn()
    assert _carried(conn, pub(author)) == []
    _forget(conn, track)
    peer = pub(_key())
    assert _pull(scratch_dsn, _Peer(peer, recorder), "segments", track, payload) == 1
    assert _carried(conn, pub(author)) == [peer]                    # the same payload, pulled


def test_a_verified_authors_proof_rides_and_is_never_read(conn, recorder, scratch_dsn):
    author = _key()
    cert = certificate(pub(author))
    identity_registry.observe(conn, cert, proof=_proof(cert))
    identity_registry.mark_verified(conn, pub(author), "ab" * 16)
    track, _rec, payload = _served(conn, author)
    assert identity_proof.proof_binds(payload["authors"][pub(author)]["author_proof"], cert)
    payload["authors"][pub(author)]["author_proof"] = {"nonce": "not a proof"}    # read by nobody
    peer = pub(_key())
    assert _pull(scratch_dsn, _Peer(peer, recorder), "segments", track, payload) == 1
    assert _carried(conn, pub(author)) == [peer]
