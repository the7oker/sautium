"""No connection to an HQPlayer the person never chose (PI-NODE-2C DESIGN §4, H1).

Before this, "the HQPlayer this node drives" had no single home: readers
fell back from the saved address to the config's default (127.0.0.1:4321),
so on a node whose owner chose no HQPlayer the Library route connected there
at every wake of its stream, five routes and the assistant's tools asked it,
the picker's scan could mint a registry row at it, and a pick with no
address wrote it down as the choice. Now the saved pick is the one fact
(hqp_library.driven); with none there is no link, one refusal, and the
link's own connect refuses an address nobody chose, registered or is
probing.

A FakeHqp listens on loopback as the address the environment names; it
counts every connection it accepts. On the module's scratch database."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

psycopg2 = pytest.importorskip("psycopg2")
pytest.importorskip("fastapi")

import db_migrate  # noqa: E402
import hqp_library  # noqa: E402
from config import settings  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from playback import hqp_link  # noqa: E402
from playback.manager import PlaybackManager  # noqa: E402
from routers import hqplayer as hqp_router  # noqa: E402
from routers import player  # noqa: E402
from routers import settings as settings_router  # noqa: E402
from tests.test_hqp_backend import FakeHqp, _fresh_links  # noqa: E402
from tests.test_scan_unreachable import cur, library, main_module  # noqa: E402,F401
from tools import definitions  # noqa: E402


@pytest.fixture
def default_hqp(cur, monkeypatch):
    """An HQPlayer at the address the environment names, on a node whose
    owner chose none: the start's overlay finds no saved pick."""
    fake = FakeHqp()
    cur.execute("DELETE FROM hqp_endpoints")
    monkeypatch.setattr(settings, "hqplayer_host", "127.0.0.1")
    monkeypatch.setattr(settings, "hqplayer_port", fake.port)
    monkeypatch.setattr(settings, "hqplayer_enabled", False)
    _fresh_links()
    settings_router.load_hqplayer_from_db()
    yield fake
    _fresh_links()
    fake.close()


def _refused(call, *a, **kw):
    with pytest.raises(HTTPException) as refused:
        call(*a, **kw)
    assert (refused.value.status_code, refused.value.detail) == (409, hqp_link.NOT_CHOSEN)


def _rows(cur):
    cur.execute("SELECT count(*) FROM hqp_endpoints")
    return cur.fetchone()[0]


def test_with_no_hqplayer_chosen_nothing_connects_to_the_default_address(default_hqp, cur, library,
                                                                        main_module, monkeypatch):
    fake = default_hqp
    assert hqp_library.driven() is None and hqp_link.selected() is None

    lib = asyncio.run(settings_router._library_state())["hqp_library"]
    assert (lib["endpoint"], lib["endpoints"]) == (None, [])
    assert settings_router.get_hqplayer_prefs()["host"] is None
    assert settings_router.get_output_prefs()["busy"] is None
    assert [e for e in player._hqplayer_entries() if e["configured"]] == []

    _refused(hqp_router.get_state)
    _refused(hqp_router.get_state, dsp=True)
    _refused(hqp_router.get_facts, "filter", "poly-sinc-gauss-long")
    _refused(hqp_router.set_config, hqp_router.ConfigRequest(filter="poly-sinc-gauss-long"))
    _refused(hqp_router.nudge_volume, hqp_router.VolumeRequest(delta=1))
    _refused(settings_router.trigger_hqp_sync)
    _refused(settings_router.trigger_hqp_sync, confirm=True)
    _refused(settings_router.trigger_hqp_rescan)

    # the assistant, both surfaces: an unset output is this device, not "HQPlayer may have it"
    for tool in hqp_router.hqp_assistant.READS:
        assert hqp_router._assistant_read(tool, {}).output == "browser"
    with pytest.raises(ConnectionError, match="not the active audio output"):
        definitions._hqp_gate()
    with pytest.raises(ConnectionError, match="not the active audio output"):
        with definitions._hqp_asking():
            pass

    # the start: nothing chosen is the browser, whatever the environment enables
    monkeypatch.setattr(settings, "hqplayer_enabled", True)
    monkeypatch.setattr(PlaybackManager, "_schedule_persist", lambda self: None)
    manager = PlaybackManager()
    manager.init_from_settings()
    try:
        assert manager.active.id == "browser"
    finally:
        manager.activate(None)
    player.start_status_poller()

    assert fake._conns == []
    assert _rows(cur) == 0


