""""This node" in the Web UI (PI-NODE-2B §4.4–§4.5): what the Hardware
profile block reads and does on 2A's interfaces.

- The state: what the install carries, the machine's name, the version the
  tree states; the downloaded models' size on a node that runs none.
- The restart: only where a restart policy holds the node, and only after
  the answer has been sent.
- The downloaded models' removal: the owner's act, refused while a model
  can still load.
- The host agent: the release a check offered while the node runs an older
  one, the daily check asked once a day, the notices `update.available` and
  `update.result`.
- The block's words, app-shell.js's own functions under Node: the name
  "Nano", the model switch's row, and every outcome of `check` and `update`
  to its sentence — a refusal's detail (the host's words, an HTML string
  among them) escaped.
On a real PostgreSQL where a row is read; skipped without one.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest

# These tests move the owner's models switch and read it back from their database
pytestmark = pytest.mark.models_switch

REPO = Path(__file__).resolve().parent.parent
BACKEND = REPO / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

psycopg2 = pytest.importorskip("psycopg2")
import psycopg2.pool  # noqa: E402

import hardware_profile  # noqa: E402
import host_channel  # noqa: E402

NODE = shutil.which("node")
SOURCE = (BACKEND / "static" / "app-shell.js").read_text(encoding="utf-8")


def _profile(models: str, name: str = "lite"):
    return hardware_profile.HardwareProfile(
        name=name, source="auto", device="cpu", accel_name=None, accel_memory_gb=4.0,
        ram_gb=4.0, cores=4, models=models)


@pytest.fixture
def db(scratch_dsn, monkeypatch):
    import db_pool
    pool = psycopg2.pool.ThreadedConnectionPool(1, 3, dsn=scratch_dsn, options="-c timezone=UTC")
    monkeypatch.setattr(db_pool, "_pool", pool)
    conn = psycopg2.connect(scratch_dsn)
    conn.autocommit = True
    with conn.cursor() as c:
        c.execute("TRUNCATE host_requests")
        c.execute("DELETE FROM user_settings")
        yield c
    conn.close()
    pool.closeall()


# -- the state ---------------------------------------------------------------------------------

def test_the_block_reads_what_the_install_carries_and_what_runs(db, tmp_path, monkeypatch):
    settings_router = pytest.importorskip("routers.settings")
    import claude_code_runner
    import model_cache
    from desktop import updater
    from streaming import service as streaming_service
    monkeypatch.setattr(hardware_profile, "resolve", lambda: _profile("off"))
    monkeypatch.setattr(settings_router.shutil, "which", lambda name: None)
    monkeypatch.setattr(streaming_service, "demo_runtime", lambda: False)
    monkeypatch.setattr(claude_code_runner, "IN_DOCKER", True)
    monkeypatch.setattr(updater, "installer_version", lambda: (0, 3, 0))
    monkeypatch.setattr(model_cache, "store_bytes", lambda: 4_200_000_000)
    cpuinfo = tmp_path / "cpuinfo"
    cpuinfo.write_text("processor\t: 0\nBogoMIPS\t: 108.00\n\nRevision\t: d04170\n"
                       "Model\t\t: Raspberry Pi 5 Model B Rev 1.0\n")
    monkeypatch.setattr(settings_router, "_CPUINFO", str(cpuinfo))
    db.execute("INSERT INTO user_settings (key, value) VALUES ('compute.models', 'false')")
    state = settings_router.get_hardware_profile()
    assert (state["nano"], state["models"], state["restart_required"]) == (True, "off", False)
    assert state["image"] == {"models": True, "assistant_cli": False, "streaming": False}
    assert state["machine"] == "Raspberry Pi 5 Model B Rev 1.0"
    assert state["software"]["version"] == "0.3.0" and "commit" in state["software"]
    assert state["models_cache_bytes"] == 4_200_000_000 and state["restartable"] is True
    # A launcher's model cache is the machine's, not the node's: no size is told, so no Remove is offered
    monkeypatch.setattr(claude_code_runner, "IN_DOCKER", False)
    assert "models_cache_bytes" not in settings_router.get_hardware_profile()
    monkeypatch.setattr(claude_code_runner, "IN_DOCKER", True)
    # A node whose models run is told no cache size: nothing there is its owner's to remove
    monkeypatch.setattr(hardware_profile, "resolve", lambda: _profile("on", name="standard"))
    monkeypatch.setattr(settings_router, "_CPUINFO", str(tmp_path / "none"))
    state = settings_router.get_hardware_profile()
    assert "models_cache_bytes" not in state and state["machine"] is None
    assert state["restart_required"] is True          # the switch says off: applied at the next start
    monkeypatch.setattr(hardware_profile, "resolve", lambda: _profile("absent"))
    state = settings_router.get_hardware_profile()
    assert state["image"]["models"] is False and state["birth_preset"] is None
    # What the setup made this node at its birth: the models row's words read it
    db.execute("INSERT INTO user_settings (key, value) VALUES ('node.birth_preset', '\"nano\"')")
    assert settings_router.get_hardware_profile()["birth_preset"] == "nano"


def test_the_block_says_whether_the_node_holds_an_offline_database(db, monkeypatch):
    """PI-NODE-2E §5: `dumps_held` — either dump family loaded or loading —
    on the hardware answer the More list reads, and on the Offline databases
    screen's own answer, one rule for both."""
    settings_router = pytest.importorskip("routers.settings")
    monkeypatch.setattr(hardware_profile, "resolve", lambda: _profile("off"))

    def held() -> tuple:
        return (settings_router.get_hardware_profile()["dumps_held"],
                settings_router.get_databases()["dumps_held"])

    assert held() == (False, False)
    # a load on its way is held: its screen shows the progress
    monkeypatch.setitem(settings_router.lb_job.state, "running", True)
    assert held() == (True, True)
    monkeypatch.setitem(settings_router.lb_job.state, "running", False)
    db.execute("INSERT INTO mb_artist (id, gid, name, sort_name) "
               "VALUES (1, gen_random_uuid(), 'Sade', 'Sade')")
    db.execute("ANALYZE mb_artist")
    try:
        # rows a slice brought are no dump: every node that asks for slices holds some (the box's
        # evening, 2026-11-26: a Nano node's More kept the row) — the full load's marker is the dump
        assert held() == (False, False)
        db.execute("INSERT INTO user_settings (key, value) VALUES ('musicbrainz.db_version', '\"20261122\"')")
        assert held() == (True, True)
    finally:
        db.execute("DELETE FROM user_settings WHERE key = 'musicbrainz.db_version'")
        db.execute("DELETE FROM mb_artist")
        db.execute("ANALYZE mb_artist")
    assert held() == (False, False)


