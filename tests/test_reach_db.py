"""An album whose files are not reachable now plays as a phantom does
(PI-NODE-2D §3): which stored paths do not answer is one recorded fact
(scanner.gone_prefixes), read by the function that picks a slot's way in
(playback.substitute.native_plays) and by every query that ranks copies
(sql_queries.HERE, owned_rank) — a copy that is not here now, its folder
gone or the copy away, is never a way in: the slot takes a copy that opens,
else streams, its origin untouched. Against a real PostgreSQL — a throwaway
database built from the migrations — with a provider on no network."""

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
from streaming.base import (FetchedAudio, ProviderManifest, Refused, ResolvedSource,  # noqa: E402
                            StreamProvider)
from streaming.proxy import MediaProxy  # noqa: E402


class _Output(PlayerBackend):
    """An output that takes every mutation as it is (the base contract) and
    keeps what it was told."""
    id = "browser"
    label = "stub"

    def __init__(self, emit):
        super().__init__(emit)
        self.changed: list = []
        self.replaced: list = []

    def start(self): pass
    def shutdown(self): pass
    def capabilities(self): return Capabilities()
    def play(self): return True
    def pause(self): return True
    def stop(self): return True
    def next(self): return True
    def previous(self): return True
    def select(self, index, track_id=None): return True

    def queue_replace(self, items, *, play):
        self.replaced.append([it.opener().get("kind") for it in items])
        return super().queue_replace(items, play=play)

    def queue_changed(self, kind, *, play=False):
        self.changed.append(kind)


class _Catalog(StreamProvider):
    """Resolves every track at its catalog length; a download waits until
    the test lets that track go."""
    manifest = ProviderManifest(id="catalog", name="Catalog", kind="direct_url", lossless=True)
    resolve_workers = 2

    def __init__(self):
        self.gates: dict = {}
        self.asked: list = []
        self.refuse: dict = {}

    def fetch(self, query):
        return self._download(self._resolve(query).source_id)

    def _resolve(self, query):
        self.asked.append(query.track_id)
        return ResolvedSource(source_id=query.track_id, duration=query.duration)

    def _download(self, sid):
        from streaming.base import FetchCancelled, fetching
        gate = self.gates.setdefault(sid, threading.Event())
        unhook = fetching().on_cancel(gate.set)
        gate.wait(20)
        unhook()
        if fetching().cancelled.is_set():
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
def node(scratch_dsn, monkeypatch, tmp_path):
    """The owner's album "Night Walks" — two files under the music folder,
    its tracklist in album_tracks as the materialiser leaves it — and a
    not-owned album of three tracks, one artist; a manager on an output that
    takes everything, its lane on a media proxy (no port) fed by a provider
    on no network. `gone(flag)` sets what the node has recorded of its music
    folder, without the event; `copy(n, path)` adds a second copy of track n
    in another folder."""
    import db_pool
    import routers.player as player
    import scanner
    from config import settings
    from playback import manager as manager_mod
    from playback.manager import PlaybackManager
    from streaming import service as streaming_service
    root = tmp_path / "music"
    (root / "NW").mkdir(parents=True)
    monkeypatch.setattr(settings, "music_library_path", str(root))
    monkeypatch.setattr(settings, "music_host_path", None)
    # The folder's state is process state: each test starts with it answering,
    # once the look worker is idle (a look still running would land here)
    assert _wait(lambda: not scanner._looking and not scanner._look_wanted)
    for name, value in (("_folder_unreachable", False), ("_shown", False), ("_recorded", 0)):
        monkeypatch.setattr(scanner, name, value)
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
    for i in (1, 2):
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

    def gone(flag: bool) -> None:
        monkeypatch.setattr(scanner, "_folder_unreachable", flag)

    elsewhere = (tmp_path / "elsewhere").as_posix()

    def copy(n: int, name: str, *, sample_rate=44100) -> int:
        """A second copy of track n at `elsewhere`/`name` — a folder that
        is not the music folder, the file there."""
        path = f"{elsewhere}/{name}"
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(b"fLaC")
        cur.execute("INSERT INTO album_variants (album_id, directory_path, sample_rate) "
                    "VALUES (%s, %s, %s) RETURNING id", (owned, path.rsplit("/", 1)[0], sample_rate))
        v = cur.fetchone()[0]
        cur.execute("INSERT INTO media_files (track_id, album_variant_id, file_path, file_format, "
                    "sample_rate, bit_depth, duration_seconds, track_number) "
                    "VALUES (%s, %s, %s, 'FLAC', %s, 16, 200, %s) RETURNING id",
                    (tracks[n], v, path, sample_rate, n + 1))
        return cur.fetchone()[0]

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
    mgr._active = out
    monkeypatch.setattr(mgr, "ensure_active", lambda: out)
    yield {"mgr": mgr, "prov": prov, "proxy": proxy, "out": out, "player": player, "cur": cur,
           "owned": owned, "streamed": streamed, "files": files, "tracks": tracks,
           "phantoms": phantoms, "variant": variant, "root": root, "db_root": db_root,
           "gone": gone, "copy": copy, "elsewhere": elsewhere, "streaming": streaming_service,
           "monkeypatch": monkeypatch}
    prov.land(*phantoms, *tracks)
    assert _wait(lambda: not scanner._looking and not scanner._look_wanted)
    _wait(lambda: not [t for t in threading.enumerate() if t.name == "queue-reach"])
    conn.close()
    pool.closeall()


# -- the fact and the way in (§3.2) --------------------------------------------


def test_the_gone_prefixes_are_the_recorded_answer_never_a_look_at_the_disk(node, monkeypatch):
    import os
    import scanner
    assert scanner.gone_prefixes() == []
    monkeypatch.setattr(os, "scandir", lambda path: pytest.fail("a reader looked at the disk"))
    node["gone"](True)
    assert scanner.gone_prefixes() == [node["db_root"] + "/"]
    node["gone"](False)
    assert scanner.gone_prefixes() == []


def test_a_file_under_a_folder_that_does_not_answer_is_no_way_in_and_its_origin_stays(node):
    from playback.queue import items_for_media_ids
    from playback.substitute import native_plays
    it = items_for_media_ids(node["files"][:1])[0]
    origin = dict(it.source)
    assert native_plays([it], "browser", None) == [None]
    node["gone"](True)
    assert native_plays([it], "browser", None) == [{"kind": "pending"}]     # a stream to come
    assert native_plays([it], "hqplayer", None) == [{"kind": "pending"}]
    assert (it.source, it.media_file_id) == (origin, node["files"][0])
    node["gone"](False)
    assert native_plays([it], "browser", None) == [None]                    # the local copy again
    assert native_plays([it], "hqplayer", None) == [None]


def test_a_second_copy_that_is_here_is_the_way_in(node):
    from playback.queue import items_for_media_ids
    from playback.substitute import native_plays
    other = node["copy"](0, "NW/01.flac")
    it = items_for_media_ids(node["files"][:1])[0]
    assert native_plays([it], "browser", None) == [None]
    node["gone"](True)
    play = native_plays([it], "browser", None)[0]
    assert (play["kind"], play["media_file_id"], play["path"]) == (
        "file", other, f"{node['elsewhere']}/NW/01.flac")
    assert it.source["path"] == f"{node['db_root']}/NW/01.flac"


def test_an_away_copy_with_no_other_streams_and_a_copy_here_stands_in_for_it(node):
    """Q4: a copy a Rescan set away follows the same rule — before it the
    slot opened the missing file."""
    from playback.queue import items_for_media_ids
    from playback.substitute import native_plays
    it = items_for_media_ids(node["files"][:1])[0]
    node["cur"].execute("UPDATE album_variants SET absent_since = now() WHERE id = %s",
                        (node["variant"],))
    assert native_plays([it], "browser", None) == [{"kind": "pending"}]
    other = node["copy"](0, "NW/01.flac")
    assert native_plays([it], "browser", None)[0]["media_file_id"] == other
    # the track itself (a slot queued while the copy was away) reads the same
    from playback.queue import QueueItem
    track = QueueItem(track_id=node["tracks"][1], media_file_id=None, source={"kind": "track"})
    assert native_plays([track], "browser", None) == [{"kind": "pending"}]


