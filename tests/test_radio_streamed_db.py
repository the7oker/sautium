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
import track_similarity  # noqa: E402

_SCORER = track_similarity.similar_tracks        # the real one, before a fixture stands in


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
        self.silent: set = set()
        self.resolved: list = []
        self.downloads: list = []

    def fetch(self, query):
        found = self._resolve(query)
        if found is None:
            raise ProviderError(f"{self.manifest.id}: no match")      # as a real miss says
        return self._download(found.source_id)

    def _resolve(self, query):
        self.resolved.append(query.track_id)
        if query.track_id in self.silent:
            from streaming.base import ProviderUnavailable
            raise ProviderUnavailable(f"{self.manifest.id}: quota")
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
    provs = [full, demo_ch, clip]           # a test may narrow the offer in place
    proxy = MediaProxy(port=0, advertised_host="127.0.0.1", file_token_key=b"k")
    proxy.link_refused = player.note_refusal
    monkeypatch.setattr(streaming_service, "_proxy", proxy)
    monkeypatch.setattr(streaming_service, "is_enabled", lambda: True)
    monkeypatch.setattr(streaming_service, "providers_preferred", lambda: list(provs))
    monkeypatch.setattr(streaming_service, "providers_registered", lambda: bool(provs))
    monkeypatch.setattr(streaming_service, "note_provider_health", lambda silent, answered: False)
    monkeypatch.setattr(player, "_chain_cache", {})
    monkeypatch.setattr(player, "_refusals", {})
    # The scorer's stand-in keeps its contract: `served` filters BEFORE the
    # limit (track_similarity.similar_tracks)
    pool_rows: list = []
    asked: list = []

    def scorer(seed, exclude=(), limit=20, served=None, **kw):
        asked.append(served)
        rows = [dict(r, title="t", album="a", similarity=0.5, media_file_id=r["file_id"])
                for r in pool_rows if r["track_id"] not in set(exclude)]
        if served is not None:
            rows = [r for r in rows if (r["is_owned"] and r["playable"]) is served]
        return rows[:limit]
    monkeypatch.setattr(player.track_similarity, "similar_tracks", scorer)
    from canon import scrobbles
    monkeypatch.setattr(streaming_service, "follow_switch", lambda settings: None)
    monkeypatch.setattr(scrobbles, "wake", lambda: None)
    mgr = PlaybackManager()
    monkeypatch.setattr(mgr, "_schedule_persist", lambda: None)
    monkeypatch.setattr(player, "manager", mgr)
    monkeypatch.setattr(manager_mod, "manager", mgr)
    mgr.lane.attach(proxy)
    out = _Output(mgr._on_backend_status)
    mgr._active = out
    monkeypatch.setattr(mgr, "ensure_active", lambda: out)
    model = str(uuid.uuid4())
    cur.execute("INSERT INTO embedding_models (id, name, dimension) VALUES (%s, 'clap', 512)", (model,))
    yield {"mgr": mgr, "player": player, "cur": cur, "owned": owned, "streamed": streamed, "model": model,
           "pool": pool_rows, "asked": asked, "providers": provs, "full": full, "demo": demo_ch, "clip": clip, "proxy": proxy}
    conn.close()
    pool.closeall()


def _embed(node, track, angle=0.0):
    """An analysed track: its embedding and one segment, at `angle` from the
    seed's direction."""
    import math
    cur = node["cur"]
    v = "[" + ",".join(map(str, (math.cos(angle), math.sin(angle)) + (0.0,) * 510)) + "]"
    cur.execute("INSERT INTO embeddings (vector, model_id, track_id) VALUES (%s::vector, %s, %s) "
                "RETURNING id", (v, node["model"], track))
    cur.execute("INSERT INTO embedding_segments (embedding_id, segment_index, vector) "
                "VALUES (%s, 0, %s::vector)", (cur.fetchone()[0], v))


def _switch(node, on: bool):
    from routers import settings as settings_router
    settings_router.put_phantom_prefs(settings_router.PhantomPrefs(radio_streamed=on))


