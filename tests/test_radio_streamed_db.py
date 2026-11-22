"""Radio's switch and its rule of the whole track (QUEUE-STREAM-DESIGN §6,
§12 Q11/Q12; B-121): radio — and Play similar — mixes in tracks the owner
does not own only while his switch "Radio plays tracks you don't own"
(`radio.streamed`, off by default) is on, a streamed candidate enters a
batch only when its chain plays it whole (a full-length head: a full
provider, or the demo channel while the track's one listen is unspent), the
batch keeps its length with owned tracks, and a radio slot never falls back
to the 30 s excerpt — a full link that fails is a failed slot, walked past.
Against a real PostgreSQL — a throwaway database built from the migrations —
with providers on no network; the similarity scorer (track_similarity, its
own tests) is replaced by the pool each test names."""

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
from streaming.base import (FetchedAudio, ProviderError, ProviderManifest,  # noqa: E402
                            ResolvedSource, StreamProvider)
from streaming.proxy import MediaProxy  # noqa: E402


class _Output(PlayerBackend):
    id = "browser"
    label = "stub"

    def start(self): pass
    def shutdown(self): pass
    def capabilities(self): return Capabilities()
    def play(self): return True
    def pause(self): return True
    def stop(self): return True
    def next(self): return True
    def previous(self): return True
    def select(self, index, track_id=None): return True


class _Provider(StreamProvider):
    """Resolves the tracks it is told it has, at their catalog length; a
    download serves at once, or fails where it is told to."""
    resolve_workers = 2

    def __init__(self, pid, *, excerpt=False, demo=False):
        self.manifest = ProviderManifest(id=pid, name=pid, kind="direct_url", lossless=not excerpt,
                                         excerpt=excerpt, demo_limited=demo)
        self.has: set = set()
        self.fails: set = set()
        self.resolved: list = []
        self.downloads: list = []

    def fetch(self, query):
        return self._download(self._resolve(query).source_id)

    def _resolve(self, query):
        self.resolved.append(query.track_id)
        if query.track_id not in self.has:
            return None
        return ResolvedSource(source_id=query.track_id, duration=query.duration)

    def _download(self, sid):
        self.downloads.append(sid)
        if sid in self.fails:
            raise ProviderError(f"{self.manifest.id}: cut off")
        return FetchedAudio(data=b"fLaC" + b"\x00" * 64, mime="audio/flac",
                            lossless=not self.manifest.excerpt, excerpt=self.manifest.excerpt,
                            seconds=30.0 if self.manifest.excerpt else None)


