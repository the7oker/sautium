"""Who made a record, and joins by similarity (ANALYSIS-3 body 2), on a real
PostgreSQL built from the migrations — the module's scratch database
(conftest.scratch_dsn), skipped without a cluster.

analysis_sources holds a row per declaration (the print and the duration a
seal binds), copy and origin, and nothing rewrites a row: this node's
registrations are first-hand, the sync import's rows imported, nothing turns
one into the other — so sign_audio, which signs by the flag, never signs
another author's record, whatever shed its seal — and a better copy is a row
of its own whose pass replaces the records at its save. The gate keys a
record by the declaration as its seal binds it, and drops a wrongly typed
item, never its batch. A track's rows of one material whose prints differ are grouped
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
from desktop.p2p import record_sig as rs  # noqa: E402
from tests.p2p.fingerprints import encode, flip, noise  # noqa: E402
from tests.p2p.sealed_records import pub, seal_track  # noqa: E402
from tests.p2p.test_witness import ISSUER  # noqa: E402
from uuid_utils import embedding_model_uuid  # noqa: E402

# the id the backend gives the model of that name (uuid_utils), as the
# importer checks it
MODEL = str(embedding_model_uuid("clap"))
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


def test_a_fresh_database_takes_a_row_per_origin_and_declaration(conn):
    """001, then every delta: 018's tail adds the two-column constraint back
    after 001, and 055 drops it by name — beside it, every twin insert would
    fail."""
    with conn.cursor() as cur:
        assert _uniques(cur) == {"uq_asrc_track_declaration_copy"}
        track = _track(cur)
        first = _source(cur, track, FP)
        twin = _source(cur, track, FP, imported=True)
        edit = _source(cur, track, FP, duration=205)
        undeclared = _source(cur, track, FP, duration=None)
        hires = _source(cur, track, FP, bit_depth=24)
        stream = _source(cur, track, FP, provider="lossless-test")
        assert len({first, twin, edit, undeclared, hires, stream}) == 6
        with pytest.raises(psycopg2.errors.UniqueViolation):      # an imported row: one copy
            _source(cur, track, FP, imported=True, lossless=False)
        with pytest.raises(psycopg2.errors.UniqueViolation):
            _source(cur, track, FP, imported=True)
        with pytest.raises(psycopg2.errors.UniqueViolation):      # NULLS NOT DISTINCT
            _source(cur, track, FP, duration=None)


def test_the_delta_moves_an_earlier_node_to_the_per_origin_key_and_runs_again_as_a_no_op(conn):
    sql = MIGRATION.read_text()
    with conn.cursor() as cur:
        track = _track(cur)
        kept = _source(cur, track, FP, imported=True)
        cur.execute("ALTER TABLE analysis_sources DROP CONSTRAINT uq_asrc_track_declaration_copy")
        cur.execute("ALTER TABLE analysis_sources DROP COLUMN material_of")
        cur.execute("ALTER TABLE analysis_sources DROP COLUMN copy_rank")
        cur.execute("ALTER TABLE analysis_sources ADD CONSTRAINT uq_asrc_track_chromaprint "
                    "UNIQUE (track_id, chromaprint_key)")
        cur.execute(sql)
        cur.execute(sql)
        assert _uniques(cur) == {"uq_asrc_track_declaration_copy"}
        cur.execute("SELECT copy_rank FROM analysis_sources WHERE id = %s", (kept,))
        assert cur.fetchone() == (-1000,)
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
    again. A better rip (CD over 24-bit) is analysed; with the print equal
    too, see test_a_better_copy_of_one_declaration_re_derives_its_records."""
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


# -- A declaration never changes under its records -----------------------------

def _features(cur, track, source):
    cur.execute("INSERT INTO audio_features (track_id, bpm, analysis_source_id, analysis_version) "
                "VALUES (%s, 99, %s, 2)", (track, source))


def _grid(cur, track, source, duration) -> tuple:
    vec = "[" + ",".join(["0.03"] * 512) + "]"
    cur.execute("""INSERT INTO embeddings (track_id, model_id, vector, analysis_source_id,
                                           analysis_version)
                   VALUES (%s, %s, %s::vector, %s, 2) RETURNING id""", (track, MODEL, vec, source))
    eid = cur.fetchone()[0]
    idxs = embeddings.segment_grid_indices(duration)
    for i in idxs:
        cur.execute("INSERT INTO embedding_segments (embedding_id, segment_index, vector) "
                    "VALUES (%s, %s, %s::vector)", (eid, i, vec))
    return eid, idxs