def _batch(node):
    player = node["player"]
    return player._radio_build_batch("seed", set(), None)[0]


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
    """Eight streamed candidates all whole, two owned tracks: four streamed
    (the cap), the owned two first."""
    _switch(node, True)
    s = node["streamed"]
    node["pool"][:] = s + node["owned"][:2]
    node["full"].has.update(r["track_id"] for r in s)
    batch = _batch(node)
    kinds = [item.source["kind"] for item, _r in batch]
    assert kinds == ["file", "file"] + ["track"] * 4


def test_the_owners_tracks_are_a_top_n_of_their_own_never_what_a_mixed_top_n_left(node):
    """Streamed rows nearer the seed than every owned one: switch off, the
    batch is still ten of the owner's own (the scorer is asked for them
    alone), and streamed ones are never asked for."""
    node["pool"][:] = node["streamed"] * 4 + node["owned"]
    batch = _batch(node)
    assert len(batch) == 10 and all(item.source["kind"] == "file" for item, _r in batch)
    assert node["asked"] == [True]


def test_the_switch_is_moot_while_the_streaming_library_is_off(node, monkeypatch):
    from streaming import service as streaming_service
    _switch(node, True)
    monkeypatch.setattr(streaming_service, "is_enabled", lambda: False)
    s = node["streamed"]
    node["pool"][:] = s[:4] + node["owned"]
    node["full"].has.update(r["track_id"] for r in s)
    assert all(item.source["kind"] == "file" for item, _r in _batch(node))
    assert node["full"].resolved == []


def test_candidates_passed_over_are_not_asked_about_again_in_the_station(node):
    _switch(node, True)
    player = node["player"]
    s = node["streamed"]
    node["pool"][:] = s[:3] + node["owned"]
    node["clip"].has.update(r["track_id"] for r in s[:3])       # excerpts only
    batch, passed = player._radio_build_batch("seed", set(), None)
    assert passed == {r["track_id"] for r in s[:3]}
    assert all(item.source["kind"] == "file" for item, _r in batch)


def test_a_lazy_head_vouches_for_nothing(node):
    player = node["player"]
    assert not player._plays_whole([(node["full"], None)])
    assert player._plays_whole([(node["full"], "sid"), (node["clip"], None)])
    assert not player._plays_whole([(node["clip"], "sid")])


def test_a_seed_that_gives_nothing_is_not_asked_again_at_every_tick(node, monkeypatch):
    mgr, player = node["mgr"], node["player"]
    node["pool"][:] = []
    mgr.set_radio_mode(True)
    monkeypatch.setattr(player, "_radio_dry_seed", None)
    player._radio_fill("seed", mgr.queue.generation)
    assert player._radio_dry_seed == "seed"
    fills = []
    monkeypatch.setattr(player, "_radio_fill", lambda *a: fills.append(a))
    monkeypatch.setattr(player, "_radio_refilling", False)
    mgr.replace_queue(player.queue_mod.items_for_owned_rows(
        [{"id": node["owned"][0]["file_id"], "location": "local"}]), play=False)
    seed_item = mgr.queue.item_at(1)
    player._radio_refill_observer({"track_index": 1}, type("I", (), {"track_id": "seed"})())
    assert fills == []
    player._radio_refill_observer({"track_index": 1}, seed_item)
    assert _wait(lambda: len(fills) == 1)
    mgr.set_radio_mode(False)


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