def test_the_mcp_servers_gate_refuses_an_unset_output(monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "assistant_server_unchosen", BACKEND.parent / "mcp" / "assistant_server.py")
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    monkeypatch.setattr(server, "_output_prefs", lambda: {"type": None, "held": None, "busy": None})
    with pytest.raises(ConnectionError, match="not the active audio output .currently: browser."):
        server._hqp_gate()


def test_a_pick_of_hqplayer_with_no_address_is_refused_and_writes_nothing(default_hqp, cur):
    _refused(settings_router._put_output_prefs, settings_router.OutputPrefs(type="hqplayer"))
    cur.execute("SELECT key FROM user_settings WHERE key IN ('output.type', 'hqplayer.host', 'hqplayer.port')")
    assert cur.fetchall() == []
    assert default_hqp._conns == []
    with pytest.raises(HTTPException) as blank:
        settings_router.put_hqplayer_prefs(settings_router.HqplayerPrefs(host="  "))
    assert blank.value.status_code == 400
    assert hqp_library.driven() is None


def test_the_links_connect_refuses_an_address_nobody_chose_registered_or_probes(default_hqp, cur):
    fake = default_hqp
    at = ("127.0.0.1", fake.port)
    with pytest.raises(hqp_link.NotChosen):
        hqp_library.get_info(*at)
    assert fake._conns == []

    # a registered row's address (a library the person imported, a box they added) is asked
    cur.execute("INSERT INTO hqp_endpoints (name, host, port) VALUES ('box', %s, %s)", at)
    assert hqp_library.get_info(*at)["product"]
    assert len(fake._conns) == 1

    cur.execute("DELETE FROM hqp_endpoints")
    _fresh_links()
    with pytest.raises(hqp_link.NotChosen):
        hqp_library.get_info(*at)

    # the person's own look at an address (the scan, "Add device by address")
    hqp_link.link_for(*at, probing=True)
    assert hqp_library.get_info(*at)["product"]


def test_the_chosen_hqplayer_is_asked_as_before(default_hqp, cur):
    fake = default_hqp
    settings_router._apply_hqplayer_address("127.0.0.1", fake.port)
    assert hqp_library.driven() == ("127.0.0.1", fake.port)
    assert hqp_link.selected() is hqp_link.chosen()
    state = hqp_router.get_state()
    assert state["connected"] is True and fake._conns
    # ...and the choice is the saved rows: a start reads it back
    settings.hqplayer_host = None
    settings_router.load_hqplayer_from_db()
    assert hqp_library.driven() == ("127.0.0.1", fake.port)


def _scan_finding(monkeypatch, host):
    """One picker scan whose sweep heard an HQPlayer at `host` that no row
    knows; returns the addresses the scan registered."""
    import async_upnp_client.search as search
    import tls_gen
    registered = []

    async def no_search(*_a, **_k):
        return None

    async def sweep(found, found_hqp):
        found_hqp[host] = {"name": "desk", "product": "Signalyst HQPlayer Desktop 6"}

    class Link:
        def refuse(self):
            return None
    monkeypatch.setattr(tls_gen, "detect_private_host_ips", lambda: [])
    monkeypatch.setattr(search, "async_search", no_search)
    monkeypatch.setattr(player, "_unicast_sweep", sweep)
    monkeypatch.setattr(player, "_dlna_pins", lambda: {})
    monkeypatch.setattr(player, "_hqp_discovered", {})
    monkeypatch.setattr(player, "_dlna_discovered", {})
    monkeypatch.setattr(player.hqp_link, "link_for", lambda host, port, **kw: Link())
    monkeypatch.setattr(hqp_library, "resolve_endpoint", lambda host, port: (None, None, {}))
    monkeypatch.setattr(hqp_library, "register", lambda host, port: registered.append((host, port)))
    asyncio.run(player._scan_run())
    return registered


