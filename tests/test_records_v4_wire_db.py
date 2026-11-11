"""The per-kind analysis records on the wire, in the files and to their
readers (2026-11-10), on a real PostgreSQL built from the migrations:

- the import gate takes an instruments record sealed by its author and
  refuses a v3 features record (its v4 rebuild does not verify), an
  instruments map no reader could threshold, an unsealed one;
- the methodology bump: an imported record of a newer methodology replaces
  an imported one of an older, never a first-hand one, and the planner asks
  for no record the importer would drop;
- the holdings payload's second filter names the tracks with instruments, and
  its version moves with them; the client tests against it alone the tracks
  whose segments and features it holds and whose instruments it lacks;
- an instruments gap is in the run's trigger set and in no DHT set;
- the identity canon carries the instruments record (a merge's per-kind
  move, a rename's shed) and keeps a track holding only instruments;
- seed v4 carries the instruments and lands whole; a v3 bundle lands its
  segments and waits for v4;
- Discovery's instruments gate, search's filter, the instrument options,
  Now Playing and the assistant read track_instruments.
Skipped without a cluster."""

import sys
import uuid
from pathlib import Path
from urllib.parse import urlparse

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

REPO = Path(__file__).resolve().parent.parent
BACKEND = REPO / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

psycopg2 = pytest.importorskip("psycopg2")
import psycopg2.extras  # noqa: E402
import psycopg2.pool  # noqa: E402

from desktop import sync_client  # noqa: E402
from desktop.p2p import birth_cert, record_sig as rs, sync_queries, witness  # noqa: E402
from tests import conftest  # noqa: E402
from tests.p2p.sealed_records import pub, seal_track  # noqa: E402
from tests.p2p.test_witness import AUTHORITY, ISSUER  # noqa: E402

MODEL = sync_client.model_uuid("clap")
AUTHOR, NEWER, OWN = (Ed25519PrivateKey.generate() for _ in range(3))
SCORES = {"piano": 0.62, "drum kit": 0.45, "violin, fiddle": 0.011}
FP = "AQAAsealed"


@pytest.fixture(autouse=True)
def trusted(monkeypatch):
    for mirror in (birth_cert, witness, sync_client):
        monkeypatch.setattr(mirror, "TRUSTED_AUTHORITIES", [ISSUER])


@pytest.fixture
def conn(scratch_dsn):
    c = psycopg2.connect(scratch_dsn)
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute("TRUNCATE tracks, albums, artists, embedding_models, signing_batches CASCADE")
        cur.execute("INSERT INTO embedding_models (id, name, dimension) VALUES (%s, 'clap', 512)",
                    (MODEL,))
    yield c
    c.close()


def _track(cur, title="t") -> str:
    tid = str(uuid.uuid4())
    cur.execute("INSERT INTO tracks (id, title) VALUES (%s, %s)", (tid, title))
    return tid


def _import(dsn: str, category: str, payload: dict) -> int:
    client = sync_client.SyncClient(api_client=None, db_dsn=dsn)
    try:
        return client._import_items(category, sync_client.answer_to(
            payload, {it["track_uuid"] for it in payload["items"]}))
    finally:
        client._close_conn()


def _forget(cur, track) -> None:
    for table in ("embeddings", "audio_features", "track_instruments", "track_mbids",
                  "analysis_sources"):
        cur.execute(f"DELETE FROM {table} WHERE track_id = %s", (track,))


def _served(conn, author, category, **seal) -> tuple:
    """`author`'s sealed analysis of a new track, with instruments, as a peer
    serves `category`, then forgotten here: (track, payload)."""
    with conn.cursor() as cur:
        track = _track(cur)
        seal_track(cur, author, track, MODEL, recording=str(uuid.uuid4()),
                   instruments=seal.pop("instruments", SCORES), **seal)
        payload = sync_queries.PULL_HANDLERS[category](conn, [track])
        _forget(cur, track)
    return track, payload


def _own_batch(author, record: dict, payload: bytes) -> dict:
    """`record` sealed by `author` over `payload` in a batch of its own,
    stamped by the test authority: the item and its `batches` entry."""
    sig = rs.sign(payload, author)
    root, proofs = rs.merkle_tree([rs.record_leaf(sig)])
    date, ip_hash = "2026-11-10T10:00:00Z", str(uuid.uuid4())
    batch = {"author_pubkey": pub(author), "worker_date": date, "ip_hash": ip_hash,
             "worker_sig": AUTHORITY.sign(rs.timestamp_payload(root, date, ip_hash)).hex(),
             "authority": ISSUER, "timestamp_version": 2}
    item = {**record, "author_pubkey": pub(author), "signature": sig, "batch_root": root,
            "merkle_proof": proofs[0]}
    return {"items": [item], "batches": {root: batch}, "authors": {}}