def test_a_whole_slot_does_not_adopt_an_excerpt_already_in_ram(node, monkeypatch):
    """The same track streamed as an excerpt for a tap (the full provider had
    it not then), then queued by radio once the full provider has it: the
    excerpt in RAM is not the radio slot's — it is fetched whole."""
    _switch(node, True)
    mgr, player = node["mgr"], node["player"]
    t = node["streamed"][0]["track_id"]
    node["clip"].has.add(t)
    q = player._phantom_track_queries([t])[t]
    mgr.replace_queue(player.queue_mod.items_for_streamed([q]), play=False)
    assert _wait(lambda: mgr.queue.item_at(1).opener()["kind"] == "proxy")
    assert mgr.queue.item_at(1).excerpt is True
    node["full"].has.add(t)
    monkeypatch.setattr(player, "_chain_cache", {})
    mgr.append(player.queue_mod.items_for_streamed([q], whole=True))
    assert _wait(lambda: mgr.queue.item_at(2).opener()["kind"] == "proxy")
    assert node["full"].downloads == [t]
    tok = mgr.queue.item_at(2).opener()["token"]
    assert node["proxy"]._peek(tok).provider is node["full"]
    assert node["proxy"]._peek(tok).audio.excerpt is False


def test_radio_appends_its_batch_whole_and_takes_the_switch_at_each_batch(node):
    _switch(node, True)
    mgr, player = node["mgr"], node["player"]
    s = node["streamed"]
    node["pool"][:] = s[:2] + node["owned"]
    node["full"].has.update(r["track_id"] for r in s[:2])
    mgr.set_radio_mode(True)
    gen = mgr.queue.generation
    player._radio_played.clear()
    player._radio_fill("seed", gen)
    assert len(mgr.queue) == 10
    assert sum(1 for it in mgr.queue.snapshot() if it.source["kind"] == "track") == 2
    assert all(it.whole for it in mgr.queue.snapshot())     # every slot of a fill says so
    # ...and which it is: his own copy, or a stream his switch allowed
    assert all(it.own_fill == (it.source["kind"] == "file") for it in mgr.queue.snapshot())
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
    _embed(node, seed)
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
    _embed(node, s[7]["track_id"])
    node["full"].has.update(r["track_id"] for r in s)
    node["pool"][:] = s[:3] + node["owned"][:3]
    answer = player.play_similar(player.PlaySimilarRequest(track_id=s[7]["track_id"], limit=6))
    assert answer["count"] == 3
    assert all(t["is_owned"] for t in answer["tracks"])
    assert all(it.source["kind"] == "file" and it.whole for it in mgr.queue.snapshot())
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


def test_play_similar_leaves_out_what_the_node_knows_plays_only_as_an_excerpt(node):
    """A track whose demo listen is spent, with no full provider in its
    order: Play similar does not commit a slot that could only fail."""
    _switch(node, True)
    mgr, player, cur = node["mgr"], node["player"], node["cur"]
    s = node["streamed"]
    _embed(node, s[7]["track_id"])
    node["providers"][:] = [node["demo"], node["clip"]]
    node["demo"].has.update(r["track_id"] for r in s[:2])
    cur.execute("INSERT INTO demo_plays (track_id, provider) VALUES (%s, 'demo')", (s[0]["track_id"],))
    node["pool"][:] = s[:2] + node["owned"][:2]
    answer = player.play_similar(player.PlaySimilarRequest(track_id=s[7]["track_id"], limit=4))
    assert [m["reason"] for m in answer["not_streamable"]] == ["excerpt_only"]
    assert answer["count"] == 3
    assert [it.track_id for it in mgr.queue.snapshot()
            if it.source["kind"] == "track"] == [s[1]["track_id"]]


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


def test_the_real_scorer_filters_the_served_rows_before_its_limit(node):
    """Against the real statement: eight streamed tracks nearer the seed than
    four owned ones — `served=True` is a top-3 of the owned ones, `False`
    of the streamed, and the mixed top-3 holds no owned track at all."""
    def embed(track, angle):
        _embed(node, track, angle)
    seed = node["streamed"][7]["track_id"]
    embed(seed, 0.0)
    streamed = [r["track_id"] for r in node["streamed"][:7]]
    owned = [r["track_id"] for r in node["owned"][:4]]
    for i, t in enumerate(streamed):
        embed(t, 0.05 * (i + 1))
    for i, t in enumerate(owned):
        embed(t, 0.6 + 0.05 * i)
    mine = _SCORER(seed, limit=3, served=True)
    assert [r["track_id"] for r in mine] == owned[:3]
    assert all(r["is_owned"] and r["playable"] for r in mine)
    theirs = _SCORER(seed, limit=3, served=False)
    assert [r["track_id"] for r in theirs] == streamed[:3]
    assert not any(r["is_owned"] for r in _SCORER(seed, limit=3))
    # past the mixed recall horizon too: a recall of 3 nearest holds no owned
    # track, the owner's own recall walks to them
    assert [r["track_id"] for r in _SCORER(seed, limit=3, served=True, pool=3)] == owned[:3]


