"""With P2P sharing off the backend's own network lanes ask no node:
Discovery's MusicBrainz search and its mint, the assistant's resolve
and an artist page's ListenBrainz request — the person's own acts, which a
node that does not give does not make of others. The MusicBrainz chip says
`off`, Discovery's routes say why, mb_resolve answers `network_off`, an
opened page records no request that would ask later, and the switch's move
wakes the walk (back on) and the chip. On a real PostgreSQL built from the
migrations; skipped without a cluster."""

import importlib.util
import json
import select
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
for p in (REPO, REPO / "backend", REPO / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

pytest.importorskip("psycopg2")
pytest.importorskip("sqlalchemy")

import mb_fakes  # noqa: E402

GID = "6b2e0000-0000-4000-8000-0000000000aa"


def _refuse(*args, **kwargs):
    raise AssertionError("a node was asked while sharing is off")


@pytest.fixture
def node(scratch_dsn, monkeypatch):
    conn, pool, engine = mb_fakes.slice_fed(scratch_dsn, monkeypatch)
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO user_settings (key, value) VALUES
                         ('mb.search_sources', %s::jsonb)
                       ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
                    (json.dumps([{"url": "https://198.51.100.9:8801", "kind": "dump"}]),))
    yield conn
    with conn.cursor() as cur:
        cur.execute("DELETE FROM user_settings WHERE key IN ('sync.p2p_enabled', 'mb.search_sources')")
        cur.execute("DELETE FROM lb_slice_requests")
    from routers import sync as sync_router
    sync_router.sharing_changed()
    conn.close()
    pool.closeall()
    engine.dispose()