def _provenance(duration=95) -> dict:
    return {"chromaprint": FP, "duration_seconds": duration, "grid_version": rs.GRID_VERSION,
            "is_lossless": True}


def _instruments_item(author, track, scores, version=2) -> dict:
    return _own_batch(author, {"track_uuid": track, "instruments": scores,
                               "analysis_version": version, "provenance": _provenance()},
                      rs.instruments_payload(pub(author), track, FP, 95, version, rs.blake2b_hex(
                          rs.canonical_instruments_blob(scores))))


# The v3 features record, as the release before signed it: the fourteen fields
# under the audio version 3.
_V3_ORDER = ["bpm", "key", "mode", "key_confidence", "energy", "energy_db", "brightness",
             "dynamic_range_db", "zero_crossing_rate", "danceability", "vocal_instrumental",
             "vocal_score", "instruments", "moods"]


def _v3_features(author, track) -> dict:
    fields = dict.fromkeys(_V3_ORDER)
    fields.update(bpm=120.5, energy=0.25, danceability=0.6, vocal_instrumental="mixed",
                  vocal_score=0.5, instruments=SCORES, moods={"calm and relaxing": 0.4})
    blob = "|".join(rs._fmt_feature(fields[c]) for c in _V3_ORDER).encode("utf-8")
    payload = ":".join(["sautium-record", "v3", "features", pub(author), track, FP, "95", "2",
                        rs.blake2b_hex(blob)]).encode("utf-8")
    return _own_batch(author, {"track_uuid": track, **fields, "analysis_version": 2,
                               "provenance": _provenance()}, payload)


def _v4_features(author, track, version=2, **values) -> dict:
    fields = dict.fromkeys(rs.FEATURE_ORDER)
    fields.update({"bpm": 120.5, "energy": 0.25, **values})
    return _own_batch(author, {"track_uuid": track, **fields, "analysis_version": version,
                               "provenance": _provenance()},
                      rs.features_payload(pub(author), track, FP, 95, version, rs.blake2b_hex(
                          rs.canonical_features_blob(fields))))


def _row(cur, table, track) -> tuple:
    cur.execute(f"""SELECT a.analysis_version, a.author_pubkey, s.imported FROM {table} a
                     LEFT JOIN analysis_sources s ON s.id = a.analysis_source_id
                    WHERE a.track_id = %s""", (track,))
    return cur.fetchone()


# -- the gate -------------------------------------------------------------------

def test_an_instruments_record_travels_sealed_and_lands_as_its_authors(conn, scratch_dsn):
    track, payload = _served(conn, AUTHOR, "instruments")
    (item,) = payload["items"]
    assert payload["category"] == "instruments"
    assert {k for k in item} == {"track_uuid", "instruments", "analysis_version", "author_pubkey",
                                 "signature", "batch_root", "merkle_proof", "provenance"}
    assert _import(scratch_dsn, "instruments", payload) == 1
    with conn.cursor() as cur:
        cur.execute("""SELECT i.instruments, i.analysis_version, i.author_pubkey, i.signature,
                              s.imported, s.chromaprint
                         FROM track_instruments i JOIN analysis_sources s ON s.id = i.analysis_source_id
                        WHERE i.track_id = %s""", (track,))
        assert cur.fetchone() == (SCORES, 2, pub(AUTHOR), item["signature"], True, FP)
    # served again from here under its author's seal, as any record is
    again = sync_queries.pull_instruments(conn, [track])
    assert again["items"][0]["signature"] == item["signature"]
    assert again["authors"].keys() <= {pub(AUTHOR)}


@pytest.mark.parametrize("category", ["tracks", "track-mbids", "track_mbids", "segments",
                                      "audio-features", "audio_features", "instruments"])
def test_the_docker_router_answers_every_pull_category_as_the_shared_handler(
        conn, scratch_dsn, monkeypatch, category):
    """The Docker node's /pull/{category} serves the launcher's handlers
    (sync_queries.PULL_HANDLERS) — the track rows and the recording bindings
    too, which the router's own copies served before."""
    import asyncio
    import contextlib
    import json

    pytest.importorskip("fastapi")
    from starlette.requests import Request

    from routers import sync as router
    with conn.cursor() as cur:
        track = _track(cur, "Through the router")
        seal_track(cur, AUTHOR, track, MODEL, recording=str(uuid.uuid4()), instruments=SCORES)
    monkeypatch.setattr(router, "_require_sharing", lambda: None)
    monkeypatch.setattr(router, "_own_slice_cert", lambda: None)
    monkeypatch.setattr(router, "get_conn",
                        lambda: contextlib.closing(psycopg2.connect(scratch_dsn)))
    answer = asyncio.run(router.pull(category, router.PullRequest(uuids=[track]),
                                     Request({"type": "http", "headers": []})))
    handler = sync_queries.PULL_HANDLERS[category]
    direct = handler(conn, [track]) if category == "tracks" else handler(conn, [track], own=None)
    served = json.loads(answer.body)
    assert served == json.loads(json.dumps(direct))
    assert [it["track_uuid"] for it in served["items"]] == [track]


