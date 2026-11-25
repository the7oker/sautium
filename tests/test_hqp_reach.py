"""What opens here changes under an HQPlayer that plays (PI-NODE-2D §3.3): the
music folder goes, or comes back, and every slot's way in is read again
(PlaybackManager.reach_changed) — the entry HQPlayer plays, holds paused or
is waited on for is never stopped, restarted or handed again; what lies
before it is the past, and what lies behind it follows: the playlist's tail
is made the queue's, ending before the first slot whose stream is still to
come. On the fake HQPlayer of test_hqp_backend with the REAL
playback.substitute.native_plays — the owner's album as rows of a throwaway
PostgreSQL built from the migrations, the folder's state the scanner's
recorded answer set directly (no look at a disk) and told to the manager as
its event is — and the lane fed by a provider on no network."""

import logging
import sys
import uuid
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(Path(__file__).resolve().parent))

psycopg2 = pytest.importorskip("psycopg2")
pytest.importorskip("sqlalchemy")

import psycopg2.pool  # noqa: E402

from test_hqp_backend import (_QUESTIONS, _attach, _queue_stays_in_memory, _routes_on, _wait,  # noqa: E402,F401
                              fake, listens)
from test_hqp_horizon import _Held  # noqa: E402
from test_roon_coexistence import FakeRoon, _from_node  # noqa: E402

from hqplayer_client import PlaybackState  # noqa: E402
from playback import hqp_link as hl  # noqa: E402
from playback.manager import PlaybackManager  # noqa: E402
from playback.queue import QueueItem, items_for_media_ids  # noqa: E402
from playback.substitute import native_plays as _native_plays  # noqa: E402
from streaming import service as streaming_service  # noqa: E402
from streaming.base import TrackQuery  # noqa: E402

PLAYING, PAUSED, STOPPED = (int(PlaybackState.PLAYING), int(PlaybackState.PAUSED),
                            int(PlaybackState.STOPPED))
FILES = [f"file:///E:/Music/NW/0{i}.flac" for i in (1, 2, 3, 4)]
# What stops, restarts or hands again the entry HQPlayer reads
_CUTS = {"Stop", "PlaylistClear", "SelectTrack", "Play", "Seek", "Next", "Previous"}


@pytest.fixture
def node(fake, listens, scratch_dsn, monkeypatch, caplog):
    """The owner's album "Night Walks", four files under the music folder
    (the fake's: stored as E:/Music/NW/0n.flac, handed to an HQPlayer on this
    machine by path), read by the real native_plays; a manager whose lane
    feeds the fake's media proxy from a provider that holds every fetch until
    the test lets it go. `gone(flag)` records what the node knows of its
    music folder and tells the manager, as the record's own event does."""
    import db_pool
    import routers.player as player
    import scanner
    from playback import manager as manager_mod
    from playback import substitute
    caplog.set_level(logging.INFO, logger="playback")       # what the node did, beside a failure
    monkeypatch.setattr(substitute, "native_plays", _native_plays)
    assert _wait(lambda: not scanner._looking and not scanner._look_wanted)
    for name, value in (("_folder_unreachable", False), ("_shown", False), ("_recorded", 0)):
        monkeypatch.setattr(scanner, name, value)
    pool = psycopg2.pool.ThreadedConnectionPool(1, 6, dsn=scratch_dsn, options="-c timezone=UTC")
    monkeypatch.setattr(db_pool, "_pool", pool)
    conn = psycopg2.connect(scratch_dsn)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("TRUNCATE tracks, albums, artists, hqp_endpoints, user_settings CASCADE")
    artist, album = str(uuid.uuid4()), str(uuid.uuid4())
    cur.execute("INSERT INTO artists (id, name) VALUES (%s, 'Hidden Orchestra')", (artist,))
    cur.execute("INSERT INTO albums (id, title) VALUES (%s, 'Night Walks')", (album,))
    cur.execute("INSERT INTO album_variants (album_id, directory_path) VALUES (%s, 'E:/Music/NW') RETURNING id",
                (album,))
    variant = cur.fetchone()[0]
    files, tracks = [], []
    for i in (1, 2, 3, 4):
        t = str(uuid.uuid4())
        cur.execute("INSERT INTO tracks (id, title) VALUES (%s, %s)", (t, f"Owned {i}"))
        cur.execute("INSERT INTO track_artists (track_id, artist_id) VALUES (%s, %s)", (t, artist))
        cur.execute("INSERT INTO media_files (track_id, album_variant_id, file_path, file_format, "
                    "sample_rate, bit_depth, duration_seconds, track_number) "
                    "VALUES (%s, %s, %s, 'FLAC', 44100, 16, 300, %s) RETURNING id",
                    (t, variant, f"E:/Music/NW/0{i}.flac", i))
        files.append(cur.fetchone()[0])
        tracks.append(t)

    prov = _Held()
    proxy = streaming_service._proxy
    monkeypatch.setattr(streaming_service, "is_enabled", lambda: True)
    monkeypatch.setattr(streaming_service, "providers_preferred", lambda: [prov])
    monkeypatch.setattr(streaming_service, "providers_registered", lambda: True)
    monkeypatch.setattr(player, "_phantom_track_query", lambda tid, album_id=None: TrackQuery(
        artist="Hidden Orchestra", title=tid, album="Night Walks", duration=300.0, track_id=tid,
        album_id=album_id))
    monkeypatch.setattr(player, "_resolve_waterfall", lambda queries: [[(prov, None)] for _ in queries])
    monkeypatch.setattr(player, "_refused_code", lambda tid: None)
    monkeypatch.setattr(hl.BuildWatch, "after", 0.3)
    mgr = PlaybackManager()
    monkeypatch.setattr(manager_mod, "manager", mgr)
    monkeypatch.setattr(proxy, "track_ready_hooks", [lambda e: mgr.stream_landed(e.token, e.reshaped)])
    monkeypatch.setattr(proxy, "track_failed_hooks", [lambda e: mgr.stream_failed(e.token)])
    monkeypatch.setattr(proxy, "state_hooks", [])
    mgr.lane.attach(proxy)

    def gone(flag: bool, *, told: bool = True) -> None:
        monkeypatch.setattr(scanner, "_folder_unreachable", flag)
        if told:
            mgr.reach_changed()

    backends: list = []

    def attach(items: list):
        mgr.queue.replace(items)
        backends.append(_attach(mgr))
        return backends[-1]

    yield {"mgr": mgr, "prov": prov, "files": files, "tracks": tracks, "gone": gone, "attach": attach,
           "monkeypatch": monkeypatch}
    prov.land(*tracks, "stream-5")
    for b in backends:
        b.shutdown()
    conn.close()
    pool.closeall()