def _canon_rewrites(node, scratch_dsn, old: str, new: str) -> None:
    """The canon's own rewrite of a track's uuid (a rename, or a merge into
    a track that exists), committed — with no event of the queue's."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from canon.identity import _update_track_uuid
    engine = create_engine(scratch_dsn)
    with Session(engine) as db:
        _update_track_uuid(db, old, new)
        db.commit()
    engine.dispose()


@pytest.mark.parametrize("how", ["rename", "merge"])
def test_a_queued_file_whose_tracks_uuid_the_canon_rewrote_still_opens_as_its_file(
        node, scratch_dsn, how):
    """A slot that is a file is read by what it is — its media_files row —
    never by the uuid its enqueue saw: the canon rewrites that uuid under
    the queue (ON UPDATE CASCADE, a merge), and the file is there all the
    same. On the stub output and through the HQPlayer mirror's call shape;
    its other copies are those of the track the file belongs to now."""
    from playback.queue import items_for_media_ids
    from playback.substitute import native_plays
    twin = node["copy"](0, "twin/01.flac")          # a second copy, the canon's to move too
    items = _queue(node)
    it, old = items[0], items[0].track_id
    new = str(uuid.uuid4()) if how == "rename" else node["phantoms"][0]
    _canon_rewrites(node, scratch_dsn, old, new)
    assert it.track_id == old                       # nothing told the queue
    assert native_plays([it], "browser", None) == [None]
    assert native_plays([it], "hqplayer", 7) == [None]
    # the folder gone: the other copy of the track the file belongs to now
    # stands in for it; back, its own file again — as any slot's
    node["gone"](True)
    node["mgr"].reach_changed()
    assert it.play["media_file_id"] == twin
    node["gone"](False)
    node["mgr"].reach_changed()
    assert it.play is None and it.opener()["path"].endswith("/NW/01.flac")
    assert items_for_media_ids(node["files"][:1])[0].track_id == new


def test_a_slot_takes_its_files_track_when_the_canon_rewrote_it(node, scratch_dsn):
    """Play tracking keys on the slot's track uuid, and a listen against a
    uuid that is gone is no row: the canon's rewrite wakes the queue's read
    of the file layer at its commit (the files listener's channel), and
    there a slot whose file belongs to another track now takes that
    track's uuid."""
    items = _queue(node)
    it, old, new = items[0], items[0].track_id, str(uuid.uuid4())
    heard = psycopg2.connect(scratch_dsn)
    heard.autocommit = True
    heard.cursor().execute("LISTEN sautium_files_removed")
    _canon_rewrites(node, scratch_dsn, old, new)
    assert _wait(lambda: (heard.poll(), bool(heard.notifies))[1])   # the rewrite's own wake
    heard.close()
    node["out"].changed.clear()
    node["mgr"].rebind_files()
    assert it.track_id == new and it.media_file_id == node["files"][0] and it.play is None
    assert items[1].track_id == node["tracks"][1]
    assert node["out"].changed == ["rebind"]
    node["mgr"].rebind_files()                      # nothing left to take: nothing changes
    assert node["out"].changed == ["rebind"]


@pytest.fixture
def lastfm(node, monkeypatch):
    """The play tracker on this node's database, no listen open; Last.fm's
    calls counted, none sent."""
    from playback import tracker
    said: list = []
    monkeypatch.setattr(tracker, "_play_session", None)
    monkeypatch.setattr(tracker, "_carried", False)
    monkeypatch.setattr(tracker, "_scrobble_async",
                        lambda method, **kw: (said.append((method, kw)), True)[1])
    return said


def _tick(node, state: str, position: float) -> None:
    from playback.base import PlaybackStatus
    node["mgr"]._on_backend_status(node["out"], PlaybackStatus(
        state=state, position=position, length=200.0, queue_index=1))


def _listens(node) -> list:
    node["cur"].execute("SELECT track_id::text, started_at, duration_listened::float, completed, "
                        "skipped, media_file_id FROM listening_history ORDER BY started_at")
    return node["cur"].fetchall()


@pytest.mark.parametrize("how", ["rename", "merge"])
@pytest.mark.parametrize("scrobbled_before", [False, True])
@pytest.mark.parametrize("word", ["the canon's word", "the file layer read", "no word before its end"])
def test_a_listen_is_one_listen_across_a_rewrite_of_its_tracks_uuid(
        node, lastfm, scratch_dsn, how, scrobbled_before, word):
    """The canon rewrites the uuid of the track that is playing — a rename,
    a merge into a track that exists. The listen in progress follows its
    item to the new uuid in the same step (the canon's own word with the
    wake; a wake that lost it reads the slot's file) — nothing saved,
    nothing reopened: one history row at the end, against the uuid that
    exists, from the real start, of the real length; one now-playing, one
    scrobble, whether it had gone before the rewrite or not; the stats
    derived from that one row. With no word at all before the listen ends
    its row is still written against the track its file belongs to."""
    from playback import manager as manager_mod
    from playback import tracker
    mgr, cur = node["mgr"], node["cur"]
    it = _queue(node)[0]
    old = it.track_id
    new = str(uuid.uuid4()) if how == "rename" else node["phantoms"][0]
    _tick(node, "playing", 2.0)
    started = tracker._play_session.started_at
    _tick(node, "playing", 120.0 if scrobbled_before else 40.0)
    assert [m for m, _kw in lastfm] == ["update_now_playing"] + ["scrobble"] * scrobbled_before
    heard = psycopg2.connect(scratch_dsn)
    heard.autocommit = True
    heard.cursor().execute("LISTEN sautium_files_removed")
    _canon_rewrites(node, scratch_dsn, old, new)
    if word == "the canon's word":
        assert _wait(lambda: (heard.poll(), bool(heard.notifies))[1])
        manager_mod._files_changed(list(heard.notifies))        # as the files listener hears it
    elif word == "the file layer read":
        mgr.rebind_files()
    heard.close()
    assert it.track_id == (old if word == "no word before its end" else new)
    _tick(node, "playing", 150.0)
    _tick(node, "stopped", 150.0)
    assert tracker._play_session is None
    assert _listens(node) == [(new, started, 150.0, True, False, node["files"][0])]
    assert [m for m, _kw in lastfm] == ["update_now_playing", "scrobble"]
    assert lastfm[-1][1]["timestamp"] == int(started.timestamp())
    cur.execute("SELECT track_id::text, play_count, skip_count FROM local_play_stats")
    assert cur.fetchall() == [(new, 1, 0)]


def test_the_canons_word_for_a_streamed_slots_uuid_changes_nothing_on_that_slot(
        node, lastfm, scratch_dsn):
    """A streamed slot keeps the uuid it was queued with: its stream is
    known to the outputs by it (the media proxy's token, HQPlayer's
    hand-over ledger), so the canon's word for that uuid leaves the slot,
    its way in and the listen in progress as they were — and the file slot
    beside it, of another track, untouched too."""
    from playback import manager as manager_mod
    from playback import tracker
    from playback.queue import items_for_media_ids
    mgr, player = node["mgr"], node["player"]
    old, new = node["phantoms"][0], str(uuid.uuid4())
    queries = player._phantom_track_queries([old])
    mgr.replace_queue(player.queue_mod.items_for_streamed([queries[old]])
                      + items_for_media_ids(node["files"][:1]), play=False)
    stream, file = mgr.queue.snapshot()
    assert _wait(lambda: (stream.play or {}).get("token"))      # the lane named its stream
    way_in = stream.play
    _tick(node, "playing", 20.0)
    heard = psycopg2.connect(scratch_dsn)
    heard.autocommit = True
    heard.cursor().execute("LISTEN sautium_files_removed")
    _canon_rewrites(node, scratch_dsn, old, new)
    assert _wait(lambda: (heard.poll(), bool(heard.notifies))[1])
    node["out"].changed.clear()
    manager_mod._files_changed(list(heard.notifies))
    heard.close()
    assert stream.track_id == old and stream.play is way_in and stream.source == {"kind": "track"}
    assert tracker._play_session is not None and tracker._play_session.track_id == old
    assert file.track_id == node["tracks"][0] and node["out"].changed == []
    assert [m for m, _kw in lastfm] == ["update_now_playing"]


def test_the_canons_words_are_read_once_each_and_a_slot_ends_where_they_end():
    """A chain of rewrites in one wake — A is B, then B is C — leaves a file
    slot of A at C, one of B at C, and a rename there and back where it
    was; each slot is asked once, whatever the number of words."""
    from playback.queue import CanonicalQueue, QueueItem
    queue = CanonicalQueue()
    slots = [QueueItem(track_id=t, media_file_id=n, source={"kind": "file", "path": f"/m/{n}.flac"})
             for n, t in enumerate(["a", "b", "x", "d", "e"])]
    queue.replace(slots)
    followed = []

    def follow(it, track_id):
        followed.append(it.media_file_id)
        it.track_id = track_id
    words = (("a", "b"), ("b", "c"), ("d", "e"), ("e", "d"), ("y", "x"))
    moved = queue.rebind(lambda items: [None] * len(items), None, words, follow)
    assert [it.track_id for it in slots] == ["c", "c", "x", "d", "d"]
    assert followed == [0, 1, 3, 4] and moved == [slots[0], slots[1], slots[3], slots[4]]


def test_a_listen_goes_on_when_its_slot_becomes_the_track_itself(node, lastfm):
    """The other in-place change of a playing item's identity: its file
    leaves the library with no other copy, and the slot becomes the track
    itself — the same uuid, so the same listen: one row, with no file."""
    from playback import tracker
    mgr, cur = node["mgr"], node["cur"]
    it = _queue(node)[0]
    _tick(node, "playing", 2.0)
    started = tracker._play_session.started_at
    cur.execute("DELETE FROM media_files WHERE id = %s", (node["files"][0],))
    mgr.rebind_files()
    assert it.source == {"kind": "track"} and it.track_id == node["tracks"][0]
    _tick(node, "playing", 150.0)
    _tick(node, "stopped", 150.0)
    assert _listens(node) == [(node["tracks"][0], started, 150.0, True, False, None)]
    assert [m for m, _kw in lastfm] == ["update_now_playing", "scrobble"]


def _ranked(node, track: int, hqp_id, gone: list) -> list:
    from db_pool import db_query
    from sql_queries import TRACK_FILES, best_rip_order, owned_rank
    return [(r["location"], r["id"]) for r in db_query(
        f"SELECT f.id, f.location::text AS location FROM ({TRACK_FILES}) f "
        f"ORDER BY {owned_rank('f')}, {best_rip_order('f')}",
        {"tid": node["tracks"][track], "hqp_id": hqp_id, "gone": gone})]


def _held(node, name: str, track: int) -> tuple:
    """A copy of track `track` in the library of an HQPlayer called `name`."""
    cur = node["cur"]
    cur.execute("INSERT INTO hqp_endpoints (name) VALUES (%s) RETURNING id", (name,))
    ep = cur.fetchone()[0]
    cur.execute("INSERT INTO album_variants (album_id, directory_path, location, hqp_endpoint_id) "
                "VALUES (%s, %s, 'hqplayer', %s) RETURNING id", (node["owned"], f"/media/{name}/NW", ep))
    cur.execute("INSERT INTO hqp_library_files (track_id, album_variant_id, hqp_path, file_format, "
                "sample_rate, bit_depth) VALUES (%s, %s, %s, 'FLAC', 44100, 16) RETURNING id",
                (node["tracks"][track], cur.fetchone()[0], f"/media/{name}/NW/01.flac"))
    return ep, cur.fetchone()[0]


def test_with_every_folder_answering_the_copies_rank_as_before(node):
    """owned_rank with an empty array is the order it had: the copy at the
    HQPlayer that is the output, the local rips best first, a copy at
    another HQPlayer, a copy that is away last."""
    cd = node["files"][0]
    hires = node["copy"](0, "hi/01.flac", sample_rate=96000)
    away = node["copy"](0, "gone/01.flac", sample_rate=192000)
    node["cur"].execute("UPDATE album_variants SET absent_since = now() WHERE id = "
                        "(SELECT album_variant_id FROM media_files WHERE id = %s)", (away,))
    ep, held = _held(node, "box", 0)
    other, held_elsewhere = _held(node, "second", 0)
    assert _ranked(node, 0, ep, []) == [("hqplayer", held), ("local", hires), ("local", cd),
                                        ("hqplayer", held_elsewhere), ("local", away)]
    assert _ranked(node, 0, None, [])[:2] == [("local", hires), ("local", cd)]
    # the music folder gone: its file ranks with the away copy, the copy
    # elsewhere and the HQPlayers' stand
    order = _ranked(node, 0, ep, [node["db_root"] + "/"])
    assert order[:3] == [("hqplayer", held), ("local", hires), ("hqplayer", held_elsewhere)]
    assert set(order[3:]) == {("local", away), ("local", cd)}


def test_with_no_folder_gone_a_copy_query_plans_as_it_did(node):
    """`here` is a column of the row source: with an empty array a copy
    query's plan has the nodes of the one that read absent_since alone —
    the same scans on the same indexes, the same joins, in the same shape —
    and one sub-plan of its own, over the empty array."""
    from playback import substitute
    from sql_queries import HERE
    cur = node["cur"]

    def nodes(sql: str) -> list:
        cur.execute("EXPLAIN (COSTS OFF) " + sql,
                    {"ids": [node["tracks"][0]], "mids": [], "hqp_id": None, "gone": []})
        kept, under = [], None
        for line in (r[0] for r in cur.fetchall()):
            indent = len(line) - len(line.lstrip())
            if under is not None and indent > under:
                continue                    # the sub-plan's own nodes
            under = indent if "SubPlan" in line else None
            if line.lstrip().startswith("->") or not kept:
                kept.append(line.rstrip())
        return kept

    assert HERE in substitute._COPIES_SQL
    with_here = nodes(substitute._COPIES_SQL)
    as_it_was = nodes(substitute._COPIES_SQL.replace(HERE, "(av.absent_since IS NULL)"))
    assert with_here == as_it_was


def test_a_slot_that_streams_for_want_of_a_file_shows_a_streams_state(node):
    """The queue row of a slot whose way in became a stream shows the
    stream row's own lines: the lane counts it among the streamed slots."""
    from playback.queue import items_for_media_ids
    from playback.substitute import streamed
    it = items_for_media_ids(node["files"][:1])[0]
    assert not streamed(it)
    it.play = {"kind": "pending"}
    assert streamed(it)
    it.play = {"kind": "pending", "prepare": "transcode", "file": dict(it.source)}
    assert not streamed(it)             # a copy here, prepared for HQPlayer


# -- the event, and who hears it (§3.3) -----------------------------------------


def _queue(node, *, play=False):
    from playback.queue import items_for_media_ids
    items = items_for_media_ids(node["files"])
    node["mgr"].replace_queue(items, play=play)
    return items


def _streams(it) -> bool:
    return it.opener().get("kind") in ("pending", "proxy")


def test_a_recorded_change_rereads_the_queue_once_and_the_folders_return_is_the_local_copy_again(node):
    """The mount table, the watcher's loss, a file found missing: each
    records the folder's answer, and a change of it is the queue's event —
    one re-read, one new version, the output told once, the status carrying
    it; the origins untouched, so the folder's return gives every slot its
    own file back with no question."""
    import scanner
    mgr, out = node["mgr"], node["out"]
    items = _queue(node)
    origins = [dict(it.source) for it in items]
    mgr._latest_status = {"state": "stopped", "track_index": 0}
    out.changed.clear()
    v = mgr.queue.version

    scanner.folder_seen(True)                       # a producer's observation: the folder is gone
    assert _wait(lambda: out.changed == ["rebind"])
    assert all(_streams(it) for it in items) and mgr.queue.version == v + 1
    assert _wait(lambda: mgr.latest_status.get("playlist_version") == v + 1)
    assert [it.source for it in items] == origins
    # each row says what its stream does, and the lane looked at its window
    assert _wait(lambda: [s["index"] for s in mgr.slots_message()["slots"]] == [1, 2])
    assert _wait(lambda: set(node["tracks"]) <= set(node["prov"].asked))
    scanner.folder_seen(True)                       # said again: nothing changed, nothing is read
    assert not _wait(lambda: len(out.changed) > 1, timeout=0.5)

    scanner.folder_seen(False)                      # a library file served: it answers again
    assert _wait(lambda: out.changed == ["rebind", "rebind"])
    assert all(it.play is None for it in items) and mgr.queue.version == v + 2
    assert [it.source for it in items] == origins
    assert _wait(lambda: mgr.slots_message()["slots"] == [])


def test_the_slot_the_output_sounds_keeps_its_way_in_until_the_output_leaves_it(node):
    """The output's own end of the file it plays is the output's: a way in
    swapped under the sounding slot would be that slot started over. It is
    read when the output moves on — the next Play of it streams."""
    import scanner
    from playback.base import PlaybackStatus
    mgr, out = node["mgr"], node["out"]
    first, second = _queue(node)
    mgr._on_backend_status(out, PlaybackStatus(state="playing", position=3.0, length=200.0,
                                               queue_index=1))
    scanner.folder_seen(True)
    assert _wait(lambda: _streams(second))
    assert first.play is None and mgr._reach_owed is first
    mgr._on_backend_status(out, PlaybackStatus(state="playing", position=4.0, length=200.0,
                                               queue_index=1))
    assert not _wait(lambda: first.play is not None, timeout=0.5)       # still sounding
    mgr._on_backend_status(out, PlaybackStatus(state="playing", position=0.0, length=200.0,
                                               queue_index=2))
    assert _wait(lambda: _streams(first))
    assert mgr._reach_owed is None


def test_a_manager_reads_its_own_owed_slot(node, monkeypatch):
    """The slot owed its reading is this manager's: read by it, whichever
    manager the process holds as its own."""
    from playback import manager as manager_mod
    from playback.base import PlaybackStatus
    from playback.manager import PlaybackManager
    mgr, out = node["mgr"], node["out"]
    first, second = _queue(node)
    mgr._on_backend_status(out, PlaybackStatus(state="paused", position=3.0, length=200.0,
                                               queue_index=1))
    monkeypatch.setattr(manager_mod, "manager", PlaybackManager())
    node["gone"](True)
    mgr.reach_changed()
    assert first.play is None and _streams(second) and mgr._reach_owed is first
    mgr._on_backend_status(out, PlaybackStatus(state="stopped", queue_index=1))
    assert _wait(lambda: _streams(first))


def test_a_play_of_his_files_whose_streams_all_fail_is_said_once_and_each_row_says_its_own(node):
    """An owned slot streaming because its disk is out shows its stream's
    state on its row, and a Play whose slots all fail is said once, when the
    last of them has — whatever each slot was queued as. The run read the
    slots' origin: a file slot was none of the Play's streams, so nothing
    of it was said (before that, "all failed" at the first failure)."""
    mgr, prov = node["mgr"], node["prov"]
    node["proxy"].link_refused = node["player"].note_refusal
    for t in node["tracks"]:
        prov.refuse[t] = "region"
    _pull_the_disk(node)
    items = _queue(node, play=True)                 # the look: both slots are streams to come
    assert [it.source["kind"] for it in items] == ["file", "file"]
    rows = mgr.playlist()["tracks"]
    assert [r["id"] for r in rows] == node["files"]
    assert all(r["stream"]["state"] in ("waiting", "buffering") for r in rows), rows

    prov.land(node["tracks"][0])
    assert _wait(lambda: items[0].opener().get("kind") == "unplayable")
    assert mgr.latest_status.get("stream_event") is None        # one of two: nothing said yet
    prov.land(node["tracks"][1])
    assert _wait(lambda: mgr.latest_status.get("stream_event") is not None)
    event = mgr.latest_status["stream_event"]
    assert (event["kind"], event["all"], event["reason"], event["album"]) == (
        "not_streamable", True, "region", "Night Walks")
    assert [r["stream"] for r in mgr.playlist()["tracks"]] == [{"state": "failed", "reason": "region"}] * 2
    assert not _wait(lambda: mgr.latest_status["stream_event"]["id"] != event["id"], timeout=0.5)   # once


def test_his_queue_whose_folder_went_where_nothing_streams_is_said_once_too(node):
    """A Nano node, the folder gone after a Play that has not sounded yet:
    the slots wait, each row saying why, and the Play's failure is said
    once — every slot failed — as for any Play none of whose slots streams."""
    import scanner
    mgr = node["mgr"]
    items = _queue(node, play=True)
    node["monkeypatch"].setattr(node["streaming"], "is_enabled", lambda: False)
    scanner.folder_seen(True)
    assert _wait(lambda: all(it.opener().get("kind") == "unplayable" for it in items))
    assert _wait(lambda: [(s["state"], s["reason"], s.get("why")) for s in mgr.slots_message()["slots"]]
                 == [("failed", "not_here", "disk")] * 2)
    assert _wait(lambda: mgr.latest_status.get("stream_event") is not None)
    event = mgr.latest_status["stream_event"]
    assert (event["all"], event["reason"]) == (True, "not_here")
    assert [it.source["kind"] for it in items] == ["file", "file"]


def _his_ten(node) -> list:
    """A queue of ten slots of his own files (the album five times over),
    nothing played."""
    from playback.queue import items_for_media_ids
    items = [it for _ in range(5) for it in items_for_media_ids(node["files"])]
    node["mgr"].replace_queue(items, play=False)
    assert len(node["mgr"].queue) == 10
    return items


def _rows(mgr) -> list:
    return [(s["index"], s["state"], s.get("reason"), s.get("why"))
            for s in mgr.slots_message()["slots"]]


def test_where_nothing_streams_every_slot_of_his_away_copy_says_so_at_once(node, caplog):
    """H1 (PI-NODE-2D §10.6): on a node that streams nothing no fetch is
    asked, so the lead window — which bounds the fetches — bounds nothing:
    every slot of his copy set away settles at once, each saying why in the
    album page's words (not_here, `away`), never "streaming is off" for
    some and "Waiting" for ever for the rest. One line in the log says how
    many."""
    import logging
    mgr, cur = node["mgr"], node["cur"]
    node["monkeypatch"].setattr(node["streaming"], "is_enabled", lambda: False)
    items = _his_ten(node)
    cur.execute("UPDATE album_variants SET absent_since = now() WHERE id = %s", (node["variant"],))
    with caplog.at_level(logging.INFO, logger="playback.substitute"):
        mgr.rebind_files()                          # as the Rescan's notify does
        assert _wait(lambda: _rows(mgr) == [(i, "failed", "not_here", "away") for i in range(1, 11)]), \
            _rows(mgr)
    assert all(it.opener()["kind"] == "unplayable" for it in items)
    assert [r["stream"] for r in mgr.playlist()["tracks"]] == [
        {"state": "failed", "reason": "not_here", "why": "away"}] * 10
    said = [r.getMessage() for r in caplog.records
            if r.name == "playback.substitute" and r.levelno >= logging.INFO]
    assert len(said) == 1 and said[0].startswith("10 queued slot(s)"), said
    assert node["prov"].asked == []


def test_where_nothing_streams_a_slot_on_a_disk_that_is_out_says_disk(node):
    """The same where his copy lies under a folder that does not answer:
    `disk`, the album page's "On a disk that is not connected"."""
    mgr = node["mgr"]
    node["monkeypatch"].setattr(node["streaming"], "is_enabled", lambda: False)
    _his_ten(node)
    node["gone"](True)
    mgr.reach_changed()
    assert _wait(lambda: _rows(mgr) == [(i, "failed", "not_here", "disk") for i in range(1, 11)]), \
        _rows(mgr)


def test_where_nothing_streams_a_track_he_has_no_copy_of_is_off_past_the_window_too(node):
    """A not-owned track's slot settles as `off` — streaming's own words —
    wherever it lies in the queue, at once."""
    mgr, player = node["mgr"], node["player"]
    node["monkeypatch"].setattr(node["streaming"], "is_enabled", lambda: False)
    queries = player._phantom_track_queries(node["phantoms"])
    items = [it for _ in range(3)
             for it in player.queue_mod.items_for_streamed([queries[t] for t in node["phantoms"]])]
    mgr.replace_queue(items, play=False)
    assert _wait(lambda: _rows(mgr) == [(i, "failed", "off", None) for i in range(1, 10)]), _rows(mgr)


def test_streaming_coming_up_asks_again_for_every_slot_it_settled(node):
    """revive: streaming up, every slot settled for want of it — his own
    away copy's and a not-owned track's, inside the window or past it — is
    a stream to come again, and the window's are asked for."""
    mgr, player, cur = node["mgr"], node["player"], node["cur"]
    up = {"on": False}
    node["monkeypatch"].setattr(node["streaming"], "is_enabled", lambda: up["on"])
    items = _his_ten(node)
    queries = player._phantom_track_queries(node["phantoms"][:1])
    tail = player.queue_mod.items_for_streamed([queries[node["phantoms"][0]]])
    mgr.append(tail)
    cur.execute("UPDATE album_variants SET absent_since = now() WHERE id = %s", (node["variant"],))
    mgr.rebind_files()
    assert _wait(lambda: [s.get("reason") for s in mgr.slots_message()["slots"]] == ["not_here"] * 10 + ["off"]), \
        _rows(mgr)
    up["on"] = True
    mgr.lane.revive()
    assert all(it.opener()["kind"] in ("pending", "proxy") for it in items + tail), \
        [it.opener() for it in items + tail]
    assert _wait(lambda: set(node["tracks"]) <= set(node["prov"].asked))


def test_a_new_queue_while_the_folder_is_gone_reads_its_file_slots(node):
    """A file slot committed while the folder does not answer is read as
    any slot's: no window in which the output is handed a path that does
    not open."""
    from playback.queue import items_for_media_ids
    mgr = node["mgr"]
    node["gone"](True)
    items = _queue(node)
    assert all(_streams(it) for it in items)
    more = items_for_media_ids(node["files"][:1])
    mgr.append(more)
    assert _streams(more[0]) and more[0].source["kind"] == "file"


def _pull_the_disk(node) -> None:
    """The folder as a pulled disk leaves it: there, and empty — with no
    event, as a share that died or a root that is itself a mount point."""
    for f in (node["root"] / "NW").iterdir():
        f.unlink()
    (node["root"] / "NW").rmdir()


def test_a_play_whose_first_file_is_missing_looks_at_the_folder_and_streams(node):
    """The play path's own look, one stat of the file about to be opened: a
    miss is a producer's observation, and the output is handed the queue as
    it reads after it — never the path that does not open."""
    import scanner
    mgr, out = node["mgr"], node["out"]
    _pull_the_disk(node)
    assert scanner.gone_prefixes() == []            # nothing told the node
    items = _queue(node, play=True)
    assert scanner.gone_prefixes() == [node["db_root"] + "/"]
    assert out.replaced == [["pending", "pending"]]
    assert [it.source["kind"] for it in items] == ["file", "file"]
    assert mgr.queue.snapshot() == items


def test_a_first_slot_that_is_the_track_itself_is_asked_by_the_file_it_would_open(node):
    """A slot queued as the track opens its copy here as a file: the look
    asks about that file, as about a slot that is a file."""
    import scanner
    from playback.queue import QueueItem
    mgr, out = node["mgr"], node["out"]
    _pull_the_disk(node)
    track = QueueItem(track_id=node["tracks"][0], media_file_id=None, source={"kind": "track"},
                      title="Owned 1", album_id=node["owned"])
    mgr.replace_queue([track], play=True)
    assert scanner.gone_prefixes() == [node["db_root"] + "/"]
    assert out.replaced == [["pending"]]


def test_a_play_of_which_nothing_opens_where_nothing_streams_is_refused_before_the_output(node):
    from playback.manager import NotHere
    from playback.queue import items_for_media_ids
    mgr, out = node["mgr"], node["out"]
    old = _queue(node)
    out.replaced.clear()
    node["monkeypatch"].setattr(node["streaming"], "is_enabled", lambda: False)
    _pull_the_disk(node)
    with pytest.raises(NotHere) as refused:
        mgr.replace_queue(items_for_media_ids(node["files"]), play=True)
    assert refused.value.why == "disk"
    assert out.replaced == [] and mgr.queue.snapshot() == old       # the old queue intact


def test_a_file_gone_from_a_folder_that_answers_is_no_way_in_of_its_slot(node):
    """A file deleted or moved outside Sautium keeps its row until a
    Rescan's yes, and its folder answers: the look before a start finds the
    file missing, the folder there — and that slot alone goes out as a
    stream, never as the path that does not open. An observation of one
    moment: the file put back, the next start's look finds it and the slot
    is its file again."""
    import scanner
    from playback import substitute
    mgr, out = node["mgr"], node["out"]
    first = node["root"] / "NW" / "01.flac"
    first.unlink()
    items = _queue(node, play=True)
    assert scanner.gone_prefixes() == []                    # the folder answers
    assert out.replaced == [["pending", "file"]]            # that slot alone
    assert [it.source["kind"] for it in items] == ["file", "file"]
    assert substitute.unopened() == {node["db_root"] + "/NW/01.flac"}
    # Play pressed on the queue held, the file still gone: still no way in
    out.changed.clear()
    mgr.before_start(out)
    assert _streams(items[0]) and out.changed == []
    first.write_bytes(b"fLaC")                              # put back
    mgr.before_start(out)
    assert items[0].play is None and out.changed == ["rebind"]
    assert substitute.unopened() == frozenset()


def test_a_file_the_look_did_not_find_gives_way_to_another_copy_that_is_here(node):
    twin = node["copy"](0, "twin/01.flac")
    (node["root"] / "NW" / "01.flac").unlink()
    items = _queue(node, play=True)
    assert items[0].play["media_file_id"] == twin and items[1].play is None
    assert node["out"].replaced == [["file", "file"]]


def test_a_play_of_a_file_the_look_did_not_find_where_nothing_streams_is_refused_in_words(node):
    """Its true why: the folder answers, so it is not its disk — the file
    is not there."""
    from playback.manager import NotHere
    from playback.queue import items_for_media_ids
    mgr, out, player = node["mgr"], node["out"], node["player"]
    node["monkeypatch"].setattr(node["streaming"], "is_enabled", lambda: False)
    (node["root"] / "NW" / "01.flac").unlink()
    with pytest.raises(NotHere) as refused:
        mgr.replace_queue(items_for_media_ids(node["files"][:1]), play=True)
    assert player._output_error(refused.value).detail == {
        "reason": "not_here", "why": "away",
        "message": "Night Walks cannot play until its files are back."}
    assert out.replaced == [] and len(mgr.queue) == 0
    # with a slot beside it that opens the queue plays, and the slot is passed
    items = _queue(node, play=True)
    assert out.replaced == [["pending", "file"]]
    # its row says why, as the refusal does
    assert _wait(lambda: ((items[0].play or {}).get("code"), (items[0].play or {}).get("why"))
                 == ("not_here", "away"))


def test_next_and_previous_take_the_look_at_the_slot_they_would_start(node):
    """Next and Previous hand the output a start as a tap on that row does:
    the same one look at the file it would open, before the output is
    asked."""
    mgr, out, player = node["mgr"], node["out"], node["player"]
    items = _queue(node)
    node["monkeypatch"].setattr(mgr, "_latest_status", {"state": "playing", "track_index": 1})
    second = node["root"] / "NW" / "02.flac"
    second.unlink()
    assert player.next_track()["ok"]
    assert _streams(items[1]) and items[0].play is None
    second.write_bytes(b"fLaC")
    mgr.before_start(out, 2)                        # a tap on its row: the file is back
    assert items[1].play is None
    node["monkeypatch"].setattr(mgr, "_latest_status", {"state": "playing", "track_index": 2})
    (node["root"] / "NW" / "01.flac").unlink()
    assert player.previous_track()["ok"]
    assert _streams(items[0]) and items[1].play is None


def test_the_look_before_a_start_never_waits_behind_a_queue_edit_in_flight(node):
    """A play intent waits for no queue edit: what its look found is read
    into the queue once the edit is done — on a thread of its own, as the
    folder's recorded change is — and the Play goes on meanwhile."""
    mgr, out = node["mgr"], node["out"]
    items = _queue(node)
    (node["root"] / "NW" / "01.flac").unlink()
    looked = threading.Event()
    held, release = threading.Event(), threading.Event()

    def edit():                                     # a queue edit still committing
        with mgr._mutate_lock:
            held.set()
            release.wait(10)
    threading.Thread(target=edit, daemon=True).start()
    assert held.wait(5)
    try:
        out.changed.clear()
        threading.Thread(target=lambda: (mgr.before_start(out), looked.set()), daemon=True).start()
        assert looked.wait(5)                       # the Play's look came back with the edit in flight
        # the slot that start opens is read already — never left to a race
        # with the re-read that waits for the edit
        assert _streams(items[0]) and out.changed == []
    finally:
        release.set()
    assert _wait(lambda: out.changed == ["rebind"])     # the output told once the edit was done
    assert _streams(items[0]) and items[1].play is None


def test_a_landing_while_the_start_slot_is_read_ahead_is_kept(node, monkeypatch):
    """The slot a start opens, read ahead of a queue edit in flight, is
    computed and written under the queue's own lock, as every re-read is:
    a stream that lands on it meanwhile lands after, on what was read —
    never to be written over with the pending mark read before it."""
    from playback import substitute
    mgr, out = node["mgr"], node["out"]
    (node["root"] / "NW" / "01.flac").unlink()
    items = _queue(node, play=True)
    assert _wait(lambda: (items[0].play or {}).get("token"))
    landed = {"kind": "proxy", "token": items[0].play["token"], "lane": True}
    substitute.observe({"/a/look/of/before"})       # the next look's observation is another one
    armed, done = [], []
    look, plays = mgr._first_opens, substitute.native_plays

    def looked(*a, **kw):
        moved = look(*a, **kw)
        armed.append(True)
        return moved

    def read(its, output_id, endpoint_id):
        ways = plays(its, output_id, endpoint_id)
        if armed and not done:                      # the read ahead: a landing arrives now
            done.append(True)
            landing = threading.Thread(
                target=lambda: mgr.queue.swap_play(items[0], items[0].play, landed))
            landing.start()
            landing.join(0.5)
        return ways
    monkeypatch.setattr(mgr, "_first_opens", looked)
    monkeypatch.setattr(substitute, "native_plays", read)
    held, release = threading.Event(), threading.Event()

    def edit():                                     # a queue edit still committing
        with mgr._mutate_lock:
            held.set()
            release.wait(10)
    threading.Thread(target=edit, daemon=True).start()
    assert held.wait(5)
    try:
        mgr.before_start(out)
    finally:
        release.set()
    assert done and _wait(lambda: not [t for t in threading.enumerate() if t.name == "queue-reach"])
    assert items[0].play == landed


def test_a_re_read_during_a_look_never_hands_the_missing_file_back(node, monkeypatch):
    """A look's observation replaces the last one in one step, when the look
    ends: a re-read of the ways in on another thread while a look is under
    way (a file event, the folder's record) still reads under the last
    observation, and the slot keeps its stream — never its missing file."""
    from playback import manager as manager_mod
    mgr, out = node["mgr"], node["out"]
    (node["root"] / "NW" / "01.flac").unlink()
    items = _queue(node, play=True)
    assert _streams(items[0])
    seen, real = [], manager_mod._opens

    def opens(path):                                # mid-look: a re-read on another thread
        other = threading.Thread(target=mgr.reach_changed)
        other.start()
        other.join(5)
        seen.append(items[0].play)
        return real(path)
    monkeypatch.setattr(manager_mod, "_opens", opens)
    mgr.before_start(out)
    assert seen and all(play is not None for play in seen)
    assert _streams(items[0])


def test_a_stat_in_flight_for_a_path_is_the_one_every_asker_waits_on(node, monkeypatch):
    """On a mount that hangs, every Play, Next or tap of the same file waits
    on the one stat already in flight — within the same patience — and
    starts no thread of its own; the stat back, the next asker asks anew."""
    import os
    from playback import manager as manager_mod
    hang, real, asked = threading.Event(), os.stat, []
    path = str(node["root"] / "NW" / "01.flac")

    def stat(p, *a, **kw):
        if str(p) == path:
            asked.append(p)
            hang.wait(10)
        return real(p, *a, **kw)
    monkeypatch.setattr(manager_mod, "_STAT_PATIENCE_S", 0.2)
    monkeypatch.setattr(os, "stat", stat)
    try:
        assert [manager_mod._opens(path) for _ in range(3)] == [False, False, False]
        assert len(asked) == 1
        assert len([t for t in threading.enumerate() if t.name == "queue-look"]) == 1
    finally:
        hang.set()
    assert _wait(lambda: path not in manager_mod._stats)
    assert manager_mod._opens(path) is True and len(asked) == 2


def test_a_refusal_names_no_disk_for_slots_that_were_never_files(node):
    """A held queue of streamed tracks, streaming off, the folder gone: none
    of it is "on a disk that is not connected" — the manager refuses nothing
    in those words, and the rows say what streaming says."""
    mgr, out, player = node["mgr"], node["out"], node["player"]
    queries = player._phantom_track_queries(node["phantoms"][:2])
    mgr.replace_queue(player.queue_mod.items_for_streamed(
        [queries[t] for t in node["phantoms"][:2]]), play=False)
    node["monkeypatch"].setattr(node["streaming"], "is_enabled", lambda: False)
    node["gone"](True)
    mgr.reach_changed()
    mgr.before_start(out)                                   # no NotHere


def test_no_provider_answering_right_now_is_not_a_node_that_streams_nothing(node):
    """Every provider aside or cooling down is "no provider answers right
    now" — the play routes' own words at a tap — never "cannot play until
    its disk is connected": this node streams."""
    from fastapi import HTTPException
    from playback import substitute
    player = node["player"]
    node["monkeypatch"].setattr(node["streaming"], "providers_preferred", lambda: [])
    assert substitute.streams()
    node["gone"](True)
    for route in _every_route(node):
        with pytest.raises(HTTPException) as refused:
            route()
        assert refused.value.status_code == 503
        assert "No streaming provider is available right now" in str(refused.value.detail)
    assert len(node["mgr"].queue) == 0
    node["monkeypatch"].setattr(node["streaming"], "providers_registered", lambda: False)
    assert not substitute.streams()


def test_a_start_that_only_waits_for_a_stream_goes_on_its_file_when_the_folder_is_back(node):
    """A slot nothing sounds yet is not held: the folder back, the slot the
    start waits on is its file at once, and the waiting start is told as a
    landing tells it."""
    mgr, out = node["mgr"], node["out"]
    items = _queue(node)
    node["gone"](True)
    mgr.reach_changed()
    assert _streams(items[0])
    node["monkeypatch"].setattr(mgr, "_latest_status", {"state": "loading", "track_index": 1})
    told = []
    node["monkeypatch"].setattr(out, "slot_ready", lambda index, item=None: told.append((index, item)))
    node["gone"](False)
    mgr.reach_changed()
    assert items[0].play is None and mgr._reach_owed is None
    assert told == [(1, items[0])]


def test_an_expected_refusal_of_play_tracks_is_no_error_in_the_log(node, caplog):
    import logging
    from fastapi import HTTPException
    player = node["player"]
    node["monkeypatch"].setattr(node["streaming"], "is_enabled", lambda: False)
    _pull_the_disk(node)
    with caplog.at_level(logging.ERROR), pytest.raises(HTTPException) as refused:
        player.play_tracks(player.PlayTracksRequest(track_ids=node["files"]))
    assert refused.value.status_code == 409 and refused.value.detail["reason"] == "not_here"
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR] == []


