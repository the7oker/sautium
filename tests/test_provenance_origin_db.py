"""Who made a record, and joins by similarity (ANALYSIS-3 body 2), on a real
PostgreSQL built from the migrations — the module's scratch database
(conftest.scratch_dsn), skipped without a cluster.

analysis_sources is unique per origin (track, address, imported): this node's
registrations work on the first-hand row of an address, the sync import on
its imported twin, and nothing turns one into the other — so sign_audio,
which signs by the flag, never signs another author's record, whatever shed
its seal. A track's rows of one material whose prints differ are grouped
(material_of, desktop/p2p/material.py's rule): the passes skip what the group
holds at a rank no lower, a stream the group holds higher is already held, a
returned file finds its analysis by the rule, the merge moves rows into the
target's groups. And "held" means first-hand or sealed: an imported record a
shed unsealed is a gap the author's sealed version fills.

Fingerprints are generated (tests/p2p/fingerprints.py); fpcalc and ffprobe
are stood in for where a registration would decode."""

import sys
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
for p in (REPO, REPO / "backend"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

psycopg2 = pytest.importorskip("psycopg2")
import psycopg2.extras  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import database  # noqa: E402
import embeddings  # noqa: E402
import audio_analysis  # noqa: E402
import p2p_identity  # noqa: E402
import provenance  # noqa: E402
import sign_audio  # noqa: E402
from canon import identity  # noqa: E402
from desktop import sync_client  # noqa: E402
from desktop.p2p import material, sync_queries  # noqa: E402
from tests.p2p.fingerprints import encode, flip, noise  # noqa: E402
from tests.p2p.sealed_records import pub, seal_track  # noqa: E402
from tests.p2p.test_witness import ISSUER  # noqa: E402

MODEL = str(uuid.UUID(int=0xB2))
AUTHOR = Ed25519PrivateKey.generate()          # another node, whose records travel
NODE = Ed25519PrivateKey.generate()            # this node's own key
MIGRATION = REPO / "desktop" / "migrations" / "055_analysis_sources_per_origin.sql"

FP = encode(noise(950, 1))
NEAR = encode(flip(material.decode(FP), 0.01, 2))      # FP under another decoder
NEARER = encode(flip(material.decode(FP), 0.005, 3))
OTHER = encode(noise(950, 4))                          # another material


@pytest.fixture
def conn(scratch_dsn, monkeypatch):
    engine = database.make_engine(scratch_dsn)
    monkeypatch.setattr(database, "SessionLocal",
                        sessionmaker(autocommit=False, autoflush=False, bind=engine))
    monkeypatch.setattr(sync_client, "TRUSTED_AUTHORITIES", [ISSUER])
    c = psycopg2.connect(scratch_dsn)
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute("TRUNCATE tracks, albums, embedding_models, signing_batches, stream_providers "
                    "CASCADE")
        cur.execute("INSERT INTO embedding_models (id, name, dimension) VALUES (%s, 'clap', 512)",
                    (MODEL,))
        cur.execute("INSERT INTO stream_providers (id, name, lossless) VALUES "
                    "('lossless-test', 'A lossless provider', true), "
                    "('lossy-test', 'A lossy provider', false)")
    yield c
    c.close()
    engine.dispose()


@pytest.fixture
def session(conn):
    s = database.SessionLocal()
    yield s
    s.rollback()
    s.close()


@pytest.fixture
def prints(monkeypatch):
    """What fpcalc would print for a path (a file's, or a stream's temp file
    — then the one entry under "stream"), and what ffprobe would read of a
    stream."""
    printed = {}
    monkeypatch.setattr(provenance, "chromaprint_file", lambda path, *a: printed.get(
        "/music/" + path.rsplit("/", 1)[-1], printed.get("stream")))
    monkeypatch.setattr(provenance, "_probe_audio", lambda path: (44100, 16, printed["stream_dur"]))
    return printed


def _track(cur, title="t") -> str:
    tid = str(uuid.uuid4())
    cur.execute("INSERT INTO tracks (id, title) VALUES (%s, %s)", (tid, title))
    return tid


def _file(cur, track, path, bit_depth=16, lossless=True, duration=240, source=True) -> int:
    album = str(uuid.uuid4())
    cur.execute("INSERT INTO albums (id, title) VALUES (%s, 'LP')", (album,))
    cur.execute("INSERT INTO album_variants (album_id, directory_path) VALUES (%s, %s) RETURNING id",
                (album, path.rsplit("/", 1)[0]))
    variant = cur.fetchone()[0]
    cur.execute("""INSERT INTO media_files (track_id, album_variant_id, file_path, bit_depth,
                                            sample_rate, is_lossless, duration_seconds,
                                            is_analysis_source)
                   VALUES (%s, %s, %s, %s, 44100, %s, %s, %s) RETURNING id""",
                (track, variant, path, bit_depth, lossless, duration, source))
    return cur.fetchone()[0]


def _register(session, conn, track, mfid):
    with conn.cursor() as cur:
        cur.execute("SELECT file_path, sample_rate, bit_depth, is_lossless, duration_seconds "
                    "FROM media_files WHERE id = %s", (mfid,))
        path, sr, bd, ll, dur = cur.fetchone()
    sid = provenance.get_or_create_local(session, track, mfid, path, sr, bd, ll, dur)
    session.commit()
    return sid


def _source(cur, track, fp, *, imported=False, duration=240, media_file=None, provider=None,
            material_of=None, lossless=True, bit_depth=16) -> int:
    cur.execute("""INSERT INTO analysis_sources (track_id, chromaprint, duration_seconds, imported,
                                                 media_file_id, provider_id, material_of,
                                                 is_lossless, bit_depth)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                (track, fp, duration, imported, media_file, provider, material_of,
                 lossless, None if imported else bit_depth))
    return cur.fetchone()[0]


def _embedding(cur, track, source) -> int:
    cur.execute("""INSERT INTO embeddings (track_id, model_id, vector, analysis_source_id,
                                           analysis_version)
                   VALUES (%s, %s, %s::vector, %s, 2) RETURNING id""",
                (track, MODEL, "[" + ",".join(["0.03"] * 512) + "]", source))
    eid = cur.fetchone()[0]
    cur.execute("INSERT INTO embedding_segments (embedding_id, segment_index, vector) "
                "VALUES (%s, 0, %s::vector)", (eid, "[" + ",".join(["0.03"] * 512) + "]"))
    return eid


def _rows(cur, track) -> list:
    cur.execute("""SELECT id, imported, material_of, media_file_id, provider_id, chromaprint
                     FROM analysis_sources WHERE track_id = %s ORDER BY id""", (track,))
    return cur.fetchall()


def _payloads(conn, track, fp, duration=240):
    """The author's sealed bundle and features for `track`, as a peer would
    serve them — built here, then taken away again so only the import puts
    anything on the track."""
    with conn.cursor() as cur:
        seal_track(cur, AUTHOR, track, MODEL, recording=str(uuid.uuid4()),
                   chromaprint=fp, duration=duration)
        segments = sync_queries.pull_segments(conn, [track])
        features = sync_queries.pull_audio_features(conn, [track])
        cur.execute("DELETE FROM track_mbids WHERE track_id = %s", (track,))
        cur.execute("DELETE FROM embeddings WHERE track_id = %s", (track,))
        cur.execute("DELETE FROM audio_features WHERE track_id = %s", (track,))
        cur.execute("DELETE FROM analysis_sources WHERE track_id = %s AND imported", (track,))
    return segments, features


def _import(scratch_dsn, category, payload) -> int:
    client = sync_client.SyncClient(None, scratch_dsn)
    try:
        return client._import_items(category, payload)
    finally:
        client._close_conn()


def _linked(cur, track) -> dict:
    cur.execute("""SELECT 'embeddings', s.imported FROM embeddings e
                     JOIN analysis_sources s ON s.id = e.analysis_source_id WHERE e.track_id = %(t)s
                   UNION ALL
                   SELECT 'features', s.imported FROM audio_features a
                     JOIN analysis_sources s ON s.id = a.analysis_source_id WHERE a.track_id = %(t)s""",
                {"t": track})
    return dict(cur.fetchall())


# -- The constraint ---------------------------------------------------------

def _uniques(cur) -> set:
    cur.execute("SELECT conname FROM pg_constraint WHERE conrelid = 'analysis_sources'::regclass "
                "AND contype = 'u'")
    return {r[0] for r in cur.fetchall()}


def test_a_fresh_database_takes_a_twin_and_one_row_per_origin(conn):
    """001, then every delta: 018's tail adds the two-column constraint back
    after 001, and 055 drops it by name — beside it, every twin insert would
    fail."""
    with conn.cursor() as cur:
        assert _uniques(cur) == {"uq_asrc_track_chromaprint_origin"}
        track = _track(cur)
        first = _source(cur, track, FP)
        twin = _source(cur, track, FP, imported=True)
        assert first != twin
        with pytest.raises(psycopg2.errors.UniqueViolation):
            _source(cur, track, FP, imported=True)


def test_the_delta_moves_an_earlier_node_to_the_per_origin_key_and_runs_again_as_a_no_op(conn):
    sql = MIGRATION.read_text()
    with conn.cursor() as cur:
        track = _track(cur)
        kept = _source(cur, track, FP, imported=True)
        cur.execute("ALTER TABLE analysis_sources DROP CONSTRAINT uq_asrc_track_chromaprint_origin")
        cur.execute("ALTER TABLE analysis_sources DROP COLUMN material_of")
        cur.execute("ALTER TABLE analysis_sources ADD CONSTRAINT uq_asrc_track_chromaprint "
                    "UNIQUE (track_id, chromaprint_key)")
        cur.execute(sql)
        cur.execute(sql)
        assert _uniques(cur) == {"uq_asrc_track_chromaprint_origin"}
        cur.execute("SELECT indexdef FROM pg_indexes WHERE indexname = 'idx_analysis_sources_material'")
        assert "WHERE (material_of IS NOT NULL)" in cur.fetchone()[0]
        _source(cur, track, FP, material_of=kept)                    # its twin, in its group
        assert [(r[0] == kept, r[1], r[2]) for r in _rows(cur, track)] == [
            (True, True, None), (False, False, kept)]


def test_a_root_deleted_leaves_its_members_roots_of_their_own(conn):
    """SET NULL is the backstop: a split group costs at most another
    analysis."""
    with conn.cursor() as cur:
        track = _track(cur)
        root = _source(cur, track, FP)
        member = _source(cur, track, NEAR, material_of=root)
        cur.execute("DELETE FROM analysis_sources WHERE id = %s", (root,))
        assert [(r[0], r[2]) for r in _rows(cur, track)] == [(member, None)]


# -- Nothing flips a row ----------------------------------------------------

def test_a_file_registered_over_an_imported_address_makes_its_own_twin(conn, session, prints):
    with conn.cursor() as cur:
        track = _track(cur)
        foreign = _source(cur, track, FP, imported=True)
        _embedding(cur, track, foreign)
        mf = _file(cur, track, "/music/a.flac")
    prints["/music/a.flac"] = FP
    sid = _register(session, conn, track, mf)
    with conn.cursor() as cur:
        assert [(r[0], r[1], r[2], r[3]) for r in _rows(cur, track)] == [
            (foreign, True, None, None),           # still the author's, still unsigned here
            (sid, False, foreign, mf)]             # a twin joins with no comparison
        assert _linked(cur, track) == {"embeddings": True}


def test_a_stream_registered_over_an_imported_address_makes_its_own_twin(conn, session, prints):
    with conn.cursor() as cur:
        track = _track(cur)
        foreign = _source(cur, track, FP, imported=True)
    prints.update(stream=FP, stream_dur=240)
    sid = provenance.create_stream_source(session, track, b"audio", "lossless-test", True)
    session.commit()
    with conn.cursor() as cur:
        assert [(r[0], r[1], r[2], r[4]) for r in _rows(cur, track)] == [
            (foreign, True, None, None), (sid, False, foreign, "lossless-test")]


def test_the_importer_links_an_authors_records_to_its_own_twin_under_a_first_hand_row(
        conn, scratch_dsn):
    """This node registered the address (a pass that fingerprinted the file
    and has not analysed it yet): the author's segments and features link to
    an imported twin, never to the first-hand row."""
    with conn.cursor() as cur:
        track = _track(cur)
        segments, features = _payloads(conn, track, FP)
        own = _source(cur, track, FP)
    assert _import(scratch_dsn, "segments", segments) == 1
    assert _import(scratch_dsn, "audio_features", features) == 1
    with conn.cursor() as cur:
        [first, twin] = _rows(cur, track)
        assert (first[0], first[1], first[2]) == (own, False, None)
        assert (twin[1], twin[2]) == (True, own)
        assert _linked(cur, track) == {"embeddings": True, "features": True}
        cur.execute("SELECT DISTINCT analysis_source_id FROM embeddings WHERE track_id = %(t)s "
                    "UNION SELECT analysis_source_id FROM audio_features WHERE track_id = %(t)s",
                    {"t": track})
        assert cur.fetchall() == [(twin[0],)]


def test_an_imported_row_new_on_its_track_joins_a_similar_row_by_the_rule(conn, scratch_dsn):
    with conn.cursor() as cur:
        near, far = _track(cur), _track(cur)
        near_segments, _f = _payloads(conn, near, NEAR)
        far_segments, _f = _payloads(conn, far, NEAR, duration=250)
        mine = _source(cur, near, FP)
        _source(cur, far, FP)
    assert _import(scratch_dsn, "segments", near_segments) == 1
    assert _import(scratch_dsn, "segments", far_segments) == 1
    with conn.cursor() as cur:
        assert [r[2] for r in _rows(cur, near)] == [None, mine]
        assert [r[2] for r in _rows(cur, far)] == [None, None]     # ten seconds apart: an edit


def test_an_item_the_importer_drops_leaves_no_twin(conn, scratch_dsn):
    """Protected (this node's own analysis): dropped before provenance is
    registered — the master's seed import would otherwise put an imported
    twin nothing links to beside every seed track it analysed itself."""
    with conn.cursor() as cur:
        track = _track(cur)
        segments, features = _payloads(conn, track, FP)
        own = _source(cur, track, FP)
        _embedding(cur, track, own)
        cur.execute("INSERT INTO audio_features (track_id, bpm, analysis_source_id) "
                    "VALUES (%s, 99, %s)", (track, own))
    assert _import(scratch_dsn, "segments", segments) == 0
    assert _import(scratch_dsn, "audio_features", features) == 0
    with conn.cursor() as cur:
        assert [(r[0], r[1]) for r in _rows(cur, track)] == [(own, False)]


def test_sign_never_signs_an_imported_twins_record_whose_seal_a_shed_removed(
        conn, scratch_dsn, session, monkeypatch):
    """The hole the per-origin key closes: before it, the import linked the
    author's segments to the first-hand row of their address, and after a
    rename's or a merge's shed this node signed them as its own."""
    monkeypatch.setattr(p2p_identity, "_signing_key_cache", NODE)
    with conn.cursor() as cur:
        track, mine = _track(cur), _track(cur)
        segments, features = _payloads(conn, track, FP)
        _source(cur, track, FP)
        _embedding(cur, mine, _source(cur, mine, OTHER))         # this node's own, unsigned
    assert _import(scratch_dsn, "segments", segments) == 1
    assert _import(scratch_dsn, "audio_features", features) == 1
    identity._shed_track_seals(session, track)
    session.commit()

    stage = psycopg2.connect(scratch_dsn, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        assert sign_audio.sign(stage) == 1                       # its own segment, nothing else
    finally:
        stage.close()
    with conn.cursor() as cur:
        cur.execute("""SELECT e.track_id::text, es.author_pubkey FROM embedding_segments es
                         JOIN embeddings e ON e.id = es.embedding_id ORDER BY 1, es.segment_index""")
        signed = cur.fetchall()
        assert {a for t, a in signed if t == track} == {None}
        assert [a for t, a in signed if t == mine] == [pub(NODE)]
        cur.execute("SELECT author_pubkey FROM audio_features WHERE track_id = %s", (track,))
        assert cur.fetchone() == (None,)


# -- "Held" is first-hand or sealed ------------------------------------------

class _Peer:
    """A peer that holds the author's sealed analysis of the tracks."""
    base_url = "https://peer:1"
    peer_pubkey = "ab" * 32

    def __init__(self, payloads):
        self.payloads, self.pulls = payloads, []

    def get_health(self):
        return {"capabilities": ["segments"]}

    def sync_inventory(self, track_uuids):
        held = [t for t in track_uuids if t in self.payloads]
        return {"tracks": held, "embeddings": [[t, 2, 3] for t in held],
                "audio_features": [[t, 2] for t in held]}

    def sync_pull(self, category, uuids):
        self.pulls.append((category, sorted(uuids)))
        key = "segments" if category == "segments" else "audio_features"
        items = [it for t in uuids for it in self.payloads[t][key]["items"]]
        batches = {}
        for t in uuids:
            batches.update(self.payloads[t][key]["batches"])
        return {"category": key, "items": items, "batches": batches}


def test_an_imported_record_a_shed_unsealed_is_a_gap_its_authors_sealed_version_fills(
        conn, scratch_dsn, session):
    with conn.cursor() as cur:
        shed, sealed = _track(cur, "shed"), _track(cur, "sealed")
        artist = str(uuid.uuid4())
        cur.execute("INSERT INTO artists (id, name) VALUES (%s, 'Held')", (artist,))
        cur.execute("INSERT INTO track_artists (track_id, artist_id, role) VALUES (%s, %s, 'primary')",
                    (shed, artist))
        payloads = {t: dict(zip(("segments", "audio_features"), _payloads(conn, t, FP)))
                    for t in (shed, sealed)}
    for t in (shed, sealed):
        assert _import(scratch_dsn, "segments", payloads[t]["segments"]) == 1
        assert _import(scratch_dsn, "audio_features", payloads[t]["audio_features"]) == 1
    assert sync_queries.get_incomplete_artist_uuids(conn) == []
    identity._shed_track_seals(session, shed)
    session.commit()

    # the gap sets and the carry see the gap ...
    assert sync_queries.get_unenriched_artist_uuids(conn) == [artist]
    assert sync_queries.get_incomplete_artist_uuids(conn) == [artist]
    with conn.cursor() as cur:
        cur.execute(f"""SELECT u::text FROM unnest(%s::uuid[]) u
                         WHERE {sync_queries._LACKS['segments'].format(t='u')}
                           AND {sync_queries._LACKS['audio_features'].format(t='u')}""",
                    ([shed, sealed],))
        assert cur.fetchall() == [(shed,)]
    # ... the planner asks for it and nothing held ...
    peer = _Peer(payloads)
    stats = sync_client.SyncClient(peer, scratch_dsn).run_sync([shed, sealed])
    assert peer.pulls == [("segments", [shed]), ("audio-features", [shed])]
    assert stats == {"segments": 1, "audio_features": 1}
    # ... and the import replaces it with the author's sealed records
    with conn.cursor() as cur:
        cur.execute("""SELECT count(*) FILTER (WHERE es.signature IS NULL), count(*)
                         FROM embedding_segments es JOIN embeddings e ON e.id = es.embedding_id
                        WHERE e.track_id = %s""", (shed,))
        assert cur.fetchone() == (0, 3)
        cur.execute("SELECT author_pubkey FROM audio_features WHERE track_id = %s", (shed,))
        assert cur.fetchone() == (pub(AUTHOR),)
    assert sync_queries.get_incomplete_artist_uuids(conn) == []


def test_first_hand_analysis_stays_held_sealed_or_not(conn, scratch_dsn):
    with conn.cursor() as cur:
        track = _track(cur)
        payloads = {track: dict(zip(("segments", "audio_features"), _payloads(conn, track, FP)))}
        own = _source(cur, track, OTHER)
        _embedding(cur, track, own)                              # unsigned, its own
        cur.execute("INSERT INTO audio_features (track_id, bpm, analysis_source_id, "
                    "analysis_version) VALUES (%s, 99, %s, 2)", (track, own))
    peer = _Peer(payloads)
    sync_client.SyncClient(peer, scratch_dsn).run_sync([track])
    assert peer.pulls == []


# -- The merge, by key and origin, and into the target's groups -------------

def _merge(session, old, new):
    identity._update_track_uuid(session, old, new)
    session.commit()


def test_a_merge_keeps_an_authors_row_its_own_twin_of_the_targets_first_hand_row(
        conn, session, scratch_dsn):
    """Before: the moving records re-pointed at whatever target row had their
    address — this node's own — and were this node's to sign after the
    merge's shed."""
    with conn.cursor() as cur:
        old, new = _track(cur, "old"), _track(cur, "new")
        segments, features = _payloads(conn, old, FP)
        own = _source(cur, new, FP)                       # registered here, not analysed yet
    assert _import(scratch_dsn, "segments", segments) == 1
    _merge(session, old, new)
    with conn.cursor() as cur:
        [first, twin] = _rows(cur, new)
        assert (first[0], first[1], first[2]) == (own, False, None)
        assert (twin[1], twin[2]) == (True, own)
        assert _linked(cur, new) == {"embeddings": True}


def test_a_merge_drops_a_duplicate_of_one_origin_and_re_points_its_members_first(conn, session):
    with conn.cursor() as cur:
        old, new = _track(cur, "old"), _track(cur, "new")
        target = _source(cur, new, OTHER, imported=True)
        target_member = _source(cur, new, FP, imported=True, material_of=target)
        dup = _source(cur, old, FP, imported=True)
        member = _source(cur, old, NEAR, imported=True, material_of=dup)
        _embedding(cur, old, dup)
        cur.execute("INSERT INTO audio_features (track_id, bpm, analysis_source_id) "
                    "VALUES (%s, 99, %s)", (old, member))
    _merge(session, old, new)
    with conn.cursor() as cur:
        rows = {r[0]: r for r in _rows(cur, new)}
        assert set(rows) == {target, target_member, member}     # the duplicate is gone
        assert rows[member][2] == target                         # its root's root, never a member
        cur.execute("SELECT analysis_source_id FROM embeddings WHERE track_id = %s", (new,))
        assert cur.fetchone() == (target_member,)
        cur.execute("SELECT analysis_source_id FROM audio_features WHERE track_id = %s", (new,))
        assert cur.fetchone() == (member,)


def test_a_moving_root_joins_the_targets_group_with_its_members(conn, session):
    """Two levels: the moving root joins the target's root by the rule, and
    its members re-point there."""
    with conn.cursor() as cur:
        old, new = _track(cur, "old"), _track(cur, "new")
        target = _source(cur, new, FP)
        root = _source(cur, old, NEAR)
        member = _source(cur, old, NEARER, material_of=root)
        far = _source(cur, old, OTHER)
        _embedding(cur, old, member)
        cur.execute("INSERT INTO audio_features (track_id, bpm, analysis_source_id) "
                    "VALUES (%s, 99, %s)", (old, root))
        cur.execute("INSERT INTO embedding_models (id, name, dimension) VALUES (%s, 'other', 512)",
                    (str(uuid.UUID(int=0xB3)),))
        cur.execute("""INSERT INTO embeddings (track_id, model_id, vector, analysis_source_id)
                       VALUES (%s, %s, %s::vector, %s)""",
                    (old, str(uuid.UUID(int=0xB3)), "[" + ",".join(["0.02"] * 512) + "]", far))
    _merge(session, old, new)
    with conn.cursor() as cur:
        rows = {r[0]: r[2] for r in _rows(cur, new)}
        assert rows == {target: None, root: target, member: target, far: None}


def test_a_merge_moves_each_kind_the_target_lacks_and_sheds_only_the_moved_seals(
        conn, session, scratch_dsn):
    """The target wins on embeddings (its own, sealed — they keep their
    seals) and has no features: the old track's features move."""
    with conn.cursor() as cur:
        old, new = _track(cur, "old"), _track(cur, "new")
        seal_track(cur, NODE, new, MODEL, recording=str(uuid.uuid4()), first_hand=True,
                   chromaprint=OTHER)
        cur.execute("DELETE FROM audio_features WHERE track_id = %s", (new,))
        _segments, features = _payloads(conn, old, FP)
    assert _import(scratch_dsn, "audio_features", features) == 1
    _merge(session, old, new)
    with conn.cursor() as cur:
        cur.execute("""SELECT count(*) FROM embedding_segments es JOIN embeddings e
                         ON e.id = es.embedding_id
                        WHERE e.track_id = %s AND es.signature IS NOT NULL""", (new,))
        assert cur.fetchone()[0] == 3
        cur.execute("""SELECT a.bpm, a.signature, s.imported FROM audio_features a
                         JOIN analysis_sources s ON s.id = a.analysis_source_id
                        WHERE a.track_id = %s""", (new,))
        assert cur.fetchone() == (120.5, None, True)


# -- The passes: the skip by rank, the predicates' group arm -----------------

def _pending(session, track, sql, reanalyze=False) -> bool:
    from sqlalchemy import text
    return bool(session.execute(text(f"SELECT 1 FROM ({sql}) q WHERE q.track_id = :t"),
                                {"min_seconds": 10, "reanalyze": reanalyze, "t": track}).fetchall())


def _elect(cur, track, mf):
    cur.execute("UPDATE media_files SET is_analysis_source = false WHERE track_id = %s", (track,))
    cur.execute("UPDATE media_files SET is_analysis_source = true WHERE id = %s", (mf,))


@pytest.mark.parametrize("old_bits,new_bits,skips", [(16, 16, True), (24, 16, False),
                                                     (16, 24, True)])
def test_a_file_of_the_analysed_material_skips_by_rank_and_stays_done(
        conn, session, prints, old_bits, new_bits, skips):
    """Another rip of the analysed master, or the same file under another
    decoder: the pass registers it, the rule joins its row to the analysis's
    group, and a rank no higher has nothing to compute — nor is it asked
    again. A better rip (CD over 24-bit) is analysed."""
    with conn.cursor() as cur:
        track = _track(cur)
        a = _file(cur, track, "/music/a.flac", bit_depth=old_bits)
        b = _file(cur, track, "/music/b.flac", bit_depth=new_bits, source=False)
    prints.update({"/music/a.flac": FP, "/music/b.flac": NEAR})
    first = _register(session, conn, track, a)
    with conn.cursor() as cur:
        _embedding(cur, track, first)
        cur.execute("INSERT INTO audio_features (track_id, bpm, analysis_source_id) "
                    "VALUES (%s, 99, %s)", (track, first))
        _elect(cur, track, b)
    for sql in (embeddings.PENDING_SQL, audio_analysis.PENDING_SQL):
        assert _pending(session, track, sql)                    # unregistered: asked once
    second = _register(session, conn, track, b)
    with conn.cursor() as cur:
        assert [r[2] for r in _rows(cur, track)] == [None, first]
    assert provenance.held_in_group(session, second, "embeddings", model_id=MODEL) is skips
    assert provenance.held_in_group(session, second, "audio_features") is skips
    for sql in (embeddings.PENDING_SQL, audio_analysis.PENDING_SQL):
        assert _pending(session, track, sql) is not skips


def test_another_material_on_the_track_is_analysed(conn, session, prints):
    with conn.cursor() as cur:
        track = _track(cur)
        a = _file(cur, track, "/music/a.flac")
        b = _file(cur, track, "/music/b.flac", source=False)
    prints.update({"/music/a.flac": FP, "/music/b.flac": OTHER})
    first = _register(session, conn, track, a)
    with conn.cursor() as cur:
        _embedding(cur, track, first)
        _elect(cur, track, b)
    second = _register(session, conn, track, b)
    assert not provenance.held_in_group(session, second, "embeddings", model_id=MODEL)
    assert _pending(session, track, embeddings.PENDING_SQL)


def test_imported_analysis_never_skips_a_first_hand_pass_and_stays_the_policys(
        conn, session, prints):
    """An owned track's imported analysis: re-derived only under
    enrichment.reanalyze_imported, and then never skipped in its favour."""
    with conn.cursor() as cur:
        track = _track(cur)
        foreign = _source(cur, track, NEAR, imported=True)
        _embedding(cur, track, foreign)
        mf = _file(cur, track, "/music/a.flac")
    prints["/music/a.flac"] = FP
    assert not _pending(session, track, embeddings.PENDING_SQL)
    assert _pending(session, track, embeddings.PENDING_SQL, reanalyze=True)
    sid = _register(session, conn, track, mf)
    with conn.cursor() as cur:
        assert [r[2] for r in _rows(cur, track)] == [None, foreign]
    assert not provenance.held_in_group(session, sid, "embeddings", model_id=MODEL)
    assert _pending(session, track, embeddings.PENDING_SQL, reanalyze=True)


# -- A stream already held ---------------------------------------------------

def test_a_stream_of_material_held_higher_in_its_group_is_already_held(conn, session, prints):
    with conn.cursor() as cur:
        track = _track(cur)
        mf = _file(cur, track, "/music/a.flac")
    prints.update({"/music/a.flac": FP, "stream": NEAR, "stream_dur": 241})
    _register(session, conn, track, mf)
    assert provenance.create_stream_source(session, track, b"audio", "lossless-test", True) is None
    session.rollback()
    with conn.cursor() as cur:
        assert len(_rows(cur, track)) == 1


def test_a_lossy_stream_under_a_lossless_one_is_held_and_a_better_stream_is_not(
        conn, session, prints):
    with conn.cursor() as cur:
        lossless, lossy = _track(cur), _track(cur)
        _source(cur, lossless, FP, provider="lossless-test")
        _source(cur, lossy, FP, provider="lossy-test", lossless=False)
    prints.update(stream=NEAR, stream_dur=240)
    assert provenance.create_stream_source(session, lossless, b"a", "lossy-test", False) is None
    better = provenance.create_stream_source(session, lossy, b"a", "lossless-test", True)
    session.commit()
    with conn.cursor() as cur:
        assert [r[2] for r in _rows(cur, lossy)][-1] is not None and better is not None


def test_an_imported_twin_holds_nothing_against_a_stream(conn, session, prints):
    with conn.cursor() as cur:
        track = _track(cur)
        _source(cur, track, NEAR, imported=True)
    prints.update(stream=FP, stream_dur=240)
    assert provenance.create_stream_source(session, track, b"a", "lossy-test", False) is not None


# -- A registration that changes the declaration ------------------------------

def test_a_registration_that_changes_a_rows_duration_sheds_its_records_seals(
        conn, session, prints):
    """A stream the owned file now holds under one address: the row declares
    the file's duration, and this node's records linked to it are sealed
    again under it (left sealed, they would no longer verify)."""
    with conn.cursor() as cur:
        track = _track(cur)
        stream = _source(cur, track, FP, provider="lossless-test", duration=241)
        eid = _embedding(cur, track, stream)
        cur.execute("UPDATE embedding_segments SET author_pubkey = %s, signature = %s "
                    "WHERE embedding_id = %s", (pub(NODE), "cd" * 64, eid))
        mf = _file(cur, track, "/music/a.flac", duration=240)
    prints["/music/a.flac"] = FP
    assert _register(session, conn, track, mf) == stream
    with conn.cursor() as cur:
        cur.execute("SELECT duration_seconds, media_file_id, provider_id FROM analysis_sources "
                    "WHERE id = %s", (stream,))
        assert cur.fetchone() == (240, mf, None)
        cur.execute("SELECT signature FROM embedding_segments WHERE embedding_id = %s", (eid,))
        assert cur.fetchall() == [(None,)]


def test_a_registration_that_keeps_the_declaration_keeps_the_seals(conn, session, prints):
    with conn.cursor() as cur:
        track = _track(cur)
        stream = _source(cur, track, FP, provider="lossless-test", duration=240)
        eid = _embedding(cur, track, stream)
        cur.execute("UPDATE embedding_segments SET author_pubkey = %s, signature = %s "
                    "WHERE embedding_id = %s", (pub(NODE), "cd" * 64, eid))
        mf = _file(cur, track, "/music/a.flac", duration=240)
    prints["/music/a.flac"] = FP
    assert _register(session, conn, track, mf) == stream
    with conn.cursor() as cur:
        cur.execute("SELECT signature FROM embedding_segments WHERE embedding_id = %s", (eid,))
        assert cur.fetchall() == [("cd" * 64,)]


# -- The relink of a returned file, by the rule -------------------------------

def test_a_returned_file_finds_its_analysis_by_the_rule(conn, prints, monkeypatch):
    """A library moved to a node whose fpcalc prints it a bit otherwise: the
    unlinked row is linked to the file whose print is the same material; a
    file of another material, or of another cut, stays pending."""
    monkeypatch.setattr(provenance, "fingerprinter",
                        lambda: provenance.Fingerprinter("ok", "/usr/bin/fpcalc"))
    monkeypatch.setattr(provenance, "_unmatched", {})
    with conn.cursor() as cur:
        moved, other, edit = _track(cur, "moved"), _track(cur, "other"), _track(cur, "edit")
        for t in (moved, other, edit):
            _source(cur, t, FP)
        files = {moved: _file(cur, moved, "/music/moved.flac"),
                 other: _file(cur, other, "/music/other.flac"),
                 edit: _file(cur, edit, "/music/edit.flac", duration=270)}
    prints.update({"/music/moved.flac": NEAR, "/music/other.flac": OTHER, "/music/edit.flac": NEAR})
    assert provenance.relink_returned_files([moved, other, edit]) == 1
    with conn.cursor() as cur:
        linked = {t: _rows(cur, t)[0][3] for t in (moved, other, edit)}
    assert linked == {moved: files[moved], other: None, edit: None}