def test_a_v3_features_record_is_refused_where_its_v4_twin_lands(conn, scratch_dsn):
    """No compatibility with the old format: a v3 features record, sealed,
    stamped and whole, is rebuilt as v4 at the gate and does not verify."""
    with conn.cursor() as cur:
        old, new = _track(cur), _track(cur)
    assert _import(scratch_dsn, "audio_features", _v3_features(AUTHOR, old)) == 0
    assert _import(scratch_dsn, "audio_features", _v4_features(AUTHOR, new)) == 1
    with conn.cursor() as cur:
        cur.execute("SELECT track_id::text FROM audio_features")
        assert cur.fetchall() == [(new,)]


@pytest.mark.parametrize("scores", [
    {"piano": "loud"}, {"piano": 1.5}, {"piano": -0.1}, {"piano": float("nan")}, {"": 0.5},
    {"x" * 65: 0.5}, {f"label {n}": 0.5 for n in range(129)}, {"pi\x00ano": 0.5},
    [["piano", 0.6]], None, {"piano": True}, {"piano": -0.0}])
def test_an_instruments_map_no_reader_could_threshold_is_refused_whatever_its_seal(
        conn, scratch_dsn, scores):
    """Sealed validly over what it carries where a map can be sealed at
    all: the database would take most of these, and a reader's cast would
    fail on them."""
    with conn.cursor() as cur:
        track = _track(cur)
    if isinstance(scores, dict):
        payload = _instruments_item(AUTHOR, track, scores)
    else:
        payload = _own_batch(AUTHOR, {"track_uuid": track, "instruments": scores,
                                      "analysis_version": 2, "provenance": _provenance()},
                             b"no map to seal")
    assert _import(scratch_dsn, "instruments", payload) == 0


def test_a_map_at_the_bounds_is_a_record(conn, scratch_dsn):
    with conn.cursor() as cur:
        many, long = _track(cur), _track(cur)
    labels = {f"label {n}": 0.5 for n in range(sync_client.INSTRUMENT_LABELS)}
    assert _import(scratch_dsn, "instruments", _instruments_item(AUTHOR, many, labels)) == 1
    named = {"x" * sync_client.INSTRUMENT_LABEL_CHARS: 0.5, "y": 1, "z": 0}
    assert _import(scratch_dsn, "instruments", _instruments_item(AUTHOR, long, named)) == 1
    with conn.cursor() as cur:
        cur.execute("SELECT instruments FROM track_instruments WHERE track_id = %s", (long,))
        assert cur.fetchone() == (named,)


@pytest.mark.parametrize("values,lands", [
    (dict(bpm=120.0, key_confidence=1.0), True),
    (dict(bpm=120), False),                  # float8 hands back 120.0: another hash
    (dict(energy_db=-14), False),
    (dict(bpm=True), False)])
def test_a_feature_is_a_float_where_its_column_is_float8(conn, scratch_dsn, values, lands):
    """The honest serializer sends a float8 column's value as a float: an
    int sealed in its place verifies here and would be re-served as the
    float its column holds, under a hash its seal does not bind."""
    with conn.cursor() as cur:
        track = _track(cur)
    assert _import(scratch_dsn, "audio_features", _v4_features(AUTHOR, track, **values)) == lands
    if lands:
        served = sync_queries.pull_audio_features(conn, [track])["items"][0]
        assert all(isinstance(served[f], float) for f in ("bpm", "energy", "key_confidence"))


def test_an_empty_map_is_a_record_and_an_unsealed_one_is_none(conn, scratch_dsn):
    with conn.cursor() as cur:
        empty, unsealed = _track(cur), _track(cur)
    assert _import(scratch_dsn, "instruments", _instruments_item(AUTHOR, empty, {})) == 1
    payload = _instruments_item(AUTHOR, unsealed, SCORES)
    payload["items"][0]["signature"] = None
    assert _import(scratch_dsn, "instruments", payload) == 0


# -- the methodology bump ----------------------------------------------------------

def _first_hand(cur, track, table, version) -> None:
    cur.execute("""INSERT INTO analysis_sources (track_id, chromaprint, duration_seconds, imported)
                   VALUES (%s, 'AQAAown', 95, false) RETURNING id""", (track,))
    source = cur.fetchone()[0]
    if table == "track_instruments":
        cur.execute("""INSERT INTO track_instruments (track_id, instruments, analysis_source_id,
                                                      analysis_version)
                       VALUES (%s, '{"organ": 0.3}', %s, %s)""", (track, source, version))
    else:
        cur.execute("""INSERT INTO audio_features (track_id, bpm, analysis_source_id,
                                                   analysis_version) VALUES (%s, 99, %s, %s)""",
                    (track, source, version))


