"""Streamed tracks take their places in the queue at the tap
(QUEUE-STREAM-DESIGN §2, B-124): the routes commit every slot in one
mutation, the fetch lane brings their streams in the queue's order, a
provider's refusal with a reason is remembered beside the resolve's answers
(availability says so before Play), and a Play none of whose first slots
streams is said at once. Against a real PostgreSQL — a throwaway database
built from the migrations — with a provider on no network whose every
download is held until the test lets it go."""

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

import psycopg2.pool  # noqa: E402

from playback.base import Capabilities, PlayerBackend  # noqa: E402
from streaming.base import (FetchedAudio, ProviderManifest, Refused,  # noqa: E402
                            ResolvedSource, StreamProvider)
from streaming.proxy import MediaProxy  # noqa: E402


class _Output(PlayerBackend):
    """An output that takes every mutation as it is (the base contract)."""
    id = "browser"
    label = "stub"

    def __init__(self, emit):
        super().__init__(emit)
        self.ready: list = []

    def start(self): pass
    def shutdown(self): pass
    def capabilities(self): return Capabilities()
    def play(self): return True
    def pause(self): return True
    def stop(self): return True
    def next(self): return True
    def previous(self): return True
    def select(self, index, track_id=None): return True

    def slot_ready(self, index):
        self.ready.append(index)


class _Catalog(StreamProvider):
    """Resolves every track at its catalog length; each download waits until
    the test lets that track go — or its fetch is cancelled, which it reads
    (contract 3) — and refuses where it is told to."""
    manifest = ProviderManifest(id="catalog", name="Catalog", kind="direct_url", lossless=True)
    resolve_workers = 2

    def __init__(self):
        self.gates: dict = {}
        self.refuse: dict = {}
        self.downloads: list = []
        self.cancelled: list = []
        self.cv = threading.Condition()

    def fetch(self, query):
        return self._download(self._resolve(query).source_id)

    def _resolve(self, query):
        return ResolvedSource(source_id=query.track_id, duration=query.duration)

    def _download(self, sid):
        from streaming.base import FetchCancelled, fetching
        with self.cv:
            self.downloads.append(sid)
            self.cv.notify_all()
        gate = self.gates.setdefault(sid, threading.Event())
        unhook = fetching().on_cancel(gate.set)
        gate.wait(20)
        unhook()
        if fetching().cancelled.is_set():
            self.cancelled.append(sid)
            raise FetchCancelled(sid)
        if sid in self.refuse:
            raise Refused(self.refuse[sid], f"catalog: {self.refuse[sid]}")
        return FetchedAudio(data=b"fLaC" + b"\x00" * 64, mime="audio/flac", lossless=True)

    def land(self, *sids):
        for s in sids:
            self.gates.setdefault(s, threading.Event()).set()