def _sent(fake, since: int) -> list:
    """What HQPlayer received since `since` (a length of fake.received), the
    node's questions aside, in order."""
    return [(c, a) for _port, c, a in fake.received[since:] if c not in _QUESTIONS]


def _cuts(fake, since: int) -> list:
    return [c for c, _ in _sent(fake, since) if c in _CUTS]


def _settled(fake, ticks: int = 3) -> None:
    k = len(fake.commands)
    assert _wait(lambda: sum(1 for c, _ in fake.commands[k:] if c == "Status") >= ticks)


def _kinds(items: list) -> list:
    return [it.opener()["kind"] for it in items]


def _plays_slot_2_of_his_files(node, fake, state: str):
    """HQPlayer plays — or holds paused — slot 2 of the owner's four files."""
    mgr = node["mgr"]
    items = items_for_media_ids(node["files"])
    b = node["attach"](items)
    assert _wait(lambda: fake.playlist == FILES), fake.playlist
    assert b.select(2)
    assert _wait(lambda: mgr._latest_status.get("state") == "playing"
                 and mgr._latest_status.get("track_index") == 2
                 and mgr._latest_status.get("position", 0) >= 2), mgr._latest_status
    if state == "paused":
        assert b.pause()
        assert _wait(lambda: mgr._latest_status.get("state") == "paused"), mgr._latest_status
    return mgr, b, items


def _plays_slot_2_as_a_stream(node, fake, state: str):
    """The folder is gone: HQPlayer plays — or holds paused — slot 2 of the
    owner's album as a stream, slot 1's landed before it, slots 3 and 4
    still to come."""
    mgr, prov, t = node["mgr"], node["prov"], node["tracks"]
    node["gone"](True, told=False)
    items = items_for_media_ids(node["files"])
    b = node["attach"](items)
    prov.land(t[0], t[1])
    assert _wait(lambda: len(fake.playlist) == 2 and all("/preview/" in u for u in fake.playlist)), fake.playlist
    assert b.select(2)
    assert _wait(lambda: mgr._latest_status.get("state") == "playing"
                 and mgr._latest_status.get("track_index") == 2
                 and mgr._latest_status.get("position", 0) >= 2), mgr._latest_status
    if state == "paused":
        assert b.pause()
        assert _wait(lambda: mgr._latest_status.get("state") == "paused"), mgr._latest_status
    return mgr, b, items