_KINDS = [
    ("instruments", "track_instruments", lambda a, t, v: _instruments_item(a, t, SCORES, v),
     rs.INSTRUMENTS_ANALYSIS_VERSION),
    ("audio_features", "audio_features", lambda a, t, v: _v4_features(a, t, v),
     rs.FEATURES_ANALYSIS_VERSION)]


def test_the_newest_methodology_is_the_one_this_release_computes():
    import audio_analysis
    import ensemble_instruments
    assert rs.FEATURES_ANALYSIS_VERSION == audio_analysis.ANALYSIS_VERSION
    assert rs.INSTRUMENTS_ANALYSIS_VERSION == ensemble_instruments.INSTRUMENTS_ANALYSIS_VERSION


@pytest.mark.parametrize("category,table,item,newest", _KINDS)
def test_a_newer_methodology_replaces_an_imported_record_never_a_first_hand_one(
        conn, scratch_dsn, category, table, item, newest):
    older = newest - 1
    with conn.cursor() as cur:
        imported, first_hand, newer_here, twice, twice_held = (_track(cur) for _ in range(5))
        _first_hand(cur, first_hand, table, older)
    assert _import(scratch_dsn, category, item(AUTHOR, imported, older)) == 1
    assert _import(scratch_dsn, category, item(AUTHOR, newer_here, newest)) == 1
    assert _import(scratch_dsn, category, item(AUTHOR, twice_held, older)) == 1
    assert _import(scratch_dsn, category, item(NEWER, imported, newest)) == 1      # replaced
    assert _import(scratch_dsn, category, item(NEWER, first_hand, newest)) == 0    # never
    assert _import(scratch_dsn, category, item(NEWER, newer_here, older)) == 0     # not older
    assert _import(scratch_dsn, category, item(NEWER, newer_here, newest)) == 0    # nor equal
    # one payload naming a track twice lands its newest methodology, held
    # here before or not, whatever the order
    for track, order in ((twice, (newest, older)), (twice_held, (older, newest))):
        payloads = [item(NEWER, track, v) for v in order]
        both = {"items": [i for p in payloads for i in p["items"]], "authors": {},
                "batches": {r: b for p in payloads for r, b in p["batches"].items()}}
        assert _import(scratch_dsn, category, both) == 1
    with conn.cursor() as cur:
        assert _row(cur, table, imported) == (newest, pub(NEWER), True)
        assert _row(cur, table, first_hand) == (older, None, False)
        assert _row(cur, table, newer_here) == (newest, pub(AUTHOR), True)
        assert _row(cur, table, twice) == (newest, pub(NEWER), True)
        assert _row(cur, table, twice_held) == (newest, pub(NEWER), True)


@pytest.mark.parametrize("category,table,item,newest", _KINDS)
def test_a_methodology_no_release_computes_is_refused_and_pins_nothing(
        conn, scratch_dsn, category, table, item, newest):
    """Probe P2 of the first review: a certified key sealing a version no
    release makes replaced every imported record of its kind and kept the
    honest ones out for good. The gate holds a record to the newest
    methodology this release computes, and the planner asks for none above
    it."""
    with conn.cursor() as cur:
        honest, gap = _track(cur), _track(cur)
    assert _import(scratch_dsn, category, item(AUTHOR, honest, newest - 1)) == 1
    for version in (newest + 1, 2 ** 15 - 1):
        assert _import(scratch_dsn, category, item(NEWER, honest, version)) == 0
        assert _import(scratch_dsn, category, item(NEWER, gap, version)) == 0
    assert _import(scratch_dsn, category, item(AUTHOR, honest, newest)) == 1      # the honest bump
    client = sync_client.SyncClient(api_client=None, db_dsn=scratch_dsn)
    try:
        needed = client._compute_needed(dict(
            sync_queries.EMPTY_INVENTORY, **{category: [[honest, 2 ** 15 - 1], [gap, newest + 1]]}),
            use_segments=False)
    finally:
        client._close_conn()
    assert needed == {}
    with conn.cursor() as cur:
        assert _row(cur, table, honest) == (newest, pub(AUTHOR), True)
        assert _row(cur, table, gap) is None