def test_the_look_waits_for_a_stat_no_longer_than_its_patience(node, monkeypatch):
    """A dead network mount answers a stat never: the Play's worker waits
    for it no longer than the look's patience, and a file that did not
    answer does not open now."""
    import os
    from playback import manager as manager_mod
    hang, real = threading.Event(), os.stat
    monkeypatch.setattr(manager_mod, "_STAT_PATIENCE_S", 0.2)
    monkeypatch.setattr(os, "stat", lambda p, *a, **kw: (hang.wait(10), real(p, *a, **kw))[1]
                        if str(p).endswith("01.flac") else real(p, *a, **kw))
    try:
        t0 = time.monotonic()
        assert manager_mod._opens(str(node["root"] / "NW" / "01.flac")) is False
        assert time.monotonic() - t0 < 2
        assert manager_mod._opens(str(node["root"] / "NW" / "02.flac")) is True
    finally:
        hang.set()


def test_a_first_file_that_is_there_asks_the_folder_nothing(node, monkeypatch):
    import scanner
    monkeypatch.setattr(scanner, "check_folder", lambda wait=False: pytest.fail("the folder was looked at"))
    items = _queue(node, play=True)
    assert node["out"].replaced == [["file", "file"]] and all(it.play is None for it in items)


def test_a_copy_set_away_streams_its_queued_slot_and_is_its_file_again_when_back(node):
    """Q4, in the queue: a slot on an away file with no other copy keeps
    its file as its origin and streams; the copy back, it is that file."""
    mgr, out, cur = node["mgr"], node["out"], node["cur"]
    first, second = _queue(node)
    out.changed.clear()
    cur.execute("UPDATE album_variants SET absent_since = now() WHERE id = %s", (node["variant"],))
    mgr.rebind_files()                              # as the Rescan's notify does
    assert _streams(first) and _streams(second)
    assert (first.media_file_id, first.source["kind"], out.changed) == (node["files"][0], "file", ["rebind"])
    cur.execute("UPDATE album_variants SET absent_since = NULL WHERE id = %s", (node["variant"],))
    mgr.rebind_files()
    assert first.play is None and second.play is None
    assert out.changed == ["rebind", "rebind"]
    mgr.rebind_files()                              # nothing moved: nothing told
    assert out.changed == ["rebind", "rebind"]