def _moves_on(fake, mgr, b, slot: int) -> None:
    """HQPlayer plays the entry it reads out and moves on by itself — at
    rest on no entry for two status reads between the two, as Desktop is
    (the tail, the next one's build). The tap that started the entry is
    watched no more by then: its advance is watched as one (HqpBackend.
    _advance watches one attempt at a time)."""
    assert _wait(lambda: b._attempt is None, timeout=30)
    with fake._lock:
        fake.advance_polls = 2
        fake.position = fake.length - 2
    assert _wait(lambda: fake.state == PLAYING and mgr._latest_status.get("state") == "playing"
                 and mgr._latest_status.get("track_index") == slot, timeout=15), (
        mgr._latest_status.get("state"), mgr._latest_status.get("track_index"), fake.state, fake.track,
        fake.playlist, [c for _p, c, _a in fake.received if c not in _QUESTIONS][-12:])


@pytest.mark.parametrize("state", ["playing", "paused"])
def test_the_folder_going_never_stops_the_entry_hqplayer_plays(node, fake, state):
    """A1 (b): the folder goes while HQPlayer plays slot 2 of 4 of the
    owner's files (or holds it paused). The node sends no Stop, no clear and
    no start, and the entry it reads is as it was; the slot keeps its way in
    until HQPlayer leaves it. Slot 1 is the past. Slots 3 and 4, streams to
    come now, end the playlist behind the entry it reads; a landing there is
    an append behind it, and when HQPlayer moves on by itself the slot it
    left is read — nothing rebuilt."""
    mgr, b, items = _plays_slot_2_of_his_files(node, fake, state)
    prov, t = node["prov"], node["tracks"]
    busy = PLAYING if state == "playing" else PAUSED
    n = len(fake.received)
    node["gone"](True)
    assert _wait(lambda: fake.playlist == FILES[:2]), fake.playlist
    _settled(fake)
    assert _cuts(fake, n) == [] and (fake.state, fake.track) == (busy, 2), (_sent(fake, n), fake.state, fake.track)
    assert [c for c, _ in _sent(fake, n)] == ["PlaylistRemove"] * 2
    assert _kinds(items) == ["pending", "file", "pending", "pending"] and mgr._reach_owed is items[1]
    assert b._mirror == "tail" and b._horizon is items[2] and b.healthy()
    if state == "paused":
        return
    prov.land(t[2])                                     # slot 3's stream: an append behind the playing entry
    assert _wait(lambda: len(fake.playlist) == 3 and "/preview/" in fake.playlist[2]), fake.playlist
    _moves_on(fake, mgr, b, 3)                             # HQPlayer leaves slot 2: its way in is read then
    assert _wait(lambda: items[1].opener()["kind"] == "pending" and mgr._reach_owed is None), _kinds(items)
    _settled(fake)
    assert _cuts(fake, n) == [] and (fake.state, fake.track) == (PLAYING, 3), _sent(fake, n)
    assert [c for c, _ in _sent(fake, n)] == ["PlaylistRemove"] * 2 + ["PlaylistAdd"]


def test_the_folder_going_and_coming_back_under_one_track_leaves_the_playlist_as_it_was(node, fake):
    """Pulled and plugged back while slot 2 plays: the tail goes, then comes
    back as the files it was — the playlist left a tail mirror by the first
    change follows the second too — and the entry HQPlayer plays is never
    touched; in step again, and nothing owed."""
    mgr, b, items = _plays_slot_2_of_his_files(node, fake, "playing")
    n = len(fake.received)
    node["gone"](True)
    assert _wait(lambda: fake.playlist == FILES[:2]) and b._mirror == "tail", (fake.playlist, b._mirror)
    node["gone"](False)
    assert _wait(lambda: fake.playlist == FILES), fake.playlist
    _settled(fake)
    assert [c for c, _ in _sent(fake, n)] == ["PlaylistRemove"] * 2 + ["PlaylistAdd"] * 2
    assert (fake.state, fake.track) == (PLAYING, 2) and _kinds(items) == ["file"] * 4
    assert b._mirror == "step" and b._horizon is None and mgr._reach_owed is None


def test_the_folder_going_on_a_node_that_streams_nothing_leaves_what_plays_and_says_the_rest(node, fake):
    """The same where nothing streams (Nano, the streaming library off): the
    entry HQPlayer plays is untouched, the tail behind it is gone from its
    playlist, and each row behind says why it does not play here."""
    node["monkeypatch"].setattr(streaming_service, "is_enabled", lambda: False)
    mgr, b, items = _plays_slot_2_of_his_files(node, fake, "playing")
    n = len(fake.received)
    node["gone"](True)
    assert _wait(lambda: fake.playlist == FILES[:2]), fake.playlist
    assert _wait(lambda: all(b.gap(it) is not None for it in items[2:])), _kinds(items)
    _settled(fake)
    assert _cuts(fake, n) == [] and (fake.state, fake.track) == (PLAYING, 2), _sent(fake, n)
    assert [bool(r.get("gap")) for r in mgr.playlist()["tracks"]][1:] == [False, True, True]


