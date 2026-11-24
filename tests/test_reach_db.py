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
from streaming.base import (FetchedAudio, ProviderManifest, ResolvedSource,  # noqa: E402
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

    def copy(n: int, path: str, *, sample_rate=44100) -> int:
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
           "gone": gone, "copy": copy, "streaming": streaming_service, "monkeypatch": monkeypatch}
    prov.land(*phantoms, *tracks)
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
    other = node["copy"](0, "/elsewhere/NW/01.flac")
    it = items_for_media_ids(node["files"][:1])[0]
    assert native_plays([it], "browser", None) == [None]
    node["gone"](True)
    play = native_plays([it], "browser", None)[0]
    assert (play["kind"], play["media_file_id"], play["path"]) == ("file", other, "/elsewhere/NW/01.flac")
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
    other = node["copy"](0, "/elsewhere/NW/01.flac")
    assert native_plays([it], "browser", None)[0]["media_file_id"] == other
    # the track itself (a slot queued while the copy was away) reads the same
    from playback.queue import QueueItem
    track = QueueItem(track_id=node["tracks"][1], media_file_id=None, source={"kind": "track"})
    assert native_plays([track], "browser", None) == [{"kind": "pending"}]


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
    hires = node["copy"](0, "/elsewhere/hi/01.flac", sample_rate=96000)
    away = node["copy"](0, "/elsewhere/gone/01.flac", sample_rate=192000)
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
                    {"ids": [node["tracks"][0]], "hqp_id": None, "gone": []})
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