def _wait(pred, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


@pytest.fixture
def node(scratch_dsn, monkeypatch):
    """The catalog: an owned album of two files and a streamed album of
    three tracks, one artist; a manager on an output that takes everything,
    its lane on a media proxy (no port) fed by the held provider."""
    import db_pool
    import routers.player as player
    from playback import manager as manager_mod
    from playback.manager import PlaybackManager
    from streaming import service as streaming_service
    pool = psycopg2.pool.ThreadedConnectionPool(1, 6, dsn=scratch_dsn, options="-c timezone=UTC")
    monkeypatch.setattr(db_pool, "_pool", pool)
    conn = psycopg2.connect(scratch_dsn)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("TRUNCATE tracks, albums, artists, listening_sessions, demo_plays CASCADE")
    artist = str(uuid.uuid4())
    cur.execute("INSERT INTO artists (id, name) VALUES (%s, 'Hidden Orchestra')", (artist,))
    owned, streamed = str(uuid.uuid4()), str(uuid.uuid4())
    cur.execute("INSERT INTO albums (id, title) VALUES (%s, 'Night Walks'), (%s, 'Dawn Chorus')",
                (owned, streamed))
    cur.execute("INSERT INTO album_variants (album_id, directory_path) VALUES (%s, 'E:/Music/NW') "
                "RETURNING id", (owned,))
    variant = cur.fetchone()[0]
    files = []
    for i in (1, 2):
        t = str(uuid.uuid4())
        cur.execute("INSERT INTO tracks (id, title) VALUES (%s, %s)", (t, f"Owned {i}"))
        cur.execute("INSERT INTO track_artists (track_id, artist_id) VALUES (%s, %s)", (t, artist))
        cur.execute("INSERT INTO media_files (track_id, album_variant_id, file_path, sample_rate, "
                    "bit_depth, duration_seconds, track_number) VALUES (%s, %s, %s, 44100, 16, 200, %s) "
                    "RETURNING id", (t, variant, f"E:/Music/NW/0{i}.flac", i))
        files.append(cur.fetchone()[0])
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
    proxy.link_refused = player.note_refusal
    monkeypatch.setattr(streaming_service, "_proxy", proxy)
    monkeypatch.setattr(streaming_service, "is_enabled", lambda: True)
    monkeypatch.setattr(streaming_service, "providers_preferred", lambda: [prov])
    monkeypatch.setattr(streaming_service, "note_provider_health", lambda silent, answered: False)
    monkeypatch.setattr(player, "_chain_cache", {})
    monkeypatch.setattr(player, "_refusals", {})
    mgr = PlaybackManager()
    monkeypatch.setattr(mgr, "_schedule_persist", lambda: None)
    monkeypatch.setattr(player, "manager", mgr)
    monkeypatch.setattr(manager_mod, "manager", mgr)
    mgr.lane.attach(proxy)
    out = _Output(mgr._on_backend_status)
    mgr._active = out
    monkeypatch.setattr(mgr, "ensure_active", lambda: out)
    yield {"mgr": mgr, "prov": prov, "proxy": proxy, "out": out, "player": player,
           "owned": owned, "streamed": streamed, "files": files, "phantoms": phantoms, "cur": cur}
    prov.land(*phantoms)
    conn.close()
    pool.closeall()


def _titles(mgr):
    return [it.title for it in mgr.queue.snapshot()]


def test_a_streamed_album_then_an_owned_album_added_to_the_end_keep_their_order(node):
    """B-124: "Add to end" of a streamed album, then of an owned one, while
    the first fetch is held: every slot in the queue at once, in the order
    tapped — whatever the fetches' timing after."""
    mgr, prov, player = node["mgr"], node["prov"], node["player"]
    mgr.replace_queue(player.queue_mod.items_for_owned_rows(
        [{"id": node["files"][0], "location": "local"}]), play=False)
    answer = player.queue_phantom_album(player.PlayPhantomAlbumRequest(
        album_id=node["streamed"], position="end"))
    assert (answer["track_count"], answer["not_streamable"]) == (3, [])
    player.queue_tracks(player.QueueTracksRequest(track_ids=node["files"], position="end"))
    want = ["Owned 1", "Stream 1", "Stream 2", "Stream 3", "Owned 1", "Owned 2"]
    assert _titles(mgr) == want
    with prov.cv:
        assert prov.cv.wait_for(lambda: prov.downloads, timeout=10)
    prov.land(*node["phantoms"][::-1])
    assert _wait(lambda: all(mgr.queue.item_at(s).opener()["kind"] == "proxy" for s in (2, 3, 4)))
    assert _titles(mgr) == want
    # fetched in the queue's order, the window from the slot Play starts at
    assert prov.downloads == node["phantoms"]
    # each row said what its stream did, apart from the version: ready now
    states = {s["index"]: s["state"] for s in mgr.slots_message()["slots"]}
    assert states == {2: "ready", 3: "ready", 4: "ready"}


def test_a_play_next_of_a_streamed_album_takes_its_places_at_once(node):
    mgr, player = node["mgr"], node["player"]
    mgr.replace_queue(player.queue_mod.items_for_owned_rows(
        [{"id": f, "location": "local"} for f in node["files"]]), play=False)
    mgr._latest_status = {"state": "playing", "track_index": 1}
    player.queue_phantom_album(player.PlayPhantomAlbumRequest(album_id=node["streamed"],
                                                             position="next"))
    assert _titles(mgr) == ["Owned 1", "Stream 1", "Stream 2", "Stream 3", "Owned 2"]


def test_a_removed_buffering_slot_cancels_its_fetch_and_the_lane_takes_the_next(node):
    mgr, prov, proxy, player = node["mgr"], node["prov"], node["proxy"], node["player"]
    player.play_phantom_album(player.PlayPhantomAlbumRequest(album_id=node["streamed"]))
    with prov.cv:
        assert prov.cv.wait_for(lambda: prov.downloads, timeout=10)
    first = mgr.queue.item_at(1)
    tok = first.play["token"]
    assert proxy.state(tok)[0] == "buffering"
    assert mgr.remove(1, first.track_id)
    assert proxy._peek(tok) is None
    with prov.cv:
        assert prov.cv.wait_for(lambda: len(prov.downloads) == 2, timeout=10)
    assert prov.downloads == node["phantoms"][:2]
    assert prov.cancelled == node["phantoms"][:1]       # it saw the cancel at the source


def test_a_refusal_is_said_on_its_row_remembered_and_availability_says_so_before_play(node):
    """A provider's refusal with a reason: the row says why, and the next
    resolve does not offer that provider for the track — availability calls
    it unavailable, with the reason; an album all of whose tracks are refused
    is not streamable here, and its Play queues nothing."""
    mgr, prov, player = node["mgr"], node["prov"], node["player"]
    for t in node["phantoms"]:
        prov.refuse[t] = "region"
    prov.land(*node["phantoms"])
    player.play_phantom_album(player.PlayPhantomAlbumRequest(album_id=node["streamed"]))
    assert _wait(lambda: all(mgr.queue.item_at(s).opener()["kind"] == "unplayable" for s in (1, 2, 3)))
    rows = mgr.playlist()["tracks"]
    assert [r["stream"] for r in rows] == [{"state": "failed", "reason": "region"}] * 3
    # said at once, with why: the first two failed and nothing played — then all
    event = mgr.latest_status.get("stream_event")
    assert event and event["all"] is True and event["reason"] == "region"
    assert event["album"] == "Dawn Chorus"
    av = player.phantom_availability(node["streamed"])
    assert sorted(u["reason"] for u in av["unavailable"]) == ["region"] * 3
    assert av["not_streamable"] == {"reason": "region"}
    again = player.play_phantom_album(player.PlayPhantomAlbumRequest(album_id=node["streamed"]))
    assert again["track_count"] == 0 and [m["reason"] for m in again["not_streamable"]] == ["region"] * 3


def test_a_play_whose_first_two_slots_fail_is_said_before_the_rest_are_known(node):
    mgr, prov, player = node["mgr"], node["prov"], node["player"]
    a, b, c = node["phantoms"]
    prov.refuse[a] = prov.refuse[b] = "no_match"
    prov.land(a, b)
    player.play_phantom_album(player.PlayPhantomAlbumRequest(album_id=node["streamed"]))
    assert _wait(lambda: mgr.latest_status.get("stream_event") is not None)
    event = mgr.latest_status["stream_event"]
    assert (event["all"], event["reason"]) == (False, "no_match")
    assert mgr.queue.item_at(3).opener()["kind"] == "pending"      # its fetch held still
    first = event["id"]
    prov.land(c)
    assert _wait(lambda: mgr.queue.item_at(3).opener()["kind"] == "proxy")
    assert mgr.latest_status["stream_event"]["id"] == first         # no second word: one streamed