def _sign(scratch_dsn) -> int:
    stage = psycopg2.connect(scratch_dsn, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        return sign_audio.sign(stage)
    finally:
        stage.close()


def _seal_own(cur, eid, track, fp, duration):
    """This node's seal on each segment of `eid`, as sign_audio makes it."""
    cur.execute("SELECT id, segment_index, vector::text FROM embedding_segments "
                "WHERE embedding_id = %s", (eid,))
    for sid, i, vec in cur.fetchall():
        sig = rs.sign(rs.segment_payload(pub(NODE), track, fp, duration, MODEL, i,
                                         rs.vector_hash(sign_audio._vector_bytes(vec)),
                                         grid_version=rs.GRID_VERSION), NODE)
        cur.execute("UPDATE embedding_segments SET author_pubkey = %s, signature = %s "
                    "WHERE id = %s", (pub(NODE), sig, sid))


def _verifies(cur, eid, track, fp) -> bool:
    """Every segment's seal verifies against its row's declaration, and the
    grid fits the duration a receiver reads off it."""
    cur.execute("""SELECT es.segment_index, es.vector::text, es.signature, es.author_pubkey,
                          s.duration_seconds, s.grid_version
                     FROM embedding_segments es JOIN embeddings e ON e.id = es.embedding_id
                     JOIN analysis_sources s ON s.id = e.analysis_source_id
                    WHERE e.id = %s ORDER BY es.segment_index""", (eid,))
    segs = cur.fetchall()
    return bool(segs) and all(
        sig and rs.verify(rs.segment_payload(author, track, fp, duration, MODEL, i,
                                             rs.vector_hash(sign_audio._vector_bytes(vec)),
                                             grid_version=grid), sig, author)
        for i, vec, sig, author, duration, grid in segs) and sync_client._on_the_grid(
        [{"i": r[0]} for r in segs], segs[0][4])


def test_an_edit_with_the_albums_opening_is_another_material_and_analysed(
        conn, session, prints, scratch_dsn, monkeypatch):
    """The album version (245 s) analysed and sealed; the single edit (205 s)
    elected — the same opening, the same print. It is a row of its own,
    another material (the duration is the second key): the pass computes it,
    and the album's grid keeps its row and its seals. Before, the album's
    row took the edit's duration, the pass skipped, and the album's 24
    windows were sealed as 205 s — off the grid at every receiver."""
    monkeypatch.setattr(p2p_identity, "_signing_key_cache", NODE)
    with conn.cursor() as cur:
        track = _track(cur)
        album = _file(cur, track, "/music/album.flac", duration=245)
        single = _file(cur, track, "/music/single.flac", duration=205, source=False)
    prints.update({"/music/album.flac": FP, "/music/single.flac": FP})
    first = _register(session, conn, track, album)
    with conn.cursor() as cur:
        eid, idxs = _grid(cur, track, first, 245)
    assert _sign(scratch_dsn) == len(idxs)
    with conn.cursor() as cur:
        _elect(cur, track, single)
    second = _register(session, conn, track, single)
    assert second != first
    with conn.cursor() as cur:
        assert [(r[0], r[2]) for r in _rows(cur, track)] == [(first, None), (second, None)]
        cur.execute("SELECT duration_seconds FROM analysis_sources WHERE id = %s", (first,))
        assert cur.fetchone() == (245,)
        assert _verifies(cur, eid, track, FP)
    assert not provenance.held_in_group(session, second, "embeddings", model_id=MODEL)
    assert _pending(session, track, embeddings.PENDING_SQL)
    assert _sign(scratch_dsn) == 0                                   # nothing to seal again


def test_another_duration_within_the_bound_joins_and_keeps_its_records_sealed(
        conn, session, prints):
    """A stream declared 241 s, the owned file 240 s: one material, another
    declaration — a row of its own in the stream's group. The stream's
    records stay under the row they were sealed with (they verify), and the
    file, a better copy, is analysed."""
    with conn.cursor() as cur:
        track = _track(cur)
        stream = _source(cur, track, FP, provider="lossless-test", duration=241)
        eid = _embedding(cur, track, stream)
        _seal_own(cur, eid, track, FP, 241)
        mf = _file(cur, track, "/music/a.flac", duration=240)
    prints["/music/a.flac"] = FP
    sid = _register(session, conn, track, mf)
    with conn.cursor() as cur:
        assert [(r[0], r[2], r[3]) for r in _rows(cur, track)] == [
            (stream, None, None), (sid, stream, mf)]
        cur.execute("SELECT duration_seconds FROM analysis_sources WHERE id = %s", (stream,))
        assert cur.fetchone() == (241,)
        assert _verifies(cur, eid, track, FP)
    assert not provenance.held_in_group(session, sid, "embeddings", model_id=MODEL)


def test_a_duration_declared_where_none_was_is_another_row_and_the_old_seal_still_verifies(
        conn, session, prints):
    with conn.cursor() as cur:
        track = _track(cur)
        stream = _source(cur, track, FP, provider="lossless-test", duration=None)
        eid = _embedding(cur, track, stream)
        cur.execute("SELECT vector::text FROM embedding_segments WHERE embedding_id = %s", (eid,))
        vh = rs.vector_hash(sign_audio._vector_bytes(cur.fetchone()[0]))
        sig = rs.sign(rs.segment_payload(pub(NODE), track, FP, None, MODEL, 0, vh,
                                         grid_version=1), NODE)
        cur.execute("UPDATE embedding_segments SET author_pubkey = %s, signature = %s "
                    "WHERE embedding_id = %s", (pub(NODE), sig, eid))
        mf = _file(cur, track, "/music/a.flac", duration=240)
    prints["/music/a.flac"] = FP
    sid = _register(session, conn, track, mf)
    assert sid != stream
    with conn.cursor() as cur:
        cur.execute("SELECT duration_seconds FROM analysis_sources WHERE id = %s", (stream,))
        assert cur.fetchone() == (None,)
        cur.execute("SELECT signature FROM embedding_segments WHERE embedding_id = %s", (eid,))
        assert cur.fetchone() == (sig,)
    assert rs.verify(rs.segment_payload(pub(NODE), track, FP, None, MODEL, 0, vh, grid_version=1),
                     sig, pub(NODE))


@pytest.mark.parametrize("before,after", [
    # a lossy stream, then the owned lossless file of the equal print
    (dict(provider="lossy-test", lossless=False, bit_depth=None), dict(bit_depth=16)),
    # a 24-bit file, then the CD rip of the equal print (hi-res→CD prints alike, FP-SIM)
    (dict(bit_depth=24), dict(bit_depth=16)),
])
def test_a_better_copy_of_one_declaration_is_a_row_of_its_own_and_its_passes_replace(
        conn, session, prints, before, after):
    """No registration deletes a record or rewrites a row's copy: the better
    copy is a row of its own in the group, the lesser copy's records stay
    linked to the row they were made from — never a gap the sync could fill
    — and both passes are pending until they replace them at their saves."""
    with conn.cursor() as cur:
        track = _track(cur)
        if "provider" in before:
            first = _source(cur, track, FP, provider=before["provider"],
                            lossless=before["lossless"], bit_depth=before["bit_depth"])
        else:
            old = _file(cur, track, "/music/old.flac", bit_depth=before["bit_depth"], source=False)
            first = _source(cur, track, FP, media_file=old, bit_depth=before["bit_depth"])
        _embedding(cur, track, first)
        _features(cur, track, first)
        cur.execute("SELECT provider_id, media_file_id, is_lossless, bit_depth "
                    "FROM analysis_sources WHERE id = %s", (first,))
        claims = cur.fetchone()
        mf = _file(cur, track, "/music/a.flac", bit_depth=after["bit_depth"])
    prints["/music/a.flac"] = FP
    second = _register(session, conn, track, mf)
    with conn.cursor() as cur:
        assert second != first
        assert [(r[0], r[2]) for r in _rows(cur, track)] == [(first, None), (second, first)]
        cur.execute("SELECT provider_id, media_file_id, is_lossless, bit_depth "
                    "FROM analysis_sources WHERE id = %s", (first,))
        assert cur.fetchone() == claims
        assert _linked(cur, track) == {"embeddings": False, "features": False}
    assert not provenance.held_in_group(session, second, "embeddings", model_id=MODEL)
    assert _pending(session, track, embeddings.PENDING_SQL)
    assert _pending(session, track, audio_analysis.PENDING_SQL)
    assert not provenance.outranked(session, first, second)     # the passes' saves replace
    assert provenance.outranked(session, second, first)


def test_a_lesser_copy_of_one_declaration_is_a_row_of_its_own_and_skips(conn, session, prints):
    """The CD rip analysed, a 24-bit copy of the equal print elected: a row
    of its own in the CD's group, the CD's records stay where they are, the
    pass has nothing to compute, nothing is asked again."""
    with conn.cursor() as cur:
        track = _track(cur)
        cd = _file(cur, track, "/music/cd.flac", bit_depth=16, source=False)
        first = _source(cur, track, FP, media_file=cd)
        _embedding(cur, track, first)
        _features(cur, track, first)
        hires = _file(cur, track, "/music/a.flac", bit_depth=24)
    prints["/music/a.flac"] = FP
    second = _register(session, conn, track, hires)
    with conn.cursor() as cur:
        assert [(r[0], r[2], r[3]) for r in _rows(cur, track)] == [
            (first, None, cd), (second, first, hires)]
        assert _linked(cur, track) == {"embeddings": False, "features": False}
    assert provenance.held_in_group(session, second, "embeddings", model_id=MODEL)
    assert not _pending(session, track, embeddings.PENDING_SQL)
    assert not _pending(session, track, audio_analysis.PENDING_SQL)


def test_an_equal_copy_registered_again_takes_only_the_rows_file(conn, session, prints):
    with conn.cursor() as cur:
        track = _track(cur)
        a = _file(cur, track, "/music/a.flac", source=False)
        first = _source(cur, track, FP, media_file=a)
        _embedding(cur, track, first)
        b = _file(cur, track, "/music/b.flac")
    prints["/music/b.flac"] = FP
    assert _register(session, conn, track, b) == first
    with conn.cursor() as cur:
        assert [(r[0], r[3]) for r in _rows(cur, track)] == [(first, b)]
        assert _linked(cur, track) == {"embeddings": False}


def test_a_second_authors_record_of_one_print_and_another_duration_has_a_row_of_its_own(
        conn, scratch_dsn):
    """Author X's segments at 240 s, author Y's features of the same print at
    241 s: Y's record lives under its own declaration and is served with it,
    so it verifies at the next node too."""
    second = Ed25519PrivateKey.generate()
    with conn.cursor() as cur:
        track = _track(cur)
        segments, _f = _payloads(conn, track, FP, duration=240)
        seal_track(cur, second, track, MODEL, recording=str(uuid.uuid4()), chromaprint=FP,
                   duration=241)
        features = sync_queries.pull_audio_features(conn, [track])
        for table in ("track_mbids", "embeddings", "audio_features"):
            cur.execute(f"DELETE FROM {table} WHERE track_id = %s", (track,))
        cur.execute("DELETE FROM analysis_sources WHERE track_id = %s", (track,))
    assert _import(scratch_dsn, "segments", segments) == 1
    assert _import(scratch_dsn, "audio_features", features) == 1
    with conn.cursor() as cur:
        cur.execute("SELECT duration_seconds, material_of IS NOT NULL FROM analysis_sources "
                    "WHERE track_id = %s ORDER BY id", (track,))
        assert cur.fetchall() == [(240, False), (241, True)]       # one material, two rows
    served = sync_queries.pull_audio_features(conn, [track])["items"][0]
    assert (served["author_pubkey"], served["provenance"]["duration_seconds"]) == (pub(second), 241)
    assert rs.verify(rs.features_payload(served["author_pubkey"], track, FP, 241,
                                         served["analysis_version"],
                                         rs.blake2b_hex(rs.canonical_features_blob(served))),
                     served["signature"], served["author_pubkey"])


def test_a_merge_moves_a_grid_as_a_row_of_its_own_declaration(conn, session, scratch_dsn,
                                                             monkeypatch):
    """The album's grid (245 s) merging onto a track that registered the edit
    (205 s) of the equal print: no duplicate — its own row, another material,
    sealed again under its own declaration."""
    monkeypatch.setattr(p2p_identity, "_signing_key_cache", NODE)
    with conn.cursor() as cur:
        old, new = _track(cur, "old"), _track(cur, "new")
        own_old = _source(cur, old, FP, duration=245)
        eid, idxs = _grid(cur, old, own_old, 245)
        own_new = _source(cur, new, FP, duration=205)
    assert _sign(scratch_dsn) == len(idxs)
    _merge(session, old, new)
    assert _sign(scratch_dsn) == len(idxs)                           # shed by the uuid, sealed again
    with conn.cursor() as cur:
        cur.execute("SELECT analysis_source_id FROM embeddings WHERE id = %s", (eid,))
        assert cur.fetchone() == (own_old,)
        assert [(r[0], r[2]) for r in _rows(cur, new)] == [(own_old, None), (own_new, None)]
        assert _verifies(cur, eid, new, FP)


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


def _relinking(monkeypatch):
    monkeypatch.setattr(provenance, "fingerprinter",
                        lambda: provenance.Fingerprinter("ok", "/usr/bin/fpcalc"))
    monkeypatch.setattr(provenance, "_unmatched", {})


@pytest.mark.parametrize("away", [False, True])
def test_a_better_file_of_the_material_is_not_relinked_to_a_lesser_copys_analysis(
        conn, session, prints, monkeypatch, away):
    """An MP3 analysed, then the FLAC: the MP3 removed, or its copy away
    beside the FLAC. The FLAC prints within the rule of the MP3, but the
    analysis is the lesser copy's — the FLAC stays pending, and its pass
    registers it in the MP3 row's group and derives the better version."""
    _relinking(monkeypatch)
    with conn.cursor() as cur:
        track = _track(cur)
        mp3 = _file(cur, track, "/music/mp3/a.mp3", bit_depth=None, lossless=False)
    prints["/music/a.mp3"] = FP
    first = _register(session, conn, track, mp3)
    with conn.cursor() as cur:
        _embedding(cur, track, first)
        _features(cur, track, first)
        if away:
            cur.execute("UPDATE album_variants SET absent_since = now() WHERE id = "
                        "(SELECT album_variant_id FROM media_files WHERE id = %s)", (mp3,))
            flac = _file(cur, track, "/music/flac/a.flac", source=False)
            _elect(cur, track, flac)
        else:
            cur.execute("DELETE FROM media_files WHERE id = %s", (mp3,))
            flac = _file(cur, track, "/music/flac/a.flac")
    prints["/music/a.flac"] = NEAR
    assert provenance.relink_returned_files([track]) == 0
    assert _pending(session, track, embeddings.PENDING_SQL)
    assert _pending(session, track, audio_analysis.PENDING_SQL)
    second = _register(session, conn, track, flac)
    with conn.cursor() as cur:
        assert [r[2] for r in _rows(cur, track)] == [None, first]
    assert not provenance.held_in_group(session, second, "embeddings", model_id=MODEL)


def test_a_returned_file_of_a_copy_no_better_than_the_analysis_is_relinked(
        conn, prints, monkeypatch):
    _relinking(monkeypatch)
    with conn.cursor() as cur:
        track = _track(cur)
        row = _source(cur, track, FP, bit_depth=16)
        mf = _file(cur, track, "/music/a.mp3", bit_depth=None, lossless=False)
    prints["/music/a.mp3"] = NEAR
    assert provenance.relink_returned_files([track]) == 1
    with conn.cursor() as cur:
        assert _rows(cur, track)[0][:4] == (row, False, None, mf)


def test_the_elected_file_replaces_another_materials_analysis_whatever_its_quality(
        conn, session, prints):
    """Before, a CD rip's analysis of another material refused the elected
    MP3's on every run, at a GPU pass each time; between copies of one
    material the quality still decides."""
    import types

    import numpy as np
    generator = embeddings.AudioEmbeddingGenerator.__new__(embeddings.AudioEmbeddingGenerator)
    model = types.SimpleNamespace(id=MODEL)
    vec = np.full((1, 512), 0.05, dtype=np.float32)
    with conn.cursor() as cur:
        track = _track(cur)
        cd = _source(cur, track, OTHER, bit_depth=16)
        _embedding(cur, track, cd)
        same = _source(cur, track, NEAR, bit_depth=None, lossless=False, material_of=None)
        other = _source(cur, track, FP, bit_depth=None, lossless=False, duration=300)
        cur.execute("UPDATE analysis_sources SET material_of = %s WHERE id = %s", (cd, same))
    def persist(sid):
        ok = generator._persist_analysis(session, track, model, [0], vec, vec[0], sid,
                                         provider_id=None, bit_depth=None, is_lossless=False)
        session.commit()
        return ok
    assert persist(same) is False                         # one material: the CD's stays
    assert persist(other) is True                         # another material: the elected file's
    with conn.cursor() as cur:
        cur.execute("SELECT analysis_source_id FROM embeddings WHERE track_id = %s", (track,))
        assert cur.fetchone() == (other,)


def test_an_owned_tracks_unlinked_grid_is_held_and_a_phantoms_is_a_gap(conn):
    with conn.cursor() as cur:
        owned, phantom = _track(cur, "owned"), _track(cur, "phantom")
        _file(cur, owned, "/music/a.flac")
        for t in (owned, phantom):
            _embedding(cur, t, None)                      # a legacy grid, its source gone
        cur.execute(f"""SELECT u::text FROM unnest(%s::uuid[]) u
                         WHERE {sync_queries._LACKS['segments'].format(t='u')}""",
                    ([owned, phantom],))
        assert cur.fetchall() == [(phantom,)]


def test_the_holdings_filter_and_the_announce_tail_name_only_held_analysis(
        conn, scratch_dsn, session):
    with conn.cursor() as cur:
        shed, sealed = _track(cur, "shed"), _track(cur, "sealed")
        for t in (shed, sealed):
            artist = str(uuid.uuid4())
            cur.execute("INSERT INTO artists (id, name) VALUES (%s, %s)", (artist, t))
            cur.execute("INSERT INTO track_artists (track_id, artist_id, role) "
                        "VALUES (%s, %s, 'primary')", (t, artist))
        payloads = {t: _payloads(conn, t, FP)[0] for t in (shed, sealed)}
    for t in (shed, sealed):
        assert _import(scratch_dsn, "segments", payloads[t]) == 1
    identity._shed_track_seals(session, shed)
    session.commit()
    held = {r["track_id"] for r in sync_queries.db_query(conn, sync_queries._HOLDINGS_TRACKS_SQL)}
    assert held == {sealed}
    tail = sync_queries.db_query(conn, sync_queries.ANNOUNCE_TAIL_SQL, [10])
    cur = conn.cursor()
    cur.execute("SELECT artist_id::text FROM track_artists WHERE track_id = %s", (sealed,))
    assert [r["artist_uuid"] for r in tail] == [cur.fetchone()[0]]


def test_a_print_longer_than_fpcalcs_window_can_make_is_dropped_at_the_gate(conn, scratch_dsn):
    """A certified peer's crafted print: refused before it is read — a decode
    of it, and every later registration's, would cost the peer surface's
    thread seconds."""
    import time
    with conn.cursor() as cur:
        track = _track(cur)
        huge = encode(noise(material.MAX_ITEMS * 40, 7))
        segments, features = _payloads(conn, track, huge)
    t = time.perf_counter()
    assert _import(scratch_dsn, "segments", segments) == 0
    assert _import(scratch_dsn, "audio_features", features) == 0
    assert time.perf_counter() - t < 2
    with conn.cursor() as cur:
        assert _rows(cur, track) == []


def test_a_root_held_for_a_new_row_follows_a_merge_that_moved_or_dropped_it(conn, session):
    with conn.cursor() as cur:
        track = _track(cur)
        root = _source(cur, track, FP)
        member = _source(cur, track, NEAR)
        other = _source(cur, track, OTHER)
        cur.execute("UPDATE analysis_sources SET material_of = %s WHERE id = %s", (other, root))
    assert provenance._held_root(session, member) == member          # still a root
    assert provenance._held_root(session, root) == other              # joined another group
    session.rollback()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM analysis_sources WHERE id = %s", (member,))
    assert provenance._held_root(session, member) is None             # gone: a root of its own
    session.rollback()



# -- The gate: a record keyed by the declaration its seal binds ------------------

def _sealed_features(track, fp, duration, *, signer=AUTHOR, **values) -> dict:
    """An author's sealed features item for `track`, in a pull answer of its
    own, with whatever values the author signed."""
    from tests.p2p.sealed_records import STAMPED_AT
    from tests.p2p.test_witness import AUTHORITY
    item = {c: None for c in rs.FEATURE_ORDER}
    item.update(bpm=120.5, energy=0.25)
    item.update(values)
    author = pub(signer)
    sig = rs.sign(rs.features_payload(author, track, fp, duration, 2,
                                      rs.blake2b_hex(rs.canonical_features_blob(item))), signer)
    root, [proof] = rs.merkle_tree([rs.record_leaf(sig)])
    ip_hash = str(uuid.uuid4())
    item.update(track_uuid=track, analysis_version=2, author_pubkey=author, signature=sig,
                batch_root=root, merkle_proof=proof,
                provenance={"chromaprint": fp, "duration_seconds": duration,
                            "grid_version": rs.GRID_VERSION, "is_lossless": True})
    return {"category": "audio_features", "items": [item], "batches": {root: {
        "author_pubkey": author, "worker_date": STAMPED_AT, "ip_hash": ip_hash,
        "worker_sig": AUTHORITY.sign(rs.timestamp_payload(root, STAMPED_AT, ip_hash)).hex(),
        "authority": ISSUER, "timestamp_version": 2}}}


def _merged(*payloads) -> dict:
    out = {"category": payloads[0]["category"], "items": [], "batches": {}}
    for p in payloads:
        out["items"] += p["items"]
        out["batches"].update(p["batches"])
    return out


@pytest.mark.parametrize("field,value", [
    ("duration_seconds", "240"),          # a relay's text: the seal still reads it as 240
    ("duration_seconds", 240.0),
    ("duration_seconds", True),
    ("grid_version", 2),                  # unsigned in features, bound by every segment seal
    ("grid_version", "1"),
    ("chromaprint", 123),
    ("chromaprint", None),
])
def test_a_features_item_whose_declaration_is_mistyped_is_dropped_and_its_batch_kept(
        conn, scratch_dsn, field, value):
    with conn.cursor() as cur:
        bad, good = _track(cur, "bad"), _track(cur, "good")
    tampered = _sealed_features(bad, FP, 240)
    tampered["items"][0]["provenance"][field] = value
    assert _import(scratch_dsn, "audio_features",
                   _merged(tampered, _sealed_features(good, FP, 240))) == 1
    with conn.cursor() as cur:
        cur.execute("SELECT a.track_id::text, s.duration_seconds, s.grid_version "
                    "FROM audio_features a JOIN analysis_sources s ON s.id = a.analysis_source_id")
        assert cur.fetchall() == [(good, 240, rs.GRID_VERSION)]


def test_a_grid_version_a_features_item_omits_is_this_builds(conn, scratch_dsn):
    with conn.cursor() as cur:
        track = _track(cur)
    payload = _sealed_features(track, FP, 240)
    payload["items"][0]["provenance"]["grid_version"] = None
    assert _import(scratch_dsn, "audio_features", payload) == 1
    with conn.cursor() as cur:
        cur.execute("SELECT grid_version FROM analysis_sources WHERE track_id = %s", (track,))
        assert cur.fetchone() == (rs.GRID_VERSION,)


def test_a_relay_cannot_move_an_honest_grid_onto_another_grid_version(conn, scratch_dsn):
    """Probe P2: a features item set to grid 2 for a track this node lacks
    would have made the imported row grid 2, and the author's honest
    segments of the same declaration were then served at grid 2 and failed
    at every receiver."""
    with conn.cursor() as cur:
        track = _track(cur)
        segments, features = _payloads(conn, track, FP, duration=240)
    features["items"][0]["provenance"]["grid_version"] = 2
    assert _import(scratch_dsn, "audio_features", features) == 0
    assert _import(scratch_dsn, "segments", segments) == 1
    served = sync_queries.pull_segments(conn, [track])
    assert served["items"][0]["provenance"]["grid_version"] == rs.GRID_VERSION
    with conn.cursor() as cur:
        for table in ("embeddings", "audio_features", "analysis_sources"):
            cur.execute(f"DELETE FROM {table} WHERE track_id = %s", (track,))
    assert _import(scratch_dsn, "segments", served) == 1          # the next node takes it


def test_a_value_the_database_refuses_drops_its_item_not_its_batch(conn, scratch_dsn):
    """A certified author's crafted features — a tempo that is text, a key
    no scale has — seal and verify, then fail the row's types: that item
    goes, the rest of the batch lands."""
    with conn.cursor() as cur:
        a, b, good = _track(cur, "a"), _track(cur, "b"), _track(cur, "good")
    payload = _merged(_sealed_features(a, FP, 240, bpm="fast"),
                      _sealed_features(b, FP, 240, key="H"),
                      _sealed_features(good, FP, 240))
    assert _import(scratch_dsn, "audio_features", payload) == 1
    with conn.cursor() as cur:
        cur.execute("SELECT track_id::text FROM audio_features")
        assert cur.fetchall() == [(good,)]


def test_a_bundle_whose_model_is_no_uuid_drops_alone(conn, scratch_dsn):
    with conn.cursor() as cur:
        bad, good = _track(cur, "bad"), _track(cur, "good")
        bundles = {t: _payloads(conn, t, FP)[0] for t in (bad, good)}
    item = bundles[bad]["items"][0]
    item["model_uuid"] = "not a uuid"
    assert _import(scratch_dsn, "segments", _merged(bundles[bad], bundles[good])) == 1
    with conn.cursor() as cur:
        cur.execute("SELECT track_id::text FROM embeddings")
        assert cur.fetchall() == [(good,)]


def test_the_importers_write_to_an_existing_row_takes_no_key_lock(conn, scratch_dsn):
    """Probe P5: the no-op on a conflict writes no key column, so an import
    never waits for a registration holding the row as a root."""
    with conn.cursor() as cur:
        track = _track(cur)
        imported = _source(cur, track, FP, imported=True)
    holder = psycopg2.connect(scratch_dsn)
    other = psycopg2.connect(scratch_dsn)
    try:
        with holder.cursor() as hc:
            hc.execute("SELECT material_of FROM analysis_sources WHERE id = %s FOR KEY SHARE",
                       (imported,))
        with other.cursor() as oc:
            oc.execute("SET lock_timeout = '300ms'")
            mapped = sync_client.SyncClient._upsert_analysis_sources(oc, [{
                "track_uuid": track, "provenance": {"chromaprint": FP, "duration_seconds": 240,
                                                    "grid_version": 1, "is_lossless": True}}])
        assert mapped == {(track, FP, 240): imported}
        other.rollback()
    finally:
        holder.rollback()
        holder.close()
        other.close()


# -- Groups: the bound never chains, a merge keeps copies apart ----------------

def test_a_registration_never_joins_a_root_beyond_the_duration_bound_through_a_member(
        conn, session, prints):
    """Probe P6: 244 s joined a 240 s root through a 242 s member and skipped
    on the 240 s analysis."""
    with conn.cursor() as cur:
        track = _track(cur)
        root = _source(cur, track, FP, duration=240)
        _embedding(cur, track, root)
        _source(cur, track, FP, imported=True, duration=242, material_of=root)
        mf = _file(cur, track, "/music/a.flac", duration=244)
    prints["/music/a.flac"] = FP
    sid = _register(session, conn, track, mf)
    with conn.cursor() as cur:
        cur.execute("SELECT material_of FROM analysis_sources WHERE id = %s", (sid,))
        assert cur.fetchone() == (None,)
    assert not provenance.held_in_group(session, sid, "embeddings", model_id=MODEL)


def test_a_merged_lossy_streams_features_never_stand_for_the_targets_cd(conn, session):
    """Probe P9: the stream's row of the CD's declaration was a duplicate,
    its features landed under the CD's row, and the features pass was never
    pending for the CD."""
    with conn.cursor() as cur:
        old, new = _track(cur, "old"), _track(cur, "new")
        stream = _source(cur, old, FP, provider="lossy-test", lossless=False, bit_depth=None)
        _features(cur, old, stream)
        cd = _file(cur, new, "/music/a.flac", bit_depth=16)
        own = _source(cur, new, FP, media_file=cd)
        _embedding(cur, new, own)
    _merge(session, old, new)
    with conn.cursor() as cur:
        assert [(r[0], r[2]) for r in _rows(cur, new)] == [(stream, own), (own, None)]
        cur.execute("SELECT analysis_source_id FROM audio_features WHERE track_id = %s", (new,))
        assert cur.fetchone() == (stream,)
    assert _pending(session, new, audio_analysis.PENDING_SQL)


def test_the_join_of_rows_registered_before_the_groups(conn, session):
    """For body 3's cutover: a track's existing rows join by the rule, in id
    order; a track of one row is not read; a re-run joins nothing."""
    with conn.cursor() as cur:
        track, alone = _track(cur), _track(cur)
        cd = _source(cur, track, FP)
        mp3 = _source(cur, track, NEAR, bit_depth=None, lossless=False)
        edit = _source(cur, track, FP, duration=205)
        other = _source(cur, track, OTHER)
        _source(cur, alone, FP)
    assert provenance.join_existing_rows(session) == 1
    assert provenance.join_existing_rows(session, track_ids=[track]) == 0
    session.commit()
    with conn.cursor() as cur:
        assert {r[0]: r[2] for r in _rows(cur, track)} == {cd: None, mp3: cd, edit: None,
                                                           other: None}


def test_a_new_imported_row_follows_its_root_through_a_concurrent_merge(conn, scratch_dsn,
                                                                        monkeypatch):
    """The root the rule chose is re-read under a hold: one a merge dropped
    leaves the new row a root of its own, one it re-rooted hands over its
    root."""
    with conn.cursor() as cur:
        track = _track(cur)
        gone = _source(cur, track, OTHER)
        moved = _source(cur, track, NEAR)
        root = _source(cur, track, encode(noise(950, 9)))
        cur.execute("UPDATE analysis_sources SET material_of = %s WHERE id = %s", (root, moved))
        cur.execute("DELETE FROM analysis_sources WHERE id = %s", (gone,))
    for chosen, expected in ((gone, None), (moved, root)):
        monkeypatch.setattr(sync_client.material, "root_among", lambda *a, c=chosen: c)
        c = psycopg2.connect(scratch_dsn)
        try:
            with c.cursor() as cur:
                cur.execute("INSERT INTO analysis_sources (track_id, chromaprint, imported, "
                            "duration_seconds) VALUES (%s, %s, true, %s) RETURNING id",
                            (track, FP, 200 + chosen))
                sid = cur.fetchone()[0]
                sync_client.SyncClient._join_groups(cur, [(sid, track)])
                cur.execute("SELECT material_of FROM analysis_sources WHERE id = %s", (sid,))
                assert cur.fetchone() == (expected,)
            c.commit()
        finally:
            c.close()


# -- Streams: rows of their own, replaced at the saves -------------------------

def test_a_better_stream_of_one_declaration_deletes_nothing_and_its_save_replaces(
        conn, session, prints):
    """Probe P4: the registration commits alone (streaming/enrichment.py)
    before the analysis runs; it used to delete the lesser stream's features
    in that commit. Now it is a row of its own, the features stay until the
    better stream's save replaces them, and a lesser stream's save never does."""
    from streaming.enrichment import PreviewEnricher
    with conn.cursor() as cur:
        track = _track(cur)
        lossy = _source(cur, track, FP, provider="lossy-test", lossless=False, bit_depth=None)
        _features(cur, track, lossy)
    prints.update(stream=FP, stream_dur=240)
    better = provenance.create_stream_source(session, track, b"a", "lossless-test", True)
    session.commit()
    with conn.cursor() as cur:
        assert better != lossy
        assert _linked(cur, track) == {"features": False}
    PreviewEnricher._save_features(session, track, {"bpm": 97.0}, better)
    session.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT bpm, analysis_source_id FROM audio_features WHERE track_id = %s",
                    (track,))
        assert cur.fetchone() == (97.0, better)
    PreviewEnricher._save_features(session, track, {"bpm": 96.0}, lossy)
    session.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT bpm FROM audio_features WHERE track_id = %s", (track,))
        assert cur.fetchone() == (97.0,)


