"""PLAYBACK-UI-1 against a real PostgreSQL (a throwaway database built from
the migrations): "from here" on the routes that start an album — the whole
album queued, the play started at the chosen track, the session the album's
own (B-156); where a session's listen stopped, kept on its slot by the
archive and read by the history page (B-158); Play from the blue dot — the
replay started at that slot and second. An output that takes every mutation
as it is and keeps what it was told; a provider on no network."""

import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

psycopg2 = pytest.importorskip("psycopg2")
pytest.importorskip("sqlalchemy")

import psycopg2.errors  # noqa: E402
import psycopg2.pool  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from playback.base import Capabilities, PlaybackStatus, PlayerBackend  # noqa: E402
from streaming.base import FetchedAudio, ProviderManifest, ResolvedSource, StreamProvider  # noqa: E402
from streaming.proxy import MediaProxy  # noqa: E402


class _Output(PlayerBackend):
    """Takes every mutation as it is (the base contract) and keeps the
    starts it was told: (slot, second) per replace that plays."""
    id = "browser"
    label = "stub"

    def __init__(self, emit):
        super().__init__(emit)
        self.starts: list = []

    def start(self): pass
    def shutdown(self): pass
    def capabilities(self): return Capabilities(seek=True)
    def play(self): return True
    def pause(self): return True
    def stop(self): return True
    def next(self): return True
    def previous(self): return True
    def select(self, index, track_id=None): return True

    def queue_changed(self, kind, *, play=False, start=None):
        if kind == "replace" and play:
            self.starts.append((self._queue.index_of(start[0]), start[1]) if start else (1, 0.0))


class _Catalog(StreamProvider):
    """Resolves every track at its catalog length; its downloads land at once."""
    manifest = ProviderManifest(id="catalog", name="Catalog", kind="direct_url", lossless=True)
    resolve_workers = 2

    def fetch(self, query):
        return FetchedAudio(data=b"fLaC" + b"\x00" * 64, mime="audio/flac", lossless=True)

    def _resolve(self, query):
        return ResolvedSource(source_id=query.track_id, duration=query.duration)