def test_the_scan_mints_no_row_at_the_default_address(default_hqp, cur, monkeypatch):
    """Found on this machine, where the old default pointed: nobody chose it."""
    assert _scan_finding(monkeypatch, "127.0.0.1") == []
    assert _rows(cur) == 0


def test_the_scan_registers_the_chosen_hqplayer_on_first_contact(default_hqp, cur, monkeypatch):
    monkeypatch.setattr(settings, "hqplayer_host", "127.0.0.1")
    monkeypatch.setattr(settings, "hqplayer_port", 4321)
    assert _scan_finding(monkeypatch, "127.0.0.1") == [("127.0.0.1", 4321)]


# -- hqp_choice_v1: the choice of an install older than the Output picker, saved once ----------------

def _saved(cur):
    cur.execute("SELECT key, value FROM user_settings WHERE key IN ('output.type', 'hqplayer.host', "
                "'hqplayer.port') ORDER BY key")
    return dict(cur.fetchall())


def _migrate(scratch_dsn):
    conn = psycopg2.connect(scratch_dsn)
    try:
        return db_migrate._save_hqp_choice(conn)
    finally:
        conn.close()


def _set(cur, **rows):
    for key, value in rows.items():
        cur.execute("INSERT INTO user_settings (key, value) VALUES (%s, %s::jsonb)",
                    (key.replace("_", ".", 1), json.dumps(value)))


def test_an_install_the_environment_enabled_gets_its_choice_saved_and_still_attaches(
        default_hqp, cur, scratch_dsn, monkeypatch):
    fake = default_hqp
    monkeypatch.setattr(settings, "hqplayer_host", "127.0.0.1")     # the environment's, at a start
    monkeypatch.setattr(settings, "hqplayer_enabled", True)
    assert _migrate(scratch_dsn) == {"chosen": True, "written": {
        "output.type": "hqplayer", "hqplayer.host": "127.0.0.1", "hqplayer.port": fake.port}}
    assert _saved(cur) == {"output.type": "hqplayer", "hqplayer.host": "127.0.0.1", "hqplayer.port": fake.port}
    settings_router.load_hqplayer_from_db()
    assert hqp_library.driven() == ("127.0.0.1", fake.port)
    assert player._hqp_configured()


def test_an_install_with_a_saved_host_and_no_output_gets_the_output_saved(default_hqp, cur, scratch_dsn):
    _set(cur, hqplayer_host="192.0.2.10", hqplayer_port=4321)
    assert _migrate(scratch_dsn)["written"] == {"output.type": "hqplayer"}
    assert _saved(cur) == {"output.type": "hqplayer", "hqplayer.host": "192.0.2.10", "hqplayer.port": 4321}


def test_a_picked_hqplayer_whose_address_was_the_environments_gets_the_address_saved(
        default_hqp, cur, scratch_dsn, monkeypatch):
    monkeypatch.setattr(settings, "hqplayer_host", "host.docker.internal")
    monkeypatch.setattr(settings, "hqplayer_port", 4321)
    _set(cur, output_type="hqplayer")
    assert _migrate(scratch_dsn)["written"] == {"hqplayer.host": "host.docker.internal", "hqplayer.port": 4321}
    # ...and with no address in the environment, the address it ran on: the old default
    cur.execute("DELETE FROM user_settings WHERE key LIKE 'hqplayer.%'")
    monkeypatch.setattr(settings, "hqplayer_host", None)
    assert _migrate(scratch_dsn)["written"] == {"hqplayer.host": "127.0.0.1", "hqplayer.port": 4321}


@pytest.mark.parametrize("rows", [{}, {"output_type": "browser"},
                                  {"output_type": "dlna", "hqplayer_host": "192.0.2.10", "hqplayer_port": 4321}])
def test_a_node_that_chose_another_output_or_none_is_untouched(default_hqp, cur, scratch_dsn, monkeypatch,
                                                               rows):
    monkeypatch.setattr(settings, "hqplayer_host", "127.0.0.1")     # the environment names one all the same
    _set(cur, **rows)
    before = _saved(cur)
    assert _migrate(scratch_dsn) == {"chosen": False, "written": {}}
    assert _saved(cur) == before
    settings_router.load_hqplayer_from_db()
    assert default_hqp._conns == []