# -- the restart ---------------------------------------------------------------------------------

def test_a_restart_ends_the_process_only_after_its_answer_and_only_in_the_image(monkeypatch):
    settings_router = pytest.importorskip("routers.settings")
    import claude_code_runner
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(settings_router.router)
    killed = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr(claude_code_runner, "IN_DOCKER", False)
    with TestClient(app) as web:
        refused = web.post("/api/settings/restart")
        assert refused.status_code == 409 and "launcher" in refused.json()["detail"] and killed == []
        monkeypatch.setattr(claude_code_runner, "IN_DOCKER", True)
        answer = web.post("/api/settings/restart")
        assert answer.status_code == 200 and answer.json() == {"restarting": True}
    assert killed == [(os.getpid(), signal.SIGTERM)]


# -- the downloaded models -------------------------------------------------------------------------

def test_the_downloaded_models_are_counted_once_and_removed_by_the_owner_alone(tmp_path, monkeypatch):
    settings_router = pytest.importorskip("routers.settings")
    import claude_code_runner
    import model_cache
    from fastapi import HTTPException
    hub = tmp_path / "cache" / "huggingface" / "hub"
    blobs = hub / "models--laion--clap" / "blobs"
    blobs.mkdir(parents=True)
    (blobs / "abc").write_bytes(b"w" * 1000)
    snapshot = hub / "models--laion--clap" / "snapshots" / "r1"
    snapshot.mkdir(parents=True)
    (snapshot / "model.safetensors").symlink_to(blobs / "abc")       # the hub's links: not counted twice
    (tmp_path / "cache" / "sautium-ct2" / "madlad").mkdir(parents=True)
    (tmp_path / "cache" / "sautium-ct2" / "madlad" / "model.bin").write_bytes(b"m" * 500)
    kept = tmp_path / "cache" / "sautium" / "yt-dlp"
    kept.mkdir(parents=True)
    (kept / "build").write_bytes(b"not a model")
    monkeypatch.setenv("HF_HOME", str(tmp_path / "cache" / "huggingface"))
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    # A launcher reads the machine's caches, every program's: none of it is counted, offered or removed
    monkeypatch.setattr(claude_code_runner, "IN_DOCKER", False)
    monkeypatch.setattr(hardware_profile, "resolve", lambda: _profile("off"))
    assert model_cache.store_dirs() == [] and model_cache.store_bytes() == 0
    with pytest.raises(HTTPException) as refused:
        settings_router.delete_models_cache()
    assert refused.value.status_code == 409 and (blobs / "abc").exists()
    assert model_cache.remove_store() == 0 and (blobs / "abc").exists()
    monkeypatch.setattr(claude_code_runner, "IN_DOCKER", True)
    assert model_cache.store_bytes() == 1500

    monkeypatch.setattr(hardware_profile, "resolve", lambda: _profile("on"))
    with pytest.raises(HTTPException) as refused:
        settings_router.delete_models_cache()
    assert refused.value.status_code == 409 and model_cache.store_bytes() == 1500
    monkeypatch.setattr(hardware_profile, "resolve", lambda: _profile("off"))
    assert settings_router.delete_models_cache() == {"freed_bytes": 1500}
    assert model_cache.store_bytes() == 0 and not (tmp_path / "cache" / "huggingface").exists()
    assert (kept / "build").read_bytes() == b"not a model"           # another cache is not its to take
    assert settings_router.delete_models_cache() == {"freed_bytes": 0}


