"""What a carrier takes from a push (desktop/sync_client.import_pushed,
desktop/p2p/sync_queries.wanted_for_push / wanted_tracks), on a real
PostgreSQL built from the migrations — skipped without a cluster: only what
its own answer to an offer would have asked for — its tracklist, for a
binding the very recording on it, the gaps, the room the carry limit leaves
for every category, the phantom switch —; a bundle's segments fit the grid
of the track, a window of slack over its declared length; a push fits every
carrier's body cap; a binding from this node's own files outranks a
different imported one, and two of its tracks on one recording leave the
canon's run whole; a pull answer counts for the tracks asked only."""

import json
import sys
import uuid
from pathlib import Path

import psycopg2
import pytest

REPO = Path(__file__).resolve().parents[2]
for p in (REPO, REPO / "backend"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from desktop import sync_client  # noqa: E402
from desktop.p2p import sync_queries, sync_walk  # noqa: E402

ALBUM = str(uuid.UUID(int=500))
OWNED, PHANTOM, OFFLIST, ANALYSED = (str(uuid.UUID(int=n)) for n in (501, 502, 503, 504))
MODEL = str(uuid.UUID(int=600))


@pytest.fixture
def conn(scratch_dsn):
    c = psycopg2.connect(scratch_dsn)
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute("TRUNCATE tracks, albums, embedding_models, track_mbids CASCADE")
        cur.execute("DELETE FROM user_settings WHERE key = 'discovery.phantom_layer'")
        cur.execute("INSERT INTO albums (id, title) VALUES (%s, 'LP')", (ALBUM,))
        cur.execute("INSERT INTO embedding_models (id, name, dimension) VALUES (%s, 'clap', 512)", (MODEL,))
        for n, tid in enumerate((OWNED, PHANTOM, OFFLIST, ANALYSED), 1):
            cur.execute("INSERT INTO tracks (id, title) VALUES (%s, %s)", (tid, f"t{n}"))
            if tid != OFFLIST:                                    # on the tracklist, canonized
                cur.execute("""INSERT INTO album_tracks (album_id, track_id, position, recording_mbid)
                               VALUES (%s, %s, %s, %s)""", (ALBUM, tid, n, str(uuid.uuid4())))
        cur.execute("INSERT INTO album_variants (album_id, directory_path) VALUES (%s, '/m') RETURNING id",
                    (ALBUM,))
        variant = cur.fetchone()[0]
        cur.execute("INSERT INTO media_files (track_id, album_variant_id, file_path) VALUES (%s, %s, '/m/1.flac')",
                    (OWNED, variant))
        cur.execute("""INSERT INTO embeddings (track_id, model_id, vector) VALUES (%s, %s, %s::vector)
                       RETURNING id""", (ANALYSED, MODEL, "[" + ",".join(["0.01"] * 512) + "]"))
        emb = cur.fetchone()[0]
        cur.execute("""INSERT INTO embedding_segments (embedding_id, segment_index, vector)
                       VALUES (%s, 0, %s::vector)""", (emb, "[" + ",".join(["0.01"] * 512) + "]"))
    yield c
    c.close()


def _carry(conn, n):
    """n tracks of carried analysis already held: imported, no file."""
    with conn.cursor() as cur:
        for _ in range(n):
            tid = str(uuid.uuid4())
            cur.execute("INSERT INTO tracks (id, title) VALUES (%s, 'carried')", (tid,))
            cur.execute("INSERT INTO analysis_sources (track_id, chromaprint, imported) "
                        "VALUES (%s, 'AQAA', true) RETURNING id", (tid,))
            cur.execute("INSERT INTO embeddings (track_id, model_id, vector, analysis_source_id) "
                        "VALUES (%s, %s, %s::vector, %s)",
                        (tid, MODEL, "[" + ",".join(["0.01"] * 512) + "]", cur.fetchone()[0]))


def _recording(conn, tid):
    with conn.cursor() as cur:
        cur.execute("SELECT recording_mbid::text FROM album_tracks WHERE track_id = %s", (tid,))
        row = cur.fetchone()
    return row[0] if row else str(uuid.uuid4())


def _wanted(conn, budget=100, category="segments"):
    items = [{"track_uuid": t, "recording_mbid": _recording(conn, t)}
             for t in (OWNED, PHANTOM, OFFLIST, ANALYSED)]
    items += [{"track_uuid": "x"}, {"track_uuid": f"urn:uuid:{OWNED}"}, {"track_uuid": OWNED.upper()}]
    return {it["track_uuid"] for it in sync_queries.wanted_for_push(conn, category, items, budget)}


def test_a_push_is_taken_only_where_the_offer_would_have_asked(conn):
    assert _wanted(conn) == {OWNED, PHANTOM}                 # off the tracklist / analysed: no
    assert _wanted(conn, category="track_mbids") == {OWNED, PHANTOM, ANALYSED}


def test_the_carry_limit_is_the_room_left_and_an_owned_gap_is_not_carrying(conn):
    _carry(conn, 3)
    assert _wanted(conn, budget=4) == {OWNED, PHANTOM}
    assert _wanted(conn, budget=3) == {OWNED}
    assert _wanted(conn, budget=0) == set()
    assert _wanted(conn, budget=3, category="audio_features") == {OWNED}       # every category
    assert _wanted(conn, budget=3, category="track_mbids") == {OWNED}


def test_a_pushed_binding_names_the_recording_on_this_nodes_tracklist(conn):
    items = [{"track_uuid": OWNED, "recording_mbid": str(uuid.uuid4())},
             {"track_uuid": PHANTOM, "recording_mbid": _recording(conn, PHANTOM)}]
    kept = sync_queries.wanted_for_push(conn, "track_mbids", items, 100)
    assert [it["track_uuid"] for it in kept] == [PHANTOM]


def test_the_phantom_switch_holds_for_the_push_and_the_offer(conn):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO user_settings (key, value) VALUES ('discovery.phantom_layer', 'false')")
        cur.execute("SELECT recording_mbid::text FROM album_tracks WHERE track_id IN (%s, %s)",
                    (OWNED, PHANTOM))
        mbids = [r[0] for r in cur.fetchall()]
    assert _wanted(conn) == {OWNED}
    assert sync_queries.wanted_tracks(conn, mbids, 100)["segments"] == [OWNED]


def test_import_pushed_hands_the_gate_only_the_wanted_items(conn, scratch_dsn, monkeypatch):
    seen = []
    monkeypatch.setattr(sync_client.SyncClient, "_import_items",
                        lambda self, category, payload: seen.append(payload["items"]) or len(payload["items"]))
    items = [{"track_uuid": t} for t in (OWNED, PHANTOM, OFFLIST, ANALYSED)]
    assert sync_client.import_pushed(scratch_dsn, "segments", {"items": items, "batches": {}}, 100) == 2
    assert [it["track_uuid"] for it in seen[0]] == [OWNED, PHANTOM]


def test_a_bundle_off_its_grid_is_dropped():
    seg = lambda i: {"i": i, "v": ""}
    assert sync_client._on_the_grid([seg(0), seg(5)], 60)
    assert sync_client._on_the_grid([seg(i) for i in range(6)], 59)           # decoded past its tag
    assert not sync_client._on_the_grid([seg(0), seg(0)], 600)                 # a duplicate index
    assert not sync_client._on_the_grid([seg(7)], 60)                          # past a 60 s track
    assert not sync_client._on_the_grid([seg(i) for i in range(8)], 60)        # more than its windows
    assert not sync_client._on_the_grid([seg(0)], "60")
    assert not sync_client._on_the_grid([seg(0)], -1)
    assert not sync_client._on_the_grid([seg(True)], 60)


def test_an_honest_grid_fits_its_declared_length():
    """The producer's grid (backend/embeddings.segment_grid_indices: the
    decoded seconds' whole windows) against the duration a seal declares —
    the file's tag, rounded, which can sit across a window boundary."""
    seg = lambda i: {"i": i, "v": ""}
    for tenth in range(5, 12000, 37):
        decoded = tenth / 10
        grid = [seg(i) for i in range(max(1, int(decoded) // 10))]
        for declared in {round(decoded - 0.6), round(decoded), round(decoded + 0.6)} - {0}:
            assert sync_client._on_the_grid(grid, declared), (decoded, declared)


def test_a_push_fits_every_carriers_body_cap():
    root = "ab" * 32
    bundle = {"track_uuid": OWNED, "segments": [{"i": i, "v": "A" * 2700, "batch_root": root}
                                                for i in range(24)]}
    payload = {"category": "segments", "items": [dict(bundle, track_uuid=str(uuid.UUID(int=i)))
                                                 for i in range(40)],
               "batches": {root: {"worker_sig": "s"}, "cd" * 32: {"worker_sig": "unused"}}}
    parts = sync_walk.push_parts(payload)
    assert len(parts) > 1
    assert all(len(json.dumps(p)) < 1024 * 1024 for p in parts)
    assert [it["track_uuid"] for p in parts for it in p["items"]] == \
        [it["track_uuid"] for it in payload["items"]]
    assert all(set(p["batches"]) == {root} for p in parts)


def test_a_binding_from_own_files_outranks_a_different_imported_one(conn):
    from canon import content
    rec, other = str(uuid.uuid4()), str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO track_mbids (recording_mbid, track_id, confidence, imported)
                       VALUES (%s, %s, 'overlap_verified', true), (%s, %s, 'overlap_verified', true)""",
                    (rec, PHANTOM, other, OWNED))
        for r in (rec, other):
            cur.execute("INSERT INTO track_mbids (recording_mbid, track_id, confidence) "
                        "VALUES (%s, %s, 'overlap_verified')" + content._LOCAL_BINDING_WINS, (r, OWNED))
        cur.execute("SELECT recording_mbid::text, track_id::text, imported FROM track_mbids "
                    "WHERE recording_mbid IN (%s, %s)", (rec, other))
        rows = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    assert rows[rec] == (OWNED, False)                         # taken over by the local claim
    assert rows[other] == (OWNED, True)                        # the same claim: left as it came


@pytest.fixture
def backend_db(scratch_dsn, monkeypatch):
    """The backend's pool on the scratch database."""
    from urllib.parse import urlparse
    from config import settings
    import db_pool
    url = urlparse(scratch_dsn)
    for key, value in dict(postgres_host=url.hostname, postgres_port=url.port,
                           postgres_user=url.username, postgres_password=url.password,
                           postgres_db=url.path.lstrip("/")).items():
        monkeypatch.setattr(settings, key, value)
    db_pool.close_pool()
    yield
    db_pool.close_pool()


def test_two_tracks_on_one_recording_leave_the_canons_run_whole(conn, backend_db):
    from canon import content
    artist, rec = str(uuid.UUID(int=700)), str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute("INSERT INTO artists (id, name) VALUES (%s, 'A') ON CONFLICT DO NOTHING", (artist,))
        cur.execute("""INSERT INTO track_mbids (recording_mbid, track_id, confidence, imported)
                       VALUES (%s, %s, 'overlap_verified', true)""", (rec, PHANTOM))
        cur.execute("SELECT id FROM album_variants LIMIT 1")
        variant = cur.fetchone()[0]
        for tid in (OWNED, ANALYSED):                  # "Song" and "Song (Remastered)"
            cur.execute("INSERT INTO track_artists (track_id, artist_id, role) VALUES (%s, %s, 'primary')",
                        (tid, artist))
            cur.execute("""INSERT INTO media_files (track_id, album_variant_id, file_path, recording_mbid)
                           VALUES (%s, %s, %s, %s)
                           ON CONFLICT (file_path, cue_start_seconds) DO UPDATE
                               SET recording_mbid = EXCLUDED.recording_mbid""",
                        (tid, variant, f"/m/{tid}.flac" if tid != OWNED else "/m/1.flac", rec))
    content.bind_owned_recordings(artist)
    with conn.cursor() as cur:
        cur.execute("SELECT track_id::text, imported FROM track_mbids WHERE recording_mbid = %s", (rec,))
        assert cur.fetchall() == [(min(OWNED, ANALYSED), False)]


def test_a_pull_answer_counts_for_the_tracks_asked_only():
    root, other = "ab" * 32, "cd" * 32
    answer = {"category": "segments",
              "items": [{"track_uuid": OWNED, "segments": [{"i": 0, "batch_root": root}]},
                        {"track_uuid": PHANTOM, "segments": [{"i": 0, "batch_root": other}]}],
              "batches": {root: {"worker_sig": "r"}, other: {"worker_sig": "o"}}}
    cut = sync_client.answer_to(answer, {OWNED})
    assert [it["track_uuid"] for it in cut["items"]] == [OWNED] and set(cut["batches"]) == {root}
    rows = [[OWNED, 1, 3], [PHANTOM, 1, 3], OWNED, PHANTOM, "x", []]
    assert sync_client._asked(rows, {OWNED}) == [[OWNED, 1, 3], OWNED]