def test_a_slot_back_on_its_file_shows_the_files_length_not_the_excerpts(node):
    """A stream's landing writes its own length on the slot (an excerpt's
    30 s): the file back, the slot shows the file's."""
    mgr = node["mgr"]
    first, _second = _queue(node)
    node["gone"](True)
    mgr.reach_changed()
    assert _streams(first)
    first.duration_seconds, first.excerpt = 30.0, True
    node["gone"](False)
    mgr.reach_changed()
    assert (first.play, first.duration_seconds, first.excerpt) == (None, 200.0, False)


def test_the_start_looks_at_the_folder_before_the_first_queue_is_read(node, monkeypatch):
    """Q3: the list is this process's observation, made again at each start
    before the persisted queue is restored and its ways in read."""
    import scanner
    from dataclasses import asdict
    from playback.manager import PlaybackManager
    from playback.queue import items_for_media_ids
    from routers import settings as settings_router
    stored = {"items": [asdict(it) for it in items_for_media_ids(node["files"])]}
    monkeypatch.setattr(settings_router, "_read",
                        lambda key: {"player.queue": stored, "output.type": "browser"}.get(key))
    monkeypatch.setattr(PlaybackManager, "_schedule_persist", lambda self: None)
    _pull_the_disk(node)
    fresh = PlaybackManager()
    known = []
    restore = fresh._restore_persisted_queue

    def restoring():
        known.append(scanner.gone_prefixes())
        restore()
    monkeypatch.setattr(fresh, "_restore_persisted_queue", restoring)
    fresh.init_from_settings()
    try:
        assert known == [[node["db_root"] + "/"]]
        assert [it.source["kind"] for it in fresh.queue.snapshot()] == ["file", "file"]
        assert all(_streams(it) for it in fresh.queue.snapshot())
    finally:
        fresh.activate(None)