def _music_folder(monkeypatch, gone: bool) -> None:
    """The node's music folder as the fixture's paths have it, and what the
    node has recorded of it."""
    import scanner
    from config import settings
    monkeypatch.setattr(settings, "music_library_path", "E:/Music")
    monkeypatch.setattr(settings, "music_host_path", None)
    monkeypatch.setattr(scanner, "_folder_unreachable", gone)


def test_the_real_scorer_serves_no_track_whose_only_copy_is_not_here_now(node, monkeypatch):
    """PI-NODE-2D Q6: a track whose only copy lies under a folder that does
    not answer is, for radio's rule, not the owner's to play — `served=True`
    leaves it out, `served=False` has it among the ones that stream."""
    seed = node["streamed"][7]["track_id"]
    _embed(node, seed, 0.0)
    owned = [r["track_id"] for r in node["owned"][:3]]
    for i, t in enumerate(owned):
        _embed(node, t, 0.1 * (i + 1))
    _music_folder(monkeypatch, gone=False)
    assert [r["track_id"] for r in _SCORER(seed, limit=5, served=True)] == owned
    _music_folder(monkeypatch, gone=True)
    assert _SCORER(seed, limit=5, served=True) == []
    out = _SCORER(seed, limit=5, served=False)
    assert [r["track_id"] for r in out] == owned
    assert all(r["is_owned"] and not r["playable"] for r in out)
    # a copy in a folder that answers serves it again
    cur = node["cur"]
    cur.execute("INSERT INTO album_variants (album_id, directory_path) "
                "SELECT album_id, '/elsewhere/M' FROM album_variants LIMIT 1 RETURNING id")
    cur.execute("INSERT INTO media_files (track_id, album_variant_id, file_path, sample_rate, bit_depth, "
                "duration_seconds, track_number) VALUES (%s, %s, '/elsewhere/M/01.flac', 44100, 16, 200, 1) "
                "RETURNING id", (owned[0], cur.fetchone()[0]))
    copy = cur.fetchone()[0]
    assert [(r["track_id"], r["file_id"]) for r in _SCORER(seed, limit=5, served=True)] == [(owned[0], copy)]


def test_radio_and_play_similar_never_stream_his_album_from_a_disk_that_is_out_on_their_own(node, monkeypatch):
    """Automatic fills do not spend his listens: with radio.streamed off a
    track with no copy here now is left out, and no provider is asked; with
    it on it is a slot played whole, like any streamed track."""
    from fastapi import HTTPException
    mgr, player, cur = node["mgr"], node["player"], node["cur"]
    monkeypatch.setattr(player.track_similarity, "similar_tracks", _SCORER)
    seed = node["streamed"][7]["track_id"]
    _embed(node, seed, 0.0)
    owned = [r["track_id"] for r in node["owned"][:3]]
    for i, t in enumerate(owned):
        _embed(node, t, 0.1 * (i + 1))
        cur.execute("INSERT INTO album_tracks (album_id, track_id, position, length_ms) "
                    "SELECT av.album_id, %s, %s, 200000 FROM album_variants av LIMIT 1", (t, i + 1))
    node["full"].has.update(owned)
    _music_folder(monkeypatch, gone=False)
    batch, _passed = player._radio_build_batch(seed, {seed}, None)
    assert [item.source["kind"] for item, _r in batch] == ["file"] * 3

    _music_folder(monkeypatch, gone=True)
    batch, _passed = player._radio_build_batch(seed, {seed}, None)
    assert batch == [] and node["full"].resolved == []
    with pytest.raises(HTTPException) as e:
        player.play_similar(player.PlaySimilarRequest(track_id=seed, limit=6))
    assert e.value.status_code == 404 and len(mgr.queue) == 0
    assert node["full"].resolved == node["demo"].resolved == node["clip"].resolved == []

    _switch(node, True)
    batch, _passed = player._radio_build_batch(seed, {seed}, None)
    assert sorted(item.track_id for item, _r in batch) == sorted(owned)
    assert all(item.source == {"kind": "track"} and item.whole for item, _r in batch)