# -- the host agent ---------------------------------------------------------------------------------

@pytest.fixture
def agent(db, tmp_path, monkeypatch):
    from desktop import updater
    requests = tmp_path / "requests"
    requests.mkdir()
    monkeypatch.setattr(host_channel, "REQUESTS_DIR", requests)
    monkeypatch.setattr(host_channel, "_KEY", b"cd" * 32)
    monkeypatch.setattr(updater, "installer_version", lambda: (0, 3, 0))
    return db, requests


def _answered(c, verb, outcome, release=None, detail=""):
    c.execute("""INSERT INTO host_requests (verb, asked_at, outcome, release, detail, answered_at)
                 VALUES (%s, now() - interval '1 minute', %s, %s, %s, now())
                 ON CONFLICT (verb) DO UPDATE SET outcome = EXCLUDED.outcome, release = EXCLUDED.release,
                     detail = EXCLUDED.detail, answered_at = EXCLUDED.answered_at""",
              (verb, outcome, release, detail))


def test_a_release_is_offered_only_while_the_node_runs_an_older_one(agent, monkeypatch):
    host = pytest.importorskip("routers.host")
    c, _ = agent
    assert host.offered_release() is None
    _answered(c, "check", "offered", "v0.3.1")
    offered = host.offered_release()
    assert offered["release"] == "v0.3.1" and offered["since"]
    # The update moved the node: the check's row is unchanged, the offer is over
    from desktop import updater
    monkeypatch.setattr(updater, "installer_version", lambda: (0, 3, 1))
    assert host.offered_release() is None
    monkeypatch.setattr(updater, "installer_version", lambda: (0, 3, 0))
    for outcome, release in (("current", "v0.3.0"), ("rewrite", "v0.4.0"), ("offered", "not-a-version")):
        _answered(c, "check", outcome, release)
        assert host.offered_release() is None, outcome
    _answered(c, "check", "offered", "v0.3.1")
    monkeypatch.setattr(host_channel, "_KEY", None)                  # no agent: nothing to offer
    assert host.offered_release() is None


def test_the_node_asks_for_a_check_once_a_day_by_itself(agent, monkeypatch):
    host = pytest.importorskip("routers.host")
    c, requests = agent
    assert host.check_if_due() is True
    assert [p.name for p in requests.iterdir()] == ["check"]
    (requests / "check").unlink()                                    # the agent took it
    assert host.check_if_due() is False and list(requests.iterdir()) == []
    c.execute("UPDATE host_requests SET asked_at = now() - interval '25 hours'")
    assert host.check_if_due() is True
    # A name the request cannot be left under (a folder a container made there): not asked, said, and
    # nothing raised into the node's start or the background pass
    (requests / "check").unlink()
    (requests / "check").mkdir()
    c.execute("UPDATE host_requests SET asked_at = now() - interval '25 hours'")
    assert host.check_if_due() is False
    c.execute("SELECT asked_at < now() - interval '1 day' FROM host_requests WHERE verb = 'check'")
    assert c.fetchone()[0] is True                                   # not recorded as asked
    (requests / "check").rmdir()
    monkeypatch.setattr(host_channel, "_KEY", None)
    assert host.check_if_due() is False


def test_an_offered_release_and_an_updates_end_are_notices(agent, monkeypatch):
    settings_router = pytest.importorskip("routers.settings")
    c, _ = agent

    def notices() -> dict:
        return {n["key"]: n for n in settings_router._notices_state()["items"]
                if n["key"].startswith("update.")}
    assert notices() == {}
    _answered(c, "check", "offered", "v0.3.1")
    offered = notices()
    assert list(offered) == ["update.available"]
    assert offered["update.available"]["data"] == {"release": "v0.3.1"}
    for outcome, kind in (("updated", "info"), ("undone", "warning")):
        _answered(c, "update", outcome, "v0.3.1")
        result = notices()["update.result"]
        assert (result["kind"], result["data"]["outcome"], result["data"]["release"]) == (
            kind, outcome, "v0.3.1")
        assert result["data"]["running"] == "0.3.0"
    # A report: dropped once the owner has seen it
    assert "update.result" in settings_router._REPORTS
    c.execute("INSERT INTO user_settings (key, value) VALUES ('notice.seen', %s::jsonb)",
              (json.dumps({"update.result": result["since"]}),))
    assert "update.result" not in notices()
    # A refusal is the block's line, never a notice
    _answered(c, "update", "refused", "v0.3.1", "the checkout has local changes")
    assert "update.result" not in notices()


# -- the block's words --------------------------------------------------------------------------------

def _block(head: str, end: str = "\n  }\n") -> str:
    start = SOURCE.index(f"\n  {head}") + 1
    return SOURCE[start:SOURCE.index(end, start) + len(end)]