def test_the_folder_watchers_loss_is_a_look_at_the_folder(node, monkeypatch):
    import library_watch
    import scanner
    looked = []
    monkeypatch.setattr(scanner, "check_folder", lambda wait=False: looked.append(wait))
    monkeypatch.setattr(library_watch, "_source", None)
    monkeypatch.setattr(library_watch, "_state", dict(library_watch._state))
    library_watch._lost("unmounted")
    assert looked == [False]
    library_watch._lost("the watch limit", library_watch.NO_EVENTS)      # no word about the folder
    assert looked == [False]


def test_the_pages_hear_the_recorded_answer_change_whatever_the_notice_said(node, monkeypatch):
    """The page's stream says what is reachable as a key — at connect and
    when the recorded answer changes. A notices reader that gave up on a
    slow look shows the notice while nothing is recorded: no word then; the
    look landing "gone" later flips no notice, and is the word the open
    album reads its payload again on (it went unheard before)."""
    import asyncio
    import json
    import scanner
    from playback import manager as manager_mod
    from routers import settings as settings_router
    player = node["player"]
    monkeypatch.setattr(settings_router, "_notices_state", lambda: {})
    monkeypatch.setattr(settings_router, "identity_event", lambda: {"status": None})

    async def run():
        stream = (await player.events_stream(tab=None)).body_iterator

        async def said(kind):
            while True:
                msg = json.loads((await asyncio.wait_for(stream.__anext__(), 10))[6:])
                if msg["t"] == kind:
                    return msg["d"]
        assert await said("reach") == {"key": ""}
        await said("identity")
        scanner._shown = True                       # a reader gave up: the notice shows
        assert manager_mod.reach_key() == ""
        scanner._record(True, scanner._next_ticket(), wake=False)      # the look lands: gone
        gone = await said("reach")
        assert gone["key"] and node["db_root"] not in gone["key"]
        scanner.folder_seen(False)
        assert await said("reach") == {"key": ""}
        reading = asyncio.ensure_future(stream.__anext__())
        await asyncio.sleep(0.05)
        reading.cancel()
        try:
            await reading
        except (StopAsyncIteration, asyncio.CancelledError):
            pass

    asyncio.run(run())