@pytest.mark.parametrize("state", ["playing", "paused"])
def test_the_folder_coming_back_never_restarts_the_stream_hqplayer_plays(node, fake, state):
    """A1 (a): the folder comes back while HQPlayer plays slot 2 as a stream
    (or holds it paused). The stream plays on — no Stop, no restart on the
    file — and slots 3 and 4, files again, are handed as files behind it;
    when HQPlayer moves on to slot 3 by itself, slot 2 is its file again,
    nothing rebuilt."""
    mgr, b, items = _plays_slot_2_as_a_stream(node, fake, state)
    busy = PLAYING if state == "playing" else PAUSED
    streamed = list(fake.playlist)
    n = len(fake.received)
    node["gone"](False)
    assert _wait(lambda: fake.playlist == streamed + FILES[2:]), fake.playlist
    _settled(fake)
    assert _cuts(fake, n) == [] and (fake.state, fake.track) == (busy, 2), (_sent(fake, n), fake.state, fake.track)
    assert [c for c, _ in _sent(fake, n)] == ["PlaylistAdd"] * 2
    assert _kinds(items) == ["file", "proxy", "file", "file"] and mgr._reach_owed is items[1]
    assert b._mirror == "tail" and b._horizon is None and b.healthy()
    if state == "paused":
        return
    _moves_on(fake, mgr, b, 3)
    assert _wait(lambda: items[1].play is None and mgr._reach_owed is None), _kinds(items)
    _settled(fake)
    assert _cuts(fake, n) == [] and (fake.state, fake.track) == (PLAYING, 3), _sent(fake, n)
    assert [c for c, _ in _sent(fake, n)] == ["PlaylistAdd"] * 2 and fake.playlist == streamed + FILES[2:]


def test_the_folder_coming_back_under_a_start_that_waits_starts_it_on_its_file(node, fake):
    """A start waits on slot 1's stream (Now Playing "loading" it), HQPlayer
    at rest on an empty playlist; the folder comes back. A slot nothing
    sounds yet is held by nothing: its file is its way in again, and the
    start that waited goes on the file — the stream is never waited for,
    and what HQPlayer is handed is the owner's four files."""
    mgr, t = node["mgr"], node["tracks"]
    node["gone"](True, told=False)
    items = items_for_media_ids(node["files"])
    b = node["attach"](items)
    assert _wait(lambda: _kinds(items) == ["pending"] * 4 and fake.playlist == [])
    assert b.select(1)
    assert _wait(lambda: (mgr._latest_status.get("state"), mgr._latest_status.get("track_index")) == ("loading", 1))
    n = len(fake.received)
    node["gone"](False)
    assert _wait(lambda: fake.state == PLAYING and fake.track == 1 and fake.playlist == FILES), (
        fake.playlist, _sent(fake, n))
    assert _kinds(items) == ["file"] * 4
    assert _wait(lambda: (mgr._latest_status.get("state"), mgr._latest_status.get("track_index")) == ("playing", 1))
    assert not any("/preview/" in a.get("uri", "") for _, a in _sent(fake, n)), _sent(fake, n)


def test_the_folder_going_under_a_start_that_waits_leaves_what_hqplayer_plays_meanwhile(node, fake):
    """HQPlayer plays slot 1 of the owner's files while a tap on a streamed
    slot waits for its stream — the status names the slot that waits, not
    the one that sounds. The folder goes: the entry HQPlayer plays is
    untouched all the same, whatever way in its slot has by now, and the
    start that waits goes when its stream lands."""
    mgr, prov = node["mgr"], node["prov"]
    items = items_for_media_ids(node["files"]) + [QueueItem(
        track_id=str(uuid.uuid4()), media_file_id=None, source={"kind": "track"}, title="Stream 5",
        artist="Hidden Orchestra", album="Dawn Chorus", duration_seconds=300.0)]
    prov.gates[items[4].track_id] = prov.gate("stream-5")
    b = node["attach"](items)
    assert _wait(lambda: fake.playlist == FILES), fake.playlist
    assert b.select(1)
    assert _wait(lambda: mgr._latest_status.get("state") == "playing" and mgr._latest_status.get("track_index") == 1
                 and mgr._latest_status.get("position", 0) >= 2), mgr._latest_status
    assert b.select(5, items[4].track_id)
    assert _wait(lambda: (mgr._latest_status.get("state"), mgr._latest_status.get("track_index")) == ("loading", 5))
    n = len(fake.received)
    node["gone"](True)
    assert _wait(lambda: fake.playlist == FILES[:1]), fake.playlist
    _settled(fake)
    assert _cuts(fake, n) == [] and (fake.state, fake.track) == (PLAYING, 1), _sent(fake, n)
    assert b._parked is not None and b._parked.item is items[4]
    prov.land("stream-5")                               # the tap's own start: its landing takes the queue to HQPlayer
    assert _wait(lambda: fake.state == PLAYING and mgr._latest_status.get("state") == "playing"
                 and mgr._latest_status.get("track_index") == 5, timeout=15), (mgr._latest_status, fake.playlist)