def _wait(pred, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


@pytest.fixture
def node(scratch_dsn, monkeypatch, tmp_path):
    """The owner's album "Night Walks" — four files under the music folder,
    its tracklist in album_tracks — and a not-owned album of three tracks; a
    manager on an output that takes everything, its lane on a media proxy
    (no port) fed by a provider on no network."""
    import db_pool
    import routers.player as player
    import scanner
    from config import settings
    from playback import manager as manager_mod, tracker
    from playback.manager import PlaybackManager
    from streaming import service as streaming_service
    root = tmp_path / "music"
    (root / "NW").mkdir(parents=True)
    monkeypatch.setattr(settings, "music_library_path", str(root))
    monkeypatch.setattr(settings, "music_host_path", None)
    assert _wait(lambda: not scanner._looking and not scanner._look_wanted)
    for name, value in (("_folder_unreachable", False), ("_shown", False), ("_recorded", 0)):
        monkeypatch.setattr(scanner, name, value)
    monkeypatch.setattr(tracker, "_play_session", None)
    monkeypatch.setattr(tracker, "_last_heard", None, raising=False)
    monkeypatch.setattr(tracker, "_scrobble_async", lambda *a, **k: True)
    pool = psycopg2.pool.ThreadedConnectionPool(1, 6, dsn=scratch_dsn, options="-c timezone=UTC")
    monkeypatch.setattr(db_pool, "_pool", pool)
    conn = psycopg2.connect(scratch_dsn)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("TRUNCATE tracks, albums, artists, listening_sessions, demo_plays, hqp_endpoints, "
                "user_settings CASCADE")
    artist = str(uuid.uuid4())
    cur.execute("INSERT INTO artists (id, name) VALUES (%s, 'Hidden Orchestra')", (artist,))
    owned, streamed = str(uuid.uuid4()), str(uuid.uuid4())
    cur.execute("INSERT INTO albums (id, title) VALUES (%s, 'Night Walks'), (%s, 'Dawn Chorus')",
                (owned, streamed))
    db_root = settings.library_db_root()
    cur.execute("INSERT INTO album_variants (album_id, directory_path) VALUES (%s, %s) RETURNING id",
                (owned, f"{db_root}/NW"))
    variant = cur.fetchone()[0]
    files, tracks = [], []
    for i in (1, 2, 3, 4):
        t = str(uuid.uuid4())
        cur.execute("INSERT INTO tracks (id, title) VALUES (%s, %s)", (t, f"Owned {i}"))
        cur.execute("INSERT INTO track_artists (track_id, artist_id) VALUES (%s, %s)", (t, artist))
        (root / "NW" / f"0{i}.flac").write_bytes(b"fLaC")
        cur.execute("INSERT INTO media_files (track_id, album_variant_id, file_path, file_format, "
                    "sample_rate, bit_depth, duration_seconds, track_number) "
                    "VALUES (%s, %s, %s, 'FLAC', 44100, 16, 200, %s) RETURNING id",
                    (t, variant, f"{db_root}/NW/0{i}.flac", i))
        files.append(cur.fetchone()[0])
        cur.execute("INSERT INTO album_tracks (album_id, track_id, position, length_ms) "
                    "VALUES (%s, %s, %s, 200000)", (owned, t, i))
        tracks.append(t)
    phantoms = []
    for i in (1, 2, 3):
        t = str(uuid.uuid4())
        cur.execute("INSERT INTO tracks (id, title) VALUES (%s, %s)", (t, f"Stream {i}"))
        cur.execute("INSERT INTO track_artists (track_id, artist_id) VALUES (%s, %s)", (t, artist))
        cur.execute("INSERT INTO album_tracks (album_id, track_id, position, length_ms) "
                    "VALUES (%s, %s, %s, 200000)", (streamed, t, i))
        phantoms.append(t)

    prov = _Catalog()
    proxy = MediaProxy(port=0, advertised_host="127.0.0.1", file_token_key=b"k")
    monkeypatch.setattr(streaming_service, "_proxy", proxy)
    monkeypatch.setattr(streaming_service, "is_enabled", lambda: True)
    monkeypatch.setattr(streaming_service, "providers_preferred", lambda: [prov])
    monkeypatch.setattr(streaming_service, "providers_registered", lambda: True)
    monkeypatch.setattr(streaming_service, "note_provider_health", lambda silent, answered: False)
    monkeypatch.setattr(player, "_chain_cache", {})
    monkeypatch.setattr(player, "_refusals", {})
    mgr = PlaybackManager()
    monkeypatch.setattr(mgr, "_schedule_persist", lambda: None)
    monkeypatch.setattr(player, "manager", mgr)
    monkeypatch.setattr(manager_mod, "manager", mgr)
    mgr.lane.attach(proxy)
    out = _Output(mgr._on_backend_status)
    out._queue = mgr.queue
    mgr._active = out
    monkeypatch.setattr(mgr, "ensure_active", lambda: out)
    yield {"mgr": mgr, "out": out, "player": player, "cur": cur, "owned": owned,
           "streamed": streamed, "files": files, "tracks": tracks, "phantoms": phantoms,
           "tracker": tracker}
    assert _wait(lambda: not scanner._looking and not scanner._look_wanted)
    _wait(lambda: not [t for t in threading.enumerate() if t.name == "queue-reach"])
    conn.close()
    pool.closeall()


def _session(node):
    """The active session's (origin, origin_album_id, seed_track_id)."""
    node["cur"].execute("SELECT origin::text, origin_album_id::text, seed_track_id::text "
                        "FROM listening_sessions WHERE ended_at IS NULL")
    return node["cur"].fetchone()


def _sessions(node) -> int:
    node["cur"].execute("SELECT count(*) FROM listening_sessions")
    return node["cur"].fetchone()[0]


# -- B-156: the whole album from the chosen track --------------------------------


def test_play_tracks_from_a_track_queues_the_album_and_starts_there(node):
    player, mgr = node["player"], node["mgr"]
    player.play_tracks(player.PlayTracksRequest(
        track_ids=node["files"], origin="album", origin_album_id=node["owned"],
        start_track_id=node["tracks"][2]))
    assert [it.track_id for it in mgr.queue.snapshot()] == node["tracks"]   # the whole album
    assert node["out"].starts[-1] == (3, 0.0)
    assert _session(node) == ("album", node["owned"], node["tracks"][2])


def test_play_entities_from_a_track_queues_the_album_and_starts_there(node):
    player, mgr = node["player"], node["mgr"]
    player.play_entities(player.QueueEntitiesRequest(
        items=[player.EntityRef(kind="album", id=node["owned"])],
        start_track_id=node["tracks"][1]))
    assert [it.track_id for it in mgr.queue.snapshot()] == node["tracks"]
    assert node["out"].starts[-1] == (2, 0.0)
    assert _session(node) == ("album", node["owned"], node["tracks"][1])


def test_a_not_owned_album_from_a_track_streams_that_slot_first(node):
    player, mgr = node["player"], node["mgr"]
    player.play_phantom_album(player.PlayPhantomAlbumRequest(
        album_id=node["streamed"], start_track_id=node["phantoms"][2]))
    assert [it.track_id for it in mgr.queue.snapshot()] == node["phantoms"]
    assert node["out"].starts[-1] == (3, 0.0)
    assert (mgr._anchor_item.track_id, mgr._anchor_slot) == (node["phantoms"][2], 3)
    assert _session(node) == ("album", node["streamed"], node["phantoms"][2])


def test_without_a_start_the_routes_play_from_the_first_track_as_before(node):
    player = node["player"]
    player.play_tracks(player.PlayTracksRequest(track_ids=node["files"], origin="album",
                                                origin_album_id=node["owned"]))
    assert node["out"].starts[-1] == (1, 0.0)
    assert _session(node)[2] == node["tracks"][0]


def test_a_start_the_list_does_not_hold_is_refused(node):
    player = node["player"]
    with pytest.raises(HTTPException) as e:
        player.play_tracks(player.PlayTracksRequest(track_ids=node["files"],
                                                    start_track_id=str(uuid.uuid4())))
    assert e.value.status_code == 400
    assert node["out"].starts == []


def test_a_start_left_out_starts_at_the_next_kept_and_none_after_it_is_refused(node, monkeypatch):
    """A track nothing streams here is left out: "from here" starts at the
    first kept after it; with none after it the request is refused in words,
    the queue as it was."""
    import routers.player as player
    known = {node["phantoms"][1]: "nowhere"}
    monkeypatch.setattr(player, "_known_unstreamable",
                        lambda queries, whole=False: {q.track_id: known[q.track_id]
                                                      for q in queries if q.track_id in known})
    player.play_phantom_album(player.PlayPhantomAlbumRequest(
        album_id=node["streamed"], start_track_id=node["phantoms"][1]))
    assert [it.track_id for it in node["mgr"].queue.snapshot()] == [node["phantoms"][0],
                                                                    node["phantoms"][2]]
    assert node["out"].starts[-1] == (2, 0.0)
    known[node["phantoms"][2]] = "nowhere"
    before = node["mgr"].queue.snapshot()
    with pytest.raises(HTTPException) as e:
        player.play_phantom_album(player.PlayPhantomAlbumRequest(
            album_id=node["streamed"], start_track_id=node["phantoms"][1]))
    assert (e.value.status_code, e.value.detail) == (404, "Nothing from this track on streams here")
    assert node["mgr"].queue.snapshot() == before


def test_a_double_tap_is_one_session_and_play_all_after_it_another(node):
    player = node["player"]
    req = player.PlayTracksRequest(track_ids=node["files"], origin="album",
                                   origin_album_id=node["owned"], start_track_id=node["tracks"][2])
    player.play_tracks(req)
    player.play_tracks(req)
    assert _sessions(node) == 1
    player.play_tracks(player.PlayTracksRequest(track_ids=node["files"], origin="album",
                                                origin_album_id=node["owned"]))
    assert _sessions(node) == 2


def test_the_look_before_the_start_is_at_the_start_slots_file(node, monkeypatch):
    """The play path's one look at a file (_first_opens) is at the file the
    start opens — the chosen track's, not the album's first."""
    mgr, player = node["mgr"], node["player"]
    looked = []
    real = mgr._first_opens
    monkeypatch.setattr(mgr, "_first_opens",
                        lambda backend, items, held=False: looked.append(items[0].track_id)
                        or real(backend, items, held=held))
    player.play_tracks(player.PlayTracksRequest(track_ids=node["files"],
                                                start_track_id=node["tracks"][3]))
    assert looked == [node["tracks"][3]]


# -- B-158: the stop kept on its slot ----------------------------------------------


def _heard(node, slot, second, state="playing"):
    """A status tick of the output at queue `slot`, `second` in."""
    mgr = node["mgr"]
    item = mgr.queue.item_at(slot)
    mgr._on_backend_status(node["out"], PlaybackStatus(
        state=state, position=second, length=200.0, queue_index=slot, item=item))


def _stops(node, session_id=None):
    node["cur"].execute(
        "SELECT st.position, st.stop_seconds::float FROM session_tracks st "
        "JOIN listening_sessions ls ON ls.id = st.session_id "
        "WHERE st.stop_seconds IS NOT NULL AND ls.ended_at IS NOT NULL "
        + ("AND ls.id = %s::uuid " if session_id else "") + "ORDER BY ls.ended_at",
        (session_id,) if session_id else None)
    return node["cur"].fetchall()


def _album(node, **kw):
    player = node["player"]
    player.play_tracks(player.PlayTracksRequest(track_ids=node["files"], origin="album",
                                                origin_album_id=node["owned"], **kw))


def _another(node):
    """Another album played: the one before is archived."""
    player = node["player"]
    player.play_phantom_album(player.PlayPhantomAlbumRequest(album_id=node["streamed"]))


def test_the_archive_keeps_where_the_listen_stopped_on_its_slot(node):
    _album(node)
    _heard(node, 3, 75.0)
    _another(node)
    assert _stops(node) == [(2, 75.0)]


def test_a_stop_survives_the_outputs_stop(node):
    _album(node)
    _heard(node, 2, 40.0)
    node["mgr"]._on_backend_status(node["out"], PlaybackStatus(state="stopped", queue_index=2))
    _another(node)
    assert _stops(node) == [(1, 40.0)]


def test_a_stop_near_the_tracks_end_is_the_next_slots_start(node):
    _album(node)
    _heard(node, 2, 195.0)
    _another(node)
    assert _stops(node) == [(2, 0.0)]


def test_a_queue_never_played_or_heard_out_keeps_no_stop(node):
    _album(node)                  # nothing sounded
    _another(node)
    assert _stops(node) == []
    _heard(node, 3, 198.0)        # the last slot, heard out
    _album(node)
    assert _stops(node) == []


def test_a_queue_that_played_out_is_archived_with_no_stop(node):
    _album(node)
    _heard(node, 4, 120.0)        # far from its end: only the play-out keeps it from a stop
    node["mgr"]._on_backend_status(node["out"], PlaybackStatus(state="stopped", queue_index=4))
    node["cur"].execute("SELECT count(*) FROM session_tracks st JOIN listening_sessions ls "
                        "ON ls.id = st.session_id WHERE ls.ended_at IS NOT NULL")
    assert node["cur"].fetchone()[0] == 4 and _stops(node) == []


def test_a_session_keeps_one_stop_and_its_slot_takes_it_along(node):
    _album(node)
    _heard(node, 2, 30.0)
    _another(node)
    cur = node["cur"]
    cur.execute("SELECT session_id::text FROM session_tracks WHERE stop_seconds IS NOT NULL")
    session = cur.fetchone()[0]
    with pytest.raises(psycopg2.errors.UniqueViolation):
        cur.execute("UPDATE session_tracks SET stop_seconds = 5 WHERE session_id = %s "
                    "AND position = 0", (session,))
    cur.execute("DELETE FROM tracks WHERE id = %s", (node["tracks"][1],))
    assert _stops(node, session) == []


# -- B-158: the history page and Play from the dot ---------------------------------


def _archived(node, slot=3, second=75.0):
    _album(node)
    _heard(node, slot, second)
    _another(node)
    node["cur"].execute("SELECT id::text FROM listening_sessions WHERE ended_at IS NOT NULL")
    return node["cur"].fetchone()[0]


def test_the_history_page_reads_each_slot_and_the_stop(node):
    from routers.home import get_listening_session
    session = _archived(node)
    d = get_listening_session(session)
    assert [t["position"] for t in d["tracks"]] == [0, 1, 2, 3]
    assert d["stop"] == {"position": 2, "seconds": 75.0}


def test_play_from_the_dot_starts_the_replay_at_that_slot_and_second(node):
    player = node["player"]
    session = _archived(node)
    player.play_session(player.PlaySessionRequest(
        session_id=session, start=player.SessionStart(position=2, seconds=75.0)))
    assert [it.track_id for it in node["mgr"].queue.snapshot()] == node["tracks"]
    assert node["out"].starts[-1] == (3, 75.0)
    assert _session(node) == ("album", node["owned"], node["tracks"][2])
    player.play_session(player.PlaySessionRequest(
        session_id=session, start=player.SessionStart(position=0, seconds=0)))
    assert node["out"].starts[-1] == (1, 0.0)


def test_play_with_no_dot_replays_from_the_top_as_before(node):
    player = node["player"]
    session = _archived(node)
    player.play_session(player.PlaySessionRequest(session_id=session))
    assert node["out"].starts[-1] == (1, 0.0)


def test_a_slot_the_session_does_not_hold_is_refused(node):
    player = node["player"]
    session = _archived(node)
    with pytest.raises(HTTPException) as e:
        player.play_session(player.PlaySessionRequest(
            session_id=session, start=player.SessionStart(position=9, seconds=0)))
    assert e.value.status_code == 400


def test_a_stop_on_a_track_that_plays_as_an_excerpt_now_starts_at_zero(node, monkeypatch):
    """A streamed slot whose demo listen is spent plays as the 30 s clip
    where the demo channel is all that plays it: a second past it is no
    place in it. A provider that plays it whole keeps the second."""
    player, cur = node["player"], node["cur"]
    player.play_phantom_album(player.PlayPhantomAlbumRequest(album_id=node["streamed"]))
    _heard(node, 2, 80.0)
    _album(node)
    cur.execute("SELECT id::text FROM listening_sessions WHERE ended_at IS NOT NULL")
    session = cur.fetchone()[0]
    cur.execute("INSERT INTO demo_plays (track_id, provider) VALUES (%s, 'catalog')",
                (node["phantoms"][1],))
    resume = player.PlaySessionRequest(session_id=session,
                                       start=player.SessionStart(position=1, seconds=80.0))
    player.play_session(resume)
    assert node["out"].starts[-1] == (2, 80.0)          # a full-length provider plays it whole
    monkeypatch.setattr(_Catalog, "manifest", ProviderManifest(
        id="catalog", name="Catalog", kind="direct_url", lossless=False, demo_limited=True))
    player.play_session(resume)
    assert node["out"].starts[-1] == (2, 0.0)


def test_the_life_data_merge_names_the_stop(node):
    src = (BACKEND / "life_merge.py").read_text()
    insert = src[src.index("INSERT INTO session_tracks"):]
    assert "stop_seconds" in insert[:insert.index("ON CONFLICT")]