# -- the routes (§3.4) ------------------------------------------------------------


def _kinds(mgr) -> list:
    return [(it.source["kind"], it.title) for it in mgr.queue.snapshot()]


def _album_ref(node):
    player = node["player"]
    return player.QueueEntitiesRequest(items=[player.EntityRef(kind="album", id=node["owned"])])


def test_an_album_half_out_is_read_per_track(node):
    """All-or-nothing before: an album with one track's copy elsewhere and
    its folder gone queued the missing file beside it."""
    player, mgr = node["player"], node["mgr"]
    other = node["copy"](0, "NW/01.flac")
    node["gone"](True)
    segments, missing, left = player._entity_segments(_album_ref(node).items)
    assert [(kind, len(rows)) for kind, rows in segments] == [("owned", 1), ("phantom", 1)]
    assert [(r["id"], r["location"]) for r in segments[0][1]] == [(other, "local")]
    assert (segments[1][1][0].track_id, segments[1][1][0].album_id) == (node["tracks"][1], node["owned"])
    assert (missing, left) == ([], [])
    answer = player.play_entities(_album_ref(node))
    assert (answer["queued"], answer["streaming"], answer["not_streamable"]) == (1, 1, [])
    assert _kinds(mgr) == [("file", "Owned 1"), ("track", "Owned 2")]
    assert mgr.queue.item_at(2).album_id == node["owned"]