def test_a_push_naming_a_track_twice_lands_what_a_pull_of_it_lands(conn, scratch_dsn):
    """Probe P3 of the second review: the carry's admission kept a track's
    first item, where the importer keeps its newest methodology — a push of
    [v1, v2] landed v1, a pull v2. The admission takes the track, and the
    importer the item, on both paths."""
    album = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute("INSERT INTO albums (id, title) VALUES (%s, 'LP')", (album,))
        pushed, pulled = _track(cur), _track(cur)
        for n, t in enumerate((pushed, pulled), 1):
            cur.execute("""INSERT INTO album_tracks (album_id, track_id, position, recording_mbid)
                           VALUES (%s, %s, %s, %s)""", (album, t, n, str(uuid.uuid4())))

    def twice(track):
        payloads = [_v4_features(AUTHOR, track, version=v) for v in (1, 2)]
        return {"items": [i for p in payloads for i in p["items"]], "authors": {},
                "batches": {r: b for p in payloads for r, b in p["batches"].items()}}

    assert sync_client.import_pushed(scratch_dsn, "audio_features", twice(pushed), 100) == 1
    assert _import(scratch_dsn, "audio_features", twice(pulled)) == 1
    with conn.cursor() as cur:
        cur.execute("SELECT track_id::text, analysis_version FROM audio_features")
        assert dict(cur.fetchall()) == {pushed: 2, pulled: 2}


def test_the_planner_asks_only_for_what_the_importer_would_take(conn, scratch_dsn):
    newest = rs.INSTRUMENTS_ANALYSIS_VERSION
    with conn.cursor() as cur:
        imported, first_hand, newer_here, absent = (_track(cur) for _ in range(4))
        _first_hand(cur, first_hand, "track_instruments", newest - 1)
    _import(scratch_dsn, "instruments", _instruments_item(AUTHOR, imported, SCORES, newest - 1))
    _import(scratch_dsn, "instruments", _instruments_item(AUTHOR, newer_here, SCORES, newest))
    client = sync_client.SyncClient(api_client=None, db_dsn=scratch_dsn)
    try:
        needed = client._compute_needed(dict(
            sync_queries.EMPTY_INVENTORY,
            instruments=[[imported, newest], [first_hand, newest], [newer_here, newest],
                         [absent, newest], [absent, str(newest)], [absent, 0]]),
            use_segments=False)
    finally:
        client._close_conn()
    assert sorted(needed["instruments"]) == sorted([absent, imported])


# -- the holdings filters, the gap sets ----------------------------------------------

def test_the_holdings_name_the_tracks_with_instruments_in_a_filter_of_their_own(conn, monkeypatch):
    from desktop.p2p.bloom import BloomFilter
    monkeypatch.setattr(sync_queries, "_holdings", {"checked_at": 0.0, "version": None,
                                                    "payload": None, "tracks_n": 0,
                                                    "instruments_n": 0})
    with conn.cursor() as cur:
        tagged, sonic, only_tagged, unsealed = (_track(cur) for _ in range(4))
        seal_track(cur, AUTHOR, tagged, MODEL, recording=str(uuid.uuid4()), instruments=SCORES)
        seal_track(cur, AUTHOR, sonic, MODEL, recording=str(uuid.uuid4()))
        seal_track(cur, AUTHOR, only_tagged, MODEL, recording=str(uuid.uuid4()),
                   instruments=SCORES)
        for table in ("embeddings", "audio_features"):       # its instruments alone stay
            cur.execute(f"DELETE FROM {table} WHERE track_id = %s", (only_tagged,))
        _first_hand(cur, unsealed, "track_instruments", 2)   # nothing the inventory serves
    version = sync_queries.holdings_version(conn)
    payload = sync_queries.get_holdings(conn)
    tracks = BloomFilter.from_dict(payload["tracks"])
    instruments = BloomFilter.from_dict(payload["instruments"])
    every = [tagged, sonic, only_tagged, unsealed]
    assert set(tracks.hits(every)) == {tagged, sonic}
    assert set(instruments.hits(every)) == {tagged, only_tagged}
    assert {r[0] for r in sync_queries.get_inventory(conn, every)["instruments"]} == {
        tagged, only_tagged}                                 # the filter's population
    assert sync_queries.holdings_summary() == {"version": version, "tracks": 2, "instruments": 2}
    with conn.cursor() as cur:
        seal_track(cur, AUTHOR, _track(cur), MODEL, recording=str(uuid.uuid4()),
                   instruments=SCORES)
    assert sync_queries.holdings_version(conn) != version


