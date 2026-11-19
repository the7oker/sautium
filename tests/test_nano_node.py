"""The smallest node, "Nano" (PI-NODE-2B): no model runs, the streaming
library is off, nothing is passed on — three owner's switches a small board
is born with.

- The birth preset (db_migrate): written once, before the seed step, only
  where the owner's row does not exist; a node born Nano downloads no seed.
- The model switch (hardware_profile): off, the process never imports torch
  — checked in a process of its own, after `import main`, the profile and a
  Sound query; absent on an install without the model stack; the route that
  moves it.
- What a node with no model says instead of failing or waiting for ever:
  Discovery's `unavailable`, the assistant's three model-backed tools, the
  analysis routes, the radio.
- Streaming follows the streaming library's switch, both ways, while the
  process runs.
- Taking without passing on: the two sides of the sharing switch.
- The image's health check is not logged; a Raspberry Pi's weak supply is
  said once.
On a real PostgreSQL where a test reads or writes rows; skipped without one.
"""

import json
import logging
import os
import subprocess
import sys
import types
from pathlib import Path
from urllib.parse import urlparse

import pytest

REPO = Path(__file__).resolve().parent.parent
BACKEND = REPO / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

psycopg2 = pytest.importorskip("psycopg2")
import psycopg2.pool  # noqa: E402

import hardware_profile  # noqa: E402


@pytest.fixture
def db(scratch_dsn, monkeypatch):
    """The scratch database behind db_pool and config.settings, its settings
    emptied; yields an autocommit cursor."""
    import db_pool
    from config import settings
    url = urlparse(scratch_dsn)
    for key, value in dict(postgres_host=url.hostname, postgres_port=url.port,
                           postgres_user=url.username, postgres_password=url.password,
                           postgres_db=url.path.lstrip("/")).items():
        monkeypatch.setattr(settings, key, value)
    pool = psycopg2.pool.ThreadedConnectionPool(1, 3, dsn=scratch_dsn)
    monkeypatch.setattr(db_pool, "_pool", pool)
    conn = psycopg2.connect(scratch_dsn)
    conn.autocommit = True
    with conn.cursor() as c:
        c.execute("DELETE FROM user_settings")
        c.execute("DELETE FROM _schema_migrations WHERE filename = 'birth_preset_v1'")
        yield c
    conn.close()
    pool.closeall()


def _settings(c) -> dict:
    c.execute("SELECT key, value FROM user_settings")
    return dict(c.fetchall())


def _profile(models: str, name: str = "lite"):
    return hardware_profile.HardwareProfile(
        name=name, source="auto", device="cpu", accel_name=None, accel_memory_gb=4.0,
        ram_gb=4.0, cores=4, models=models)


# -- the birth preset ------------------------------------------------------------------

def _other_steps_done(monkeypatch, db_migrate):
    marked = db_migrate._marked
    monkeypatch.setattr(db_migrate, "_marked", lambda c, name: (
        marked(c, name) if name == "birth_preset_v1" else not name.startswith("seed_v")))


def test_a_node_born_nano_gets_its_switches_before_the_seed_and_downloads_none(db, monkeypatch):
    db_migrate = pytest.importorskip("db_migrate")
    import seed_import
    _other_steps_done(monkeypatch, db_migrate)
    monkeypatch.setenv("SAUTIUM_BIRTH_PRESET", "nano")
    monkeypatch.setattr(seed_import, "bundle_info", lambda: {
        "version": 0, "url": "https://seed.invalid/bundle", "sha256": "0" * 64, "size": 1})

    def no_download(*a, **kw):
        raise AssertionError("a node born with its streaming library off fetched the seed")
    monkeypatch.setattr(seed_import.urllib.request, "urlopen", no_download)
    monkeypatch.setattr(seed_import, "bundle_path", lambda info: Path("/nonexistent/bundle"))

    out = db_migrate.apply_pending()
    assert out["birth_preset"] == {"preset": "nano", "written": [
        "compute.models", "discovery.phantom_layer", "sync.pass_on"]}
    assert out["seed"] == {"complete": False, "skipped": "phantom_layer_off"}
    assert _settings(db) == {"compute.models": False, "discovery.phantom_layer": False,
                             "sync.pass_on": False, "node.birth_preset": "nano"}
    assert hardware_profile.models_switch() is False

    # The owner turns the models on; no later start writes the preset again
    db.execute("UPDATE user_settings SET value = 'true' WHERE key = 'compute.models'")
    assert "birth_preset" not in db_migrate.apply_pending()
    assert _settings(db)["compute.models"] is True