def test_no_route_queues_a_file_that_is_not_here(node):
    """play-tracks, queue-tracks and play-track take file ids — the pages'
    own: a file not here is queued as its track, through the one split; a
    row of rank 3 never reaches the queue as a file."""
    player, mgr = node["player"], node["mgr"]
    node["gone"](True)
    rows = player._album_rows(node["owned"])
    assert [(r["id"], r["why"]) for r in rows] == [(None, "disk"), (None, "disk")]
    segments, _left = player._reach_segments(rows)
    assert [kind for kind, _rows in segments] == ["phantom"]

    answer = player.play_tracks(player.PlayTracksRequest(track_ids=node["files"], origin="album",
                                                         origin_album_id=node["owned"]))
    assert (answer["count"], answer["not_streamable"]) == (2, [])
    assert _kinds(mgr) == [("track", "Owned 1"), ("track", "Owned 2")]
    assert {it.album_id for it in mgr.queue.snapshot()} == {node["owned"]}
    player.queue_tracks(player.QueueTracksRequest(track_ids=node["files"][:1], position="end"))
    assert _kinds(mgr)[2] == ("track", "Owned 1")
    player.play_track(player.PlayTrackRequest(track_id=node["files"][1]))
    assert _kinds(mgr) == [("track", "Owned 2")]
    player.play_album(player.PlayAlbumRequest(album_id=node["owned"]))
    assert _kinds(mgr) == [("track", "Owned 1"), ("track", "Owned 2")]

    # the folder back: the same routes queue the files, and a slot queued as
    # the track while it was out opens the local copy
    node["gone"](False)
    assert [r["id"] for r in player._album_rows(node["owned"])] == node["files"]
    mgr.reach_changed()
    assert [it.opener().get("media_file_id") for it in mgr.queue.snapshot()] == node["files"]
    player.play_tracks(player.PlayTracksRequest(track_ids=node["files"]))
    assert _kinds(mgr) == [("file", "Owned 1"), ("file", "Owned 2")]


def test_an_away_copy_is_read_by_the_routes_as_a_copy_on_a_disk_that_is_out(node):
    player, mgr = node["player"], node["mgr"]
    node["cur"].execute("UPDATE album_variants SET absent_since = now() WHERE id = %s",
                        (node["variant"],))
    assert [(r["id"], r["why"]) for r in player._album_rows(node["owned"])] == [(None, "away")] * 2
    player.play_tracks(player.PlayTracksRequest(track_ids=node["files"]))
    assert _kinds(mgr) == [("track", "Owned 1"), ("track", "Owned 2")]
    other = node["copy"](0, "NW/01.flac")     # a present copy stands in
    player.play_tracks(player.PlayTracksRequest(track_ids=node["files"]))
    assert [(it.source["kind"], it.media_file_id) for it in mgr.queue.snapshot()] == [
        ("file", other), ("track", None)]


def _refusals(node, calls) -> list:
    from fastapi import HTTPException
    out = []
    for call in calls:
        with pytest.raises(HTTPException) as refused:
            call()
        out.append((refused.value.status_code, refused.value.detail))
    return out


def _every_route(node) -> list:
    player = node["player"]
    return [
        lambda: player.play_track(player.PlayTrackRequest(track_id=node["files"][0])),
        lambda: player.play_tracks(player.PlayTracksRequest(track_ids=node["files"])),
        lambda: player.queue_tracks(player.QueueTracksRequest(track_ids=node["files"], position="end")),
        lambda: player.play_entities(_album_ref(node)),
        lambda: player.queue_entities(_album_ref(node)),
        lambda: player.play_album(player.PlayAlbumRequest(album_id=node["owned"])),
    ]


def test_where_nothing_streams_the_routes_refuse_before_the_queue_or_the_output(node):
    """A Nano node, the streaming library off, no provider: 409 not_here
    with why and the sentence, nothing queued, the output never asked."""
    mgr, out = node["mgr"], node["out"]
    old = _queue(node)
    out.replaced.clear()
    out.changed.clear()
    node["monkeypatch"].setattr(node["streaming"], "is_enabled", lambda: False)
    node["gone"](True)
    disk = {"reason": "not_here", "why": "disk",
            "message": "Night Walks cannot play until its disk is connected."}
    assert _refusals(node, _every_route(node)) == [(409, disk)] * 6
    assert mgr.queue.snapshot() == old and out.replaced == [] and out.changed == []

    node["gone"](False)
    node["cur"].execute("UPDATE album_variants SET absent_since = now() WHERE id = %s",
                        (node["variant"],))
    away = {"reason": "not_here", "why": "away",
            "message": "Night Walks cannot play until its files are back."}
    assert _refusals(node, _every_route(node)) == [(409, away)] * 6
    assert mgr.queue.snapshot() == old and out.replaced == []
    # no provider registered is the same as the library off
    node["monkeypatch"].setattr(node["streaming"], "is_enabled", lambda: True)
    node["monkeypatch"].setattr(node["streaming"], "providers_registered", lambda: False)
    assert _refusals(node, _every_route(node)[:1]) == [(409, away)]


def test_a_mix_keeps_its_files_and_leaves_the_rest_out_with_the_reason(node):
    player, mgr = node["player"], node["mgr"]
    other = node["copy"](0, "NW/01.flac")
    node["monkeypatch"].setattr(node["streaming"], "is_enabled", lambda: False)
    node["gone"](True)
    left = [{"track_id": node["tracks"][1], "title": "Owned 2", "reason": "not_here", "why": "disk"}]
    answer = player.play_entities(_album_ref(node))
    assert (answer["queued"], answer["streaming"], answer["not_streamable"]) == (1, 0, left)
    assert [(it.source["kind"], it.media_file_id) for it in mgr.queue.snapshot()] == [("file", other)]
    answer = player.play_tracks(player.PlayTracksRequest(track_ids=node["files"]))
    assert (answer["count"], answer["not_streamable"]) == (1, left)
    answer = player.queue_tracks(player.QueueTracksRequest(track_ids=node["files"], position="end"))
    assert (answer["count"], answer["not_streamable"]) == (1, left)
    assert len(mgr.queue) == 2


def test_a_never_owned_album_with_streaming_off_is_the_streaming_refusal_as_before(node):
    """not_here is the owner's own music alone: a phantom with streaming
    off is still told as streaming being off."""
    player = node["player"]
    node["monkeypatch"].setattr(node["streaming"], "is_enabled", lambda: False)
    node["gone"](True)
    ref = player.QueueEntitiesRequest(items=[player.EntityRef(kind="album", id=node["streamed"])])
    (status, detail), = _refusals(node, [lambda: player.play_entities(ref)])
    assert status == 503 and detail["reason"] == "streaming"


def test_a_history_card_of_an_album_that_is_out_streams_or_is_refused(node):
    player, mgr, cur = node["player"], node["mgr"], node["cur"]
    cur.execute("INSERT INTO listening_sessions (origin, origin_album_id, track_count, ended_at) "
                "VALUES ('album', %s, 2, now()) RETURNING id::text", (node["owned"],))
    session = cur.fetchone()[0]
    for n, (track, file) in enumerate(zip(node["tracks"], node["files"])):
        cur.execute("INSERT INTO session_tracks (session_id, position, track_id, media_file_id, album_id) "
                    "VALUES (%s, %s, %s, %s, %s)", (session, n, track, file, node["owned"]))
    player.play_session(player.PlaySessionRequest(session_id=session))
    assert _kinds(mgr) == [("file", "Owned 1"), ("file", "Owned 2")]
    node["gone"](True)
    answer = player.play_session(player.PlaySessionRequest(session_id=session))
    assert (answer["queued"], answer["streaming"], answer["missing"]) == (0, 2, 0)
    assert _kinds(mgr) == [("track", "Owned 1"), ("track", "Owned 2")]
    old = mgr.queue.snapshot()
    node["monkeypatch"].setattr(node["streaming"], "is_enabled", lambda: False)
    disk = {"reason": "not_here", "why": "disk",
            "message": "Night Walks cannot play until its disk is connected."}
    assert _refusals(node, [
        lambda: player.play_session(player.PlaySessionRequest(session_id=session)),
        lambda: player.queue_session(player.QueueSessionRequest(session_id=session, position="end")),
    ]) == [(409, disk)] * 2
    assert mgr.queue.snapshot() == old


def test_the_album_payload_says_what_is_here_and_whether_this_node_streams_it(node):
    """Per track `here`, and the album's `away`: null, or why and whether
    this node streams — `away_part` where only some tracks are out."""
    from routers import albums
    here = albums.get_album(node["owned"], variant_id=None)
    assert [t["here"] for t in here["tracks"]] == [True, True]
    assert (here["away"], here["away_part"], here["is_owned"]) == (None, None, True)

    node["gone"](True)
    out = albums.get_album(node["owned"], variant_id=None)
    assert [(t["here"], t["why"]) for t in out["tracks"]] == [(False, "disk"), (False, "disk")]
    assert (out["away"], out["away_part"]) == ({"why": "disk", "streams": True}, None)
    assert (out["is_owned"], out["quality"]) == (True, here["quality"])      # still his, his copy's badge
    assert [t["media_file_id"] for t in out["tracks"]] == node["files"]
    node["monkeypatch"].setattr(node["streaming"], "is_enabled", lambda: False)
    assert albums.get_album(node["owned"], variant_id=None)["away"] == {"why": "disk", "streams": False}

    other = node["copy"](0, "NW/01.flac")
    part = albums.get_album(node["owned"], variant_id=None)
    assert [(t["media_file_id"], t["here"]) for t in part["tracks"]] == [(other, True), (node["files"][1], False)]
    assert (part["away"], part["away_part"]) == (None, {"why": "disk", "streams": False})

    node["gone"](False)
    node["cur"].execute("UPDATE album_variants SET absent_since = now() WHERE id = %s",
                        (node["variant"],))
    away = albums.get_album(node["owned"], variant_id=None)
    assert [(t["here"], t["why"]) for t in away["tracks"]] == [(True, None), (False, "away")]
    assert away["away_part"] == {"why": "away", "streams": False}