def _station(node, monkeypatch, n: int = 4) -> list:
    """A station of the owner's first `n` tracks, radio's own fill; each has
    a tracklist slot, so nothing but the rule keeps it from streaming."""
    mgr, player, cur = node["mgr"], node["player"], node["cur"]
    _music_folder(monkeypatch, gone=False)
    node["pool"][:] = node["owned"][:n]
    for i, r in enumerate(node["owned"][:n]):
        cur.execute("INSERT INTO album_tracks (album_id, track_id, position, length_ms) "
                    "SELECT av.album_id, %s, %s, 200000 FROM album_variants av LIMIT 1",
                    (r["track_id"], i + 1))
    mgr.set_radio_mode(True)
    player._radio_played.clear()
    player._radio_fill("seed", mgr.queue.generation)
    items = mgr.queue.snapshot()
    assert [it.source["kind"] for it in items] == ["file"] * n and all(it.whole for it in items)
    return items


def _no_resolve_runs() -> bool:
    return not [t for t in threading.enumerate() if t.name == "queue-resolve"]


def test_a_stations_own_files_whose_folder_goes_are_gaps_never_streams(node, monkeypatch):
    """PI-NODE-2D Q6 / §3.4, at the re-read: radio on, the owner's switch
    off, the folder goes mid-station — no slot of the station becomes a
    stream, no provider is asked and nothing is fetched; each is a gap said
    on its row, which an output walks past; a batch built before the pull
    and appended after it the same; the folder back, every slot is its file
    again."""
    from playback import queue as queue_mod
    mgr, player = node["mgr"], node["player"]
    items = _station(node, monkeypatch)
    node["full"].has.update(it.track_id for it in items)
    _music_folder(monkeypatch, gone=True)
    mgr.reach_changed()
    gap = {"kind": "unplayable", "reason": "its file is not here now", "code": "not_here",
           "lane": True}
    assert [it.play for it in items] == [gap] * 4
    assert _wait(_no_resolve_runs) and mgr.lane.pick() is None
    assert node["full"].resolved == node["demo"].resolved == node["clip"].resolved == []
    assert node["full"].downloads == node["demo"].downloads == node["clip"].downloads == []
    assert [(s["state"], s["reason"]) for s in mgr.lane.slots()] == [("failed", "not_here")] * 4
    late = player._fills(queue_mod.items_for_owned_rows(
        [{"id": node["owned"][5]["file_id"], "location": "local"}]))
    assert mgr.append(late) == 1 and late[0].play == gap
    assert _wait(_no_resolve_runs) and node["full"].resolved == []
    _music_folder(monkeypatch, gone=False)
    mgr.reach_changed()
    assert [it.play for it in mgr.queue.snapshot()] == [None] * 5
    mgr.set_radio_mode(False)


def test_with_radios_switch_on_a_stations_file_that_is_not_here_streams_whole_or_not_at_all(
        node, monkeypatch):
    """The owner's switch lets radio stream what he has no copy of here:
    then a station's own slot whose folder went streams — from a
    full-length link or not at all, radio's rule for every slot of its."""
    mgr = node["mgr"]
    items = _station(node, monkeypatch)
    node["full"].has.update(it.track_id for it in items[:3])
    node["clip"].has.add(items[3].track_id)             # only its 30 s clip streams
    _music_folder(monkeypatch, gone=True)
    mgr.reach_changed()                                 # the switch off: gaps
    assert all(it.play["code"] == "not_here" for it in items)
    _switch(node, True)
    mgr.reach_changed()
    assert _wait(lambda: items[3].opener().get("code") == "excerpt_only")
    assert _wait(lambda: all(it.opener()["kind"] == "proxy" for it in items[:3]))
    assert node["clip"].downloads == []
    mgr.set_radio_mode(False)