def test_a_lesser_lossless_stream_of_one_declaration_is_already_held(conn, session, prints,
                                                                    monkeypatch):
    """Probe P7: a 24-bit stream over a 16-bit stream's row got the row back
    and went on to the GPU."""
    with conn.cursor() as cur:
        track = _track(cur)
        cd = _source(cur, track, FP, provider="lossless-test", bit_depth=16)
        _features(cur, track, cd)
    monkeypatch.setattr(provenance, "_probe_audio", lambda path: (96000, 24, 240))
    prints.update(stream=FP)
    assert provenance.create_stream_source(session, track, b"a", "lossless-test", True) is None
    session.rollback()


# -- The registration's race ---------------------------------------------------

def test_a_registration_that_loses_its_insert_to_another_takes_the_others_row(
        conn, session, prints, monkeypatch):
    real = provenance._INSERT_SQL
    from sqlalchemy import text
    raced = text("WITH ins AS (" + str(real).strip() + ") SELECT NULL::int")
    with conn.cursor() as cur:
        track = _track(cur)
        mf = _file(cur, track, "/music/a.flac")
    prints["/music/a.flac"] = FP
    monkeypatch.setattr(provenance, "_INSERT_SQL", raced)      # it inserts, then says nothing
    sid = _register(session, conn, track, mf)
    with conn.cursor() as cur:
        assert [r[0] for r in _rows(cur, track)] == [sid]
    monkeypatch.setattr(provenance, "_INSERT_SQL", text("SELECT NULL::int"))
    with conn.cursor() as cur:
        other = _track(cur)
        mf2 = _file(cur, other, "/music/b.flac")
    prints["/music/b.flac"] = FP
    with pytest.raises(RuntimeError, match="changed under two registrations"):
        _register(session, conn, other, mf2)
    session.rollback()