def test_the_client_tests_a_gap_of_instruments_alone_against_the_second_filter(conn, scratch_dsn):
    """What _holdings_inventory tests against a peer's instruments filter
    alone: the tracks whose one want is their instruments — segments held,
    features no pull would replace, no instruments. A features record of an
    older methodology keeps its track on the tracks filter, where a peer's
    newer one is found. Read once per walk run."""
    with conn.cursor() as cur:
        tagged, sonic, older, only_tagged, unsegmented = (_track(cur) for _ in range(5))
        seal_track(cur, AUTHOR, tagged, MODEL, recording=str(uuid.uuid4()), instruments=SCORES)
        seal_track(cur, AUTHOR, sonic, MODEL, recording=str(uuid.uuid4()))
        seal_track(cur, AUTHOR, older, MODEL, recording=str(uuid.uuid4()), analysis_version=1)
        _first_hand(cur, only_tagged, "track_instruments", 2)
        _first_hand(cur, unsegmented, "audio_features", 2)
    run: dict = {}
    client = sync_client.SyncClient(api_client=None, db_dsn=scratch_dsn, run_cache=run)
    try:
        assert client._lacking_only_instruments() == {sonic}
    finally:
        client._close_conn()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM embeddings WHERE track_id = %s", (sonic,))
    later = sync_client.SyncClient(api_client=None, db_dsn=scratch_dsn, run_cache=run)
    try:
        assert later._lacking_only_instruments() == {sonic}      # the run's read, not another
    finally:
        later._close_conn()


def test_an_instruments_gap_triggers_a_run_and_costs_no_dht_traffic(conn):
    with conn.cursor() as cur:
        artist = str(uuid.uuid4())
        cur.execute("INSERT INTO artists (id, name) VALUES (%s, 'gapped')", (artist,))
        track = _track(cur)
        cur.execute("INSERT INTO track_artists (track_id, artist_id, role) VALUES (%s, %s, 'primary')",
                    (track, artist))
        seal_track(cur, AUTHOR, track, MODEL, recording=str(uuid.uuid4()))   # no instruments
        cur.execute("INSERT INTO listening_history (track_id, started_at, completed, skipped) "
                    "VALUES (%s, now(), true, false)", (track,))         # engaged
    assert artist in sync_queries.get_incomplete_artist_uuids(conn)
    assert artist not in sync_queries.get_rare_search_uuids(conn)
    with conn.cursor() as cur:
        _first_hand(cur, track, "track_instruments", 2)
    assert artist not in sync_queries.get_incomplete_artist_uuids(conn)


# -- the identity canon ------------------------------------------------------------

@pytest.fixture
def session(scratch_dsn, monkeypatch):
    import database
    from sqlalchemy.orm import sessionmaker
    engine = database.make_engine(scratch_dsn)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()
    engine.dispose()


def test_a_merge_carries_the_instruments_and_a_rename_sheds_their_seal(conn, session):
    """The instruments are an analysis kind of their own (provenance.
    ANALYSIS_KINDS): the merge moves them where the target holds none, even
    when the target wins on the embedding; a rename sheds their seal (it
    binds the track's uuid); a track holding only them is no orphan."""
    from sqlalchemy import text

    from canon import identity
    with conn.cursor() as cur:
        renamed = _track(cur)
        seal_track(cur, AUTHOR, renamed, MODEL, recording=str(uuid.uuid4()), instruments=SCORES)
    to = str(uuid.uuid4())
    assert identity._update_track_uuid(session, renamed, to) == to
    session.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT signature FROM track_instruments WHERE track_id = %s", (to,))
        assert cur.fetchone() == (None,)
        cur.execute("DELETE FROM tracks WHERE id = %s", (to,))
        old, new = _track(cur), _track(cur)
        seal_track(cur, AUTHOR, old, MODEL, recording=str(uuid.uuid4()), instruments=SCORES)
        cur.execute("""INSERT INTO analysis_sources (track_id, chromaprint, duration_seconds)
                       VALUES (%s, 'AQAAown', 95) RETURNING id""", (new,))
        cur.execute("""INSERT INTO embeddings (track_id, model_id, vector, analysis_source_id)
                       VALUES (%s, %s, %s::vector, %s)""",
                    (new, MODEL, "[" + ",".join(["0.04"] * 512) + "]", cur.fetchone()[0]))
    identity._merge_analysis(session, old, new)
    session.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT track_id::text, signature FROM track_instruments")
        assert cur.fetchall() == [(new, None)]                   # moved, its seal shed
        cur.execute("SELECT count(*) FROM embeddings WHERE track_id = %s", (new,))
        assert cur.fetchone() == (1,)                            # the target's own stood
        cur.execute("DELETE FROM embeddings WHERE track_id = %s", (new,))
        cur.execute("DELETE FROM audio_features WHERE track_id = %s", (new,))
    assert session.execute(text(
        f"SELECT count(*) FROM tracks t WHERE t.id = :t AND {identity.ORPHAN_TRACK_SQL.format(t='t')}"),
        {"t": new}).scalar() == 0


# -- the seed --------------------------------------------------------------------