DRIVER = r"""
'use strict';
const PROFILE_DISPLAY_NAMES = { full: 'Curator', standard: 'Standard', lite: 'Lite' };
const escapeProfileHtml = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const fmtRelative = iso => '2 h ago';
%(fns)s
const text = html => html.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
const out = { names: [], models: {}, software: {} };
for (const hw of %(profiles)s) out.names.push(hwProfileName(hw));
for (const [name, hw] of Object.entries(%(models)s)) out.models[name] = text(hwModelsRow(hw));
for (const [name, c] of Object.entries(%(cases)s)) {
  const html = hwSoftwareRow(c.hw, c.host);
  out.software[name] = { text: text(html), raw: html };
}
process.stdout.write(JSON.stringify(out));
"""

HW = {"software": {"version": "0.3.0", "commit": "4f2a91c0e7b3"}, "restartable": True}
T0, T1, T2 = "2026-11-19T10:00:00+00:00", "2026-11-19T10:05:00+00:00", "2026-11-19T10:10:00+00:00"


def _host(check=None, update=None, offered=None, available=True) -> dict:
    return {"available": available, "offered": offered,
            "requests": {k: v for k, v in (("check", check), ("update", update)) if v}}


CASES = {
    "no agent": {"hw": HW, "host": _host(available=False)},
    "a launcher's": {"hw": {**HW, "restartable": False}, "host": _host(available=False)},
    "never checked": {"hw": HW, "host": _host()},
    "checking": {"hw": HW, "host": _host(check={"asked_at": T1})},
    "current": {"hw": HW, "host": _host(check={"asked_at": T0, "answered_at": T1, "outcome": "current"})},
    "offered": {"hw": HW, "host": _host(offered="v0.3.1", check={
        "asked_at": T0, "answered_at": T1, "outcome": "offered", "release": "v0.3.1"})},
    "floor": {"hw": HW, "host": _host(check={"asked_at": T0, "answered_at": T1, "outcome": "floor"})},
    "rewrite": {"hw": HW, "host": _host(check={"asked_at": T0, "answered_at": T1, "outcome": "rewrite"})},
    "refused": {"hw": HW, "host": _host(check={"asked_at": T0, "answered_at": T1, "outcome": "refused"})},
    "updating": {"hw": HW, "host": _host(offered="v0.3.1", update={"asked_at": T1}, check={
        "asked_at": T0, "answered_at": T0, "outcome": "offered"})},
    "update refused": {"hw": HW, "host": _host(offered="v0.3.1", check={
        "asked_at": T0, "answered_at": T1, "outcome": "offered"}, update={
        "asked_at": T1, "answered_at": T2, "outcome": "refused",
        "detail": "the checkout has <b>local</b> changes"})},
}