def test_the_preset_keeps_a_row_the_owner_already_has(db, scratch_dsn, monkeypatch):
    db_migrate = pytest.importorskip("db_migrate")
    db.execute("INSERT INTO user_settings (key, value) VALUES ('discovery.phantom_layer', 'true')")
    monkeypatch.setenv("SAUTIUM_BIRTH_PRESET", "nano")
    conn = psycopg2.connect(scratch_dsn)
    try:
        assert db_migrate._apply_birth_preset(conn) == {
            "preset": "nano", "written": ["compute.models", "sync.pass_on"]}
    finally:
        conn.close()
    assert _settings(db)["discovery.phantom_layer"] is True


def test_a_node_that_already_lives_is_not_born_again(db, monkeypatch):
    """The first pass under this code marks a database with no preset; the
    variable set in .env afterwards changes nothing."""
    db_migrate = pytest.importorskip("db_migrate")
    import seed_import
    _other_steps_done(monkeypatch, db_migrate)
    monkeypatch.setattr(seed_import, "bundle_info", lambda: None)
    monkeypatch.delenv("SAUTIUM_BIRTH_PRESET", raising=False)
    assert db_migrate.apply_pending()["birth_preset"] == {"preset": None, "written": []}
    monkeypatch.setenv("SAUTIUM_BIRTH_PRESET", "nano")
    assert "birth_preset" not in db_migrate.apply_pending()
    assert _settings(db) == {}


def test_a_name_that_is_no_preset_stops_the_start_and_marks_nothing(db, monkeypatch):
    db_migrate = pytest.importorskip("db_migrate")
    import seed_import
    _other_steps_done(monkeypatch, db_migrate)
    monkeypatch.setattr(seed_import, "bundle_info", lambda: None)
    monkeypatch.setenv("SAUTIUM_BIRTH_PRESET", "tiny")
    with pytest.raises(ValueError, match="names no preset"):
        db_migrate.apply_pending()
    db.execute("SELECT 1 FROM _schema_migrations WHERE filename = 'birth_preset_v1'")
    assert db.fetchone() is None


# -- the model switch --------------------------------------------------------------------

def test_the_switch_is_on_while_never_set_and_off_only_by_the_owners_false(db):
    assert hardware_profile.models_switch() is True
    db.execute("INSERT INTO user_settings (key, value) VALUES ('compute.models', 'false')")
    assert hardware_profile.models_switch() is False
    db.execute("UPDATE user_settings SET value = 'true'")
    assert hardware_profile.models_switch() is True