def _pick(cur, track) -> dict:
    album = str(uuid.uuid4())
    cur.execute("INSERT INTO albums (id, title) VALUES (%s, 'Seeded')", (album,))
    cur.execute("INSERT INTO album_tracks (album_id, track_id, position, length_ms) "
                "VALUES (%s, %s, 1, 95000)", (album, track))
    return {"album_id": album, "tier": 1, "rank": 1, "artist": "a", "title": "Seeded",
            "owned_on_master": False}


@pytest.fixture
def fresh_node(monkeypatch):
    name = "sautium_test_records_v4_seeded"
    if not conftest._recreate(name, create=True):
        pytest.skip("no PostgreSQL for a scratch database")
    from desktop import db_init
    build = psycopg2.connect(dbname=name, **conftest.PG)
    db_init.apply_migrations(build)
    build.commit()
    yield name, build
    build.close()
    conftest._recreate(name, create=False)


def _apply_seed(name, conn, bundle, monkeypatch) -> dict:
    import seed_import
    monkeypatch.setattr(seed_import, "_load_bundle", lambda info: bundle)
    pg = conftest.PG
    dsn = f"postgresql://{pg['user']}:{pg['password']}@{pg['host']}:{pg['port']}/{name}"
    return seed_import.apply_seed(conn, dsn, {"version": bundle["version"]})


def test_a_pick_without_sealed_instruments_ships_with_a_warning(conn, monkeypatch):
    """The instruments are no pick's requirement: a pick whose tracks hold
    sealed segments and features and no instruments is exported, said."""
    import seed_export
    picks = []
    with conn.cursor() as cur:
        for rank, scores in ((1, SCORES), (2, None)):
            track, album = _track(cur), str(uuid.uuid4())
            root = seal_track(cur, AUTHOR, track, MODEL, recording=str(uuid.uuid4()),
                              instruments=scores)
            cur.execute("""INSERT INTO albums (id, title, signature, batch_root)
                           VALUES (%s, %s, %s, %s)""", (album, f"Pick {rank}", "5e" * 64, root))
            cur.execute("""INSERT INTO album_tracks (album_id, track_id, position, length_ms,
                                                     signature, batch_root)
                           VALUES (%s, %s, 1, 95000, %s, %s)""", (album, track, "5e" * 64, root))
            cur.execute("INSERT INTO album_descriptions (album_id, source, summary) "
                        "VALUES (%s, 'test', 'A pick')", (album,))
            picks.append({"rank": rank, "tier": 1, "artist": "a", "title": f"Pick {rank}",
                          "analysis_waiver": False, "album_id": album, "owned_on_master": False,
                          "_found": True, "_row": {"mbid": str(uuid.uuid4()), "cover_url": None}})
    monkeypatch.setattr(seed_export, "_manifest_cache", {"picks": [
        {"rank": rank, "owned_on_master": False} for rank in (1, 2)]})
    fatal, warns, report = seed_export._coverage(conn, picks)
    assert fatal == [] and [r["status"] for r in report] == ["ok", "ok"]
    assert [w for w in warns if "instruments" in w] == [
        "# 2 a — Pick 2: 1/1 tracks without sealed instruments (informational)"]


def test_seed_v4_carries_the_instruments_and_lands_whole(conn, fresh_node, monkeypatch):
    import seed_export
    with conn.cursor() as cur:
        track = _track(cur)
        seal_track(cur, AUTHOR, track, MODEL, recording=str(uuid.uuid4()), instruments=SCORES)
        pick = _pick(cur, track)
    bundle = seed_export.build_bundle(conn, [pick])
    assert bundle["version"] == seed_export.BUNDLE_VERSION == 4
    assert [it["track_uuid"] for it in bundle["analysis"]["instruments"]["items"]] == [track]
    name, node = fresh_node
    out = _apply_seed(name, node, bundle, monkeypatch)
    assert out["complete"] and out["counts"]["instruments"] == 1
    with node.cursor() as cur:
        cur.execute("SELECT instruments, author_pubkey FROM track_instruments WHERE track_id = %s",
                    (track,))
        assert cur.fetchone() == (SCORES, pub(AUTHOR))


def test_a_seed_v3_bundle_lands_its_segments_and_waits_for_v4(conn, fresh_node, monkeypatch):
    """Between the two pushes: seed v3's structure and segments land, its v3
    features fail the gate, it carries no instruments, and the import is
    not complete — no marker, the next bundle completes it."""
    import seed_export
    with conn.cursor() as cur:
        track = _track(cur)
        seal_track(cur, AUTHOR, track, MODEL, recording=str(uuid.uuid4()), instruments=SCORES)
        pick = _pick(cur, track)
    bundle = seed_export.build_bundle(conn, [pick])
    bundle["version"] = 3
    del bundle["analysis"]["instruments"]
    v3 = _v3_features(AUTHOR, track)
    bundle["analysis"]["audio_features"] = {"category": "audio_features", **v3}
    name, node = fresh_node
    out = _apply_seed(name, node, bundle, monkeypatch)
    assert not out["complete"]
    assert out["counts"]["segments"] == 1 and out["counts"]["audio_features"] == 0
    assert "instruments" not in out["counts"]