def _wait(pred, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


@pytest.fixture
def node(scratch_dsn, monkeypatch):
    """Twelve owned tracks, eight streamed ones (an album of the catalog),
    one artist each; a full-length provider, the demo channel and the
    excerpt provider, best first; a manager on an output that takes
    everything, its lane on a media proxy (no port)."""
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
    cur.execute("TRUNCATE tracks, albums, artists, listening_sessions, demo_plays, "
                "embedding_models CASCADE")
    cur.execute("DELETE FROM user_settings WHERE key IN ('radio.streamed', 'discovery.phantom_layer')")
    owned_album, streamed_album = str(uuid.uuid4()), str(uuid.uuid4())
    cur.execute("INSERT INTO albums (id, title) VALUES (%s, 'Mine'), (%s, 'Theirs')",
                (owned_album, streamed_album))
    cur.execute("INSERT INTO album_variants (album_id, directory_path) VALUES (%s, 'E:/Music/M') "
                "RETURNING id", (owned_album,))
    variant = cur.fetchone()[0]

    def track(title, n):
        a, t = str(uuid.uuid4()), str(uuid.uuid4())
        cur.execute("INSERT INTO artists (id, name) VALUES (%s, %s)", (a, f"Artist {title}"))
        cur.execute("INSERT INTO tracks (id, title) VALUES (%s, %s)", (t, title))
        cur.execute("INSERT INTO track_artists (track_id, artist_id) VALUES (%s, %s)", (t, a))
        return t, f"Artist {title}"

    owned = []
    for i in range(1, 13):
        t, artist = track(f"Owned {i}", i)
        cur.execute("INSERT INTO media_files (track_id, album_variant_id, file_path, sample_rate, "
                    "bit_depth, duration_seconds, track_number) VALUES (%s, %s, %s, 44100, 16, 200, %s) "
                    "RETURNING id", (t, variant, f"E:/Music/M/{i:02}.flac", i))
        owned.append({"track_id": t, "artist": artist, "is_owned": True, "playable": True,
                      "file_id": cur.fetchone()[0], "location": "local"})
    streamed = []
    for i in range(1, 9):
        t, artist = track(f"Stream {i}", i)
        cur.execute("INSERT INTO album_tracks (album_id, track_id, position, length_ms) "
                    "VALUES (%s, %s, %s, 200000)", (streamed_album, t, i))
        streamed.append({"track_id": t, "artist": artist, "is_owned": False, "playable": False,
                         "file_id": None, "location": None})
    full, demo_ch, clip = _Provider("full"), _Provider("demo", demo=True), _Provider("clip", excerpt=True)
    provs = [full, demo_ch, clip]
    proxy = MediaProxy(port=0, advertised_host="127.0.0.1", file_token_key=b"k")
    proxy.link_refused = player.note_refusal
    monkeypatch.setattr(streaming_service, "_proxy", proxy)
    monkeypatch.setattr(streaming_service, "is_enabled", lambda: True)
    monkeypatch.setattr(streaming_service, "providers_preferred", lambda: list(provs))
    monkeypatch.setattr(streaming_service, "note_provider_health", lambda silent, answered: False)
    monkeypatch.setattr(player, "_chain_cache", {})
    monkeypatch.setattr(player, "_refusals", {})
    pool_rows: list = []
    monkeypatch.setattr(player, "_radio_similar",
                        lambda seed, exclude, limit: [dict(r) for r in pool_rows
                                                      if r["track_id"] not in exclude][:limit])
    monkeypatch.setattr(player.track_similarity, "similar_tracks",
                        lambda seed, limit=20, **kw: [dict(r, title="t", album="a", similarity=0.5,
                                                           media_file_id=r["file_id"])
                                                      for r in pool_rows][:limit])
    mgr = PlaybackManager()
    monkeypatch.setattr(mgr, "_schedule_persist", lambda: None)
    monkeypatch.setattr(player, "manager", mgr)
    monkeypatch.setattr(manager_mod, "manager", mgr)
    mgr.lane.attach(proxy)
    out = _Output(mgr._on_backend_status)
    mgr._active = out
    monkeypatch.setattr(mgr, "ensure_active", lambda: out)
    yield {"mgr": mgr, "player": player, "cur": cur, "owned": owned, "streamed": streamed,
           "pool": pool_rows, "full": full, "demo": demo_ch, "clip": clip, "proxy": proxy}
    conn.close()
    pool.closeall()


def _switch(node, on: bool):
    from routers import settings as settings_router
    settings_router.put_phantom_prefs(settings_router.PhantomPrefs(radio_streamed=on))


def _batch(node):
    player = node["player"]
    return player._radio_build_batch("seed", set(), None)


def test_the_switch_is_off_by_default_and_the_streaming_library_screen_carries_it(node):
    from routers import settings as settings_router
    assert settings_router.get_phantom_state()["radio_streamed"] is False
    _switch(node, True)
    state = settings_router.get_phantom_state()
    assert (state["radio_streamed"], state["enabled"]) == (True, True)
    # Discover new's own write leaves radio's switch be
    settings_router.put_phantom_prefs(settings_router.PhantomPrefs(enabled=True))
    assert settings_router.get_phantom_state()["radio_streamed"] is True


def test_switch_off_radio_is_owned_tracks_only_and_asks_no_provider(node):
    node["pool"][:] = node["streamed"][:4] + node["owned"]
    for p in (node["full"], node["demo"], node["clip"]):
        p.has.update(r["track_id"] for r in node["streamed"])
    batch = _batch(node)
    assert len(batch) == 10
    assert all(item.source["kind"] == "file" for item, _r in batch)
    assert node["full"].resolved == node["demo"].resolved == node["clip"].resolved == []


def test_an_excerpt_only_track_never_enters_a_batch_and_owned_ones_keep_its_length(node):
    """Two streamed candidates only the excerpt provider has, one the full
    provider has, one the demo channel has (its listen unspent): the two
    whole ones qualify, owned tracks fill the rest — ten."""
    _switch(node, True)
    s = node["streamed"]
    node["pool"][:] = s[:4] + node["owned"]
    node["clip"].has.update(r["track_id"] for r in s[:4])
    node["full"].has.add(s[2]["track_id"])
    node["demo"].has.add(s[3]["track_id"])
    batch = _batch(node)
    assert len(batch) == 10
    streamed = [(item, r) for item, r in batch if item.source["kind"] == "track"]
    assert {r["track_id"] for _i, r in streamed} == {s[2]["track_id"], s[3]["track_id"]}
    assert all(item.whole for item, _r in streamed)
    # never first, never adjacent: owned tracks between them
    kinds = [item.source["kind"] for item, _r in batch]
    assert kinds[0] == "file" and "track,track" not in ",".join(kinds)


def test_a_track_whose_demo_listen_is_spent_does_not_qualify(node):
    _switch(node, True)
    s = node["streamed"]
    node["pool"][:] = s[:2] + node["owned"]
    for r in s[:2]:
        node["demo"].has.add(r["track_id"])
        node["clip"].has.add(r["track_id"])
    node["cur"].execute("INSERT INTO demo_plays (track_id, provider) VALUES (%s, 'demo')",
                        (s[0]["track_id"],))
    batch = _batch(node)
    picked = {r["track_id"] for item, r in batch if item.source["kind"] == "track"}
    assert picked == {s[1]["track_id"]}
    assert len(batch) == 10


def test_at_most_four_qualify_a_batch_from_the_resolved_candidates(node):
    _switch(node, True)
    s = node["streamed"]
    node["pool"][:] = s + node["owned"]
    node["full"].has.update(r["track_id"] for r in s)
    batch = _batch(node)
    assert len(batch) == 10
    assert sum(1 for item, _r in batch if item.source["kind"] == "track") <= 4


def test_a_radio_slot_whose_full_link_fails_is_failed_never_an_excerpt(node):
    """The lane strips the excerpt from a whole slot's chain: the full link
    cut off is a failed slot, and the excerpt provider is never asked to
    download it."""
    _switch(node, True)
    mgr, player = node["mgr"], node["player"]
    t = node["streamed"][0]["track_id"]
    node["full"].has.add(t)
    node["clip"].has.add(t)
    node["full"].fails.add(t)
    q = player._phantom_track_queries([t])[t]
    mgr.replace_queue(player.queue_mod.items_for_streamed([q], whole=True), play=False)
    assert _wait(lambda: mgr.queue.item_at(1).opener()["kind"] == "unplayable")
    assert node["full"].downloads == [t]
    assert node["clip"].downloads == []
    assert mgr.queue.item_at(1).opener()["code"] == "unknown"


def test_a_radio_slot_whose_demo_listen_was_spent_since_the_batch_fails_as_excerpt_only(node):
    _switch(node, True)
    mgr, player = node["mgr"], node["player"]
    t = node["streamed"][0]["track_id"]
    node["demo"].has.add(t)
    node["clip"].has.add(t)
    q = player._phantom_track_queries([t])[t]
    node["cur"].execute("INSERT INTO demo_plays (track_id, provider) VALUES (%s, 'demo')", (t,))
    mgr.replace_queue(player.queue_mod.items_for_streamed([q], whole=True), play=False)
    assert _wait(lambda: mgr.queue.item_at(1).opener()["kind"] == "unplayable")
    assert mgr.queue.item_at(1).opener()["code"] == "excerpt_only"
    assert node["clip"].downloads == []
    # the same track queued by a person's tap streams as its excerpt, as ever
    mgr.replace_queue(player.queue_mod.items_for_streamed([q]), play=False)
    assert _wait(lambda: mgr.queue.item_at(1).opener()["kind"] == "proxy")
    assert mgr.queue.item_at(1).excerpt is True


def test_a_whole_slot_does_not_adopt_an_excerpt_already_in_ram(node):
    """The same track streamed as an excerpt for a tap, then queued by
    radio: the excerpt's bytes are not the radio slot's."""
    _switch(node, True)
    mgr, player = node["mgr"], node["player"]
    t = node["streamed"][0]["track_id"]
    node["clip"].has.add(t)
    q = player._phantom_track_queries([t])[t]
    mgr.replace_queue(player.queue_mod.items_for_streamed([q]), play=False)
    assert _wait(lambda: mgr.queue.item_at(1).opener()["kind"] == "proxy")
    mgr.append(player.queue_mod.items_for_streamed([q], whole=True))
    assert _wait(lambda: mgr.queue.item_at(2).opener()["kind"] == "unplayable")
    assert mgr.queue.item_at(2).opener()["code"] == "excerpt_only"


def test_radio_appends_its_batch_whole_and_takes_the_switch_at_each_batch(node):
    _switch(node, True)
    mgr, player = node["mgr"], node["player"]
    s = node["streamed"]
    node["pool"][:] = s[:2] + node["owned"]
    node["full"].has.update(r["track_id"] for r in s[:2])
    mgr.set_radio_mode(True)
    gen = mgr.queue.generation
    player._radio_fill("seed", gen)
    assert len(mgr.queue) == 10
    assert sum(1 for it in mgr.queue.snapshot() if it.whole) == 2
    _switch(node, False)
    player._radio_played.clear()
    player._radio_fill("seed", gen)
    assert len(mgr.queue) == 20
    assert all(it.source["kind"] == "file" for it in mgr.queue.snapshot()[10:])
    mgr.set_radio_mode(False)


def test_radio_start_with_the_switch_off_and_nothing_owned_names_the_switch(node):
    """Nothing of the owner's like the seed and the switch off: said at once,
    in words that name the switch, before the queue is touched."""
    from fastapi import HTTPException
    mgr, player, cur = node["mgr"], node["player"], node["cur"]
    seed = node["streamed"][7]["track_id"]
    model = str(uuid.uuid4())
    cur.execute("INSERT INTO embedding_models (id, name, dimension) VALUES (%s, 'clap', 512)", (model,))
    cur.execute("INSERT INTO embeddings (vector, model_id, track_id) VALUES (%s::vector, %s, %s)",
                ("[" + ",".join(["0.1"] * 512) + "]", model, seed))
    node["pool"][:] = node["streamed"][:4]
    mgr.replace_queue(player.queue_mod.items_for_owned_rows(
        [{"id": node["owned"][0]["file_id"], "location": "local"}]), play=False)
    with pytest.raises(HTTPException) as e:
        player.radio_start(player.RadioStartRequest(track_uuid=seed))
    assert e.value.status_code == 409
    assert "Radio plays tracks you don't own" in e.value.detail
    assert [it.title for it in mgr.queue.snapshot()] == ["Owned 1"]
    assert mgr.radio_mode is False
    # with owned tracks like it, the station starts — owned only
    node["pool"][:] = node["streamed"][:4] + node["owned"][1:]
    assert player.radio_start(player.RadioStartRequest(track_uuid=seed))["ok"]
    assert _wait(lambda: len(mgr.queue) >= 10)
    assert all(it.source["kind"] == "file" for it in mgr.queue.snapshot())
    mgr.set_radio_mode(False)


def test_play_similar_follows_the_switch(node):
    from fastapi import HTTPException
    mgr, player = node["mgr"], node["player"]
    s = node["streamed"]
    node["full"].has.update(r["track_id"] for r in s)
    node["pool"][:] = s[:3] + node["owned"][:3]
    answer = player.play_similar(player.PlaySimilarRequest(track_id=s[7]["track_id"], limit=6))
    assert answer["count"] == 3
    assert all(t["is_owned"] for t in answer["tracks"])
    assert all(it.source["kind"] == "file" for it in mgr.queue.snapshot())
    _switch(node, True)
    answer = player.play_similar(player.PlaySimilarRequest(track_id=s[7]["track_id"], limit=6))
    assert answer["count"] == 6
    streamed = [it for it in mgr.queue.snapshot() if it.source["kind"] == "track"]
    assert len(streamed) == 3 and all(it.whole for it in streamed)
    _switch(node, False)
    node["pool"][:] = s[:3]
    with pytest.raises(HTTPException) as e:
        player.play_similar(player.PlaySimilarRequest(track_id=s[7]["track_id"], limit=6))
    assert e.value.status_code == 404 and "Radio plays tracks you don't own" in e.value.detail


def test_a_radio_slot_keeps_its_rule_across_a_restart(node):
    from playback.manager import PlaybackManager
    mgr, player = node["mgr"], node["player"]
    t = node["streamed"][0]["track_id"]
    q = player._phantom_track_queries([t])[t]
    mgr.replace_queue(player.queue_mod.items_for_streamed([q], whole=True), play=False)
    mgr._persist_queue()
    again = PlaybackManager()
    again._restore_persisted_queue()
    assert [(it.track_id, it.whole) for it in again.queue.snapshot()] == [(t, True)]