def test_models_off_or_absent_is_decided_before_torch_is_imported(monkeypatch):
    """`None` in sys.modules makes any `import torch` fail: the off and
    absent answers come without one."""
    monkeypatch.setitem(sys.modules, "torch", None)
    monkeypatch.setattr(hardware_profile.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(hardware_profile, "models_switch", lambda: False)
    assert hardware_profile._detect_hardware()[0::5] == ("cpu", "off")
    monkeypatch.setattr(hardware_profile.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(hardware_profile, "models_switch", lambda: pytest.fail("asked of no stack"))
    assert hardware_profile._detect_hardware()[0::5] == ("cpu", "absent")


def test_a_node_with_no_model_is_lite_and_named_nano():
    for models in ("off", "absent"):
        profile = _profile(models)
        assert profile.nano and not profile.ml_available and not profile.local_analysis
        assert profile.prewarm_keys == () and not profile.instruments_by_default
        assert not profile.translation_available
        assert profile.describe()["models"] == models and profile.describe()["nano"] is True
    assert not _profile("on").nano
    # A forced tier a machine cannot honour is still no Nano, and runs no model
    forced = _profile("off", name="full")
    assert not forced.nano and forced.prewarm_keys == ()


_NO_TORCH_PROBE = """
import sys
import main                                   # every router, as the node imports them
import hardware_profile
profile = hardware_profile.resolve()
assert profile.models == "off" and profile.nano, profile
from routers.discovery import run_search
answer = run_search("track", sound="calm piano at night")
assert answer["unavailable"] and not answer["warming"] and answer["results"] == [], answer
assert hardware_profile.cap_torch_threads() is None
loaded = sorted(m for m in ("torch", "transformers", "librosa", "sentence_transformers",
                            "ctranslate2", "device") if m in sys.modules)
print("LOADED", loaded)
"""


def test_a_process_whose_switch_is_off_never_imports_torch(db, scratch_dsn):
    pytest.importorskip("torch")              # where the stack is absent there is nothing to hold
    db.execute("INSERT INTO user_settings (key, value) VALUES ('compute.models', 'false')")
    url = urlparse(scratch_dsn)
    env = dict(os.environ, POSTGRES_HOST=url.hostname, POSTGRES_PORT=str(url.port),
               POSTGRES_USER=url.username, POSTGRES_PASSWORD=url.password,
               POSTGRES_DB=url.path.lstrip("/"), PYTHONPATH=os.pathsep.join([str(BACKEND), str(REPO)]))
    env.pop("SAUTIUM_PROFILE", None)
    out = subprocess.run([sys.executable, "-c", _NO_TORCH_PROBE], capture_output=True, text=True,
                         env=env, cwd=BACKEND, timeout=300)
    assert out.returncode == 0, out.stderr[-3000:]
    assert "LOADED []" in out.stdout, out.stdout[-500:]


def test_the_switchs_route_stores_it_and_says_a_restart_applies_it(db, monkeypatch):
    settings_router = pytest.importorskip("routers.settings")
    from fastapi import HTTPException
    monkeypatch.setattr(hardware_profile, "resolve", lambda: _profile("on", name="standard"))
    state = settings_router.put_models_switch(settings_router.ModelsPrefs(on=False))
    assert _settings(db) == {"compute.models": False}
    assert (state["models"], state["models_switch"], state["restart_required"]) == ("on", False, True)
    # After the restart the profile and the switch agree
    monkeypatch.setattr(hardware_profile, "resolve", lambda: _profile("off"))
    state = settings_router.get_hardware_profile()
    assert (state["models"], state["nano"], state["restart_required"]) == ("off", True, False)
    assert settings_router.put_models_switch(settings_router.ModelsPrefs(on=True))["restart_required"]
    # An install set up without the models has no switch to move
    monkeypatch.setattr(hardware_profile, "resolve", lambda: _profile("absent"))
    with pytest.raises(HTTPException) as refused:
        settings_router.put_models_switch(settings_router.ModelsPrefs(on=True))
    assert refused.value.status_code == 409 and "set up without" in refused.value.detail
    assert not settings_router.get_hardware_profile()["restart_required"]


# -- what a node with no model says --------------------------------------------------------

@pytest.mark.parametrize("params", [{"sound": "calm piano"}, {"lyrics": "rain and sadness"},
                                    {"q": "a British rock band", "scope": "bio"}])
def test_a_model_backed_scope_is_unavailable_not_warming_where_no_model_runs(db, monkeypatch, params):
    discovery = pytest.importorskip("routers.discovery")
    import discovery_engine
    kicked = []
    monkeypatch.setattr(discovery_engine, "_kick_model", kicked.append)      # no model is loaded here
    monkeypatch.setattr(hardware_profile, "resolve", lambda: _profile("off"))
    target = "artist" if params.get("scope") == "bio" else "track"
    answer = discovery.run_search(target, **params)
    assert (answer["unavailable"], answer["warming"], answer["limited"]) == (True, False, False)
    # ...and where one does, a cold model is "warming" as before
    monkeypatch.setattr(hardware_profile, "resolve", lambda: _profile("on"))
    answer = discovery.run_search(target, **params)
    assert (answer["unavailable"], answer["warming"]) == (False, True)


def test_the_names_scope_needs_no_model_anywhere(db, monkeypatch):
    discovery = pytest.importorskip("routers.discovery")
    monkeypatch.setattr(hardware_profile, "resolve", lambda: _profile("absent"))
    answer = discovery.run_search("track", q="hello")
    assert (answer["unavailable"], answer["warming"]) == (False, False)


def test_the_assistants_model_backed_tools_say_why(monkeypatch):
    """Both tool surfaces word the engine's state through one function; a
    bare "(0 results)" told an agent nothing it could pass on."""
    import assistant_queries as aq
    assert aq.search_note({"results": []}) == ""
    assert "none runs on this node" in aq.search_note({"unavailable": True})
    assert "still loading" in aq.search_note({"warming": True})
    assert aq.with_note("x", "") == "x" and aq.with_note("x", "why") == "x\n\nwhy"

    definitions = pytest.importorskip("tools.definitions")
    from routers import discovery
    monkeypatch.setattr(discovery, "run_search", lambda target, **params: {
        "results": [], "unavailable": True, "warming": False, "limited": False})
    for text in (definitions._h_search_semantic("calm piano"),
                 definitions._h_search_lyrics("rain"),
                 definitions._h_search_artists("a rock band", by_bio=True)):
        assert "none runs on this node" in text, text

    source = (REPO / "mcp" / "assistant_server.py").read_text(encoding="utf-8")
    # its definition, the plain reader over it, and the three tools
    assert source.count("_discovery_noted(") == 5
    assert "aq.search_note(data)" in source


def test_the_analysis_routes_refuse_where_no_model_runs(monkeypatch):
    monkeypatch.chdir(BACKEND)                 # main mounts static/ relative to its directory
    main = pytest.importorskip("main")
    import asyncio
    from fastapi import HTTPException
    for models, words in (("off", "switched off"), ("absent", "no AI models installed")):
        monkeypatch.setattr(hardware_profile, "resolve", lambda: _profile(models))
        for call in (main.generate_embeddings_endpoint, main.enrich_tracks_endpoint):
            with pytest.raises(HTTPException) as refused:
                asyncio.run(call())
            assert refused.value.status_code == 400 and words in refused.value.detail
        with pytest.raises(HTTPException) as refused:
            main._require_models()
        assert "arrives from other Sautium nodes" in refused.value.detail
    monkeypatch.setattr(hardware_profile, "resolve", lambda: _profile("on"))
    main._require_models()


def test_the_radio_does_not_promise_an_analysis_this_node_never_makes():
    source = (BACKEND / "routers" / "player.py").read_text(encoding="utf-8")
    at = source.index("This track isn't analysed yet — play it a moment")
    assert "hardware_profile.resolve().ml_available" in source[at:at + 300]
    assert "This node analyses nothing itself" in source[at:at + 500]


# -- streaming follows the streaming library ---------------------------------------------------

@pytest.fixture
def streaming(monkeypatch):
    service = pytest.importorskip("streaming.service")
    for name, value in dict(_registry=None, _enricher=None, _lyrics_enricher=None, _loop=None,
                            _refresh_task=None, _wired=False).items():
        monkeypatch.setattr(service, name, value)
    proxy = types.SimpleNamespace(track_ready_hooks=[], track_failed_hooks=[], link_admissible=None)
    monkeypatch.setattr(service, "ensure_proxy", lambda: proxy)
    monkeypatch.setattr(service, "_persist_registry", lambda registry: None)
    monkeypatch.setattr(service, "demo_runtime", lambda: False)
    observers = []
    from playback.manager import manager
    monkeypatch.setattr(manager, "subscribe_status", observers.append)
    switch = {"on": True}
    from routers import settings as settings_router
    monkeypatch.setattr(settings_router, "_read", lambda key: (
        switch["on"] if key == "discovery.phantom_layer" else None))
    monkeypatch.setattr(hardware_profile, "resolve", lambda: _profile("off"))
    conf = types.SimpleNamespace(
        streaming_preview_enabled=True, streaming_preview_analyze=True, ffmpeg_location=None,
        streaming_providers_dir=str(REPO / "tests" / "no-such-providers"),
        media_proxy_host="127.0.0.1", media_proxy_port=1, media_proxy_advertised_host="127.0.0.1")
    return service, switch, conf, proxy, observers


def test_streaming_starts_and_stops_with_the_streaming_library(streaming):
    service, switch, conf, proxy, observers = streaming
    switch["on"] = False
    assert service.follow_switch(conf) is False and not service.is_enabled()
    assert service.providers_preferred() == [] and proxy.track_ready_hooks == []

    switch["on"] = True
    assert service.follow_switch(conf) is True and service.is_enabled()
    assert [p.manifest.id for p in service.providers_preferred()] == ["deezer_preview"]
    hooks = (len(proxy.track_ready_hooks), len(proxy.track_failed_hooks), len(observers))
    assert hooks == (2, 1, 1)
    lyrics = service._lyrics_enricher
    # No model on this node: a stream's audio is not analysed, its lyrics still fetched
    assert service._enricher is None and lyrics is not None

    switch["on"] = False
    assert service.follow_switch(conf) is False and not service.is_enabled()
    assert service.get_provider() is None and lyrics._stopped
    assert service._lyrics_enricher is None

    # On again: providers again, and the proxy's hooks are not attached twice
    switch["on"] = True
    assert service.follow_switch(conf) is True
    assert (len(proxy.track_ready_hooks), len(proxy.track_failed_hooks), len(observers)) == hooks
    service.stop_enrichers()


def test_a_deployments_own_off_keeps_streaming_down_whatever_the_switch(streaming):
    service, switch, conf, proxy, _ = streaming
    conf.streaming_preview_enabled = False
    assert service.follow_switch(conf) is False and not service.is_enabled()


def test_a_stream_ready_after_the_switch_went_off_is_not_enriched(streaming):
    service, switch, conf, proxy, _ = streaming
    service.follow_switch(conf)
    switch["on"] = False
    service.follow_switch(conf)
    service._on_track_ready(types.SimpleNamespace(query=None, audio=None, provider=None))


def test_the_switchs_route_moves_streaming(db, monkeypatch):
    settings_router = pytest.importorskip("routers.settings")
    from streaming import service
    seen = []
    monkeypatch.setattr(service, "follow_switch", lambda conf: seen.append(
        settings_router._read("discovery.phantom_layer")))
    monkeypatch.setattr(settings_router, "_phantom_section", lambda: {})
    from canon import scrobbles
    monkeypatch.setattr(scrobbles, "wake", lambda: None)
    settings_router.put_phantom_prefs(settings_router.PhantomPrefs(enabled=False))
    settings_router.put_phantom_prefs(settings_router.PhantomPrefs(enabled=True))
    assert seen == [False, True]              # asked after the write, with the new value


# -- taking without passing on -----------------------------------------------------------------

@pytest.fixture
def sync_router(db):
    router = pytest.importorskip("routers.sync")
    router.sharing_changed()
    yield router
    router.sharing_changed()


def test_a_node_takes_without_passing_on(db, sync_router):
    from fastapi import HTTPException
    assert (sync_router.sharing_enabled(), sync_router.serving_enabled()) == (True, True)
    sync_router._require_sharing()

    db.execute("INSERT INTO user_settings (key, value) VALUES ('sync.pass_on', 'false')")
    sync_router.sharing_changed()
    assert (sync_router.sharing_enabled(), sync_router.serving_enabled()) == (True, False)
    with pytest.raises(HTTPException) as refused:
        sync_router._require_sharing()
    assert refused.value.status_code == 403

    # Sharing off stops both sides, whatever the second switch says
    db.execute("UPDATE user_settings SET value = 'true' WHERE key = 'sync.pass_on'")
    db.execute("INSERT INTO user_settings (key, value) VALUES ('sync.p2p_enabled', 'false')")
    sync_router.sharing_changed()
    assert (sync_router.sharing_enabled(), sync_router.serving_enabled()) == (False, False)


def test_the_peer_surface_says_it_does_not_share_while_it_passes_nothing_on():
    """/health's `sharing` is what a walker and a slice requester pick sources
    by: a node that passes nothing on is walked by nobody."""
    source = (BACKEND / "p2p_app.py").read_text(encoding="utf-8")
    assert '"sharing": serving_enabled(),' in source
    assert "sharing_enabled" not in source


def test_the_launchers_server_reads_the_same_two_sides(db, scratch_dsn):
    from desktop.p2p.sync_server import SyncServer
    server = SyncServer.__new__(SyncServer)
    server._sharing, server._serving, server._sharing_checked = True, True, 0.0
    pool = psycopg2.pool.ThreadedConnectionPool(1, 1, dsn=scratch_dsn)
    server._pool, server._pool_lock = pool, __import__("threading").Lock()
    try:
        assert server.sharing_changed() == (True, True)
        db.execute("INSERT INTO user_settings (key, value) VALUES ('sync.pass_on', 'false')")
        assert server.sharing_changed() == (True, False)
        assert server._sharing_enabled() and not server._serving_enabled()
        db.execute("INSERT INTO user_settings (key, value) VALUES ('sync.p2p_enabled', 'false')")
        assert server.sharing_changed() == (False, False)
    finally:
        pool.closeall()
    # Every route answers by the serving side; nothing below the handlers reads the taking one
    handlers = (REPO / "desktop" / "p2p" / "sync_server.py").read_text(encoding="utf-8").split(
        "    # Route handlers\n", 1)[1]
    assert handlers.count("self._serving_enabled()") == 9
    assert "self._sharing_enabled()" not in handlers


def test_the_walk_offers_a_carrier_nothing_while_it_passes_nothing_on(monkeypatch):
    from desktop.p2p import sync_queries
    from desktop.p2p.sync_walk import SyncWalk
    walk = SyncWalk("postgresql://unused", identity=lambda: None,
                    sharing_enabled=lambda: True, passing_on=lambda: False)
    offered = []

    class Conn:
        def close(self):
            pass
    monkeypatch.setattr("desktop.p2p.sync_walk._connect", lambda dsn: Conn())
    monkeypatch.setattr(sync_queries, "get_pushable_tracks",
                        lambda conn, limit: [{"recordings": ["11111111-1111-1111-1111-111111111111"]}])
    peer = types.SimpleNamespace(carry_offer=lambda recordings: offered.append(recordings) or {})
    assert walk._push_to_carrier(peer, lambda msg: None) == 0
    assert offered == []
    # Unset, the second side is the sharing switch itself
    assert SyncWalk("postgresql://unused", identity=lambda: None,
                    sharing_enabled=lambda: True)._passing_on() is True


def test_the_sync_route_moves_the_second_switch_and_wakes_serving(db, sync_router, monkeypatch):
    settings_router = pytest.importorskip("routers.settings")
    monkeypatch.setattr(settings_router, "_sync_state", lambda: {})
    assert sync_router.serving_enabled()
    settings_router.put_sync_prefs(settings_router.SyncPrefs(pass_on=False))
    assert _settings(db) == {"sync.pass_on": False}
    assert sync_router.sharing_enabled() and not sync_router.serving_enabled()


# -- the health check's line, the weak supply ----------------------------------------------------

def test_the_images_health_check_is_served_unsigned_and_left_out_of_the_log():
    import auth_hmac
    import config
    assert config.HEALTH_PROBE_PATH in auth_hmac.WHITELIST_EXACT
    probe = config.AccessLineNotTheProbe()

    def line(path, status):
        return logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                                 ("127.0.0.1:1", "GET", path, "1.1", status), None)
    assert not probe.filter(line("/healthz", 204))
    assert probe.filter(line("/", 200)) and probe.filter(line("/health", 200))
    assert probe.filter(line("/healthz", 500))          # a failing check is worth its line
    assert config.LOGGING_CONFIG["loggers"]["uvicorn.access"]["filters"] == [
        "access_without_query", "access_not_the_probe"]


def test_a_weak_supply_is_said_once_and_only_where_the_flag_exists(tmp_path, monkeypatch):
    import power_watch
    assert power_watch.attribute(tmp_path / "none") is None
    for name, device in (("hwmon0", "cpu_thermal"), ("hwmon1", "rpi_volt")):
        (tmp_path / name).mkdir()
        (tmp_path / name / "name").write_text(device + "\n")
        (tmp_path / name / "in0_lcrit_alarm").write_text("0\n")
    assert power_watch.attribute(tmp_path) == tmp_path / "hwmon1" / "in0_lcrit_alarm"

    readings, waits, raised = iter(["0\n", "0\n", "1\n"]), [], []
    power_watch.watch(lambda: next(readings), lambda: waits.append(1), lambda: raised.append(1))
    assert (len(waits), raised) == (2, [1])             # blocked twice, said once, then done

    monkeypatch.setattr(power_watch, "_since", None)
    assert power_watch.notice() is None
    woken = []
    import db_pool
    monkeypatch.setattr(db_pool, "db_execute", lambda sql, *a: woken.append(sql))
    power_watch._raise()
    notice = power_watch.notice()
    assert notice["key"] == "power.undervoltage" and notice["kind"] == "warning"
    assert woken == ["NOTIFY sautium_notices"]
    settings_router = pytest.importorskip("routers.settings")
    assert "power.undervoltage" in settings_router._REPORTS
    assert "NOTICE_COPY['power.undervoltage']" in (
        BACKEND / "static" / "app-shell.js").read_text(encoding="utf-8")