def test_a_fills_own_slot_whose_file_left_the_library_is_still_no_stream_behind_his_back(
        node, monkeypatch):
    """A station's own file leaves the library with no other copy, the track
    kept: the re-bind makes the slot the track itself — and it is still the
    slot a fill queued as his own copy (QueueItem.own_fill, whatever its
    origin became, kept by a persisted queue too): no stream with his
    switch off, no provider asked; with it on, whole."""
    from dataclasses import asdict
    from playback.queue import QueueItem
    mgr, cur = node["mgr"], node["cur"]
    items = _station(node, monkeypatch)
    node["full"].has.update(it.track_id for it in items)
    cur.execute("DELETE FROM media_files WHERE id = %s", (items[0].media_file_id,))
    mgr.rebind_files()
    assert items[0].source == {"kind": "track"} and items[0].own_fill
    assert items[0].play["code"] == "not_here"
    assert _wait(_no_resolve_runs) and node["full"].resolved == node["demo"].resolved == []
    assert QueueItem(**asdict(items[0])).own_fill           # as a persisted queue restores it
    _switch(node, True)
    mgr.reach_changed()
    assert _wait(lambda: items[0].opener()["kind"] == "proxy")
    assert node["clip"].downloads == []
    mgr.set_radio_mode(False)


def test_play_similar_on_a_seed_not_analysed_says_so_whatever_the_switch(node):
    from fastapi import HTTPException
    player = node["player"]
    node["pool"][:] = node["owned"][:3]
    with pytest.raises(HTTPException) as e:
        player.play_similar(player.PlaySimilarRequest(track_id=node["streamed"][7]["track_id"]))
    assert e.value.status_code == 404 and "not be analysed" in e.value.detail


def test_a_candidate_a_silent_provider_did_not_answer_for_is_asked_again(node):
    _switch(node, True)
    player = node["player"]
    s = node["streamed"]
    node["pool"][:] = s[:2] + node["owned"]
    node["full"].has.add(s[0]["track_id"])
    node["full"].silent.add(s[1]["track_id"])
    _batch_rows, passed = player._radio_build_batch("seed", set(), None)
    assert s[1]["track_id"] not in passed


def test_a_fill_overtaken_by_a_new_station_teaches_it_nothing(node, monkeypatch):
    mgr, player = node["mgr"], node["player"]
    mgr.set_radio_mode(True)
    gen = mgr.queue.generation
    monkeypatch.setattr(player, "_radio_dry_seed", None)
    monkeypatch.setattr(player, "_radio_played", set())

    def build(seed, played, prev):
        mgr.queue.clear_for_radio(None)             # a restart while this one resolved
        return [], {"passed-over"}
    monkeypatch.setattr(player, "_radio_build_batch", build)
    player._radio_fill("seed", gen)
    assert player._radio_dry_seed is None and player._radio_played == set()
    mgr.set_radio_mode(False)


def test_the_words_name_no_switch_where_the_deployment_streams_nothing(node, monkeypatch):
    from config import settings as app_settings
    from streaming import service as streaming_service
    player = node["player"]
    from routers import settings as settings_router
    monkeypatch.setattr(streaming_service, "is_enabled", lambda: False)
    settings_router._write("discovery.phantom_layer", False)
    assert "Discover new and" in player._radio_owned_only_words("Radio")
    _switch(node, True)
    assert "Turn on Discover new in" in player._radio_owned_only_words("Radio")
    monkeypatch.setattr(app_settings, "streaming_preview_enabled", False)
    assert player._radio_owned_only_words("Radio") == "Radio found nothing of yours like this track."