def test_the_album_payloads_variants_say_which_copy_is_away_and_which_is_not_connected(node):
    """PI-NODE-2E H2: each variant the picker offers carries `away` (its
    copy set aside by a Rescan) and `here` (a file of it opens now — the
    album's own rule, sql_queries.album_away, over that copy alone)."""
    from routers import albums
    cur = node["cur"]
    cur.execute("SELECT album_variant_id FROM media_files WHERE id = %s",
                (node["copy"](0, "Elsewhere/01.flac"),))
    other = cur.fetchone()[0]

    def variants() -> dict:
        return {v["variant_id"]: (v["away"], v["here"])
                for v in albums.get_album(node["owned"], variant_id=None)["variants"]}

    assert variants() == {node["variant"]: (False, True), other: (False, True)}
    node["gone"](True)                                 # the music folder's disk is out
    assert variants() == {node["variant"]: (False, False), other: (False, True)}
    node["gone"](False)
    cur.execute("UPDATE album_variants SET absent_since = now() WHERE id = %s", (node["variant"],))
    assert variants() == {node["variant"]: (True, False), other: (False, True)}


def test_a_sessions_tracks_say_what_is_here_by_the_album_pages_rule(node):
    """PI-NODE-2E H5: a Listening history session's owned tracks carry
    `here` and `why` as the album payload's rows do — the track's best copy
    across every copy — and the session whether this node streams; a track
    never owned says neither."""
    from routers import home
    cur = node["cur"]
    cur.execute("INSERT INTO listening_sessions (origin, track_count, ended_at) "
                "VALUES ('mix', 3, now()) RETURNING id::text")
    session = cur.fetchone()[0]
    slots = [(node["tracks"][0], node["files"][0], node["owned"]),
             (node["tracks"][1], node["files"][1], node["owned"]),
             (node["phantoms"][0], None, node["streamed"])]
    for n, (track, file, album) in enumerate(slots):
        cur.execute("INSERT INTO session_tracks (session_id, position, track_id, media_file_id, album_id) "
                    "VALUES (%s, %s, %s, %s, %s)", (session, n, track, file, album))

    def said() -> tuple:
        d = home.get_listening_session(session)
        return [(t["here"], t["why"]) for t in d["tracks"]], d["streams"]

    assert said() == ([(True, None), (True, None), (None, None)], True)
    node["gone"](True)
    assert said() == ([(False, "disk"), (False, "disk"), (None, None)], True)
    node["copy"](0, "Elsewhere/01.flac")               # another copy of the first track opens
    assert said()[0][:2] == [(True, None), (False, "disk")]
    node["gone"](False)
    cur.execute("UPDATE album_variants SET absent_since = now() WHERE id = %s", (node["variant"],))
    assert said()[0][:2] == [(True, None), (False, "away")]
    node["monkeypatch"].setattr(node["streaming"], "is_enabled", lambda: False)
    assert said()[1] is False


def test_the_pages_ask_whether_this_node_streams(node):
    player = node["player"]
    assert player.reach() == {"streams": True}
    node["monkeypatch"].setattr(node["streaming"], "providers_preferred", lambda: [])
    assert player.reach() == {"streams": True}       # none answers right now: it streams all the same
    node["monkeypatch"].setattr(node["streaming"], "providers_registered", lambda: False)
    assert player.reach() == {"streams": False}


# -- the shelves' tiles (§3.4, §4.5, Q5) -------------------------------------------


def _tiles(node) -> dict:
    """The owned album's mark on each Home shelf that shows owned albums:
    New in my collection, the Recommendations ranking, the cold start and
    the random fill."""
    from routers import home
    node["monkeypatch"].setattr(home._ranking, "album_ids", lambda: [node["owned"]])
    shelves = {
        "new": home.get_new_in_library(limit=20, before=None, before_arrived=None, before_id=None)["albums"],
        "ranked": home.get_recommendations(limit=20)["albums"],
        "cold": home._cold_start_albums(20),
        "random": home._random_fill(20, set()),
    }
    return {name: next(t for t in tiles if t["id"] == node["owned"])["not_here"]
            for name, tiles in shelves.items()}


def _page_says(node):
    from routers import albums
    away = albums.get_album(node["owned"], variant_id=None)["away"]
    return away and away["why"]


def test_a_tile_says_of_an_album_that_is_out_what_its_page_says(node):
    """An album whose disk is out stays on its shelves, marked, and an away
    copy too (Q5) — by the album payload's own definition of away
    (sql_queries.album_away), whatever copy the shelf orders the album by."""
    from db_pool import db_query_one
    from sql_queries import album_away
    cur = node["cur"]
    everywhere = dict.fromkeys(("new", "ranked", "cold", "random"))
    assert _tiles(node) == everywhere and _page_says(node) is None

    node["gone"](True)
    assert _tiles(node) == {k: "disk" for k in everywhere} and _page_says(node) == "disk"

    # the shelf's key copy is the newest one — here the copy on the disk that
    # is out — while an older copy elsewhere opens: the album is not away
    older = node["copy"](0, "NW/01.flac")
    cur.execute("UPDATE album_variants SET entered_at = now() - interval '1 day' WHERE id = "
                "(SELECT album_variant_id FROM media_files WHERE id = %s)", (older,))
    assert _tiles(node) == everywhere and _page_says(node) is None

    node["gone"](False)
    cur.execute("UPDATE album_variants SET absent_since = now() WHERE album_id = %s", (node["owned"],))
    assert _tiles(node) == {k: "away" for k in everywhere} and _page_says(node) == "away"

    # an album he does not own is not "away"
    assert db_query_one(f"SELECT {album_away('%(id)s::uuid')} AS why",
                        {"id": node["streamed"], "gone": []})["why"] is None


# -- the threat (§3.5): his own album through the demo channel --------------------


def test_a_shelf_asks_about_the_albums_its_tiles_show_by_the_tiles_one_definition(node):
    """The marks of the tiles on the page, whatever page brought them: asked
    by album id when what is reachable changes — the same definition the
    tiles were drawn by (album_away), a not-owned album never among them."""
    from routers import home

    def away() -> dict:
        return home.albums_away(home.AlbumsShown(ids=[node["owned"], node["streamed"]]))
    assert away() == {"not_here": {}}
    node["gone"](True)
    assert away() == {"not_here": {node["owned"]: "disk"}}
    assert _tiles(node)["new"] == "disk"
    node["gone"](False)
    node["cur"].execute("UPDATE album_variants SET absent_since = now() WHERE id = %s",
                        (node["variant"],))
    assert away() == {"not_here": {node["owned"]: "away"}}


def test_an_owned_track_streamed_for_want_of_its_file_spends_the_demo_listen_like_any_other(node):
    """No exemption for a track "owned" on a disk that is out: the node has
    rows, not bytes. Its one full listen on the demo channel is spent past
    90 %, and the next resolve leaves that channel out; the provider was
    asked by title, artist and length — nothing says the album is owned."""
    from playback.base import PlaybackStatus
    from streaming import demo
    player, mgr, out, prov = node["player"], node["mgr"], node["out"], node["prov"]
    prov.manifest = ProviderManifest(id="catalog", name="Catalog", kind="direct_url",
                                     lossless=False, demo_limited=True)
    node["monkeypatch"].setattr(node["streaming"], "get_provider", lambda pid: prov)
    seen = []
    resolve = prov._resolve
    prov._resolve = lambda query: (seen.append(query), resolve(query))[1]
    node["gone"](True)
    player.play_tracks(player.PlayTracksRequest(track_ids=node["files"][:1]))
    prov.land(node["tracks"][0])
    first = mgr.queue.item_at(1)
    assert _wait(lambda: first.stream_token)
    assert demo._demo_provider_id(first) == "catalog"
    for position in (10.0, 185.0):
        demo.status_observer({"state": "playing", "position": position, "length": 200.0}, first)
    assert demo.consumed([node["tracks"][0]]) == {node["tracks"][0]}
    assert not demo.admits(prov, node["tracks"][0], demo.consumed([node["tracks"][0]]))
    demo.status_observer({"state": "stopped", "position": 0.0, "length": 200.0}, first)
    query = seen[0]
    assert (query.title, query.artist, query.duration) == ("Owned 1", "Hidden Orchestra", 200.0)
    assert query.media_file_id is None              # no word of a file of his


# -- F11: what a refused scan says (§2.7, §9.2) ----------------------------------


def test_a_refused_scan_says_what_brings_the_folder_back_on_this_node(monkeypatch):
    """One sentence said "a Docker node sees it again only after a restart"
    on every node: true only where the folder is the .env line's bind."""
    import claude_code
    import library_root
    import scanner
    monkeypatch.setattr(library_root, "picks", lambda: True)
    monkeypatch.setattr(claude_code, "is_launcher_mode", lambda: True)
    assert scanner.unreachable_words() == (
        "The music folder is not connected, so nothing was scanned. "
        "Connect its disk — Sautium scans it by itself.")
    monkeypatch.setattr(library_root, "picks", lambda: False)
    assert scanner.unreachable_words() == (
        "The music folder is empty or not connected, so nothing was scanned. "
        "Reconnect it and scan again.")
    monkeypatch.setattr(claude_code, "is_launcher_mode", lambda: False)
    assert scanner.unreachable_words() == (
        "The music folder is empty or not mounted, so nothing was scanned. "
        "Reconnect it and restart Sautium: this node sees a disk only as it was at its start.")
    with pytest.raises(scanner.LibraryUnreachable, match="restart Sautium"):
        monkeypatch.setattr(scanner, "library_unreachable", lambda wake=True: True)
        scanner.refuse_if_unreachable()