def _sharing(conn, on: bool) -> None:
    from routers import sync as sync_router
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO user_settings (key, value) VALUES ('sync.p2p_enabled', %s::jsonb)
                       ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""", (json.dumps(on),))
    sync_router.sharing_changed()


def test_discovery_asks_no_node_and_says_sharing_is_off(node, monkeypatch):
    import mb_discovery
    from fastapi import HTTPException
    from routers import discovery
    monkeypatch.setattr(mb_discovery, "_client", _refuse)
    monkeypatch.setattr("desktop.api_client.BackendAPIClient", _refuse)
    _sharing(node, False)
    assert mb_discovery.state() == {"available": False, "remote": False, "state": "off"}
    assert not mb_discovery.remote_available()
    assert mb_discovery.remote_search("sade") == {"status": "sharing_off"}
    assert mb_discovery._fetch_remote_slice("Sade") == {"ok": False, "sharing_off": True}
    assert discovery.mb_search(q="sade", limit=5) == {
        "available": False, "sharing_off": True, "artists": [], "albums": []}
    with pytest.raises(HTTPException) as refused:
        discovery.mb_mint({"artist_gid": GID, "artist_name": "Sade"})
    assert refused.value.status_code == 409 and "sharing is off" in refused.value.detail


def test_with_sharing_on_the_chip_offers_the_known_source(node):
    import mb_discovery
    _sharing(node, True)
    assert mb_discovery.state() == {"available": True, "remote": True, "state": "remote"}


def test_an_opened_artist_records_no_request_while_sharing_is_off(node, monkeypatch):
    import uuid
    from routers import artists
    artist = str(uuid.uuid4())
    with node.cursor() as cur:
        cur.execute("INSERT INTO artists (id, name) VALUES (%s, 'Opened Act')", (artist,))
        cur.execute("INSERT INTO artist_mbids (mbid, artist_id, confidence) VALUES (%s, %s, 'user')",
                    (GID, artist))
    _sharing(node, False)
    page = artists.get_artist(artist, sort="release_year", mbid=None)
    assert page["lb_pending"] is False
    with node.cursor() as cur:
        cur.execute("SELECT count(*) FROM lb_slice_requests")
        assert cur.fetchone()[0] == 0
    _sharing(node, True)
    assert artists.get_artist(artist, sort="release_year", mbid=None)["lb_pending"] is True


def _assistant(monkeypatch, answers: dict):
    spec = importlib.util.spec_from_file_location("_g7_assistant", REPO / "mcp" / "assistant_server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_backend_get", lambda path, params: answers[path])
    return module


def test_the_assistant_s_resolve_says_the_network_is_off(monkeypatch):
    pytest.importorskip("mcp")
    assistant = _assistant(monkeypatch, {
        "/api/discovery/mb-search": {"available": False, "sharing_off": True, "artists": [],
                                     "albums": []},
        "/api/discovery/mb-status": {"available": False, "remote": False, "state": "off"},
    })
    out = json.loads(assistant.mb_resolve("Sade"))
    assert out["status"] == "network_off"


def _listening(dsn, channels=("sautium_sync_request", "sautium_mb_sources")):
    import psycopg2
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("; ".join(f"LISTEN {c}" for c in channels))
    return conn


def _heard(conn, timeout: float = 2.0) -> set:
    got = set()
    while select.select([conn], [], [], timeout) != ([], [], []):
        conn.poll()
        got |= {n.channel for n in conn.notifies}
        conn.notifies.clear()
        timeout = 0.2
    return got


def test_the_switch_wakes_the_chip_and_turned_on_the_walk(node, scratch_dsn):
    from routers import settings
    listener = _listening(scratch_dsn)
    try:
        settings.put_sync_prefs(settings.SyncPrefs(p2p_enabled=False))
        assert _heard(listener) == {"sautium_mb_sources"}
        settings.put_sync_prefs(settings.SyncPrefs(p2p_enabled=True))
        assert _heard(listener) == {"sautium_mb_sources", "sautium_sync_request"}
    finally:
        listener.close()


def test_an_opened_artist_wakes_the_statistics_cycle_for_its_new_request_alone(node, scratch_dsn):
    """The cycle asks a page's artist at once, outside its pace, the first
    time: the page read again while its request waits — after each batch the
    cycle imports — wakes nothing, or every read would cut the pace short."""
    import uuid
    from routers import artists
    artist = str(uuid.uuid4())
    with node.cursor() as cur:
        cur.execute("INSERT INTO artists (id, name) VALUES (%s, 'Opened Twice')", (artist,))
        cur.execute("INSERT INTO artist_mbids (mbid, artist_id, confidence) VALUES (%s, %s, 'name_exact')",
                    (str(uuid.uuid4()), artist))
    _sharing(node, True)
    listener = _listening(scratch_dsn, ("sautium_lb_request",))
    try:
        assert artists.get_artist(artist, sort="release_year", mbid=None)["lb_pending"] is True
        assert _heard(listener) == {"sautium_lb_request"}
        assert artists.get_artist(artist, sort="release_year", mbid=None)["lb_pending"] is True
        assert _heard(listener, timeout=0.5) == set()
    finally:
        listener.close()


def test_a_click_asks_for_its_name_in_the_stream_s_one_form(node, monkeypatch):
    """MusicBrainz's casing, as a search result carries it, would tell a
    click from a name the background stream drew."""
    import mb_discovery
    from desktop.p2p import mb_slice_queries
    asked = []

    class _Source:
        def __init__(self, url, peer=None):
            pass

        def get_health(self):
            return {}

        def mb_slice(self, names):
            asked.append(list(names))
            return {"v": mb_slice_queries.PROTOCOL_VERSION, "slices": {}, "missing": list(names)}

    monkeypatch.setattr("desktop.api_client.BackendAPIClient", _Source)
    monkeypatch.setattr("p2p_identity.peer_identity", lambda settings: None)
    _sharing(node, True)
    assert mb_discovery._fetch_remote_slice(" Sigur Rós ")["ok"] is False
    assert asked == [["sigur rós"]]


def test_a_page_s_request_made_while_no_one_listened_is_asked_once_the_listen_is_up(node, scratch_dsn):
    """A page wakes the statistics cycle once, for its new request: one made
    while no LISTEN was up (a reconnect, a launcher not yet running) is asked
    whenever the notifications are listened for again."""
    import asyncio
    import uuid

    from desktop.p2p import lb_slice_cycle
    from desktop.p2p.sync_walk import DeadAddresses
    cycle = lb_slice_cycle.LbSliceCycle(scratch_dsn, connect=_refuse, dead=DeadAddresses(),
                                        sharing_enabled=lambda: True)
    asyncio.run(cycle.pages_waiting())
    assert cycle._reasons == set()
    artist = str(uuid.uuid4())
    with node.cursor() as cur:
        cur.execute("INSERT INTO artists (id, name) VALUES (%s, 'Opened Unheard')", (artist,))
        cur.execute("INSERT INTO lb_slice_requests (artist_id) VALUES (%s)", (artist,))
    asyncio.run(cycle.pages_waiting())
    assert cycle._reasons == {"request"}