# -- the readers -------------------------------------------------------------------

@pytest.fixture
def library(conn, scratch_dsn, monkeypatch):
    """Two owned tracks of one album, one tagged with a piano well above its
    threshold and a saxophone under it, one never tagged; db_pool and the
    sessions on the module's database."""
    import db_pool
    from config import settings
    url = urlparse(scratch_dsn)
    for key, value in dict(postgres_host=url.hostname, postgres_port=url.port,
                           postgres_user=url.username, postgres_password=url.password,
                           postgres_db=url.path.lstrip("/")).items():
        monkeypatch.setattr(settings, key, value)
    db_pool.close_pool()
    with conn.cursor() as cur:
        artist, album = str(uuid.uuid4()), str(uuid.uuid4())
        cur.execute("INSERT INTO artists (id, name) VALUES (%s, 'the tagger')", (artist,))
        cur.execute("INSERT INTO albums (id, title) VALUES (%s, 'tagged')", (album,))
        cur.execute("INSERT INTO album_artists (album_id, artist_id, role) VALUES (%s, %s, 'primary')",
                    (album, artist))
        cur.execute("INSERT INTO album_variants (album_id, directory_path) VALUES (%s, '/t') "
                    "RETURNING id", (album,))
        variant = cur.fetchone()[0]
        tracks = {}
        for n, name in enumerate(("tagged", "bare")):
            track = tracks[name] = _track(cur, name)
            cur.execute("INSERT INTO track_artists (track_id, artist_id, role) "
                        "VALUES (%s, %s, 'primary')", (track, artist))
            cur.execute("""INSERT INTO media_files (track_id, album_variant_id, file_path,
                                                    track_number, duration_seconds)
                           VALUES (%s, %s, %s, %s, 200) RETURNING id""",
                        (track, variant, f"/t/{name}.flac", n + 1))
            tracks[name + "_file"] = cur.fetchone()[0]
            cur.execute("INSERT INTO audio_features (track_id, bpm) VALUES (%s, %s)", (track, 100 + n))
        cur.execute("""INSERT INTO track_instruments (track_id, instruments, analysis_version)
                       VALUES (%s, '{"piano": 0.62, "saxophone": 0.02}', 2)""", (tracks["tagged"],))
    yield {"tracks": tracks, "album": album, "artist": artist}
    db_pool.close_pool()


def test_the_discovery_gate_reads_the_instruments_record(library, session):
    import discovery_engine
    from sqlalchemy import text
    tracks = library["tracks"]

    def found(active: dict, level: str = "track") -> set:
        sql, params = discovery_engine.build(active, {}, corpus="owned", limit=50)[level]
        return {str(r.id) for r in session.execute(text(sql), params)}

    assert found({"instruments": ["piano"]}) == {tracks["tagged"]}
    assert found({"instruments": ["saxophone"]}) == set()       # under its threshold
    assert found({"instruments": ["piano"]}, "album") == {library["album"]}


def test_search_and_the_instrument_options_read_the_instruments_record(library, session):
    import search
    from routers import discovery
    by_title = {r["title"]: r for r in search.search_by_features(session, {"bpm_min": 1})["results"]}
    assert by_title["tagged"]["instruments"] == {"piano": 0.62}
    assert by_title["bare"]["instruments"] == {}
    assert {r["title"] for r in search.search_by_features(session, {"instrument": "Piano"})[
        "results"]} == {"tagged"}
    assert discovery.instrument_options(q="pia", limit=8) == {
        "instruments": [{"name": "piano", "track_count": 1}]}
    assert discovery.instrument_options(q="sax", limit=8) == {"instruments": []}


def test_now_playing_and_the_assistant_show_the_instruments_record(library):
    import assistant_queries
    import db_pool
    from routers import player
    tracks = library["tracks"]
    detail = player.now_playing_detail(media_file_id=tracks["tagged_file"])
    assert detail["instruments"] == {"piano": 0.62}
    assert player.now_playing_detail(media_file_id=tracks["bare_file"])["instruments"] == {}
    features = assistant_queries.track_features(db_pool.db_query, tracks["tagged"])
    assert features["instruments"] == {"piano": 0.62, "saxophone": 0.02}
    assert "Instruments: piano (0.62)" in assistant_queries.format_track_info(
        {"artist": "the tagger", "title": "tagged", "album": "tagged",
         "track_id": tracks["tagged"]}, features)