# -- Held: the holdings version, a file no pass reads --------------------------

def test_the_holdings_version_moves_with_a_shed(conn, scratch_dsn, session):
    with conn.cursor() as cur:
        track = _track(cur)
        segments, _f = _payloads(conn, track, FP)
    assert _import(scratch_dsn, "segments", segments) == 1
    before = sync_queries.holdings_version(conn)
    identity._shed_track_seals(session, track)
    session.commit()
    assert sync_queries.holdings_version(conn) != before


def test_an_unlinked_grid_held_only_at_an_hqplayer_or_away_is_a_gap(conn):
    with conn.cursor() as cur:
        away, here = _track(cur, "away"), _track(cur, "here")
        a = _file(cur, away, "/music/away.flac")
        cur.execute("UPDATE album_variants SET absent_since = now() WHERE id = "
                    "(SELECT album_variant_id FROM media_files WHERE id = %s)", (a,))
        _file(cur, here, "/music/here.flac")
        for t in (away, here):
            _embedding(cur, t, None)
        cur.execute(f"""SELECT u::text FROM unnest(%s::uuid[]) u
                         WHERE {sync_queries._LACKS['segments'].format(t='u')}""",
                    ([away, here],))
        assert cur.fetchall() == [(away,)]


@pytest.mark.parametrize("craft", ["vector", "model_name"])
def test_a_bundle_no_column_can_hold_drops_alone(conn, scratch_dsn, craft):
    """A segment of another dimension than vector(512), a model name longer
    than its column: only a crafted answer carries either, and it drops at
    the gate, not the page in the database."""
    import base64
    with conn.cursor() as cur:
        bad, good = _track(cur, "bad"), _track(cur, "good")
        bundles = {t: _payloads(conn, t, FP)[0] for t in (bad, good)}
    item = bundles[bad]["items"][0]
    if craft == "vector":
        item["segments"][0]["v"] = base64.b64encode(rs.vector_to_bytes([0.5] * 256)).decode()
    else:
        item["model_name"] = "m" * 101
    assert _import(scratch_dsn, "segments", _merged(bundles[bad], bundles[good])) == 1
    with conn.cursor() as cur:
        cur.execute("SELECT track_id::text FROM embeddings")
        assert cur.fetchall() == [(good,)]