def test_a_new_order_around_a_slot_owed_its_reading_plays_it_on_as_it_was(node, fake, monkeypatch):
    """The folder went while slot 1 plays: it keeps its way in, owed its
    reading. A new order that needs HQPlayer's playlist rebuilt around the
    playing slot (the protocol has no insert) reads every other slot again
    and that one not: it plays on at its second as the file it was — read
    under it, a stream still to come, it was not started at all — and is
    owed its reading still."""
    mgr, t = node["mgr"], node["tracks"]
    items = items_for_media_ids(node["files"])
    b = node["attach"](items)
    assert _wait(lambda: fake.playlist == FILES), fake.playlist
    assert b.select(1)
    assert _wait(lambda: mgr._latest_status.get("state") == "playing" and mgr._latest_status.get("track_index") == 1
                 and mgr._latest_status.get("position", 0) >= 2), mgr._latest_status
    player_router = _routes_on(monkeypatch, mgr)
    node["gone"](True)
    assert _wait(lambda: fake.playlist == FILES[:1]), fake.playlist
    assert b._mirror == "step" and mgr._reach_owed is items[0]
    with fake._lock:
        fake.takes_seek = True
    n = len(fake.received)
    assert player_router.reorder(player_router.ReorderRequest(order=[t[1], t[0], t[2], t[3]]))["ok"]
    assert _wait(lambda: fake.state == PLAYING and mgr._latest_status.get("state") == "playing"
                 and mgr._latest_status.get("track_index") == 2, timeout=15), (fake.playlist, mgr._latest_status)
    _settled(fake)
    sent = [c for c, _ in _sent(fake, n)]
    assert sent[0] == "Stop" and "SelectTrack" in sent, sent          # rebuilt around it
    assert fake.playlist == FILES[:1] and (fake.state, fake.track) == (PLAYING, 1)
    assert _kinds(items) == ["file", "pending", "pending", "pending"] and mgr._reach_owed is items[0]


def test_what_opens_here_changing_sends_a_shared_hqplayer_nothing(node, fake):
    """Another controller holds HQPlayer: the queue is the queue's alone
    there, and the folder going or coming back sends HQPlayer nothing —
    every slot's way in is read all the same."""
    mgr = node["mgr"]
    fake.raw_streams = "uri"
    items = items_for_media_ids(node["files"])
    b = node["attach"](items)
    assert _wait(lambda: fake.playlist == FILES), fake.playlist
    other = FakeRoon(fake.port)
    try:
        other.play_track()
        assert _wait(lambda: b._taken() and mgr._latest_status.get("source") == "external", 15)
        left = (fake.state, fake.track, list(fake.playlist))
        n = len(fake.received)
        node["gone"](True)
        _settled(fake)
        assert _kinds(items) == ["pending"] * 4
        node["gone"](False)
        _settled(fake)
        assert _kinds(items) == ["file"] * 4
        assert _from_node(fake, n, other) == [] and (fake.state, fake.track, fake.playlist) == left
    finally:
        other.close()


def test_a_stopped_hqplayer_takes_the_queue_afresh_when_what_opens_here_changes(node, fake):
    """At rest, nothing sounding and no start waiting, HQPlayer takes the
    queue afresh as before: emptied while every slot is a stream to come,
    the four files again when the folder is back."""
    items = items_for_media_ids(node["files"])
    node["attach"](items)
    assert _wait(lambda: fake.playlist == FILES), fake.playlist
    n = len(fake.received)
    node["gone"](True)
    assert fake.playlist == [] and [c for c, _ in _sent(fake, n)] == ["Stop", "PlaylistClear"]
    n = len(fake.received)
    node["gone"](False)
    assert fake.playlist == FILES and [c for c, _ in _sent(fake, n)] == ["Stop"] + ["PlaylistAdd"] * 4
    assert fake.state == STOPPED