FULL_NODE = ("Offline databases (the MusicBrainz catalogue, 7.5 GB) are offered on a full node: "
             "turning the AI models on makes this one.")


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_blocks_rows_say_each_state_in_its_words():
    fns = "\n".join([_block("const DUMPS_ON_A_FULL_NODE = ", ";\n"), "let _hardware = null;",
                     *(_block(head) for head in (
                         "function dumpsOffered(hw = _hardware) {", "function hwProfileName(hw) {",
                         "function hwModelsRow(hw) {", "function hwSoftwareRow(hw, host) {"))])
    profiles = [{"nano": True, "profile": "lite"}, {"nano": False, "profile": "lite"},
                {"nano": False, "profile": "full"}]
    models = {
        "on": {"models": "on", "models_switch": True, "restart_required": False},
        "off": {"models": "off", "models_switch": False, "restart_required": False},
        "turned on, not restarted": {"models": "off", "models_switch": True, "restart_required": True},
        "absent in an image": {"models": "absent", "restartable": True},
        "absent on a node born Nano": {"models": "absent", "restartable": True, "birth_preset": "nano"},
        "absent on a machine": {"models": "absent", "restartable": False},
        # PI-NODE-2E §5: a Nano node holding no offline database is told where they are offered
        "off on a Nano node": {"models": "off", "models_switch": False, "restart_required": False,
                               "nano": True, "dumps_held": False},
        "absent on a Nano node": {"models": "absent", "restartable": True, "birth_preset": "nano",
                                  "nano": True, "dumps_held": False},
        "a Nano node holding one": {"models": "off", "models_switch": False, "restart_required": False,
                                    "nano": True, "dumps_held": True},
    }
    driver = DRIVER % {"fns": fns, "profiles": json.dumps(profiles), "models": json.dumps(models),
                       "cases": json.dumps(CASES)}
    proc = subprocess.run([NODE, "-e", driver], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["names"] == ["Nano", "Lite", "Curator"]
    assert out["models"] == {
        "on": "AI models On Turn off",
        "off": "AI models Off on this machine Turn on",
        "turned on, not restarted": "AI models Off on this machine Turn off On from the next start of Sautium.",
        "absent in an image": ("AI models Off — this install was set up without the models. "
                               "On the machine, run: ./sautium setup --preset full"),
        # PI-NODE-2C §18.4: what the node is and why nothing is missing, then how
        "absent on a node born Nano": (
            "AI models Off — this node was set up as Nano, without the AI models: sound features arrive "
            "from other Sautium nodes, and nothing is missing from your library. To analyse music on this "
            "machine instead, run on it: ./sautium setup --preset full (it downloads about 10 GB, and needs "
            "25 GB of free disk and 8 GB of memory to work well)."),
        "absent on a machine": "AI models Not available: no AI models exist for this machine.",
        "off on a Nano node": f"AI models Off on this machine Turn on {FULL_NODE}",
        "absent on a Nano node": (
            "AI models Off — this node was set up as Nano, without the AI models: sound features arrive "
            "from other Sautium nodes, and nothing is missing from your library. To analyse music on this "
            "machine instead, run on it: ./sautium setup --preset full (it downloads about 10 GB, and needs "
            f"25 GB of free disk and 8 GB of memory to work well). {FULL_NODE}"),
        "a Nano node holding one": "AI models Off on this machine Turn on",
    }
    said = {name: case["text"] for name, case in out["software"].items()}
    head = "Sautium 0.3.0"
    assert said == {
        "no agent": f"{head} Updates: on the machine, run ./sautium update",
        "a launcher's": head,
        "never checked": f"{head} Check for updates",
        "checking": f"{head} Checking…",
        "current": f"{head} Check for updates Up to date · checked 2 h ago",
        "offered": f"{head} Update to 0.3.1",
        "floor": f"{head} Check for updates Checked less than an hour ago.",
        "rewrite": (f"{head} Check for updates A newer release cannot be reached from this install. "
                    "A fresh install moves to it."),
        "refused": (f"{head} Check for updates A release offered to this node is not signed by a "
                    "Sautium release key. Nothing was changed."),
        "updating": f"{head} Updating… Sautium will stop and come back.",
        "update refused": (f"{head} Update to 0.3.1 The update did not run: the checkout has "
                           "&lt;b&gt;local&lt;/b&gt; changes"),
    }
    # The host's own sentence never becomes markup, and no button is offered mid-update
    assert "<b>local</b>" not in out["software"]["update refused"]["raw"]
    assert "<button" not in out["software"]["updating"]["raw"]
    assert 'data-action="host-check" disabled' in out["software"]["checking"]["raw"]
    # The version is what a person reads; the commit is support's and rides in the value's title
    assert "0.3.0" in said["never checked"] and "4f2a91c0e7b3" not in said["never checked"]
    assert 'title="4f2a91c0e7b3"' in out["software"]["never checked"]["raw"]


# -- Nano's More (PI-NODE-2E §5) ------------------------------------------------------------------

NANO = {"nano": True, "dumps_held": False}
NANO_HOLDING = {"nano": True, "dumps_held": True}
FULL = {"nano": False, "dumps_held": False}


def _method(head: str) -> str:
    start = SOURCE.index(f"\n    {head}") + 1
    return SOURCE[start:SOURCE.index("\n    },\n", start) + len("\n    }")]


MORE_DRIVER = r"""
'use strict';
let _hardware = null, _hardwareAsk = null;
%(fns)s
const answers = %(answers)s;
let asked = 0;
const fetch = url => {
  if (url !== '/api/settings/hardware') throw new Error(url);
  const a = answers[asked++];
  return a === 'fail' ? Promise.reject(new Error('down'))
                      : Promise.resolve({ ok: true, json: () => Promise.resolve(a) });
};
const log = console.log;
console.warn = () => {};
const row = { hidden: false };
const drawer = { el: { querySelector: sel => {
    if (sel !== '[data-go="more/databases"]') throw new Error(sel); return row; } },
  %(method)s };
(async () => {
  const seen = [];
  await drawer._syncDatabasesRow(false); seen.push([row.hidden, asked]);   // the drawer's build
  await drawer._syncDatabasesRow(false); seen.push([row.hidden, asked]);   // the answer shared
  for (let i = 1; i < answers.length; i++) {                               // each open asks again
    const painted = drawer._syncDatabasesRow(true);
    const before = row.hidden;                                            // drawn from the answer held
    await painted; seen.push([before, row.hidden, asked]);
  }
  log(JSON.stringify(seen));
})();
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_more_list_draws_offline_databases_only_where_the_node_offers_them():
    """A Nano node holding no dump has no "Offline databases" row; one that
    holds one, and every other node, has it. Read from the hardware answer:
    once at the drawer's build, shared, and again at each open — drawn at
    once from the answer the page holds, so the row does not blink."""
    fns = "\n".join([_block("function nodeHardware(fresh = false) {"),
                     _block("function dumpsOffered(hw = _hardware) {")])
    answers = [NANO, NANO_HOLDING, FULL, "fail", NANO]
    out = subprocess.run([NODE, "-e", MORE_DRIVER % {
        "fns": fns, "answers": json.dumps(answers),
        "method": _method("async _syncDatabasesRow(fresh) {")}],
        capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == [
        [True, 1], [True, 1],
        [True, False, 2],        # it came to hold a dump
        [False, False, 3],       # a full node
        [False, False, 4],       # the ask failed: the answer held stands
        [False, True, 5],
    ]
    # the drawer reads it at its build and at each open; the route stays, with its own words
    at = SOURCE.index("\n  const moreDrawer = {")
    drawer = SOURCE[at:SOURCE.index("\n  };\n", at)]
    assert drawer.count("this._syncDatabasesRow(false);") == 1
    assert drawer.count("this._syncDatabasesRow(true);") == 1
    assert "['databases',    root => renderDatabases(root)]," in SOURCE


WORDS_DRIVER = r"""
'use strict';
let _aiCanonShown = [];
const canonRowHtml = () => '';
const escapeProfileHtml = s => String(s);
%(fns)s
const text = html => html.replace(/<[^>]*>/g, '').replace(/\s+/g, ' ').replace(/&amp;/g, '&').trim();
const canon = { canonization: { available: true, enabled: true, free: true, mb_dump: false, job: {} } };
const out = {};
for (const offered of [true, false]) {
  out[offered] = {
    off: text(mbScopeWords('off', offered)),
    searching: text(mbScopeWords('searching', offered)),
    none: text(mbScopeWords('none', offered)),
    cooldown: mbCooldownWords(12, offered),
    canon: text(_renderAiCanon(canon, offered)).match(/Needs the local.*?(?= Run now)/)[0],
  };
}
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_pointers_to_offline_databases_name_the_network_alone_where_none_is_offered():
    """Discovery's MusicBrainz chip (its three states), the rate-limit line
    and the AI canonization's need: on a Nano node holding no dump none of
    them sends the person to a screen that offers nothing."""
    fns = "\n".join(_block(head) for head in (
        "function mbScopeWords(state, offered) {", "function mbCooldownWords(seconds, offered) {",
        "function _renderAiCanon(ai, offered) {"))
    out = subprocess.run([NODE, "-e", WORDS_DRIVER % {"fns": fns}],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    said = json.loads(out.stdout)
    assert all("Offline databases" in words for words in said["true"].values())
    assert said["false"] == {
        "off": ("P2P sharing is off, so this node asks no other node about artists. Turn it on in "
                "More → Sync & P2P, and it searches the network's catalog nodes."),
        "searching": "Looking for MusicBrainz catalog nodes on the network — give it a minute.",
        "none": "No MusicBrainz catalog nodes are reachable right now.",
        "cooldown": "Network search is rate-limited (shared volunteer nodes) — retry in ~12s.",
        "canon": "Needs the local MusicBrainz catalogue — canonization resolves names against it.",
    }
    # each place asks the one rule, from the answer the page holds
    assert "mbScopeWords(chip.dataset.mbState, dumpsOffered())" in SOURCE
    assert "mbCooldownWords(data.cooldown, dumpsOffered())" in SOURCE
    assert "_renderAiCanon(ai, dumpsOffered(hw))" in SOURCE
    assert "if (!dumpsOffered(data)) {" in SOURCE
    # the route says why; the way to a full node is said on Profile
    nano_words = _block("const DUMPS_NOT_ON_NANO = ", "';\n")
    assert "Profile" not in nano_words and "AI models" not in nano_words


# -- the trail's three new marks (§4.1) ---------------------------------------------------------

@pytest.fixture
def trail(db, monkeypatch):
    """The guidance rules over an account, with no scan, no notice and no
    output question in the way."""
    settings_router = pytest.importorskip("routers.settings")
    monkeypatch.chdir(BACKEND)                 # main mounts static/ relative to its directory
    import device_auth
    import library_root
    import main  # noqa: F401 — the trail reads its workers' state
    from config import settings
    account = {"yes": True}
    monkeypatch.setattr(device_auth, "account_configured", lambda: account["yes"])
    monkeypatch.setattr(settings_router, "_notices_state", lambda: {"items": []})
    monkeypatch.setattr(hardware_profile, "resolve", lambda: _profile("on", name="standard"))
    monkeypatch.setattr(settings, "sautium_library_roots", "")
    db.execute("INSERT INTO user_settings (key, value) VALUES ('output.type', '\"browser\"')")
    db.execute("""INSERT INTO user_profile (id, email_verified) VALUES (1, FALSE)
                  ON CONFLICT (id) DO UPDATE SET email_verified = FALSE""")

    def tasks() -> list:
        return [t for t in settings_router._guidance_state()["tasks"] if t != "analyse_library"]
    return type("Trail", (), {"tasks": staticmethod(tasks), "db": db, "account": account,
                              "router": settings_router, "settings": settings, "roots": library_root})


def test_an_unverified_email_is_on_the_trail_until_it_is_verified(trail, monkeypatch):
    assert trail.tasks() == ["verify_email"]
    trail.db.execute("UPDATE user_profile SET email_verified = TRUE WHERE id = 1")
    assert trail.tasks() == []                         # a state, never a visit
    assert "verify_email" not in trail.router._GUIDANCE_DISMISSIBLE
    trail.db.execute("UPDATE user_profile SET email_verified = FALSE WHERE id = 1")
    trail.account["yes"] = False                       # no account yet: nothing to verify
    assert trail.tasks() == []


def test_on_a_node_that_runs_no_model_the_email_leads_the_trail(trail, monkeypatch, tmp_path):
    trail.db.execute("DELETE FROM user_settings WHERE key = 'output.type'")
    assert trail.tasks() == ["audio_output", "verify_email"]
    monkeypatch.setattr(hardware_profile, "resolve", lambda: _profile("off"))
    assert trail.tasks() == ["verify_email", "audio_output"]


def test_a_node_that_picks_its_folder_is_led_to_choose_one(trail, monkeypatch, tmp_path):
    trail.db.execute("UPDATE user_profile SET email_verified = TRUE WHERE id = 1")
    monkeypatch.setattr(trail.settings, "sautium_library_roots", str(tmp_path))
    monkeypatch.setattr(trail.settings, "music_library_path", "")
    trail.db.execute("TRUNCATE library_folders")
    library_root_mod = pytest.importorskip("library_root")
    library_root_mod.invalidate()
    assert trail.tasks() == ["music_folder"]
    (tmp_path / "Music").mkdir()
    import library_root
    library_root.add(str(tmp_path / "Music"))
    try:
        assert trail.tasks() == []                     # cleared by the first folder
    finally:
        trail.db.execute("TRUNCATE library_folders")
        library_root.invalidate()
    assert "music_folder" not in trail.router._GUIDANCE_DISMISSIBLE


def test_a_node_born_with_a_preset_is_shown_its_block_once(trail):
    trail.db.execute("UPDATE user_profile SET email_verified = TRUE WHERE id = 1")
    assert trail.tasks() == []                         # born with none: nothing to show
    trail.db.execute("INSERT INTO user_settings (key, value) VALUES ('node.birth_preset', '\"nano\"')")
    assert trail.tasks() == ["this_node"]
    assert trail.router.post_guidance_seen("this_node")["tasks"] is not None
    assert trail.tasks() == []                         # the visit retires it, for good


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_row_several_tasks_pass_through_is_marked_for_any_of_them():
    driver = r"""
'use strict';
const els = %s.map(g => ({ dataset: { guide: g }, on: false, hasAttribute: () => false,
                           classList: { toggle(_c, on) { this.el.on = on; } } }));
els.forEach(el => { el.classList.el = el; });
const document = { querySelectorAll: () => els };
const detached = [];
const attachGuidePuck = () => {}, detachGuidePuck = (t) => { detached.push(t.name); _guidePucks.delete(t); };
// a control drawn again in place left its puck with the old element: the next paint takes it away
const _guidePucks = new Map([[{ name: 'gone', isConnected: false }, {}], [{ name: 'here', isConnected: true }, {}]]);
%s
const out = {};
for (const tasks of [[], ['verify_email'], ['this_node'], ['music_folder'], ['support_request']]) {
  guide._apply(tasks);
  out[tasks.join() || 'none'] = els.map(el => el.on);
}
out.detached = detached.filter(n => n !== undefined);
process.stdout.write(JSON.stringify(out));
"""
    start = SOURCE.index("\n  const guide = {") + 1
    guide = SOURCE[start:SOURCE.index("\n  };\n", start) + len("\n  };\n")]
    marks = ["*", "verify_email this_node", "analyse_library music_folder", "verify_email"]
    proc = subprocess.run([NODE, "-e", driver % (json.dumps(marks), guide)],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {
        "none": [False, False, False, False],
        "verify_email": [True, True, False, True],
        "this_node": [True, True, False, False],
        "music_folder": [True, False, True, False],
        "support_request": [True, False, False, False],
        "detached": ["gone"]}
    # The drawer's rows and the controls carry the marks
    assert 'data-go="more/profile" data-guide="verify_email this_node update"' in SOURCE
    assert 'data-go="more/library" data-guide="analyse_library music_folder"' in SOURCE
    assert 'data-action="add-folder" data-guide="music_folder"' in SOURCE
    assert '<div class="form-group" data-guide="this_node">' in SOURCE
    assert SOURCE.count('data-row="email" data-guide="verify_email"') == 2


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_block_reads_the_node_again_when_the_hosts_side_moved_and_draws_only_a_change():
    """A wake of the Library stream is also a scan's progress, several a
    second: it costs one read of the host's side and no redraw. An update
    that ended, or another run of the node answering (a restart the block's
    own button asked for), has the node's own state read again — the version
    and the models' state the block shows are that run's."""
    driver = r"""
'use strict';
const calls = [];
let hw = %(hw)s, host = %(host)s, failing = false;
const fetch = async url => {
  calls.push(url);
  if (failing && url !== '/api/host') return { ok: false, json: async () => ({}) };
  return { ok: true, json: async () => JSON.parse(JSON.stringify(url === '/api/host' ? host : hw)) };
};
let wake = null, draws = 0, html = '';
const holder = { set innerHTML(v) { draws += 1; html = v; }, get innerHTML() { return html; },
                 querySelector: () => null };
const root = { querySelector: sel => sel === '[data-hw-block]' ? holder : null };
const document = { contains: () => true };
const libraryWake = { on(fn) { wake = fn; return { abort() {} }; } };
const guide = { paint() {}, seen() {} };
const escapeProfileHtml = s => String(s), fmtBytes = n => n + ' B', onceInFlight = (el, fn) => fn();
const _hwMachineLine = () => 'a machine', hwProfileName = h => h.profile;
const hwModelsRow = h => `models:${h.models}`, hwSoftwareRow = (h, o) => `sautium:${h.software.version}:${o.run}`;
const paintEmailLine = () => {};
let _hwBlockSub = null;
%(fn)s
const settle = () => new Promise(r => setTimeout(r, 0));
(async () => {
  const out = [];
  const step = async (label, act) => {
    calls.length = 0; const before = draws;
    await act(); await settle(); await settle();
    out.push([label, calls.slice(), draws - before, html.includes('sautium:' + hw.software.version + ':' + host.run),
              html.includes('models:' + hw.models)]);
  };
  await step('first', () => _loadHwBlock(root));
  await step('a scan ticks', () => wake());
  await step('a scan ticks again', () => wake());
  host = { ...host, requests: { update: { outcome: 'updated', release: 'v0.3.1' } } };
  hw = { ...hw, software: { version: '0.3.1', commit: 'bbbb' } };
  await step('an update ended', () => wake());
  host = { ...host, run: 'second' };
  hw = { ...hw, models: 'on' };
  failing = true;                       // the node answers the host's side before its own state
  await step('restarted, its state not read yet', () => wake());
  failing = false;
  await step('the node restarted', () => wake());
  await step('quiet again', () => wake());
  process.stdout.write(JSON.stringify(out));
})();
"""
    hw = {"profile": "lite", "models": "off", "software": {"version": "0.3.0", "commit": "aaaa"},
          "restartable": True, "detected": {}}
    host = {"available": True, "run": "first", "requests": {}, "offered": None}
    proc = subprocess.run([NODE, "-e", driver % {
        "hw": json.dumps(hw), "host": json.dumps(host),
        "fn": _block("async function _loadHwBlock(root) {", "\n    await paint();\n  }\n")}],
        capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    both, host_only = ["/api/settings/hardware", "/api/host"], ["/api/host"]
    assert json.loads(proc.stdout) == [
        ["first", both, 1, True, True],
        ["a scan ticks", host_only, 0, True, True],
        ["a scan ticks again", host_only, 0, True, True],
        ["an update ended", ["/api/host", "/api/settings/hardware"], 1, True, True],
        # not drawn, and not taken as seen: the next wake reads the node's state again
        ["restarted, its state not read yet", ["/api/host", "/api/settings/hardware"], 0, False, False],
        ["the node restarted", ["/api/host", "/api/settings/hardware"], 1, True, True],
        ["quiet again", host_only, 0, True, True]]


def test_an_offer_is_dated_by_its_first_answer_and_a_daily_check_is_no_news(agent, monkeypatch):
    """The daily check offering the same release again moves the check's row,
    never the offer's onset: its lasting toast is not raised again every day.
    A newer release is a new onset."""
    import asyncio
    import types
    host = pytest.importorskip("routers.host")
    c, _ = agent
    settings_router = pytest.importorskip("routers.settings")
    monkeypatch.setattr(settings_router, "notify_library_subscribers", lambda: None)
    req = types.SimpleNamespace(state=types.SimpleNamespace(signer="host-agent"))

    def answer(release):
        asyncio.run(host.answer(host.Answer(verb="check", outcome="offered", release=release), req))
        return host.offered_release()
    first = answer("v0.3.1")
    c.execute("UPDATE host_requests SET answered_at = answered_at + interval '1 day' WHERE verb = 'check'")
    again = answer("v0.3.1")
    assert again == first
    newer = answer("v0.3.2")
    assert newer["release"] == "v0.3.2" and newer["since"] != first["since"]
    # a check that did not run says nothing of the offer: made again, the same onset
    asyncio.run(host.answer(host.Answer(verb="check", outcome="failed", detail="no network"), req))
    assert answer("v0.3.2")["since"] == newer["since"]
    # an offer that stopped and is made again later is a new onset
    asyncio.run(host.answer(host.Answer(verb="check", outcome="current"), req))
    assert host.offered_release() is None
    assert answer("v0.3.2")["since"] != newer["since"]
