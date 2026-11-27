"""
Settings endpoints — single-roundtrip read of everything the Settings
screen needs (library stats + scan state, AI provider/model/auth/usage,
sync & P2P preferences, HQPlayer connection summary), plus PUT/POST
endpoints to mutate the user-controlled bits.

Preferences live in user_settings (JSONB K/V) so the values survive
across backend restarts and are sync-able through P2P in the future.
The scan/enrich actions are thin wrappers around the existing top-
level /scan/* and /enrich/* endpoints so the Settings UI does not
need to know about them directly.
"""

import asyncio
import json
import logging
import os
import secrets
import shutil
import threading
import uuid
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import text

import claude_code
import codex_cli
import device_auth
import diag_node
import gear_research_worker
import host_channel
import library_watch
from config import settings as app_settings
from database import get_db_context
from db_pool import Listener, db_execute, db_query, db_query_one, get_conn
from desktop.p2p import load_meter
from serving import EventStream
from routers.auth import StepUp, boxed, step_up
from sql_queries import ARTIST_OWNED

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"])


# ============================================================
# SSE: the Library stream
# ============================================================
#
# The one connection a page holds on every route (app-shell.js
# libraryWake). Three things ride it:
#
#   a wake ("data: {}") — workers (scan / enrich) mutate _scan_state /
#     _enrich_state in main.py and call notify_library_subscribers() at a
#     meaningful checkpoint; the page re-fetches what its open screen shows.
#     Wake-event, not payload queue, so workers don't pay serialization
#     cost on every progress tick.
#   `event: guidance` — the guidance trail, at connect and whenever its set
#     of tasks changes (_GuidanceTrail).
#   `event: load` — the node's load as its meter published it (NOTIFY
#     sautium_load), sent only to a connection whose page said it shows the
#     load (PUT …/library/stream/load, by the name `event: stream` gave the
#     connection): the Sync screen, while it is open. A connection that
#     drops takes its interest with it.
#
# A named event is no wake: a page acts on one only where it subscribed to
# it by name. Until 2026-11-24 every NOTIFY on sautium_identity and
# sautium_notices woke this stream too, the load meter said its band changes
# on sautium_identity, and the page asked GET /guidance at every wake: under
# any activity, a request every two seconds from every open page (B-138).


class _LibraryStream:
    """One connection of the Library stream: what it is owed, said from any
    thread (a worker, a listener), read on its own loop."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.evt = asyncio.Event()
        self._owed: set = set()         # the loop's alone
        self.id = secrets.token_hex(8)  # the connection's name, said to its page at connect
        self.shows_load = False         # its page's word (put_stream_load), under _library_sse_lock
        self.load_seq = 0               # ... and the number of that word: two in flight apply in order

    def owe(self, kind: str) -> None:
        self.loop.call_soon_threadsafe(self._owe, kind)

    def _owe(self, kind: str) -> None:
        self._owed.add(kind)
        self.evt.set()

    def take(self) -> set:
        owed, self._owed = self._owed, set()
        self.evt.clear()
        return owed


_library_streams: List[_LibraryStream] = []
_library_sse_lock = threading.Lock()


def _tell_library_streams(kind: str) -> None:
    with _library_sse_lock:
        # the load is sent to the connections that asked for it, no others
        streams = [s for s in _library_streams if kind != "load" or s.shows_load]
    for stream in streams:
        try:
            stream.owe(kind)
        except RuntimeError:
            # Loop closed; the stream leaves the list in its generator's
            # finally block.
            continue

# Mirror channel for the AI > Claude Code screen. Wake events fire
# from the install worker thread when state transitions (running →
# done / error) so the Web UI re-fetches /api/settings/ai/claude/state
# without polling. Same wake-event pattern as the library channel.
_claude_sse_clients: List[Tuple[asyncio.Event, asyncio.AbstractEventLoop]] = []
_claude_sse_lock = threading.Lock()

# Same channel for the AI > Codex screen (install/sign-in state).
_codex_sse_clients: List[Tuple[asyncio.Event, asyncio.AbstractEventLoop]] = []
_codex_sse_lock = threading.Lock()

# AI-canonization run: a background thread resolves the residue, pushing batch
# progress + the AI's reasoning to the More→AI screen over the same wake-event
# SSE pattern. Single-flight (one run at a time); state is read via GET /ai.
_aicanon_sse_clients: List[Tuple[asyncio.Event, asyncio.AbstractEventLoop]] = []
_aicanon_sse_lock = threading.Lock()
_aicanon: Dict[str, Any] = {
    "running": False, "total": 0, "processed": 0, "canonized": 0,
    "skipped": 0, "guard_rejected": 0, "errors": 0, "model": None, "items": [],
    "stopped": None,     # why a run ended before its residue did, in words
}
_aicanon_lock = threading.Lock()


def _notify_aicanon() -> None:
    with _aicanon_sse_lock:
        for evt, loop in list(_aicanon_sse_clients):
            try:
                loop.call_soon_threadsafe(evt.set)
            except RuntimeError:
                continue


def _aicanon_should_run() -> bool:
    """Whether an event trigger (dump load / scan) may auto-start AI canon: a
    provider is authed AND the tier is enabled (free default On, paid Off unless
    the user opted in). The dump presence is enforced inside ai_canonize_stream."""
    st = _ai_state()
    c = st.get("canonization") or {}
    # also defer to a dump op's data phases (mb_load_active) — AI must not read a
    # half-TRUNCATEd dump (the dump's own post-load AI trigger fires in its
    # 'canonicalizing' phase, where mb_load_active is already False).
    return bool(c.get("available")) and bool(c.get("enabled")) and not mb_load_active()


def start_aicanon_job(since=None, *, force: bool = False) -> bool:
    """Single-flight: start the background AI-canon worker. `since` scopes to
    new-file artists (post-scan). `force` bypasses the enabled/provider gate (the
    UI 'Run now'); event triggers pass force=False so a paid/disabled tier stays
    quiet. Returns True if a run was started."""
    if not force and not _aicanon_should_run():
        return False
    with _aicanon_lock:
        if _aicanon["running"]:
            return False
        _aicanon.update(running=True, total=0, processed=0, canonized=0,
                        skipped=0, guard_rejected=0, errors=0, items=[], stopped=None)
    threading.Thread(target=_aicanon_worker, args=(None, since), daemon=True).start()
    _notify_aicanon()
    return True


def _aicanon_worker(limit: Optional[int], since=None) -> None:
    from canon.ai_canon import ai_canonize_stream
    try:
        for ev in ai_canonize_stream(limit=limit, dry_run=False, since=since):
            with _aicanon_lock:
                if ev["event"] == "start":
                    _aicanon.update(running=True, total=ev["total"], model=ev.get("model"))
                elif ev["event"] == "batch":
                    _aicanon.update(processed=ev["processed"], canonized=ev["canonized"],
                                    skipped=ev["skipped"], guard_rejected=ev["guard_rejected"],
                                    errors=ev["errors"])
                    # newest first, keep a bounded reasoning log for the UI
                    _aicanon["items"] = (ev["items"] + _aicanon["items"])[:80]
                elif ev["event"] == "done":
                    _aicanon.update(running=False, skipped_reason=ev.get("skipped"),
                                    stopped=ev.get("stopped"))
            _notify_aicanon()
    except Exception:
        logging.getLogger(__name__).exception("ai_canon worker crashed")
    finally:
        with _aicanon_lock:
            _aicanon["running"] = False
        _notify_aicanon()


# /api/events subscribers waiting for a fresh notices snapshot. Registered
# by the multiplexed stream in routers/player.py; woken by the
# sautium_notices NOTIFY (api_cooldown arm/clear, the launcher's slice
# cycle, the music folder's state changing) through the sync listener above.
_notice_sse_clients: List[Tuple[asyncio.Event, asyncio.AbstractEventLoop]] = []
_notice_sse_lock = threading.Lock()


def notices_sse_register(evt, loop) -> None:
    with _notice_sse_lock:
        _notice_sse_clients.append((evt, loop))


def notices_sse_unregister(evt, loop) -> None:
    with _notice_sse_lock:
        try:
            _notice_sse_clients.remove((evt, loop))
        except ValueError:
            pass


def notify_notice_subscribers() -> None:
    with _notice_sse_lock:
        for evt, loop in list(_notice_sse_clients):
            try:
                loop.call_soon_threadsafe(evt.set)
            except RuntimeError:
                continue


# /api/events subscribers of this node's identity state (the worker's
# p2p.identity; NOTIFY sautium_identity): Friends and every chat header say
# why nothing connects while the node has no identity it can use.
_identity_sse_clients: List[Tuple[asyncio.Event, asyncio.AbstractEventLoop]] = []
_identity_sse_lock = threading.Lock()


def identity_sse_register(evt, loop) -> None:
    with _identity_sse_lock:
        _identity_sse_clients.append((evt, loop))


def identity_sse_unregister(evt, loop) -> None:
    with _identity_sse_lock:
        try:
            _identity_sse_clients.remove((evt, loop))
        except ValueError:
            pass


def notify_identity_subscribers() -> None:
    with _identity_sse_lock:
        for evt, loop in list(_identity_sse_clients):
            try:
                loop.call_soon_threadsafe(evt.set)
            except RuntimeError:
                continue


def identity_event() -> Dict[str, Any]:
    """This node's identity as the worker last published it, for the page:
    its status and detail, the lottery's progress, and whether the account's
    email is verified — the other way to an identity, offered beside a proof
    that is still being mined or waits for memory. No status while no
    worker ever published (no account yet, P2P off). `support_known`: what a
    node with no identity can still reach (routers.p2p.support_known)."""
    from routers.p2p import support_known
    state = _read("p2p.identity") or {}
    row = db_query_one("SELECT email_verified FROM user_profile WHERE id = 1")
    return {"status": state.get("status"), "detail": state.get("detail"),
            "attempts": state.get("attempts"), "p_done": state.get("p_done"),
            "email_verified": bool(row and row.get("email_verified")),
            "support_known": support_known()}


def _identity_row() -> Dict[str, Any]:
    """The worker's state as the Sync & P2P Identity row shows it."""
    from routers.p2p import support_known
    return {**(_read("p2p.identity") or {}), "support_known": support_known()}


def notify_library_subscribers() -> None:
    """Thread-safe wake of every connected Library SSE client.
    Workers call this after start / phase transition / completion. What
    moved may be something the guidance trail reads: it is computed again."""
    _tell_library_streams("wake")
    _guidance.moved()


class _GuidanceTrail:
    """The guidance trail (_guidance_state) as the Library streams hear it.

    One thread computes it, once for every stream, whenever something it
    reads moved — a Library wake, the node's identity, the notices — and the
    streams are told only when the set of tasks changed. The routes that
    answer with the trail and a stream's connect compute it through here
    too, under the same lock: computations never overlap, so a later one
    read later state, and `seq` orders every answer a page gets — on its
    stream, to its first read, to a "seen" — whichever arrives first. `run`
    names this process: a node started again counts from one."""

    # Workers report in bursts (an enrichment's first second holds a dozen
    # progress wakes, a scan several a second): one computation serves a
    # burst — the page's own 200 ms fan-out did this while it asked.
    PACE_S = 0.2

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._moved = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._id = secrets.token_hex(4)
        self._seq = 0
        self.said: Optional[Dict[str, Any]] = None      # the answer of the last change

    def moved(self) -> None:
        self._moved.set()

    def compute(self) -> Dict[str, Any]:
        with self._lock:
            tasks = _guidance_state()["tasks"]
            self._seq += 1
            answer = {"tasks": tasks, "seq": self._seq, "run": self._id}
            if self.said is None or self.said["tasks"] != tasks:
                self.said = answer
                _tell_library_streams("guidance")
            return answer

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="guidance-trail")
        self._thread.start()

    def stop(self) -> None:
        thread, self._thread = self._thread, None
        if thread is None:
            return
        self._stop.set()
        self._moved.set()
        thread.join(2.0)

    def _run(self) -> None:
        while True:
            self._moved.wait()
            if self._stop.is_set():
                return
            self._moved.clear()
            with _library_sse_lock:
                listened = bool(_library_streams)
            # With no page open nobody is told: a stream's connect computes
            if listened:
                try:
                    self.compute()
                except Exception:
                    logger.exception("The guidance trail was not computed; the next wake computes it")
            if self._stop.wait(self.PACE_S):
                return


_guidance = _GuidanceTrail()


def notify_claude_subscribers() -> None:
    """Thread-safe wake of every AI > Claude Code SSE client. Called
    from the install worker thread on state transitions."""
    with _claude_sse_lock:
        for evt, loop in list(_claude_sse_clients):
            try:
                loop.call_soon_threadsafe(evt.set)
            except RuntimeError:
                continue


def notify_codex_subscribers() -> None:
    """Thread-safe wake of every AI > Codex SSE client."""
    with _codex_sse_lock:
        for evt, loop in list(_codex_sse_clients):
            try:
                loop.call_soon_threadsafe(evt.set)
            except RuntimeError:
                continue


def _claude_signin_changed(signed_in: bool) -> None:
    """The Claude Code verdict flipped — a refused call, a completed
    sign-in, a look: the AI screen, the notices set and the guidance
    trail (woken with the notices) all read it."""
    notify_claude_subscribers()
    db_execute("NOTIFY sautium_notices")


def _codex_signin_changed(signed_in: bool) -> None:
    """The Codex verdict flipped — same readers as Claude Code's."""
    notify_codex_subscribers()
    db_execute("NOTIFY sautium_notices")


claude_code.auth.on_change(_claude_signin_changed)
codex_cli.auth.on_change(_codex_signin_changed)


# Sync DB listener: bridge between what another process says (the
# launcher's P2PManager, a CLI) or another thread of this one, and the
# pages. Each channel wakes the readers of what changed, and no others:
#
#   sautium_sync_done — a P2P sync ended (sync_walk): the Library stream, so
#     the Settings screen re-fetches last_sync_at / items_received.
#   sautium_library — the Library's state moved where no thread of this
#     backend's could call notify_library_subscribers(): records an import
#     landed (sync_client.SyncClient._import_items — a pull's batch, a peer's
#     push, a share file, in the launcher's process, this one's or a CLI's),
#     a file's structural rows (seed_import.insert_structural): the Library
#     stream.
#   sautium_identity — the node's network state for its pages (identity
#     proof progress, the reachability verdict, what Support knows, the
#     Support row's moments, the account's email verified): the identity
#     subscribers of /api/events.
#   sautium_notices — a condition began or ended: the notices subscribers
#     of /api/events.
#   sautium_load — the load meter's band change, with its snapshot
#     (desktop/p2p/load_meter.message): the `load` event of the Library
#     streams whose page shows the load.
#
# The guidance trail reads the identity state and the notices, so those two
# also have it computed again; a page hears of it only when its set changed.
# Same NOTIFY pattern used by chat (routers/p2p.py); no polling.

_load_said: Optional[Dict[str, Any]] = None     # the snapshot last heard (or read, _load_now)
_load_lock = threading.Lock()


def _load_now() -> Dict[str, Any]:
    """The node's load as last said — and, while no band change was heard
    yet (a node at rest since this start), as its meter stored it."""
    global _load_said
    with _load_lock:
        if _load_said is None:
            _load_said = {"load": _read("p2p.load"), "gate": _gate_snapshot()}
        return _load_said


def _on_sync_notify(notifies: list) -> None:
    global _load_said
    channels = {note.channel for note in notifies}
    if channels & {"sautium_sync_done", "sautium_library"}:
        notify_library_subscribers()
    if "sautium_notices" in channels:
        notify_notice_subscribers()
        _guidance.moved()
    if "sautium_identity" in channels:
        notify_identity_subscribers()
        _guidance.moved()
    loads = [note.payload for note in notifies if note.channel == load_meter.CHANNEL]
    if loads:
        said = json.loads(loads[-1])
        with _load_lock:
            _load_said = said
        _tell_library_streams("load")


_sync_listener = Listener("sync-sse-listener",
                          ("sautium_sync_done", "sautium_library", "sautium_identity",
                           "sautium_notices", load_meter.CHANNEL),
                          _on_sync_notify)


def start_sync_listener() -> None:
    _sync_listener.start()
    _guidance.start()


def stop_sync_listener() -> None:
    _guidance.stop()
    _sync_listener.stop()


# ============================================================
# Preference keys + defaults (single source of truth)
# ============================================================

# user_settings JSONB stores one row per key, value can be any JSON.
# Defaults are returned to the UI when the row is missing so first-
# run users get a sensible Settings screen with no prior writes.
_DEFAULTS: Dict[str, Any] = {
    "sync.p2p_enabled":          True,
    # "Pass on what this node holds": the serving side of sharing — the peer
    # routes, what /health says, the LAN beacon, the DHT's tail, the carry
    # this node offers (routers/sync.serving_enabled). Off, the node still
    # asks and takes. On by default on every node, a node born Nano included
    # since 2026-11-22 (one born before keeps the off its birth wrote); the
    # row is shown only where it is off or the node is Nano.
    "sync.pass_on":              True,
    "sync.auto_interval_min":    30,
    # Size of the rare-artist DHT announce tail (0/null = node key only).
    # 150 = half the traversal lane (dht_service MIN_ANNOUNCE_SPACING): a
    # pass refreshes every key inside one DHT entry lifetime with the
    # rare-artist search taking the other half. Announced only under the
    # network-size verdict (p2p.rare_mode, desktop/p2p/network_size.py).
    "sync.announce_limit":       150,
    # Foreign TRACKS whose audio analysis this node will hold on other
    # peers' behalf (push-seeding). A node behind CGNAT cannot be pulled
    # FROM, so its own analysis only reaches the network if somebody
    # carries it. ~46 KB per track measured; 0/null = don't carry.
    # Mirrored by sync_queries.CARRY_DEFAULT_BUDGET (the launcher's sync
    # server reads user_settings directly and needs the same fallback) —
    # keep in step.
    "sync.carry_limit":          2000,
    "enrichment.background_enabled": True,
    # Relay for others (Phase D): a reachable non-lite node announces
    # Sautium-cap:relay and carries wake subscriptions + live forwards for
    # CGNAT-bound strangers (voucher-authorized). Traffic is chat frames —
    # tiny; the default is on because a relay layer that nobody opts into
    # never cold-starts.
    "p2p.relay_enabled": True,
    # Owned tracks whose analysis arrived over P2P: False = keep the
    # imported copy (no GPU re-spend on tracks that already work), True =
    # re-derive first-hand from the local file (matches this rip exactly,
    # makes the node an author for those tracks, and is the network's
    # poison detector). Read by embeddings.py / audio_analysis.py pending
    # queries.
    "enrichment.reanalyze_imported": False,
    # "Recognise instruments": the instrument pass (the AST+PaSST tagger,
    # after the sonic pass) and the tagging of streams. None = never set,
    # so the profile answers (HardwareProfile.instruments_by_default: on for
    # full and standard, off for lite, where the tagger costs ~19 s per
    # minute of audio on a Pi) — read through instruments_on(). Off computes
    # none and removes nothing (sign() still seals what the node made
    # earlier, after a shed): the node still pulls, serves and carries the
    # instruments other nodes made.
    "enrichment.instruments":    None,
    # The phantom layer — albums and tracks the owner does not have (MB
    # discographies, Last.fm neighbours), browsable and streamable next to
    # the library. Its size is the OWNER's call, never the hardware
    # profile's: on = keep minting, off = nothing new is minted and what
    # exists stays. Removal is the explicit Settings action
    # (POST /phantoms/prune) — no startup job ever drops it.
    "discovery.phantom_layer":   True,
    # "Radio plays tracks you don't own": off, radio and Play similar are the
    # owner's own tracks alone; on, they mix in streamed ones — each played
    # whole (a full-length link, the demo channel's one listen included),
    # never a 30 s excerpt. Moot while the streaming library is off. Off by
    # default for every node (the owner, 2026-11-22, QUEUE-STREAM Q12).
    "radio.streamed":            False,
    # "AI models on this machine" (hardware_profile.MODELS_SETTING): off, the
    # node loads no model and analyses nothing — the state a small board is
    # born with (db_migrate's birth preset). Read once at the process's
    # start, so a change takes a restart; never read through _read by the
    # profile, which must not import this module.
    "compute.models":            True,
    # Provider/model default to None so the first-run UI shows
    # "Not selected" instead of pretending Claude is picked when the
    # wizard offered an explicit "no AI" option. An install-time pick
    # (the wizard's, compose's) lands in the row once, at the first boot
    # without one — seed_ai_provider_from_env().
    "ai.provider":               None,
    "ai.model":                  None,
    "ai.api_key":                None,
    # HQPlayer connection — env defaults still apply at first boot;
    # user_settings overrides them on PUT /api/settings/hqplayer.
    "hqplayer.host":             None,
    "hqplayer.port":             None,
    # Artist-screen Albums block sort. release_year is the default;
    # other valid values: time_listened, popularity, recently_added,
    # a_z. UI exposes this through a bottom-sheet picker on the
    # Albums section header.
    "albums.sort":               "release_year",
    # The language this node speaks. None = never chosen, so the machine
    # answers instead (desktop/os_locale.py) and keeps answering after the
    # owner switches the system language. A value here is a human's explicit
    # choice and always wins — nothing auto-detected is ever written back.
    "ui.language":               None,
    # last sync metadata — written by the sync runner when a cycle
    # completes; surfaced in the "Last sync · N new items" row.
    "sync.last_at":              None,
    "sync.last_items_received":  None,
    "p2p.identity":              None,   # {status, detail, method, attempts, difficulty, p_done, …}
    "p2p.load":                  None,   # {profile, ceiling, cpu_frac, headroom, dormant, playback, pace, …}
    "p2p.gate_mode":             "shadow",   # off | shadow | enforce — arming is a release decision
    # Support diagnostics (P2P_NETWORK.md § "Support diagnostics"): the
    # node answers the master's signed warrants with a boxed bundle and
    # sends content-free event reports on its own. Silent by design, so
    # the switch is here and the "Support" row says what went out when.
    "support.diagnostics_enabled": True,
    "support.last_report_at":    None,
    "support.last_bundle_at":    None,
    # MusicBrainz local dump — optional auxiliary layer for artist
    # canonicalization. version/last-update written by the loader.
    "musicbrainz.auto_update":   False,
    "musicbrainz.last_update_at": None,
    # ListenBrainz statistics dump — the listening-statistics layer (CC0),
    # same opt-in shape. version written by the loader
    # (listenbrainz.db_version), status by the slice cycle (lb_slice.status).
    "listenbrainz.auto_update":   False,
    "listenbrainz.last_update_at": None,
    # AI canonicalization (the judgment tier). null = use the computed default
    # (On when the AI provider is free/subscription, Off when it's a paid API key).
    "ai.canonization_enabled":   None,
    # Playback output selection (Output picker). null type = legacy resolution:
    # HQPlayer when an endpoint is configured, otherwise no active output.
    "output.type":               None,   # 'hqplayer' | 'local' | 'dlna'
    "output.local_device":       None,   # "{hostapi}::{device name}"
    "output.local_exclusive":    False,  # WASAPI exclusive / bit-perfect claim
    "output.dlna_renderer":      None,   # {udn, location, name} of the picked renderer
    # Stream quality for DLNA / This device (browser) — outdoor bandwidth
    # saving. Lossless is the default and the everyday mode; the Opus tiers
    # are a deliberate metered-connection choice. HQPlayer / local always
    # lossless (they never carry the quality param into the media URL).
    "output.stream_quality":     "lossless",   # lossless | opus_192 | opus_96
    # Guidance trail (see _guidance_state): set once the owner has been
    # taken to the Audio output picker, whatever they chose there.
    "guidance.audio_output_seen": False,
    "guidance.this_node_seen":    False,
    # The preset a node was born with (db_migrate._apply_birth_preset), a
    # fact kept for the trail's one-time "this node" mark; None on a node
    # born with none
    "node.birth_preset":          None,
    "guidance.hqp_library_seen":  None,    # the HQPlayer address whose library block was shown
}


def _read(key: str) -> Any:
    row = db_query_one("SELECT value FROM user_settings WHERE key = %(k)s", {"k": key})
    if row is None:
        return _DEFAULTS.get(key)
    return row.get("value")


def instruments_on() -> bool:
    """Whether this node recognises instruments: the owner's switch, or the
    profile's value while it was never set — and never on a node without
    the ML stack."""
    import hardware_profile
    profile = hardware_profile.resolve()
    value = _read("enrichment.instruments")
    if value is None:
        return profile.instruments_by_default
    return profile.ml_available and bool(value)


def _write(key: str, value: Any) -> None:
    """A setting's value; `updated_at` moves only when the value does — it is
    when the owner last changed it (the support switch's rule reads it,
    desktop/p2p/diag_events.SWITCH_ALLOWS), and a tab re-sending a value
    changes nothing."""
    db_execute(
        """
        INSERT INTO user_settings (key, value) VALUES (%s, %s::jsonb)
        ON CONFLICT (key) DO UPDATE
            SET value = EXCLUDED.value,
                updated_at = CURRENT_TIMESTAMP
            WHERE user_settings.value IS DISTINCT FROM EXCLUDED.value
        """,
        (key, json.dumps(value)),
    )


# ============================================================
# MusicBrainz local dump (optional auxiliary layer)
# ============================================================

# One background job per dump family (backend/dump_job.py): in-memory
# progress surfaced in the Library payload, woken over the shared library
# SSE channel; jobs of both families run one at a time on one worker.
from dump_job import DumpBusy, DumpJob, InsufficientDisk


def _mb_post_load(job: DumpJob, result: Dict) -> None:
    """The freshly-loaded dump makes every owned artist canon-pending
    (last_mb_sync IS NULL) — canonicalize now so the EXISTING library gets
    MBIDs + RG titles without waiting for a re-scan. Incremental on later
    dump refreshes (only artists with new content since their last canon)."""
    job.set_phase("canonicalizing", "Canonicalizing library (MusicBrainz)…")
    # The dump tables exist only now — re-evaluate the data source so this
    # process (which may have started dump-less, LOCAL_DUMP=False) actually
    # canonicalizes instead of silently skipping. No backend restart needed.
    import mb_backend as mb
    mb.refresh()
    from canon.content import canonicalize_pending
    from canon import algo_canon
    canon = {}
    with algo_canon() as _ok:   # priority over AI; dump lock already released here
        if _ok:
            canon = canonicalize_pending()
            # A fresh dump may expose release-groups the matcher couldn't reach;
            # catch the NULL-rg owned-album residue (deluxe/multi-disc, fragments) by
            # name, then the content-only studio uniques (cross-script / reworded).
            from canon.content import distill_album_residue, distill_album_coverage
            canon["album_residue_bound"] = (distill_album_residue().get("bound", 0)
                                            + distill_album_coverage().get("bound", 0))
    logger.info(f"Post-load MB canon: {canon}")
    # A new dump is new MB data for every imported Last.fm name still waiting.
    from canon import scrobbles
    scrobbles.wake(names=())
    # A fresh dump is the moment to run the AI judgment tier once over the
    # whole residue (async, in the background — it's LLM-slow). Gated: no-op
    # unless the tier is enabled + a provider is authed.
    if start_aicanon_job():
        logger.info("Post-load: AI canonization started")


# The MB job's own post-load canon runs in 'canonicalizing', where
# mb_load_active is already False so it is not blocked by its own op.
mb_job = DumpJob("musicbrainz", "mb_dump_load", "musicbrainz",
                 read=_read, write=_write, notify=notify_library_subscribers,
                 post_load=_mb_post_load, passive_phases=frozenset({"canonicalizing"}))
lb_job = DumpJob("listenbrainz", "lb_dump_load", "listenbrainz",
                 read=_read, write=_write, notify=notify_library_subscribers)

# Scan / background enrichment / AI canon defer while an MB-dump op is in its
# data-mutating phases — they read the mb_* tables the load is rewriting.
mb_load_active = mb_job.load_active


def maybe_auto_update() -> None:
    """Backend startup: each family with auto-update on checks its mirror in
    the background and queues a newer dump (or the first one)."""
    for job in (mb_job, lb_job):
        job.maybe_auto_update()


# ============================================================
# Phantom layer — the owner's switch + the explicit removal job
# ============================================================

# In-memory progress for the (single) running removal. Surfaced in the
# Library payload and woken over the shared library SSE channel, like the
# MB job above.
_phantom_state: Dict[str, Any] = {"running": False, "cancel_requested": False,
                                  "progress": "", "pct": None, "error": None}
_phantom_lock = threading.Lock()


def _phantom_worker() -> None:
    try:
        from discography import prune_phantom_layer

        def progress(done: int, total: int) -> None:
            _phantom_state["pct"] = int(done * 100 / total) if total else 100
            _phantom_state["progress"] = f"Removing… {done:,} / {total:,} artists"
            notify_library_subscribers()

        stats = prune_phantom_layer(
            cancel_flag=lambda: _phantom_state["cancel_requested"],
            progress_cb=progress)
        _phantom_state["pct"] = 100
        _phantom_state["progress"] = "Cancelled" if stats["cancelled"] else "Removed"
        logger.info(f"Phantom layer removal finished: {stats}")
    except Exception as e:
        _phantom_state["error"] = str(e)
        _phantom_state["progress"] = f"Failed: {e}"
        logger.error(f"Phantom layer removal failed: {e}", exc_info=True)
    finally:
        _phantom_state["running"] = False
        notify_library_subscribers()


def _phantom_section() -> Dict[str, Any]:
    """Footprint + switch + live job state for the Streaming library screen
    ("phantom" is the internal term for the same thing, and stays).

    Every count is exact. The track figure is total-minus-owned rather than
    a correlated NOT EXISTS over 3M rows: the two agree exactly (owned_files
    .track_id is NOT NULL and FK-valid) but the subtraction is a pair of
    aggregates at 0.13 s against ~2 s. It used to be a planner estimate,
    which drifted 4.4 % low here — the layer grows faster than autovacuum
    refreshes reltuples, and a 7-digit number nobody can round is a bad
    place to be approximate. The byte figure is the phantom-bearing tables
    whole; the owned share is ~1 %.

    Enrichment here is a count, never a ratio. A phantom track has no file,
    so no local pipeline can ever derive its embedding or features — those
    arrive over P2P as sealed foreign analysis. A progress bar against 3M
    would read 0 % forever and promise work that will never happen."""
    row = db_query_one(f"""
        SELECT GREATEST(t.total - o.tracks, 0)::bigint AS tracks,
               (SELECT count(*) FROM artists a WHERE NOT {ARTIST_OWNED})    AS artists,
               (SELECT count(*) FROM albums al
                 WHERE NOT EXISTS (SELECT 1 FROM album_variants av
                                     JOIN owned_files mf ON mf.album_variant_id = av.id
                                    WHERE av.album_id = al.id))              AS albums,
               (SELECT count(*) FROM genres g
                 WHERE NOT EXISTS (SELECT 1 FROM album_genres ag
                                     JOIN album_variants av ON av.album_id = ag.album_id
                                     JOIN owned_files mf ON mf.album_variant_id = av.id
                                    WHERE ag.genre_id = g.id))               AS genres,
               (SELECT count(*) FROM embeddings e
                 WHERE NOT EXISTS (SELECT 1 FROM owned_files mf
                                    WHERE mf.track_id = e.track_id))         AS embeddings,
               (SELECT count(*) FROM audio_features f
                 WHERE NOT EXISTS (SELECT 1 FROM owned_files mf
                                    WHERE mf.track_id = f.track_id))         AS features,
               (SELECT count(*) FROM track_lyrics l
                 WHERE NOT EXISTS (SELECT 1 FROM owned_files mf
                                    WHERE mf.track_id = l.track_id))         AS lyrics,
               (SELECT count(*) FROM artists a
                 WHERE NOT {ARTIST_OWNED}
                   AND EXISTS (SELECT 1 FROM artist_bios ab
                                WHERE ab.artist_id = a.id AND ab.source = 'lastfm'))
                                                                             AS lastfm,
               pg_total_relation_size('tracks') + pg_total_relation_size('album_tracks')
             + pg_total_relation_size('albums') + pg_total_relation_size('track_artists')
             + pg_total_relation_size('album_artists') AS size_bytes
        FROM (SELECT count(*) AS total FROM tracks) t,
             (SELECT count(DISTINCT track_id) AS tracks FROM owned_files) o
    """)
    from streaming import service as streaming_service
    return {
        "enabled":    bool(_read("discovery.phantom_layer")),
        "radio_streamed": bool(_read("radio.streamed")),
        # False on an install set up without the demo channel's runtime
        # (deno): what streams there is the 30 s excerpt
        "demo_runtime": streaming_service.demo_runtime(),
        "tracks":     int(row["tracks"]),
        "artists":    int(row["artists"]),
        "albums":     int(row["albums"]),
        "genres":     int(row["genres"]),
        "size_bytes": int(row["size_bytes"]),
        "enrichment": {
            "embeddings": int(row["embeddings"]),
            "features":   int(row["features"]),
            "lastfm":     int(row["lastfm"]),
            "lyrics":     int(row["lyrics"]),
        },
        "prune": {
            "running":          bool(_phantom_state["running"]),
            "cancel_requested": bool(_phantom_state["cancel_requested"]),
            "progress":         _phantom_state["progress"],
            "pct":              _phantom_state["pct"],
            "error":            _phantom_state["error"],
        },
    }


# ============================================================
# Reads
# ============================================================

def _folders_state() -> Dict[str, Any]:
    """The library's folders as Settings › Library lists them, oldest first:
    {id, path, name (its disk's label and the folder under it, else its
    path), disk (it lies on a disk in the disks' folder), connected (it
    answers by this process's recorded look — the one definition: a folder
    not there reads not answering there too),
    albums, tracks, away_copies, forgettable (not the folder a setup node's
    .env names), pullable (on a disk mounted read-only)} — the counts in one
    statement over the files by prefix — and `elsewhere`, the local copies
    under no folder ({albums, tracks}, or None)."""
    import library_root
    from scanner import gone_prefixes
    folders = library_root.given()
    prefixes = [f["path"].rstrip("/") + "/" for f in folders]
    counts = {r["p"]: r for r in db_query("""
        SELECT f.p, count(DISTINCT av.album_id) AS albums, count(DISTINCT mf.track_id) AS tracks,
               count(DISTINCT av.id) FILTER (WHERE av.absent_since IS NOT NULL) AS away_copies
          FROM unnest(%(p)s::text[]) AS f(p)
          LEFT JOIN media_files mf ON starts_with(mf.file_path, f.p)
          LEFT JOIN album_variants av ON av.id = mf.album_variant_id
         GROUP BY f.p""", {"p": prefixes})}
    elsewhere = db_query_one("""
        SELECT count(DISTINCT av.album_id) AS albums, count(DISTINCT mf.track_id) AS tracks
          FROM media_files mf JOIN album_variants av ON av.id = mf.album_variant_id
         WHERE NOT mf.file_path ^@ ANY(%(p)s::text[])""", {"p": prefixes})
    gone = set(gone_prefixes())
    env = library_root.env_folder()
    disks = [r for r in library_root.roots() if library_root._disks_folder(r)]
    listed = []
    for f, prefix in zip(folders, prefixes):
        c = counts.get(prefix) or {"albums": 0, "tracks": 0, "away_copies": 0}
        connected = prefix not in gone
        listed.append({"id": f["id"], "path": f["path"], "name": library_root.folder_name(f),
                       "label": f["label"], "disk": any(prefix.startswith(d.rstrip("/") + "/") for d in disks),
                       "connected": connected,
                       "albums": int(c["albums"]), "tracks": int(c["tracks"]),
                       "away_copies": int(c["away_copies"]),
                       "forgettable": f["id"] is not None and f["path"] != env,
                       "pullable": connected and library_root.pullable(f["path"])})
    return {"folders": listed,
            "elsewhere": ({"albums": int(elsewhere["albums"]), "tracks": int(elsewhere["tracks"])}
                          if elsewhere["albums"] else None)}


async def _library_state() -> Dict[str, Any]:
    """Stats + ongoing scan progress for the Library section.

    The Library screen is where these numbers live — Tracks / Artists /
    Albums / Genres on the Library side, Embeddings / Features /
    Last.fm / Lyrics on the Enrichment side."""
    from main import get_stats, _scan_state, _enrich_state, _prune_plan_counts
    from scanner import check_folder

    try:
        stats = await get_stats()
    except Exception as e:
        logger.warning(f"Library stats query failed: {e}")
        stats = {}

    scan = {
        "running":           bool(_scan_state.get("running")),
        "cancel_requested":  bool(_scan_state.get("cancel_requested")),
        "progress":          _scan_state.get("progress"),
        "stats":             _scan_state.get("stats"),
        # the last run's outcome: a removal refused when it looked
        # (result.prune.refused) is said by the page that said yes
        "result":            _scan_state.get("result"),
        "prune_plan":        (_prune_plan_counts(_scan_state["prune_plan"])
                              if _scan_state.get("prune_plan") else None),
    }
    enrich = {
        "running":           bool(_enrich_state.get("running")),
        "cancel_requested":  bool(_enrich_state.get("cancel_requested")),
        "progress":          _enrich_state.get("progress"),
    }

    # The screen reads each folder's state from the looks; opening it is a
    # moment to look, as a producer — not waited for, so the screen never
    # hangs on a folder: an answer that differs from what the notices show
    # wakes them, and the folder list is drawn again with it.
    check_folder()
    # off the event loop: a folder's mount (a dead share) may stall what reads it
    folders = await asyncio.to_thread(_folders_state)
    # Copies a Rescan found none of the files of: kept, and removed only from
    # the row these numbers make (the partial index holds only them). Of
    # their albums, those that are away — no copy holding a file is not
    # (sql_queries.album_away's rule): what a removal would take with them.
    away = db_query_one("""
        SELECT count(*) AS copies,
               count(DISTINCT av.album_id) FILTER (WHERE NOT EXISTS (
                   SELECT 1 FROM album_variants o
                    WHERE o.album_id = av.album_id AND o.absent_since IS NULL
                      AND (EXISTS (SELECT 1 FROM media_files m WHERE m.album_variant_id = o.id)
                           OR EXISTS (SELECT 1 FROM hqp_library_files h
                                       WHERE h.album_variant_id = o.id)))) AS albums
        FROM album_variants av WHERE av.absent_since IS NOT NULL""")

    import library_root
    from playback import substitute
    return {
        # The library's folders (library_root.given), each with its state
        # and what it holds (_folders_state); `elsewhere` — the copies under
        # no folder, a folder replaced before the library had several
        "folders":            folders["folders"],
        "elsewhere":          folders["elsewhere"],
        # Who gives the library its folders: "pick" — the owner, here, under
        # the roots the console setup mounted (library_root); "launcher" —
        # the launcher's window; "setup" — the node's .env (a Docker node's
        # MUSIC_LIBRARY_PATH)
        "music_folder":       library_root.kind(),
        # The disks plugged in that the library never had, for one tap
        # (library_root.new_disks; nothing of one passed over is stored)
        "new_disks":          await asyncio.to_thread(library_root.new_disks),
        # What the owner's music with no file here plays as meanwhile: a
        # stream, or nothing until its files are back (the words of a
        # folder that is not connected)
        "streams":            substitute.streams(),
        # Library counts (matches launcher's Library block 2×2)
        "total_tracks":       stats.get("total_tracks", 0),
        "total_artists":      stats.get("total_artists", 0),
        "total_albums":       stats.get("total_albums", 0),
        "total_genres":       stats.get("unique_genres", 0),
        "total_size_bytes":   stats.get("total_file_size_bytes", 0),
        # Enrichment coverage (matches launcher's Enrichment 2×2). The audio
        # rows read against analysable_tracks — total_tracks minus material
        # under the analysis floor — not the library total.
        "analysable_tracks":  stats.get("analysable_tracks", 0),
        "embeddings_done":    stats.get("tracks_with_embeddings", 0),
        "features_done":      stats.get("tracks_with_features", 0),
        "instruments_done":   stats.get("tracks_with_instruments", 0),
        # The Lyrics row reads against the tracks with a copy that is here;
        # the Tracks cell above counts the library, away albums included
        "present_tracks":     stats.get("present_tracks", 0),
        "lyrics_done":        stats.get("tracks_with_lyrics", 0),
        "lastfm_done":        stats.get("artists_with_lastfm", 0),
        "lastfm_total":       stats.get("library_artists", 0),
        "analysis_available": bool(stats.get("analysis_available")),
        "local_analysis":     bool(stats.get("local_analysis")),
        # Last scan + runtime workers
        "last_scan_at":       _read("library.last_scan_at"),
        "scan":               scan,
        "enrich":             enrich,
        # `waiting`: of the copies away, those on a folder that is not
        # connected now — their removal waits for it
        "away":               {"albums": int(away["albums"]), "copies": int(away["copies"]),
                               "waiting": sum(f["away_copies"] for f in folders["folders"]
                                              if not f["connected"])},
        # whether each folder's watcher is armed (library_watch.state):
        # watching, no_events with why, unreachable, not_set — no line in the
        # Library block shows it (the owner, 2026-11-17); support and the
        # tests read it
        "watch":              library_watch.state(),
        # its estimate is synchronous DB work: off the event loop
        "instruments":        (await asyncio.to_thread(_instruments_state)
                               if stats.get("analysis_available") else None),
        "hqp_library":        _hqp_library_state(),
    }


def _instruments_state() -> Dict[str, Any]:
    """The "Recognise instruments" row: the switch and what the instrument
    pass has left here at this machine's pace (analysis_pace.estimate) —
    read with every library event, so the estimate moves as the pass's
    batches land."""
    import analysis_pace
    enabled = instruments_on()
    try:
        with get_db_context() as db:
            left = analysis_pace.estimate(db, reanalyze=bool(_read("enrichment.reanalyze_imported")))
    except Exception as e:
        # as the stats above: the rest of the screen stands, the row says
        # nothing of the time
        logger.warning(f"Instruments estimate failed: {e}")
        left = {"tracks": None, "audio_seconds": None, "pace": None, "basis": None,
                "seconds": None, "too_long": None}
    return {"enabled": enabled, **left}


def _hqp_library_state() -> Dict[str, Any]:
    """The HQPlayer library as a source: the HQPlayer the person chose
    (hqp_library.driven), what of it this node holds, whether the owner ever
    synced it (only then does the output re-check it on attach), and the
    running job. Read from the registry alone — it asks no HQPlayer
    anything: with none chosen there is no row and no connection (it dialled
    the config's default address at every wake of the Library stream until
    2026-11-22), and a row's product is what its first contact stored."""
    import hqp_library
    host, port = hqp_library.driven() or (None, None)
    state = hqp_library.job_state()
    # Every library this node holds copies from, the configured HQPlayer's
    # first — a friend's streamer that went home still has its rows here
    # until the owner forgets them (an address is not an identity).
    rows = db_query("""
        SELECT e.id, e.name, e.host, e.port, e.product, e.library_hash IS NOT NULL AS synced,
               e.last_synced_at,
               (SELECT count(*) FROM album_variants av JOIN hqp_library_files f ON f.album_variant_id = av.id
                 WHERE av.hqp_endpoint_id = e.id) AS files,
               (SELECT count(DISTINCT av.album_id) FROM album_variants av WHERE av.hqp_endpoint_id = e.id) AS albums,
               (SELECT count(*) FROM hqp_dsp_speed s WHERE s.hqp_endpoint_id = e.id) AS measured,
               (SELECT count(*) FROM hqp_ab_sittings a WHERE a.hqp_endpoint_id = e.id) AS compared
        FROM hqp_endpoints e
        ORDER BY (e.host = %(h)s AND e.port = %(p)s) DESC, e.id
    """, {"h": host, "p": port})
    endpoints = [{"id": r["id"], "name": r["name"], "label": hqp_library.label(r["name"], r["product"]),
                  "address": f"{r['host']}:{r['port']}" if r["host"] else None,
                  "product": r["product"],
                  # a library of its own = an HQPlayer on another machine
                  "own_library": hqp_library.has_own_library(r["host"]) if r["host"] else None,
                  "synced": bool(r["synced"]),
                  "last_synced_at": r["last_synced_at"].isoformat() if r["last_synced_at"] else None,
                  "files": int(r["files"] or 0), "albums": int(r["albums"] or 0),
                  # DSP settings measured on it (playback.hqp_load): they go
                  # with the row when it is forgotten
                  "measured": int(r["measured"] or 0),
                  # the person's comparisons by ear on it (AB-1 Q15): they go too
                  "compared": int(r["compared"] or 0),
                  "configured": bool(host and r["host"]
                                     and hqp_library.address_key(r["host"], r["port"]) == hqp_library.address_key(host, port))}
                 for r in rows]
    configured = next((e for e in endpoints if e["configured"]), None)
    # The picker registers every HQPlayer the owner chose; a LIBRARY is one
    # on another machine — the row of one on this machine is an output, not
    # a source, and stays out of this list unless it somehow holds copies.
    endpoints = [e for e in endpoints if e["own_library"] is not False or e["synced"] or e["files"]]
    if host:
        product = configured["product"] if configured else None
        state.update(endpoint=f"{host}:{port}", name=configured["name"] if configured else None,
                     label=hqp_library.label(configured["name"] if configured else None, product),
                     product=product,
                     own_library=hqp_library.has_own_library(host),
                     last_synced_at=configured["last_synced_at"] if configured else None,
                     files=configured["files"] if configured else 0,
                     albums=configured["albums"] if configured else 0,
                     measured=configured["measured"] if configured else 0,
                     compared=configured["compared"] if configured else 0,
                     synced=bool(configured and configured["synced"]))
    else:
        state.update(endpoint=None, name=None, label=None, product=None, own_library=None,
                     last_synced_at=None, files=0, albums=0, measured=0, compared=0, synced=False)
    state["endpoints"] = endpoints
    return state


def _hqp_target(endpoint_id: Optional[int]) -> tuple:
    """The HQPlayer a library action names: the row's address when an
    endpoint id is given (a library listed in Settings — a friend's box
    that is not the configured output), else the configured endpoint."""
    if endpoint_id is None:
        import hqp_library
        from playback.hqp_link import NOT_CHOSEN
        address = hqp_library.driven()
        if address is None:
            raise HTTPException(status_code=409, detail=NOT_CHOSEN)
        return address
    row = db_query_one("SELECT host, port FROM hqp_endpoints WHERE id = %(e)s", {"e": endpoint_id})
    if not row:
        raise HTTPException(status_code=404, detail="No such HQPlayer library")
    if not row["host"]:
        raise HTTPException(status_code=409, detail="This HQPlayer's address is not known — it last answered elsewhere")
    return row["host"], int(row["port"])


def _ai_state() -> Dict[str, Any]:
    """Provider/model/auth/usage snapshot for the AI section.

    Authentication state is one of:
      - 'oauth_signed_in' — the CLI agent picked (Claude Code, Codex) is
        signed in, by its own verdict (claude_code.auth, codex_cli.auth).
      - 'api_key_set'    — user has provided a raw API key.
        masked_key shows last 4 chars.
      - 'not_authenticated' — neither.

    Usage (balance, monthly spent, limit, days_left) is null when the
    provider doesn't expose a billing API for the current credential
    type — UI hides the row in that case."""
    provider = _read("ai.provider")
    model    = _read("ai.model")
    api_key  = _read("ai.api_key")

    auth_state = "not_authenticated"
    masked_key: Optional[str] = None
    # Authentication state is only meaningful once a provider has been
    # picked. Without one we leave auth_state at the default so the UI
    # hides the auth/usage rows entirely.
    if provider:
        if api_key:
            auth_state = "api_key_set"
            masked_key = "●" * 8 + (api_key[-4:] if len(api_key) >= 4 else api_key)
        elif provider == "claude_code":
            # Claude Code is OAuth (subscription). claude_code.get_state() knows
            # where the credentials live — incl. /home/agent/.claude in Docker,
            # where the CLI runs demoted to AGENT_USER. (Previously this imported a
            # non-existent routers.chat.oauth_status and swallowed the ImportError,
            # so auth_state was ALWAYS not_authenticated — which hid Claude Code on
            # Docker and disabled the AI-canon tier there.)
            try:
                import claude_code
                state = claude_code.get_state()
                _diag_observe_agent("claude", state)
                if state == "ready":
                    auth_state = "oauth_signed_in"
            except Exception:
                logger.exception("claude_code auth-state check failed")
        elif provider == "codex":
            # Codex mirrors the Claude Code CLI flow: auth.json from
            # `codex login` (or minted from OPENAI_API_KEY by the
            # runner). Either way the credential is CLI-side, so it
            # surfaces as the OAuth-style state, not api_key_set.
            try:
                import codex_cli
                state = codex_cli.get_state()
                _diag_observe_agent("codex", state)
                if state == "ready":
                    auth_state = "oauth_signed_in"
            except Exception:
                logger.exception("codex auth-state check failed")

    # Usage is intentionally null until we wire the provider billing
    # API; UI handles "row hidden when null". Placeholder structure
    # documents the shape we expect:
    usage = None  # {"spent": 3.20, "limit": 20, "days_left": 5}

    # AI canonization: default On only when the AI is "free" (subscription /
    # OAuth) — a paid API key bills per token, so default Off + the user opts in.
    # The tier runs on claude_code/anthropic only (ai_canon._provider skips
    # codex), so with codex selected it is available exactly when Claude Code
    # is ready underneath; codex auth alone must not enable it.
    if provider == "codex":
        try:
            import claude_code
            canon_capable = claude_code.get_state() == "ready"
        except Exception:
            logger.exception("claude_code auth-state check failed")
            canon_capable = False
        canon_free = canon_capable
    else:
        canon_capable = bool(provider) and auth_state != "not_authenticated"
        canon_free = auth_state == "oauth_signed_in"
    canon_pref = _read("ai.canonization_enabled")
    canon_enabled = canon_free if canon_pref is None else bool(canon_pref)
    try:
        mb_dump = db_query_one("SELECT 1 FROM mb_artist LIMIT 1") is not None
    except Exception:
        mb_dump = False
    with _aicanon_lock:
        canon_job = dict(_aicanon)

    return {
        "provider":       provider,
        "model":          model,
        "auth_state":     auth_state,
        "masked_key":     masked_key,
        "usage":          usage,
        "canonization": {
            "enabled":   canon_enabled,
            "is_default": canon_pref is None,
            "free":      canon_free,
            "available": canon_capable,
            "mb_dump":   mb_dump,   # the tier resolves against the local MB dump
            "job":       canon_job,
        },
    }


def _sync_state() -> Dict[str, Any]:
    from routers.p2p import ONLINE_WINDOW_MIN

    friends_online = 0
    friends_total = 0
    try:
        row = db_query_one(f"""
            SELECT
                COUNT(*) FILTER (WHERE is_blocked = FALSE) AS total,
                COUNT(*) FILTER (
                    WHERE is_blocked = FALSE
                      AND last_seen IS NOT NULL
                      AND last_seen > NOW()
                          - make_interval(mins => {ONLINE_WINDOW_MIN})
                ) AS online
            FROM friends
        """)
        if row:
            friends_total  = int(row.get("total") or 0)
            friends_online = int(row.get("online") or 0)
    except Exception as e:
        logger.warning(f"friends count failed: {e}")

    bg_status = None
    try:
        import background_enrichment
        bg_status = background_enrichment.status()
    except Exception as e:
        logger.warning(f"background_enrichment status read failed: {e}")
    import notary

    def _relay_client_count() -> int:
        # Docker mode: the relay registry lives in this process. Launcher
        # mode: the count lives in the launcher's sync server, which this
        # backend cannot see — 0 is honest there ("none via this surface").
        try:
            from routers.peer_chat import relay_client_count
            return relay_client_count()
        except Exception:
            return 0

    return {
        "p2p_enabled":             bool(_read("sync.p2p_enabled")),
        "pass_on":                 bool(_read("sync.pass_on")),
        # The pass-on switch is shown where it matters: off, or on a node
        # that was born small (hardware_profile.nano)
        "nano":                    _nano(),
        # This node has a host agent: its machine's diagnostics can be
        # collected from the Support row (routers/host.py)
        "host_agent":              host_channel.available(),
        "auto_interval_min":       _read("sync.auto_interval_min"),
        "announce_limit":          _read("sync.announce_limit"),
        "carry_limit":             _read("sync.carry_limit"),
        "background_enrichment":   bool(_read("enrichment.background_enabled")),
        "reanalyze_imported":      bool(_read("enrichment.reanalyze_imported")),
        "relay_enabled":           bool(_read("p2p.relay_enabled")),
        "relay_clients":           _relay_client_count(),
        "background_status":       bg_status,
        "notary_status":           notary.status(),
        "last_sync_at":            _read("sync.last_at"),
        "last_items_received":     _read("sync.last_items_received"),
        "friends_online":          friends_online,
        "friends_total":           friends_total,
        "reachability":            _read("p2p.reachability") or "unknown",
        "reachability_detail":     _read("p2p.reachability_detail"),
        "reachability_checked_at": _read("p2p.reachability_checked_at"),
        # Network-size verdict (desktop/p2p/network_size.py): the tail is
        # announced and searched only past the threshold.
        "network_estimate":        _read("p2p.network_estimate"),
        "rare_mode":               bool(_read("p2p.rare_mode")),
        "identity":                _identity_row(),
        "load":                    _read("p2p.load"),
        "gate":                    _gate_snapshot(),
        "support":                 _support_state(),
    }


def _nano() -> bool:
    import hardware_profile
    return hardware_profile.resolve().nano


def _support_state() -> Dict[str, Any]:
    from desktop.p2p import diag_events
    with get_conn() as conn:
        pending = diag_events.pending_count(conn)
        requests = diag_events.waiting_warrants(conn)
    return {
        "enabled":        bool(_read("support.diagnostics_enabled")),
        # Whether this database has a shipper: a launcher's, or since
        # 2026-11-21 a Docker node's worker beside its chat client (diag_node);
        # without one (the master, no account yet) the reports stay here
        "sends_reports":  claude_code.is_launcher_mode() or diag_node.ships(),
        "last_report_at": _read("support.last_report_at"),
        "last_bundle_at": _read("support.last_bundle_at"),
        "pending_events": pending,
        "requests":       [_support_request(r) for r in requests],
    }


def _support_request(row: Dict[str, Any]) -> Dict[str, Any]:
    """A support request waiting for the person, as the request sheet shows
    it: what support asked for, split into the structural part (it goes
    either way) and the content scopes the person ticks."""
    from desktop.p2p.diag_protocol import CHAT_DEFAULT_DAYS, CONTENT_SCOPES
    return {**row,
            "structural": [s for s in row["scopes"] if s not in CONTENT_SCOPES],
            "content": [s for s in CONTENT_SCOPES if s in row["scopes"]],
            # the conversations' window when support named no `since`
            "window_days": CHAT_DEFAULT_DAYS}


class SupportDecision(BaseModel):
    send: List[str] = Field(default_factory=list, max_length=8)


@router.post("/support/requests/{warrant_id}")
def decide_support_request(warrant_id: uuid.UUID, req: SupportDecision) -> Dict[str, Any]:
    """The person's answer to a support request (the request sheet): `send`
    names the content scopes they ticked — none sends the structural part
    alone. 409 when the request no longer waits (decided on another tab,
    expired, or withdrawn by the switch). The node's support worker collects and
    sends it on the decision's own wake (diag_events.decide)."""
    from desktop.p2p import diag_events
    from desktop.p2p.diag_protocol import CONTENT_SCOPES
    unknown = set(req.send) - set(CONTENT_SCOPES)
    if unknown:
        raise HTTPException(status_code=400, detail=f"not a content scope: {sorted(unknown)}")
    with get_conn() as conn:
        decided = diag_events.decide(conn, str(warrant_id), sorted(set(req.send)))
    if not decided:
        raise HTTPException(status_code=409, detail="This request no longer waits for an answer")
    logger.info("Support request %s… decided: sent=%s", str(warrant_id)[:8],
                ",".join(sorted(set(req.send))) or "-")
    return {"ok": True}


def _diag_observe_agent(agent: str, state: str) -> None:
    """Support diagnostics: agent auth state, observed on the reads the UI
    already makes — a change becomes an `agent.state_changed` event."""
    from desktop.p2p import diag_events
    with get_conn() as conn:
        diag_events.observe_agent_state(conn, agent, state)


def _diag_signin_opened(agent: str, state: str, flow: str) -> None:
    from desktop.p2p import diag_events
    with get_conn() as conn:
        diag_events.record(conn, "agent.signin_opened",
                           {"agent": agent, "state": state, "flow": flow,
                            "source": "settings"})


def _signin_changed(agent: str, snap: Dict[str, Any]) -> None:
    """The sign-in driver's on_change, on its reader thread: every parsed
    change wakes the screen's SSE clients; the exit also observes the new
    auth state right here — the tab that started the sign-in may be gone,
    so the state read that used to notice cannot be relied on — and keeps
    a failure for support."""
    if agent == "claude":
        import claude_code as cli
        notify = notify_claude_subscribers
    else:
        import codex_cli as cli
        notify = notify_codex_subscribers
    if snap["running"]:
        notify()
        return
    state = cli.get_state()
    if state == "ready":
        from providers import reset as _reset_providers
        _reset_providers()
    _diag_observe_agent(agent, state)
    if snap.get("timed_out") or snap.get("error"):
        from desktop.p2p import diag_events
        kind = "agent.signin_timeout" if snap.get("timed_out") else "agent.signin_failed"
        with get_conn() as conn:
            diag_events.record(conn, kind, {
                "agent": agent, "flow": snap.get("flow"),
                "error": (snap.get("error") or "")[:300], "source": "settings",
            })
    notify()


def _gate_snapshot() -> Optional[Dict[str, Any]]:
    """The admission gate's pricer as seen by this process (the peer surface
    lives in the same process on Docker); the launcher publishes its own
    snapshot to user_settings['p2p.gate']."""
    try:
        from desktop.p2p import pricing
        pricer = pricing.current()
        if pricer is not None:
            return pricer.snapshot()
    except Exception:
        pass
    return _read("p2p.gate")


def _audio_output_state() -> Dict[str, Any]:
    """Lightweight Audio output stub.

    We deliberately do NOT call routers.hqplayer.get_state() here —
    that endpoint does a full multi-roundtrip status/info/modes/rates
    fetch and can block for seconds when HQPlayer is unreachable. The
    frontend pings /api/hqplayer/state separately (with its own
    timeout / re-render) for the live connection indicator. Settings
    just returns the configured host:port so the row paints
    instantly with `connected: null`."""
    return {
        "hqplayer_connected": None,
        "hqplayer_host":      app_settings.hqplayer_host,
        "hqplayer_port":      app_settings.hqplayer_port,
    }


# ============================================================
# Guidance — the setup trail
# ============================================================
#
# What a node still needs a human for. The rule for "this is undone"
# lives here, next to the workers that satisfy it, because three
# surfaces point at the same task (the More tab, the drawer row, the
# control itself) and a trail whose steps disagree points nowhere.
# The client owns only the route each id maps to.

_GUIDANCE_DISMISSIBLE = {"audio_output", "update", "hqp_library", "this_node"}
_SIGNIN_NOTICES = {"claude_code.signed_out", "codex.signed_out"}


def _analysis_pending() -> bool:
    """Any owned track the sonic pass would still analyse: an analysis-
    source file at or above the material floor (the queues' own predicate
    — under one grid window nothing is analysed, provenance.
    MIN_MATERIAL_SECONDS — not of a copy that is away, and not longer than
    the pass can hold here now, analysis_pace.longest_track_seconds) without
    embeddings or without features. The instruments are not the trail's:
    their pass is the owner's switch, and their progress shows in their own
    row of the Library screen. EXISTS,
    not a count — the trail is a boolean, so the query stops at the
    first hit. Driven from media_files, never from tracks: the phantom
    layer makes `tracks` three million rows, and the owned-first shape
    is the difference between 46 ms and 2.9 s (measured)."""
    import analysis_pace
    import provenance
    row = db_query_one("""
        SELECT EXISTS (
            SELECT 1 FROM media_files mf
              JOIN album_variants av ON av.id = mf.album_variant_id
             WHERE mf.is_analysis_source AND av.absent_since IS NULL
               AND (mf.duration_seconds IS NULL
                    OR mf.duration_seconds >= %(min_seconds)s)
               AND (mf.duration_seconds IS NULL
                    OR mf.duration_seconds <= %(longest)s)
               AND (NOT EXISTS (SELECT 1 FROM embeddings e
                                 WHERE e.track_id = mf.track_id)
                    OR NOT EXISTS (SELECT 1 FROM audio_features af
                                    WHERE af.track_id = mf.track_id))
        ) AS pending
    """, {"min_seconds": provenance.MIN_MATERIAL_SECONDS,
          "longest": analysis_pace.longest_track_seconds([analysis_pace.SONIC])})
    return bool(row and row.get("pending"))


def _synced_since_scan() -> bool:
    """Has a sync run over what the last scan added?

    Analysis is the LAST step of setup, not the first: the scan gives
    the network something to match on, the sync brings back whatever
    peers already computed, and only the remainder is worth the GPU.
    A node with P2P off has nothing to wait for."""
    if not _read("sync.p2p_enabled"):
        return True
    last_sync = _read("sync.last_at")
    if not last_sync:
        return False
    last_scan = _read("library.last_scan_at")
    if not last_scan:
        return True
    from datetime import datetime
    # sync.last_at is the run's START, last_scan_at its FINISH — a sync
    # that began before the scan ended never saw the new files.
    return datetime.fromisoformat(last_sync) >= datetime.fromisoformat(last_scan)


def _guidance_state() -> Dict[str, Any]:
    from main import _scan_state, _enrich_state

    tasks: List[str] = []

    # The account has no verified email (B-96): on a node that never earns
    # its identity by computing it is the way to one, so it leads the trail
    # there; on every other node it follows the rest. More tab, the Profile
    # row, the Email row. Cleared by the verification — a state, never a
    # visit.
    unverified = _email_unverified()
    if unverified and _nano():
        tasks.append("verify_email")

    # A node whose owner adds its music folders here has none yet: More tab,
    # the Library row, its "Add a folder". Cleared by the first folder.
    import library_root
    if library_root.picks() and not library_root.paths():
        tasks.append("music_folder")

    # A node born with a preset (db_migrate): its owner is shown, once, what
    # the setup chose for the machine and where to change it — More tab, the
    # Profile row, the Hardware profile block. Cleared by the visit.
    if _read("node.birth_preset") and not _read("guidance.this_node_seen"):
        tasks.append("this_node")

    # Nothing plays until an output is picked, and the picker is two
    # levels down. Cleared by VISITING it: an owner who looked and kept
    # what was there has decided, and a node that keeps nagging after
    # that is broken, not helpful.
    if _read("output.type") is None and not _read("guidance.audio_output_seen"):
        tasks.append("audio_output")

    # `local_analysis`: the sonic pass runs on every profile with the ML
    # stack, lite included since 2026-11-11, so the gap this trail points at
    # closes locally wherever the button exists. A node with no ML runtime
    # has no button at all.
    if not _scan_state.get("running") and not _enrich_state.get("running"):
        import hardware_profile
        if (hardware_profile.resolve().local_analysis
                and _synced_since_scan() and _analysis_pending()):
            tasks.append("analyse_library")

    # A notice lights no trail of its own: a network line needs no attention,
    # and every other state shows on its own domain's screen. Two of them ask
    # something of the person that a page opened after their onset (no toast
    # then) would never tell: a release offered — More tab, the Profile row,
    # the Update button, retired by the visit, again for a newer release —
    # and a support request waiting for its review — More tab, the Sync row,
    # the Support row's Review, done when it is answered or expires.
    notices = _notices_state()["items"]
    offered = next((n for n in notices if n["key"] == "update.available"), None)
    if offered is not None and _read("guidance.update_seen") != offered["data"]["release"]:
        tasks.append("update")
    if any(n["key"].startswith("support.request.") for n in notices):
        tasks.append("support_request")

    # An agent the node needs has lost its sign-in (the notices name it):
    # the trail leads to its Sign in button — More tab, the AI assistant
    # row, the button. Done when the sign-in is back, a state the verdict
    # reads, never by visiting.
    if any(n["key"] in _SIGNIN_NOTICES for n in notices):
        tasks.append("ai_signin")

    # The HQPlayer chosen as the output runs on another machine and has a
    # library of its own that was never imported: the trail leads to the
    # Library block at the bottom of the HQPlayer screen — More tab, the
    # HQPlayer row, the Import chip — once per address; being shown it is
    # the whole of it (importing is the owner's call).
    if _read("output.type") == "hqplayer":
        import hqp_library
        host, port = hqp_library.driven() or (None, None)
        if host and hqp_library.has_own_library(host):
            ep = hqp_library.endpoint_by_address(host, port)
            if ((ep is None or ep["library_hash"] is None)
                    and _read("guidance.hqp_library_seen") != f"{host}:{port}"):
                tasks.append("hqp_library")

    if unverified and "verify_email" not in tasks:
        tasks.append("verify_email")

    return {"tasks": tasks}


def _email_unverified() -> bool:
    """This node has an account whose email is not verified (or has none)."""
    if not device_auth.account_configured():
        return False
    row = db_query_one("SELECT email_verified FROM user_profile WHERE id = 1")
    return not (row and row.get("email_verified"))


# Conditions with no ledger of their own (a vanished music folder, a missing
# binary) keep their `since` here from the moment they are first observed
# until they are not — process memory, which is enough: after a restart the
# condition is either gone or freshly observed.
_derived_since: Dict[str, str] = {}
# The binaries THIS process shells out to (analysis + provenance). `flac`
# is launcher-side only — the Docker image never ships it, and a check
# there would light every Docker node for nothing.
_MEDIA_TOOLS = ("ffmpeg", "fpcalc")


def _derived(key: str, active: bool) -> Optional[str]:
    from datetime import datetime, timezone
    if not active:
        _derived_since.pop(key, None)
        return None
    return _derived_since.setdefault(key, datetime.now(timezone.utc).isoformat())


def _notices_state() -> Dict[str, Any]:
    """What the notices channel serves: every derived notice (_notice_items)
    but a report the person has seen (the restored run's, the wrong
    passwords'), which is dropped. A report is seen where it is told — its
    toast's close or button (post_notice_seen)."""
    items = []
    for it in _notice_items():
        it["report"] = it["key"] in _REPORTS
        if not (it["report"] and it["seen"]):
            items.append(it)
    return {"items": items}


_REPORTS = ("hqplayer.run_restored", "hqplayer.ab_refused", "auth.login_failures",
            "auth.pin_failures", "power.undervoltage", "update.result")
# MusicBrainz's web service is asked to confirm what other nodes' slices
# propose (mb_answers): its pause is the network's line. Last.fm's and
# Deezer's are the background enrichment's state.
_COOLDOWN_DOMAIN = {"musicbrainz": "network"}


def _failures_notice(key: str) -> Optional[Dict[str, Any]]:
    """Wrong credentials since the owner last saw them — passwords
    (device_auth.login_refused) or pairing codes (device_auth.pin_refused),
    each record's key its notice's: a report, dropped once seen. Its onset is
    the latest failure, so one after the owner looked is news."""
    failures = _read(key)
    if not failures:
        return None
    return {"key": key, "kind": "warning", "domain": "account", "since": failures["last"], "until": None,
            "data": {"count": failures["count"], "first": failures["since"], "last": failures["last"]}}


def _notice_items() -> List[Dict[str, Any]]:
    """Active conditions that change what the user sees without any action
    of theirs: an external API that cooled us down, catalog data the
    network has not served yet. Like the guidance trail, the rule for WHEN
    a condition exists lives here next to the workers that create it; the
    view layer owns the words and the clock. Derived, never stored — the
    cooldown ledger and the slice cycles' status rows are the sources, so a
    condition ends the moment its source does. Each folder of the library
    comes from the readers' shared look (scanner.folders_unreachable_now),
    which waits a few seconds at most — a look that takes longer reads as a
    dead mount —
    so no stream hangs on a dead network mount, and the derivation never
    wakes the channel itself.

    Each item: `key` (stable — what toasts coalesce on), `kind`, `domain`
    (whose screen holds it: `network` alone is a row of Sync & P2P; every
    other state shows on its own domain's screen), `since` (the onset: a
    re-arm bumps it), `until` (when the condition clears or is retried, or
    null), `data` (facts for the copy), `seen` (a report the person has
    seen at this onset: _REPORTS, dropped by _notices_state)."""
    items: List[Dict[str, Any]] = []
    for row in db_query("""
        SELECT source, strikes, updated_at, cooldown_until, reason
        FROM external_api_cooldown
        WHERE cooldown_until > now()
        ORDER BY source
    """):
        items.append({
            "key": f"cooldown.{row['source']}",
            "kind": "warning",
            "domain": _COOLDOWN_DOMAIN.get(row["source"], "enrichment"),
            "since": row["updated_at"].isoformat(),
            "until": row["cooldown_until"].isoformat(),
            "data": {"source": row["source"], "strikes": row["strikes"],
                     "reason": row["reason"] or ""},
        })
    # A node that holds a family's dump asks nobody for it: no deferral,
    # whatever its cycle's row still says (a run that raced the load).
    for family, dump_marker in (("mb_slice", "musicbrainz.db_version"),
                                ("lb_slice", "listenbrainz.db_version")):
        slices = _read(f"{family}.status") or {}
        if slices.get("unserved") and not _read(dump_marker):
            items.append({
                "key": f"{family}.deferred",
                "kind": "info",
                "domain": "network",
                "since": slices.get("since"),
                "until": slices.get("next_attempt_at"),
                "data": {k: slices.get(k) for k in
                         ("unserved", "pending", "pending_capped", "served",
                          "reason", "sources")},
            })
    # A folder of the library that does not answer (its disk pulled, a share
    # gone): a warning toast at its onset, and its row in Settings › Library
    # its standing state — never an error that stays for a friend's stick
    # out for months (the owner, LIBRARY-FOLDERS-1 Q9). One a folder, its
    # onset when its look first said so.
    import library_root
    from scanner import folders_unreachable_now
    answers = folders_unreachable_now()
    out = [f for f in library_root.given() if answers.get(f["path"])]
    held = {r["p"]: r["albums"] for r in db_query("""
        SELECT f.p, count(DISTINCT av.album_id) AS albums FROM unnest(%(p)s::text[]) AS f(p)
          JOIN media_files mf ON starts_with(mf.file_path, f.p)
          JOIN album_variants av ON av.id = mf.album_variant_id
         GROUP BY f.p""", {"p": [f["path"].rstrip("/") + "/" for f in out]})} if out else {}
    keys = set()
    for folder in out:
        key = f"library.folder_out.{folder['id'] if folder['id'] is not None else 'env'}"
        keys.add(key)
        from playback import substitute
        items.append({"key": key, "kind": "warning", "domain": "library",
                      "since": _derived(key, True), "until": None,
                      "data": {"name": library_root.folder_name(folder),
                               "albums": int(held.get(folder["path"].rstrip("/") + "/", 0)),
                               "streams": substitute.streams()}})
    for key in [k for k in _derived_since if k.startswith("library.folder_out.") and k not in keys]:
        _derived(key, False)
    # A disk plugged in that the library never had and that holds music
    # (library_root.new_disks): offered for one tap while it is in, its
    # onset the first derivation that saw it — gone with the disk, and new
    # again at its next plug (nothing of one passed over is stored, Q8). A
    # toast only for a disk the running node saw plugged in: one there at
    # the start, or offered again after a Forget, has its row in Settings ›
    # Library alone (the owner, on the box).
    keys = set()
    for disk in (d for d in library_root.new_disks() if d["plugged"]):
        key = f"disk.new.{disk['uuid']}"
        keys.add(key)
        items.append({"key": key, "kind": "info", "domain": "library",
                      "since": _derived(key, True), "until": None,
                      "data": {k: disk[k] for k in ("path", "label", "audio_files", "capped")}})
    for key in [k for k in _derived_since if k.startswith("disk.new.") and k not in keys]:
        _derived(key, False)
    # A volume plugged into this machine that nothing mounted (the
    # automount's own word, library_root.unread): there while its file is,
    # gone with the disk. Its onset is the file's, so the same disk plugged
    # in again is news.
    for volume in library_root.unread():
        items.append({"key": f"disk.unread.{volume['device']}", "kind": "warning", "domain": "library",
                      "since": volume["since"], "until": None,
                      "data": {k: volume[k] for k in ("label", "format", "mounts", "driver", "unclear")}})
    missing = [t for t in _MEDIA_TOOLS if not shutil.which(t)]
    since = _derived("tools.missing", bool(missing))
    if since:
        items.append({"key": "tools.missing", "kind": "warning", "domain": "library",
                      "since": since, "until": None, "data": {"tools": missing}})
    # An fpcalc that is there and fails the fingerprint self-test (a missing
    # one is tools.missing): analysis refuses while it lasts
    import provenance
    verdict = provenance.fingerprinter()
    since = _derived("tools.fingerprint", verdict.state in ("failed", "mismatch"))
    if since:
        items.append({"key": "tools.fingerprint", "kind": "warning", "domain": "library",
                      "since": since, "until": None,
                      "data": {"state": verdict.state, "detail": verdict.detail,
                               "path": verdict.path}})
    from streaming import service as streaming_service
    for pid, health in sorted(streaming_service.provider_health.items()):
        items.append({"key": f"streaming.silent.{pid}", "kind": "warning", "domain": "streaming",
                      "since": health["silent_since"], "until": None,
                      "data": {"provider": pid,
                               "name": streaming_service.provider_name(pid),
                               "reason": health["reason"]}})
    # A provider that stepped aside by its own word (streaming.base
    # SteppedAside): its state read as it stands, under the provider's own
    # id — never the shared upstream's health key above, which any provider
    # on that upstream clears by answering. The words are the module's.
    for prov, aside in streaming_service.providers_aside():
        items.append({"key": f"streaming.aside.{prov.manifest.id}", "kind": "warning", "domain": "streaming",
                      "since": aside.since, "until": None,
                      "data": {"provider": prov.manifest.id, "name": prov.manifest.name,
                               "code": aside.code, "title": aside.title,
                               "text": aside.text, "detail": aside.detail}})
    # A dead HQPlayer output, as opposed to one track that would not play: the
    # same failure three plays in a row (playback.hqp_diagnostics). One failed
    # track is the toast Now Playing raises.
    from playback import hqp_diagnostics
    run = hqp_diagnostics.failing_run()
    if run and _read("output.type") == "hqplayer":
        items.append({"key": "hqplayer.failing", "kind": "error", "domain": "hqplayer",
                      "since": run["since"], "until": None,
                      "data": {"code": run["code"], "count": run["count"],
                               "title": run["title"]}})
    # Files HQPlayer refused of the last batch it was handed — one notice per
    # batch, its causes worded where it is shown
    refused = hqp_diagnostics.refused()
    if refused and _read("output.type") == "hqplayer":
        items.append({"key": "hqplayer.refused", "kind": "warning", "domain": "hqplayer",
                      "since": refused["since"], "until": None,
                      "data": {k: refused[k] for k in ("what", "count", "total", "stream")}})
    # A queue that meets a rate HQPlayer cannot reach, or may not (hqp_rate_check): said
    # while a track of it that can play plays or is paused, with the onset
    # the HQPlayer backend keeps (its _rate_notice_now) — the backend holds
    # the check; the words are the module's own, drawn as phrases, and
    # Why?, on the toast and on the row, the HQPlayer screen
    from playback.manager import manager
    active = manager.active
    rate = active.rate_notice() if active is not None and active.id == "hqplayer" else None
    if rate is not None:
        items.append({"key": "hqplayer.rate_check", "kind": "warning", "domain": "hqplayer",
                      "since": rate["since"], "until": None, "data": rate["data"]})
    # A benchmark run cut short whose put-back the owner's own change
    # superseded: what HQPlayer still holds of its test settings — the volume
    # among them, left at the run's level when a person's DSP change or
    # switch came first (a knob they set in HQPlayer gives it back) — until
    # they changed it all. Its run row is the source, whatever the output is
    # now
    from playback import hqp_benchmark
    left = hqp_benchmark.left_notice()
    if left is not None:
        items.append({"key": "hqplayer.run_left", "kind": "warning", "domain": "hqplayer",
                      "since": left["since"].isoformat(), "until": None,
                      "data": {"items": left["items"]}})
    # A comparison's put-back HQPlayer refused twice alike, left to the
    # owner (AB-1-DESIGN §17): a report, dropped once seen
    from playback import hqp_ab
    ab_refused = hqp_ab.refused_notice()
    if ab_refused is not None:
        items.append({"key": "hqplayer.ab_refused", "kind": "warning", "domain": "hqplayer",
                      "since": ab_refused["since"].isoformat(), "until": None,
                      "data": ab_refused["data"]})
    # One put back after HQPlayer went away under it, or answered nothing
    # past the build limit in it (`slow_start`): a report, dropped once seen
    # (_notices_state)
    restored = hqp_benchmark.restored_notice()
    if restored is not None:
        items.append({"key": "hqplayer.run_restored", "kind": "info", "domain": "hqplayer",
                      "since": restored["since"].isoformat(), "until": None,
                      "data": {"run_at": restored["run_at"].isoformat(),
                               "volume_left": restored["volume_left"],
                               "volume_before": restored["volume_before"],
                               "slow_start": restored["slow_start"]}})
    # One whose put-back could not read the mode it measured, or set the
    # owner's selection there back — HQPlayer did not let it — until that
    # mode is judged at HQPlayer's nearest rest or the next run's start (only
    # the next run's, once HQPlayer at rest did not let it be read), or the
    # owner leaves it as it is (`refused`: a mode the output no longer offers
    # is never judged)
    unchecked = hqp_benchmark.unchecked_notice()
    if unchecked is not None:
        items.append({"key": "hqplayer.run_unchecked", "kind": "info", "domain": "hqplayer",
                      "since": unchecked["since"].isoformat(), "until": None,
                      "data": {"mode": unchecked["mode"], "run": unchecked["run"],
                               "refused": unchecked["refused"]}})
    # One whose check left HQPlayer in that mode, until it is back where it
    # was found (`back_to`) or the owner leaves it as it is — its own key and
    # onset: a new condition, said even over a "not checked yet" already
    # seen, and anew once HQPlayer at rest refused it (`refused`)
    stayed = hqp_benchmark.stayed_notice()
    if stayed is not None:
        items.append({"key": "hqplayer.run_stayed", "kind": "warning", "domain": "hqplayer",
                      "since": stayed["since"].isoformat(), "until": None,
                      "data": {"mode": stayed["mode"], "run": stayed["run"],
                               "unchecked": stayed["unchecked"], "refused": stayed["refused"],
                               "back_to": stayed["back_to"]}})
    # A CLI agent signed out while something needs it: the assistant runs on
    # it, or — Claude Code only — gear research waits for it (the drain
    # claims nothing until the sign-in returns). Each verdict is the CLI's
    # own (claude_code.auth, codex_cli.auth); it is read as it stands, never
    # asked for here, and its onset dates the notice.
    provider = _read("ai.provider")
    agent = claude_code.auth.verdict()
    if agent is not None and not agent["signed_in"]:
        waiting = 0
        if gear_research_worker.waits_for_claude(look=False):
            waiting = db_query_one("""
                SELECT (SELECT count(*) FROM gear_models WHERE research_state = 'queued')
                     + (SELECT count(*) FROM gear_pair_notes WHERE research_state = 'queued')
                       AS n
            """)["n"]
        assistant = provider == "claude_code"
        if waiting or assistant:
            items.append({"key": "claude_code.signed_out", "kind": "error", "domain": "assistant",
                          "since": agent["since"], "until": None,
                          "data": {"waiting": waiting, "assistant": assistant}})
    agent = codex_cli.auth.verdict()
    if agent is not None and not agent["signed_in"] and provider == "codex":
        items.append({"key": "codex.signed_out", "kind": "error", "domain": "assistant",
                      "since": agent["since"], "until": None, "data": {}})
    # A support request that names the person's conversations, logs or
    # playback traces waits for their own decision on the request sheet
    # (desktop/p2p/diag_events.waiting_warrants); its Review opens it. Only a
    # launcher node receives one.
    from desktop.p2p import diag_events
    with get_conn() as conn:
        waiting = diag_events.waiting_warrants(conn)
    for row in waiting:
        items.append({"key": f"support.request.{row['id']}", "kind": "info", "domain": "support",
                      "since": row["received_at"], "until": row["expires_at"],
                      "data": _support_request(row)})
    for key in (device_auth.LOGIN_FAILURES_KEY, device_auth.PIN_FAILURES_KEY):
        failures = _failures_notice(key)
        if failures is not None:
            items.append(failures)
    # A signed release newer than the one this node runs, as the host
    # agent's last check found it (routers/host.py): the Update is one tap,
    # in Profile's Hardware profile block
    from routers import host as host_router
    offered = host_router.offered_release()
    if offered is not None:
        items.append({"key": "update.available", "kind": "info", "domain": "node", "since": offered["since"],
                      "until": None, "data": {"release": offered["release"]}})
    # How the last update ended (the node on the new release, or back on the
    # one before): a report, dropped once seen
    updated = host_router.update_report()
    if updated is not None:
        items.append({"key": "update.result", "domain": "node",
                      "kind": "info" if updated["outcome"] == "updated" else "warning",
                      "since": updated["since"], "until": None,
                      "data": {"outcome": updated["outcome"], "release": updated["release"],
                               "running": _software()["version"]}})
    # A Raspberry Pi's supply dropped under its voltage since this start
    # (power_watch): a report, dropped once seen
    import power_watch
    undervoltage = power_watch.notice()
    if undervoltage is not None:
        items.append(undervoltage)
    seen = _read("notice.seen") or {}
    for item in items:
        item["seen"] = seen.get(item["key"]) == item["since"]
    return items


@router.get("/guidance")
def get_guidance() -> Dict[str, Any]:
    """The trail for a page's first paint — {"tasks", "seq", "run"}; every
    change after it comes on the page's Library stream."""
    return _guidance.compute()


@router.get("/notices")
def get_notices() -> Dict[str, Any]:
    return _notices_state()


class NoticeSeen(BaseModel):
    since: str


@router.post("/notices/{key}/seen")
def post_notice_seen(key: str, body: NoticeSeen) -> Dict[str, Any]:
    """A report seen where it was told — its toast closed, or its button
    pressed, on any of the person's pages: that onset is kept, so the
    channel drops it everywhere and a later onset of it is news again. Only a
    report: a state ends with its source, never by being looked at."""
    if key not in _REPORTS:
        raise HTTPException(status_code=400, detail=f"not a report: {key}")
    # One statement: two pages closing two reports at once keep both
    moved = db_execute("""
        INSERT INTO user_settings (key, value) VALUES ('notice.seen', jsonb_build_object(%(k)s, %(s)s))
        ON CONFLICT (key) DO UPDATE
            SET value = CASE jsonb_typeof(user_settings.value) WHEN 'object' THEN user_settings.value
                             ELSE '{}'::jsonb END || EXCLUDED.value,
                updated_at = CURRENT_TIMESTAMP
            WHERE user_settings.value -> %(k)s IS DISTINCT FROM to_jsonb(%(s)s::text)
        RETURNING 1 AS moved""", {"k": key, "s": body.since})
    if moved:
        if key == device_auth.PIN_FAILURES_KEY:
            # The wrong pairing codes' record starts again at the next one
            device_auth.pin_failures_seen()
        db_execute("NOTIFY sautium_notices")
    return {"key": key, "since": body.since}


@router.post("/streaming/providers/{provider_id}/check")
def post_stream_provider_check(provider_id: str) -> Dict[str, Any]:
    """The person's "Check again" on a provider that stepped aside: the
    provider re-reads its own state now (it asks its service — a worker
    thread waits, never the event loop) and the answer is what it says
    afterwards. A change wakes the notices channel from the provider itself;
    the response is for the tap, which hears "still so" as well."""
    from streaming import service as streaming_service
    prov = streaming_service.get_provider(provider_id)
    if prov is None:
        raise HTTPException(status_code=404, detail="No such stream provider")
    prov.check_again()
    aside = prov.stepped_aside()
    return {"provider": prov.manifest.id, "name": prov.manifest.name,
            "aside": None if aside is None else {
                "code": aside.code, "since": aside.since, "title": aside.title,
                "text": aside.text, "detail": aside.detail}}


@router.post("/guidance/{task}/seen")
def post_guidance_seen(task: str) -> Dict[str, Any]:
    """Retire a task the user has been shown. Only the ones whose
    completion is a human's judgement rather than a state we can read."""
    if task not in _GUIDANCE_DISMISSIBLE:
        raise HTTPException(status_code=400, detail=f"not dismissible: {task}")
    if task == "update":
        # per release: a newer one offered later lights the trail again
        from routers import host as host_router
        offered = host_router.offered_release()
        if offered is not None:
            _write("guidance.update_seen", offered["release"])
    elif task == "hqp_library":
        # per HQPlayer: another one chosen later lights the trail again
        import hqp_library
        address = hqp_library.driven()
        if address is not None:
            _write("guidance.hqp_library_seen", f"{address[0]}:{address[1]}")
    else:
        _write(f"guidance.{task}_seen", True)
    notify_library_subscribers()
    return _guidance.compute()


@router.get("/library")
async def get_library_state() -> Dict[str, Any]:
    return await _library_state()


@router.get("/library/stream")
async def library_stream() -> EventStream:
    """The Library stream (the section at this module's top). A wake —
    "data: {}" — whenever scan / enrich workers transition state: the client
    pulls fresh state via GET /api/settings/library. Replaces 1.5s polling.

    `event: stream` names the connection, {"id"}, once, at connect: what
    the page quotes to say what this connection shows (put_stream_load).
    `event: guidance` carries the trail, {"tasks", "seq", "run"}: at
    connect and whenever the set differs from the last one this connection
    sent. `event: load` carries the node's load, {"load", "gate"} — to a
    connection that asked: at once, and at each band change of the meter."""
    stream = _LibraryStream(asyncio.get_event_loop())

    def named(event: str, payload: Dict[str, Any]) -> str:
        return f"event: {event}\ndata: {json.dumps(payload)}\n\n"

    async def event_generator():
        sent = None                     # the tasks of this connection's last `guidance`

        def guidance() -> Optional[str]:
            nonlocal sent
            said = _guidance.said
            if said is None or said["tasks"] == sent:
                return None
            sent = said["tasks"]
            return named("guidance", said)

        try:
            with _library_sse_lock:
                _library_streams.append(stream)

            # Initial ping so the client knows the channel is live.
            yield "data: {}\n\n"
            yield named("stream", {"id": stream.id})
            try:
                await asyncio.to_thread(_guidance.compute)
            except Exception:
                # The stream stands without it: the next wake computes
                logger.exception("The guidance trail was not computed for a stream's connect")
            message = guidance()
            if message:
                yield message

            while True:
                try:
                    await asyncio.wait_for(stream.evt.wait(), timeout=20.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                owed = stream.take()
                if "wake" in owed:
                    yield "data: {}\n\n"
                if "guidance" in owed:
                    message = guidance()
                    if message:
                        yield message
                if "load" in owed and stream.shows_load:    # not one owed before its page took it back
                    yield named("load", _load_said)
        except asyncio.CancelledError:
            pass
        finally:
            with _library_sse_lock:
                _library_streams[:] = [s for s in _library_streams if s is not stream]

    return EventStream(event_generator())


class StreamLoad(BaseModel):
    stream: str = Field(..., pattern=r"^[0-9a-f]{16}$")
    on: bool
    seq: int = Field(..., ge=1)


@router.put("/library/stream/load")
def put_stream_load(req: StreamLoad) -> Dict[str, Any]:
    """The page's word that its connection of the Library stream shows the
    node's load — the Sync screen's Headroom and Market rows, drawn — or no
    longer does. A connection that asks is sent the snapshot at once (a
    screen opened between two band changes shows the load, not nothing) and
    every band change after it; no other connection is sent any. `seq` is
    the page's count of its words: one that arrives after a later one
    changes nothing. 404 for a connection that is gone — its interest went
    with it, and the page's next connection says its own."""
    with _library_sse_lock:
        stream = next((s for s in _library_streams if s.id == req.stream), None)
        if stream is not None and req.seq > stream.load_seq:
            stream.load_seq, stream.shows_load, said = req.seq, req.on, True
        else:
            said = False
    if stream is None:
        raise HTTPException(status_code=404, detail="No such connection of the Library stream")
    if said and req.on:
        _load_now()
        stream.owe("load")
    return {"on": stream.shows_load}


@router.get("/ai")
def get_ai_state() -> Dict[str, Any]:
    return _ai_state()


@router.get("/sync")
def get_sync_state() -> Dict[str, Any]:
    return _sync_state()


@router.get("")
async def get_settings() -> Dict[str, Any]:
    """Aggregate roundtrip (kept for clients that want one fetch)."""
    return {
        "library":      await _library_state(),
        "ai":           _ai_state(),
        "sync":         _sync_state(),
        "audio_output": _audio_output_state(),
    }


# ============================================================
# AI preferences
# ============================================================

class AiProviderUpdate(BaseModel):
    provider: str = Field(..., max_length=40)


class AiModelUpdate(BaseModel):
    model: str = Field(..., max_length=80)


class AiKeySubmit(StepUp):
    # Clearing is DELETE /ai/key, on the token: a deletion lets nobody in
    api_key: str = Field(min_length=1, max_length=300)


def _canon_provider(p: Optional[str]) -> Optional[str]:
    """Web UI used to expose the Anthropic API provider as 'claude';
    backend registers it as 'anthropic'. Mapping at this layer keeps
    per-provider key/model buckets coherent across the rename."""
    return "anthropic" if p == "claude" else p


def _select_provider(new_provider: str) -> None:
    """The one way a provider becomes the node's pick — the Web UI's PUT
    and the first-boot seed both come through here. Stash the current
    model/key under per-provider keys so the next switch back restores
    them, then activate the new provider's saved model/key (or clear when
    there's nothing saved). The single `ai.api_key` / `ai.model` rows stay
    the source of truth for `_ai_state()` and `_resolve_*()` — switching
    just swaps what lives in them."""
    cur_provider = _canon_provider(_read("ai.provider"))

    # Stash the current pair under the *outgoing* provider's bucket
    # before overwriting the active slot.
    if cur_provider and cur_provider != new_provider:
        _write(f"ai.{cur_provider}.api_key", _read("ai.api_key"))
        _write(f"ai.{cur_provider}.model",   _read("ai.model"))

    # Restore the *incoming* provider's pair, or clear if unset.
    next_key   = _read(f"ai.{new_provider}.api_key")
    next_model = _read(f"ai.{new_provider}.model")
    _write("ai.api_key", next_key)
    _write("ai.model",   next_model)
    _write("ai.provider", new_provider)

    # Re-overlay credentials onto Pydantic settings and bust the
    # providers cache — same path PUT /ai/key uses.
    _apply_api_key_to_runtime(next_key)

    # The Claude Code sign-in notice names the assistant only while it
    # runs on Claude Code.
    db_execute("NOTIFY sautium_notices")

    # Connecting the AI after a dump was already loaded is the third trigger
    # (besides dump-load + scan): catch up the residue now. Gated + no-op without
    # the dump / when disabled, async in the background.
    start_aicanon_job()


@router.put("/ai/provider")
def put_ai_provider(req: AiProviderUpdate) -> Dict[str, Any]:
    new_provider = _canon_provider(req.provider)
    _select_provider(new_provider)
    return {"provider": new_provider}


@router.put("/ai/model")
def put_ai_model(req: AiModelUpdate) -> Dict[str, Any]:
    _write("ai.model", req.model)
    provider = _canon_provider(_read("ai.provider"))
    if provider:
        _write(f"ai.{provider}.model", req.model)
    return {"model": req.model}


def _apply_api_key_to_runtime(key: Optional[str]) -> None:
    """Push the saved key onto Settings so providers/__init__ creates
    the right provider on the next access. Mirrors the lastfm overlay
    in main.py — Pydantic Settings only reads .env at startup, so
    Web-UI key edits would otherwise stay invisible to the provider
    registry. Key is attributed by the currently-selected provider —
    the UI only exposes one key slot at a time, so we know whose
    credential this is."""
    from config import settings as app_settings
    provider = _read("ai.provider")
    if provider == "anthropic":
        app_settings.anthropic_api_key = key
    elif provider == "openai":
        app_settings.openai_api_key = key
    # Tear down the cached registry so the next get_provider() call
    # rebuilds with the new credential.
    try:
        from providers import reset as _reset_providers
        _reset_providers()
    except Exception:
        pass


def _store_ai_key(key: Optional[str]) -> Dict[str, Any]:
    _write("ai.api_key", key)
    provider = _canon_provider(_read("ai.provider"))
    if provider:
        _write(f"ai.{provider}.api_key", key)
    _apply_api_key_to_runtime(key)
    return _ai_state()


@router.put("/ai/key")
async def put_ai_key(opened=boxed(AiKeySubmit)) -> Dict[str, Any]:
    """Set the user-supplied API key. It rides the credential box — the Web
    UI is plain HTTP on the LAN — and takes the account's password
    (routers/auth.step_up): the owner's assistant traffic through another
    account outlasts "Sign out everywhere". The answer, sealed, shows the
    key masked. Validation against the provider's API is deferred — the
    UI's chat surface will surface auth errors on first call. Also stored
    under `ai.<provider>.api_key` so the next provider switch preserves it
    for restoration."""
    req, reply = opened
    key = req.api_key.strip()
    if not key:
        raise HTTPException(status_code=422, detail="Paste a key first")
    await step_up(req)
    return reply(await asyncio.to_thread(_store_ai_key, key))


@router.delete("/ai/key")
def delete_ai_key() -> Dict[str, Any]:
    return _store_ai_key(None)


class AiCanonUpdate(BaseModel):
    enabled: bool


@router.put("/ai/canonization")
def put_ai_canonization(req: AiCanonUpdate) -> Dict[str, Any]:
    """Toggle the AI canonization tier (explicit user choice overrides the
    free/paid default)."""
    _write("ai.canonization_enabled", bool(req.enabled))
    return _ai_state()


@router.post("/ai/canonization/run")
def run_ai_canonization() -> Dict[str, Any]:
    """Manual single-flight AI canonization run over the full residue. Returns
    immediately; progress streams over /ai/canonization/stream + GET /ai. force=
    True so the manual button works even if the auto-trigger gate is off."""
    started = start_aicanon_job(force=True)
    return {"started": started, "running": not started}


@router.get("/ai/canonization/stream")
async def ai_canonization_stream() -> EventStream:
    """SSE wake channel for the AI canonization run — client re-pulls GET /ai on
    each event. Same wake-event pattern as the library/claude channels."""
    loop = asyncio.get_event_loop()
    evt = asyncio.Event()

    async def event_generator():
        try:
            with _aicanon_sse_lock:
                _aicanon_sse_clients.append((evt, loop))
            yield "data: {}\n\n"
            while True:
                try:
                    await asyncio.wait_for(evt.wait(), timeout=20.0)
                    evt.clear()
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield "data: {}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            with _aicanon_sse_lock:
                _aicanon_sse_clients[:] = [
                    (e, l) for e, l in _aicanon_sse_clients if e is not evt
                ]

    return EventStream(event_generator())


# The registry's pick ids (providers/__init__.py). DEFAULT_PROVIDER may
# also say "none" — the wizard's explicit "no AI" — which seeds nothing.
_SEEDABLE_PROVIDERS = frozenset(
    {"claude_code", "codex", "anthropic", "openai", "openai_compat"})


def seed_ai_provider_from_env() -> None:
    """First boot: the install-time pick reaches the node as environment
    (the wizard's choice through the launcher's backend.env, compose's
    DEFAULT_PROVIDER for Docker), while the `ai.provider` row is what
    every reader consults — chat, the AI screen, the canon tier. Copy
    the pick and its key into the row ONCE, through the same switch the
    Web UI uses; the environment is never read for the pick again, so
    the launcher rewriting backend.env from config.json at each start
    cannot undo a choice made in the Web UI. A row that exists, whatever
    it holds, is the pick. Until 2026-09-22 the wizard's choice never
    reached the row: chat worked through its env fallback while Settings
    said "Not selected" and the canon tier stayed off."""
    if db_query_one("SELECT 1 FROM user_settings WHERE key = 'ai.provider'"):
        return
    provider = _canon_provider(app_settings.default_provider)
    if provider not in _SEEDABLE_PROVIDERS:
        return
    key = {"anthropic": app_settings.anthropic_api_key,
           "openai":    app_settings.openai_api_key}.get(provider)
    if key:
        _write(f"ai.{provider}.api_key", key)
    _select_provider(provider)
    logger.info(f"AI provider seeded from the install-time pick: {provider}")


def load_ai_credentials_from_db() -> None:
    """Startup-time overlay: bring DB-stored credentials onto Settings
    so the first request after a backend restart already sees them.
    Called from main.lifespan."""
    try:
        provider = _read("ai.provider")
        key = _read("ai.api_key")
        if not key:
            return
        from config import settings as app_settings
        if provider == "anthropic":
            app_settings.anthropic_api_key = key
        elif provider == "openai":
            app_settings.openai_api_key = key
    except Exception as e:
        logger.warning(f"Failed to load AI credentials from DB: {e}")


# ============================================================
# Claude Code (subscription CLI) state + install + sign-in
# ============================================================

# Reuse the existing _scan/_enrich pattern: a single dict tracks the
# install job so the UI can poll it while npm runs in a thread.
_claude_install_state: Dict[str, Any] = {
    "running": False,
    "progress": "",
    "error": None,
}


@router.get("/ai/claude/stream")
async def claude_stream() -> EventStream:
    """SSE channel for the AI > Claude Code screen. Wakes whenever the
    install worker mutates `_claude_install_state` or the signin
    endpoint is invoked, so the client re-fetches /api/settings/ai/claude/state
    in response to a real event instead of polling on a 2s timer.

    Same wake-event pattern as /api/settings/library/stream — server
    emits "data: {}" and the client re-pulls state."""
    loop = asyncio.get_event_loop()
    evt = asyncio.Event()

    async def event_generator():
        try:
            with _claude_sse_lock:
                _claude_sse_clients.append((evt, loop))
            yield "data: {}\n\n"
            while True:
                try:
                    await asyncio.wait_for(evt.wait(), timeout=20.0)
                    evt.clear()
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield "data: {}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            with _claude_sse_lock:
                _claude_sse_clients[:] = [
                    (e, l) for e, l in _claude_sse_clients if e is not evt
                ]

    return EventStream(event_generator())


@router.get("/ai/claude/state")
def get_claude_state(look: bool = False) -> Dict[str, Any]:
    """State machine for the Claude Code subscription CLI. The Web UI
    branches on `state` to show install / sign-in / ready affordances.
    `host_unsupported` means the backend can't shell out (Docker mode)
    and the UI should point the user at the Desktop Launcher.

    When the state transitions to 'ready' we invalidate the providers
    cache so /api/chat picks up the freshly-installed CLI without a
    backend restart.

    `look` is the explicit look at the sign-in — the screen opened, its
    Refresh: the CLI is asked again, so a sign-in made in a terminal shows
    here. The screen's own re-renders (a stream wake, a canon batch) read
    the held verdict. `auth` carries the verdict's facts — a refused
    call's own words among them — for the Sign in panel."""
    from claude_code import (
        auth, get_state, get_claude_executable, detect_node_version,
        is_launcher_mode, signin_snapshot,
    )
    state = get_state(fresh=look)
    _diag_observe_agent("claude", state)
    if state == "ready":
        from providers import reset as _reset_providers
        _reset_providers()
    node_ver = detect_node_version()
    claude = get_claude_executable()
    return {
        "state":          state,
        "launcher_mode":  is_launcher_mode(),
        "node_version":   ".".join(str(p) for p in node_ver) if node_ver else None,
        "claude_path":    str(claude) if claude else None,
        "install":        dict(_claude_install_state),
        "signin":         signin_snapshot(),
        "auth":           auth.verdict() if claude else None,
    }


@router.post("/ai/claude/install")
def post_claude_install() -> Dict[str, Any]:
    """Kick off `npm install @anthropic-ai/claude-code` in a worker
    thread. Returns immediately; the client polls /state until the
    job's `install.running` goes false. Idempotent — calling while a
    job is already running returns the in-flight state."""
    import threading
    from claude_code import (
        is_launcher_mode, detect_node_version, install_claude_runtime,
    )
    if not is_launcher_mode():
        raise HTTPException(
            status_code=400,
            detail="Claude Code install is only available in the native launcher. "
                   "Open Sautium's Desktop Launcher to install.",
        )
    if _claude_install_state.get("running"):
        return dict(_claude_install_state)

    node_ver = detect_node_version()
    if node_ver is None or node_ver[0] < 18:
        raise HTTPException(
            status_code=400,
            detail="Node.js 18+ is required. Re-run the Sautium installer to repair the Node bundle.",
        )

    _claude_install_state.update({
        "running": True,
        "progress": "Running npm install…",
        "error":   None,
    })
    notify_claude_subscribers()

    def _worker():
        try:
            ok, msg = install_claude_runtime()
            _claude_install_state["progress"] = msg
            _claude_install_state["error"] = None if ok else msg
        except Exception as e:
            _claude_install_state["error"] = str(e)
        finally:
            _claude_install_state["running"] = False
            notify_claude_subscribers()

    threading.Thread(target=_worker, daemon=True, name="claude-install").start()
    return dict(_claude_install_state)


# An agent's sign-in is the owner's assistant traffic through whichever
# account it signs into, and it outlasts "Sign out everywhere": it starts on
# the account's password (routers/auth.step_up), and the start's sealed reply
# carries the grant (device_auth "grants") the rest of that sign-in quotes —
# the Claude code pasted in, the Codex device code read out — for as long as
# the CLI waits.
def _signin_grant(agent: str) -> str:
    from desktop.agent_login import DEADLINE_SECONDS
    return device_auth.grant(f"signin.{agent}", DEADLINE_SECONDS)


def _signin_granted(agent: str, grant: str) -> None:
    if not device_auth.grant_holds(f"signin.{agent}", grant):
        raise HTTPException(status_code=403,
                            detail="This sign-in was started elsewhere, or has ended: start it again here",
                            headers={"X-Sautium-Auth-Error": "grant"})


class SigninStart(StepUp):
    # Codex's device-code flow: for a browser that cannot reach this host's
    # localhost:1455 (a phone). Forced inside a container.
    device: bool = False


class SigninGrant(BaseModel):
    grant: str = Field(max_length=64)


class SigninCode(SigninGrant):
    code: str = Field(max_length=512)


def _start_claude_signin() -> Dict[str, Any]:
    if claude_code.get_claude_executable() is None:
        raise HTTPException(status_code=400, detail="Claude Code is not installed.")
    snap = claude_code.start_signin(lambda s: _signin_changed("claude", s))
    _diag_signin_opened("claude", claude_code.get_state(), snap["flow"])
    return snap


@router.post("/ai/claude/signin")
async def post_claude_signin(opened=boxed(StepUp)) -> Dict[str, Any]:
    """Start the headless `claude auth login` (desktop/agent_login.py), on
    the account's password. Returns, sealed, the sign-in snapshot and the
    grant its pasted code is quoted with; every later change — the link and
    the paste prompt, the exit — wakes /ai/claude/stream. Idempotent while a
    sign-in runs (a new grant replaces the one before)."""
    req, reply = opened
    await step_up(req)
    snap = await asyncio.to_thread(_start_claude_signin)
    return reply({**snap, "grant": _signin_grant("claude")})


@router.post("/ai/claude/signin/code")
async def post_claude_signin_code(opened=boxed(SigninCode)) -> Dict[str, Any]:
    """The code the platform page showed (a browser that could not reach
    the CLI's localhost callback), into the CLI's stdin — sealed, with the
    grant the start handed this browser."""
    req, reply = opened
    _signin_granted("claude", req.grant)
    try:
        snap = await asyncio.to_thread(claude_code.submit_signin_code, req.code)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return reply(snap)


@router.delete("/ai/claude/signin")
def delete_claude_signin() -> Dict[str, Any]:
    import claude_code
    return claude_code.cancel_signin() or {"running": False}


# ============================================================
# OpenAI Codex (subscription CLI) state + install + sign-in
# ============================================================

_codex_install_state: Dict[str, Any] = {
    "running": False,
    "progress": "",
    "error": None,
}


@router.get("/ai/codex/stream")
async def codex_stream() -> EventStream:
    """SSE wake channel for the AI > Codex screen — same pattern as
    /ai/claude/stream."""
    loop = asyncio.get_event_loop()
    evt = asyncio.Event()

    async def event_generator():
        try:
            with _codex_sse_lock:
                _codex_sse_clients.append((evt, loop))
            yield "data: {}\n\n"
            while True:
                try:
                    await asyncio.wait_for(evt.wait(), timeout=20.0)
                    evt.clear()
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield "data: {}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            with _codex_sse_lock:
                _codex_sse_clients[:] = [
                    (e, l) for e, l in _codex_sse_clients if e is not evt
                ]

    return EventStream(event_generator())


@router.get("/ai/codex/state")
def get_codex_state(look: bool = False) -> Dict[str, Any]:
    """State machine for the OpenAI Codex CLI — Web UI branches on
    `state` exactly like the Claude Code screen. Transition to 'ready'
    busts the providers cache so chat picks the CLI up without a
    restart. `look` and `auth` as on the Claude Code screen."""
    from claude_code import detect_node_version, is_launcher_mode
    state = codex_cli.get_state(fresh=look)
    _diag_observe_agent("codex", state)
    if state == "ready":
        from providers import reset as _reset_providers
        _reset_providers()
    node_ver = detect_node_version()
    codex = codex_cli.get_codex_executable()
    return {
        "state":          state,
        "launcher_mode":  is_launcher_mode(),
        "node_version":   ".".join(str(p) for p in node_ver) if node_ver else None,
        "codex_path":     str(codex) if codex else None,
        "auth_method":    codex_cli.codex_auth_method(),
        "install":        dict(_codex_install_state),
        "signin":         _public_signin(codex_cli.signin_snapshot()),
        "auth":           codex_cli.auth.verdict() if codex else None,
    }


@router.post("/ai/codex/install")
def post_codex_install() -> Dict[str, Any]:
    """Kick off `npm install @openai/codex` in a worker thread —
    mirror of /ai/claude/install."""
    import threading
    import codex_cli
    from claude_code import detect_node_version, is_launcher_mode
    if not is_launcher_mode():
        raise HTTPException(
            status_code=400,
            detail="Codex install is only available in the native launcher. "
                   "Open Sautium's Desktop Launcher to install.",
        )
    if _codex_install_state.get("running"):
        return dict(_codex_install_state)

    node_ver = detect_node_version()
    if node_ver is None or node_ver[0] < 18:
        raise HTTPException(
            status_code=400,
            detail="Node.js 18+ is required. Re-run the Sautium installer to repair the Node bundle.",
        )

    _codex_install_state.update({
        "running": True,
        "progress": "Running npm install…",
        "error":   None,
    })
    notify_codex_subscribers()

    def _worker():
        try:
            ok, msg = codex_cli.install_codex_runtime()
            _codex_install_state["progress"] = msg
            _codex_install_state["error"] = None if ok else msg
        except Exception as e:
            _codex_install_state["error"] = str(e)
        finally:
            _codex_install_state["running"] = False
            notify_codex_subscribers()

    threading.Thread(target=_worker, daemon=True, name="codex-install").start()
    return dict(_codex_install_state)


def _public_signin(snap: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """A Codex sign-in as the state shows it to every signed-in browser, in
    the clear: the device code is a way in — whoever types it first at
    OpenAI signs this node into his account, and nothing comes back through
    the node — so the state says only that there is one; the browser that
    started the sign-in reads it sealed (POST /ai/codex/signin/code)."""
    if snap is None:
        return None
    return {**snap, "code": None, "code_ready": bool(snap.get("code"))}


def _start_codex_signin(device: bool) -> Dict[str, Any]:
    if codex_cli.get_codex_executable() is None:
        raise HTTPException(status_code=400, detail="Codex is not installed.")
    snap = codex_cli.start_signin(lambda s: _signin_changed("codex", s), device=device)
    _diag_signin_opened("codex", codex_cli.get_state(), snap["flow"])
    return snap


@router.post("/ai/codex/signin")
async def post_codex_signin(opened=boxed(SigninStart)) -> Dict[str, Any]:
    """Start the headless `codex login` — mirror of /ai/claude/signin: on the
    account's password, the snapshot and the grant sealed in the reply."""
    req, reply = opened
    await step_up(req)
    snap = await asyncio.to_thread(_start_codex_signin, req.device)
    return reply({**snap, "grant": _signin_grant("codex")})


@router.post("/ai/codex/signin/code")
async def post_codex_signin_code(opened=boxed(SigninGrant)) -> Dict[str, Any]:
    """The device code of the sign-in this browser started, sealed to it —
    the code the public state leaves out (_public_signin)."""
    req, reply = opened
    _signin_granted("codex", req.grant)
    snap = codex_cli.signin_snapshot()
    return reply({"code": snap.get("code") if snap and snap["running"] else None})


@router.delete("/ai/codex/signin")
def delete_codex_signin() -> Dict[str, Any]:
    import codex_cli
    return codex_cli.cancel_signin() or {"running": False}


# ============================================================
# Albums sort (Artist screen)
# ============================================================

_ALBUMS_SORTS = {
    "release_year", "time_listened", "popularity",
    "recently_added", "a_z",
}


class AlbumsSortUpdate(BaseModel):
    sort: str = Field(..., max_length=40)


@router.get("/albums-sort")
def get_albums_sort() -> Dict[str, Any]:
    val = _read("albums.sort")
    if val not in _ALBUMS_SORTS:
        val = "release_year"
    return {"sort": val}


@router.put("/albums-sort")
def put_albums_sort(req: AlbumsSortUpdate) -> Dict[str, Any]:
    if req.sort not in _ALBUMS_SORTS:
        raise HTTPException(status_code=400, detail="Unknown sort key")
    _write("albums.sort", req.sort)
    return {"sort": req.sort}


# ============================================================
# Hardware profile (full / standard / lite — HARDWARE-TIERS.md)
# ============================================================

def _hardware_state() -> Dict[str, Any]:
    """The profile this process resolved at its start, and the owner's model
    switch as it is stored now: the two differ from a change of the switch
    until the restart that applies it (`restart_required`)."""
    import hardware_profile
    profile = hardware_profile.resolve()
    state = profile.describe()
    wanted = hardware_profile.models_switch()
    state["models_switch"] = wanted
    state["restart_required"] = profile.models != "absent" and wanted != profile.ml_available
    # What this install carries beside the models (an image's build
    # arguments, backend/Dockerfile.mac): a screen that would offer one says
    # "set up without it" instead
    from streaming import service as streaming_service
    state["image"] = {"models": profile.models != "absent",
                      "assistant_cli": shutil.which("claude") is not None,
                      "streaming": streaming_service.demo_runtime()}
    # The downloaded models on a node that runs none, where the cache is the
    # node's own: its owner may remove them
    import model_cache
    if not profile.ml_available and model_cache.store_is_own():
        state["models_cache_bytes"] = model_cache.store_bytes()
    state["machine"] = _machine_name()
    state["software"] = _software()
    # What the setup made this node at its birth ("nano", or None): the
    # models row says what the node is where it was set up without them
    state["birth_preset"] = _read("node.birth_preset")
    # A node a restart policy holds (the image) can be restarted from here
    from claude_code_runner import IN_DOCKER
    state["restartable"] = IN_DOCKER
    # Whether an offline database is here: More's list and the pointers to
    # it offer a Nano node the screen only then
    state["dumps_held"] = _dumps_held(mb_job.section(), lb_job.section())
    return state


_CPUINFO = "/proc/cpuinfo"


def _machine_name() -> Optional[str]:
    """The board as its kernel names it (a Raspberry Pi's "Model" line —
    /proc/device-tree is masked in a container, /proc/cpuinfo is not); None
    where the kernel names none."""
    try:
        with open(_CPUINFO, encoding="ascii", errors="replace") as f:
            for line in f:
                if line.startswith("Model"):
                    return line.partition(":")[2].strip()[:80] or None
    except OSError:
        pass
    return None


def _software() -> Dict[str, Any]:
    """What this node runs: the version its tree states (the release it was
    cut for, desktop/build_common.py) and the checkout's commit."""
    from config import build_identity
    from desktop import updater
    version = updater.installer_version()
    return {"version": ".".join(map(str, version)) if version != (0, 0, 0) else None,
            **build_identity()}


@router.get("/hardware")
def get_hardware_profile() -> Dict[str, Any]:
    """The tier's selection is automatic (SAUTIUM_PROFILE env is the only
    override, for diagnostics); the model axis is the owner's switch
    (PUT /hardware/models). Feeds the Hardware profile block."""
    return _hardware_state()


class ModelsPrefs(BaseModel):
    on: bool


@router.put("/hardware/models")
def put_models_switch(req: ModelsPrefs) -> Dict[str, Any]:
    """"AI models on this machine". Stored now, applied at the next start:
    the profile is resolved once a process, before anything imports torch.
    Off removes nothing stored — the node's own analysis stays, sealed and
    served; it only stops making more. An install set up without the models
    has no switch to move."""
    import hardware_profile
    if hardware_profile.resolve().models == "absent":
        raise HTTPException(status_code=409,
                            detail="This install was set up without the AI models")
    _write(hardware_profile.MODELS_SETTING, bool(req.on))
    return _hardware_state()


@router.delete("/hardware/models/cache")
def delete_models_cache() -> Dict[str, Any]:
    """"Downloaded models · Remove", on a node that runs none: its owner's
    explicit act on files in the node's own cache folder. Refused while a
    model can still be loaded from them, and where that folder is not the
    node's own (a launcher's is the machine's, shared with every program)."""
    import hardware_profile
    import model_cache
    if not model_cache.store_is_own():
        raise HTTPException(status_code=409,
                            detail="The models here are in this machine's shared cache, not Sautium's own")
    if hardware_profile.resolve().ml_available:
        raise HTTPException(status_code=409,
                            detail="The models are in use: turn them off and restart Sautium first")
    return {"freed_bytes": model_cache.remove_store()}


@router.post("/restart")
async def post_restart():
    """"Restart Sautium": the backend ends its own process as a stop does —
    its shutdown runs — and the container's restart policy starts it again.
    Only where such a policy holds the node (the image); a launcher's
    backend is the launcher's to restart. Any signed-in device may ask: it
    can already do everything a signed-in device can."""
    import signal
    from claude_code_runner import IN_DOCKER
    if not IN_DOCKER:
        raise HTTPException(status_code=409, detail="This node is restarted from its launcher")
    from starlette.background import BackgroundTask
    from starlette.responses import JSONResponse
    logger.info("restart asked from the Web UI")
    # Run once this answer has been sent: the stop closes every connection
    return JSONResponse({"restarting": True},
                        background=BackgroundTask(os.kill, os.getpid(), signal.SIGTERM))


# ============================================================
# HQPlayer connection
# ============================================================

class HqplayerPrefs(BaseModel):
    host: Optional[str] = Field(default=None, max_length=255)
    port: Optional[int] = Field(default=None, ge=1, le=65535)


@router.get("/hqplayer")
def get_hqplayer_prefs() -> Dict[str, Any]:
    import hqp_library
    from playback.hqp_backend import _stream_mode
    host, port = hqp_library.driven() or (None, None)
    return {
        "host": host,
        "port": port,
        # How HQPlayer reaches the files — read off its address, never set:
        # by path on this machine, streams from anywhere else.
        "file_access": "stream" if _stream_mode() else "path",
    }


def _apply_hqplayer_address(host: Optional[str], port: Optional[int]) -> None:
    """The one place the HQPlayer this node drives is set — the person's
    pick: persisted to user_settings, copied onto the runtime settings
    (hqp_library.driven reads the copy), and its link chosen
    (hqp_link.choose) — another HQPlayer's link takes over and the old one
    retires: what it still owes goes, everything else it held gives up. The
    same HQPlayer picked again (any alias) keeps its link, and the build it
    may be in stays watched. Its endpoint row comes from the output's first
    contact (hqp_library.request_sync) or the next scan — never from here:
    a save must return at once, not after two control round-trips. A port
    alone moves a chosen HQPlayer's port and chooses none."""
    import hqp_library
    from config import settings as app_settings
    from playback import hqp_benchmark, hqp_link
    before = hqp_link.selected()
    if host is not None:
        _write("hqplayer.host", host)
        app_settings.hqplayer_host = host
    if port is not None:
        _write("hqplayer.port", port)
        app_settings.hqplayer_port = port
    address = hqp_library.driven()
    if address is None:
        return
    logger.info("HQPlayer settings updated: host=%s, port=%s", *address)
    if hqp_link.choose(*address) is not before and before is not None:
        # Another HQPlayer chosen: a run cut short on the one before owes
        # its put-back no more
        hqp_benchmark.left_by_person(before.host, before.port, "another HQPlayer was chosen")
        from playback import hqp_ab
        hqp_ab.left_by_person(before.host, before.port)


@router.put("/hqplayer")
def put_hqplayer_prefs(req: HqplayerPrefs) -> Dict[str, Any]:
    """Update the HQPlayer endpoint by address (the Output picker's own
    path is PUT /output with `hqplayer`). When HQPlayer is the selected
    output and the address names another HQPlayer, the backend is detached
    and re-attached, so a new address takes effect at once — and with it
    the way files reach it, read off that address: the attach mirrors the
    canonical queue in the URI form the address dictates. Another alias of
    the HQPlayer driven changes nothing in playback (its link dials it).
    (activate() is a no-op for an already-active type,
    so a plain re-activate would leave HQPlayer holding URIs of the old
    form.)"""
    from playback.manager import OutputHeld, manager
    if req.host is not None:
        req.host = req.host.strip()
        if not req.host:
            raise HTTPException(status_code=400, detail="hqplayer.host is required")
    # Not while the benchmark drives HQPlayer: its connection follows the
    # address, and nothing of the change is written meanwhile.
    try:
        with manager.output_change():
            from playback import hqp_link
            before = hqp_link.selected()
            _apply_hqplayer_address(req.host, req.port)
            # Only when HQPlayer IS the selection. Unset no longer means
            # "HQPlayer may have it" — an unset output resolves to this
            # device, so saving an address here must not quietly move
            # playback. Nothing is sent to the previous endpoint
            # (stop_old=False): an HQPlayer the owner just pointed away from
            # is left as it was. Another alias of the HQPlayer driven is the
            # same HQPlayer: it stays attached as it is.
            if (_read("output.type") == "hqplayer"
                    and not (hqp_link.selected() is before
                             and getattr(manager.active, "link", None) is before)):
                from routers.player import _hqp_configured, stop_status_poller
                if _hqp_configured():
                    # Registered before anything is released, as the picker
                    # does: a Stop an earlier switch away still owes the
                    # HQPlayer at this address is superseded
                    hqp_link.chosen().accept("attach")
                    manager.activate(None, stop_old=False)
                    try:
                        manager.activate("hqplayer")
                    except Exception as e:
                        # The address is saved either way; the picker shows
                        # the endpoint as disconnected until it answers.
                        logger.warning("HQPlayer re-attach after a settings change "
                                       "failed: %s", e)
                else:
                    stop_status_poller()
    except OutputHeld as e:
        raise HTTPException(status_code=409, detail=str(e))
    return get_hqplayer_prefs()


class OutputPrefs(BaseModel):
    type: Optional[str] = None          # 'hqplayer' | 'local' | 'dlna'
    device_id: Optional[str] = None     # local: "{hostapi}::{name}"
    exclusive: Optional[bool] = None    # local: WASAPI exclusive mode
    renderer: Optional[Dict[str, Any]] = None   # dlna: {udn, location, name}
    hqplayer: Optional[Dict[str, Any]] = None   # hqplayer: {host, port} — WHICH HQPlayer
    stream_quality: Optional[str] = None    # lossless | opus_192 | opus_96


@router.get("/output")
def get_output_prefs() -> Dict[str, Any]:
    from playback import hqp_link
    from playback.manager import manager
    hold = manager.held
    link = hqp_link.selected()
    return {
        "type": _read("output.type"),
        "device_id": _read("output.local_device"),
        "exclusive": bool(_read("output.local_exclusive")),
        "renderer": _read("output.dlna_renderer"),
        "stream_quality": _read("output.stream_quality") or "lossless",
        # What the output is lent to (the HQPlayer benchmark), and what
        # HQPlayer's link refuses a person's command with now, in its words
        # (HqpLink.busy) — the MCP assistant quotes both to keep its HQPlayer
        # tools off meanwhile.
        "held": hold["label"] if hold else None,
        "busy": link.busy() if link is not None else None,
    }


@router.put("/output")
def put_output_prefs(req: OutputPrefs) -> Dict[str, Any]:
    """Select the playback output (Output picker). Persisted, then applied
    live: the old backend stops what it plays (an HQPlayer building or not
    answering is stopped once it answers, and `hqplayer_stop` tells the
    person so), the canonical queue survives, and the new output takes the
    old one's place in it — the track, the second, playing or paused as it
    was: a play press once the change is over, through the play gate as any
    other (one it did not take is said in `resume_refused`). The HQPlayer
    or the renderer already driven, picked again, stays the output as it
    is."""
    from playback.manager import OutputHeld, manager

    # Not while the benchmark has the output: checked under the lock a hold
    # takes, so nothing of the change is written while one lasts.
    try:
        with manager.output_change():
            out, spot = _put_output_prefs(req)
    except OutputHeld as e:
        raise HTTPException(status_code=409, detail=str(e))
    if spot is not None and spot.playing:
        refused = _play_on()
        if refused:
            out["resume_refused"] = refused
    return out


def _play_on() -> Optional[str]:
    """The play press that carries playing across an output switch, as the
    person's own would go (routers.player._transport): None when the new
    output took it, else what it said."""
    from routers.player import _transport
    try:
        answer = _transport(True, "play_on")
    except HTTPException as e:
        return str(e.detail)
    return None if answer.get("ok") else (answer.get("refusal") or "the output did not take Play")


def _put_output_prefs(req: OutputPrefs) -> tuple:
    """The change itself, under the output-change locks. Returns the answer
    and where the output left switched from stood (PlaybackManager.spot),
    None when the output did not change."""
    from playback.manager import manager
    # Every field is checked before anything is written or chosen: a 400
    # after the HQPlayer address was applied left the output on a link the
    # choice had retired.
    if req.type is not None and req.type not in ("hqplayer", "local", "dlna", "browser"):
        raise HTTPException(status_code=400, detail=f"unknown output type: {req.type}")
    if req.type == "local":
        from playback.local import devices as local_devices
        if not local_devices.list_devices():
            raise HTTPException(
                status_code=409,
                detail="No local audio devices — the backend must run natively "
                       "(launcher mode) for WASAPI/ASIO/CoreAudio output")
    if req.type == "dlna" and not (req.renderer or _read("output.dlna_renderer")):
        raise HTTPException(status_code=409, detail="No DLNA renderer selected")
    if req.stream_quality is not None and req.stream_quality not in ("lossless", "opus_192",
                                                                     "opus_96"):
        raise HTTPException(status_code=400, detail=f"unknown stream quality: {req.stream_quality}")
    address = None
    if req.hqplayer is not None:
        # The picker lists every HQPlayer found or added; the tapped one
        # becomes THE HQPlayer this node drives — its address is the
        # selection, exactly as a renderer's record is for DLNA.
        host = str(req.hqplayer.get("host") or "").strip()
        if not host:
            raise HTTPException(status_code=400, detail="hqplayer.host is required")
        raw = req.hqplayer.get("port") or 4321
        try:
            port = int(raw)
        except (TypeError, ValueError):
            port = None
        if port is None or not 1 <= port <= 65535:
            raise HTTPException(status_code=400, detail=f"hqplayer.port is not a port: {raw!r}")
        address = (host, port)
    if req.type == "hqplayer" and address is None:
        # A pick names its HQPlayer: no default address stands in for one
        # (until 2026-11-22 the config's 127.0.0.1 was written here)
        import hqp_library
        if hqp_library.driven() is None:
            from playback.hqp_link import NOT_CHOSEN
            raise HTTPException(status_code=409, detail=NOT_CHOSEN)

    if address is not None:
        _apply_hqplayer_address(*address)
    if req.stream_quality is not None:
        _write("output.stream_quality", req.stream_quality)
    if req.type is not None:
        _write("output.type", req.type)
    if req.device_id is not None:
        _write("output.local_device", req.device_id or None)
    if req.exclusive is not None:
        _write("output.local_exclusive", bool(req.exclusive))
    if req.renderer is not None:
        _write("output.dlna_renderer", req.renderer or None)

    # A quality-only change must NOT re-activate the output (that would cut
    # the current track). It just alters the next track's media URL —
    # exactly the "current track plays out, change applies from the next
    # one" behavior. Re-activate only when the output identity changed.
    if not (req.type is not None or req.device_id is not None
            or req.exclusive is not None or req.renderer is not None
            or req.hqplayer is not None):
        return get_output_prefs(), None

    otype = _read("output.type")
    told = None         # what the person is told of a Stop the old output still owes
    # Where the music is — read before the detach, whose "disconnected"
    # wipes it: the new output starts there
    spot = manager.spot()
    try:
        if otype == "browser":
            told = manager.activate(None)
            manager.activate("browser", spot=spot)
        elif otype == "dlna":
            renderer = _read("output.dlna_renderer")
            active = manager.active
            if active is not None and active.id == "dlna" and active.drives(renderer):
                # The renderer already driven, picked again, stays the output
                # as it is — a play press made on it goes on there — and is
                # attached again only where it answers no more, as a play
                # press finds it. Let go and attached afresh, a re-pick while
                # the play gate attached the phone loaded its track twice,
                # once from the instance let go (2026-11-01)
                manager.answering()
                spot = None
            else:
                told = manager.activate(None)
                manager.activate("dlna", spot=spot, renderer=renderer)
        elif otype == "local":
            # Deactivate first — re-selecting "local" with a different device
            # must reconstruct the engine, and activate() is idempotent per type.
            told = manager.activate(None)
            manager.activate("local", spot=spot,
                             device_id=_read("output.local_device"),
                             exclusive=bool(_read("output.local_exclusive")))
        elif otype == "hqplayer":
            from playback import hqp_link
            link = hqp_link.chosen()
            # The HQPlayer already driven, picked again, stays the output as
            # it is — no detach, no attach: its DSP change, a resume, its
            # spot go on (the link took the alias picked, choose)
            if getattr(manager.active, "link", None) is not link:
                # Registered before anything is released: a Stop an earlier
                # switch away still owes this HQPlayer (its answer awaited
                # through a build or a silence) is superseded, even if it
                # answers while the output it switched to is let go
                link.accept("attach")
                told = manager.activate(None)
                manager.activate("hqplayer", spot=spot)
            else:
                spot = None     # no change: the HQPlayer plays on as it does
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))
    notify_library_subscribers()   # the guidance trail reads output.type
    out = get_output_prefs()
    if told:
        out["hqplayer_stop"] = told
    return out, spot


def load_hqplayer_from_db() -> None:
    """The process's copy of the person's saved pick (hqp_library.driven),
    made at the start: the saved host and port, or none. The environment's
    HQPLAYER_HOST is dropped here — it was the choice of an install older
    than the Output picker, which db_migrate's hqp_choice_v1 saved once; left
    in place it was the address every "saved, else the default" reader
    dialled on a node whose owner chose no HQPlayer. Dropped before the
    read: a failed read leaves no default behind."""
    from config import settings as app_settings
    app_settings.hqplayer_host = None
    host = _read("hqplayer.host")
    port = _read("hqplayer.port")
    if host:
        app_settings.hqplayer_host = host
        app_settings.hqplayer_port = int(port or 4321)


# ============================================================
# Sync & P2P preferences
# ============================================================

class SyncPrefs(BaseModel):
    p2p_enabled:             Optional[bool] = None
    pass_on:                 Optional[bool] = None
    auto_interval_min:       Optional[int]  = None  # null disables
    announce_limit:          Optional[int]  = None  # rare-artist tail size
    carry_limit:             Optional[int]  = None  # foreign tracks carried
    background_enrichment:   Optional[bool] = None
    reanalyze_imported:      Optional[bool] = None
    relay_enabled:           Optional[bool] = None
    diagnostics_enabled:     Optional[bool] = None


@router.put("/sync")
def put_sync_prefs(req: SyncPrefs) -> Dict[str, Any]:
    if req.pass_on is not None:
        _write("sync.pass_on", bool(req.pass_on))
    if req.p2p_enabled is not None or req.pass_on is not None:
        if req.p2p_enabled is not None:
            _write("sync.p2p_enabled", bool(req.p2p_enabled))
        # The launcher's LAN beacon and its serving follow the switch now
        # (desktop/p2p/p2p_manager._sharing_changed), not at a cache's expiry,
        # and so does this process's peer surface — and every requester of
        # both runtimes, which reads the same switch: the Discovery chip
        # follows it live, and a node turned back on asks the network now,
        # not at the walk's next interval (its run wakes both slice cycles).
        db_execute("NOTIFY sautium_sharing")
        from routers import sync as sync_router
        sync_router.sharing_changed()
        db_execute("NOTIFY sautium_mb_sources")
        if req.p2p_enabled:
            db_execute("NOTIFY sautium_sync_request")
        # The guidance trail reads the switch (_synced_since_scan: with
        # sharing off there is no sync to wait for before the analysis)
        notify_library_subscribers()
    if req.auto_interval_min is not None:
        # None gets coerced to null serverside via _write — explicit
        # 0 here means "every 0 min" (disabled in the UI). We store
        # the raw int; UI maps 0/null → "Disabled".
        _write("sync.auto_interval_min",
               int(req.auto_interval_min) if req.auto_interval_min > 0 else None)
    if req.announce_limit is not None:
        _write("sync.announce_limit",
               int(req.announce_limit) if req.announce_limit > 0 else None)
    if req.carry_limit is not None:
        _write("sync.carry_limit",
               int(req.carry_limit) if req.carry_limit > 0 else None)
    if req.reanalyze_imported is not None:
        _write("enrichment.reanalyze_imported", bool(req.reanalyze_imported))
    if req.relay_enabled is not None:
        _write("p2p.relay_enabled", bool(req.relay_enabled))
        if not req.relay_enabled:
            # The slots already held end now — on this surface here, on a
            # launcher's peer server from its LISTEN (p2p_manager).
            from routers.peer_chat import close_relay_clients
            close_relay_clients()
            db_execute("NOTIFY sautium_relay_off")
    if req.diagnostics_enabled is not None:
        _write("support.diagnostics_enabled", bool(req.diagnostics_enabled))
        # A support request waiting for the person is withdrawn by the
        # switch: every open tab drops its notice now
        db_execute("NOTIFY sautium_notices")
    if req.background_enrichment is not None:
        want = bool(req.background_enrichment)
        _write("enrichment.background_enabled", want)
        # Drive the loop directly off the flag — the toggle is the
        # source of truth, so the persisted value and the running
        # thread can never disagree.
        try:
            import background_enrichment
            if want:
                background_enrichment.start()
            else:
                background_enrichment.stop()
        except Exception as e:
            logger.warning(f"background_enrichment toggle failed: {e}")
    return _sync_state()


class DumpPrefs(BaseModel):
    auto_update: Optional[bool] = None


def _dump_prefs(job: DumpJob, req: DumpPrefs) -> Dict[str, Any]:
    if req.auto_update is not None:
        _write(f"{job.prefix}.auto_update", bool(req.auto_update))
    return job.section()


def _dump_delete(job: DumpJob) -> Dict[str, Any]:
    """Remove a local dump. The wizard pre-ticks the download when the disk
    allows it; this is the other half of that bargain."""
    try:
        result = job.delete()
    except (DumpBusy, RuntimeError) as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        logger.error(f"{job.family} dump delete failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    notify_library_subscribers()
    return {"success": True, **result}


def _dump_status(job: DumpJob) -> Dict[str, Any]:
    """Dump state + disk budget in one read — what the assistant's
    *_dump_status tool quotes before offering (or declining) a download."""
    return {**job.section(), "disk": job.loader.disk_budget()}


def _dump_update(job: DumpJob, force: bool) -> Dict[str, Any]:
    """Download (if newer) + load a dump in the background (queued behind a
    running job of the other family)."""
    try:
        job.start(force)
    except InsufficientDisk as e:
        raise HTTPException(status_code=507, detail=str(e))
    except DumpBusy as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"success": True}


@router.put("/musicbrainz")
def put_mb_prefs(req: DumpPrefs) -> Dict[str, Any]:
    return _dump_prefs(mb_job, req)


@router.post("/musicbrainz/delete")
def mb_delete() -> Dict[str, Any]:
    return _dump_delete(mb_job)


@router.get("/musicbrainz/status")
def mb_status() -> Dict[str, Any]:
    return _dump_status(mb_job)


@router.post("/musicbrainz/update")
def mb_update(force: bool = False) -> Dict[str, Any]:
    return _dump_update(mb_job, force)


@router.put("/listenbrainz")
def put_lb_prefs(req: DumpPrefs) -> Dict[str, Any]:
    return _dump_prefs(lb_job, req)


@router.post("/listenbrainz/delete")
def lb_delete() -> Dict[str, Any]:
    return _dump_delete(lb_job)


@router.get("/listenbrainz/status")
def lb_status() -> Dict[str, Any]:
    return _dump_status(lb_job)


@router.post("/listenbrainz/update")
def lb_update(force: bool = False) -> Dict[str, Any]:
    return _dump_update(lb_job, force)


@router.get("/databases")
def get_databases() -> Dict[str, Any]:
    """Read for the Offline databases screen — every optional bulk dump this
    node can hold, in one round-trip. Its own endpoint rather than a field on
    /phantoms: that payload counts the whole phantom layer (~0.4 s) and this
    screen wakes on every load-progress tick. A new dump family is a key
    here and an entry in the UI's DUMP_FAMILIES."""
    # `nano`: the screen says why the dumps are not offered there
    mb, lb = mb_job.section(), lb_job.section()
    return {"musicbrainz": mb, "listenbrainz": lb, "nano": _nano(), "dumps_held": _dumps_held(mb, lb)}


def _dumps_held(*sections: Dict[str, Any]) -> bool:
    """A dump family loaded, or its load on the way (a running job's
    progress is the screen's to show): what a Nano node is offered the
    Offline databases screen for — a restore, an owner who loaded one
    before. `sections`: DumpJob.section() of each family."""
    return any(s["loaded"] or s["update"]["running"] for s in sections)


# ============================================================
# The library's folders (library_root.py; LIBRARY-FOLDERS-1)
# ============================================================

@router.get("/library/places")
def get_library_places(under: Optional[str] = Query(None, max_length=4096)) -> Dict[str, Any]:
    """The folder sheet's listing: the places an added folder can start at,
    or the folders of `under` and where it stands against the library. Only
    what stays under a root is ever listed; every name in the answer is a
    stranger's text."""
    import library_root
    try:
        return library_root.places(under)
    except library_root.Refused as e:
        raise HTTPException(status_code=400 if library_root.picks() else 409, detail=str(e))


class LibraryFolderAdd(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


@router.post("/library/folders")
def add_library_folder(req: LibraryFolderAdd, request: Request) -> Dict[str, Any]:
    """"Add this folder" (the sheet; a new disk's Add; the launcher's
    window): stored, its watch armed, and the folder scanned for what is
    there — the rest of the library is not walked. Folders it takes in go
    with their watches. On a launcher the folder is any the OS gives, so
    only the launcher's own call adds one — the person at the machine, the
    OS's dialog — never a page's (LIBRARY-FOLDERS-1 Q5)."""
    import library_root
    from main import request_scan
    from scanner import drop_look
    if library_root.kind() == "launcher" and getattr(request.state, "signer", None) != "host":
        raise HTTPException(status_code=403, detail="Folders are added in the Sautium window on this computer.")
    try:
        added = library_root.add(req.path)
    except library_root.Refused as e:
        raise HTTPException(status_code=409 if library_root.kind() == "setup" else 400, detail=str(e))
    for path in added["merged"]:
        library_watch.forget(path)
        drop_look(path)
    path = added["folder"]["path"]
    library_watch.add(path)
    library_root.offer_again()          # a disk added is no longer offered
    # Where the mount delivers no events the watch queues nothing: asked here
    request_scan({path: True}, explicit=True)
    db_execute("NOTIFY sautium_notices")
    notify_library_subscribers()
    return {"folder": {**added["folder"], "name": library_root.folder_name(added["folder"])},
            "merged": added["merged"]}


@router.post("/library/folders/{folder_id}/forget")
async def forget_library_folder(folder_id: int) -> Dict[str, Any]:
    """"Forget this disk (folder) and its albums": the plan of what it alone
    holds, asked as every removal is (scan.prune_plan, kind "folder")."""
    from main import scan_forget
    return await scan_forget(folder_id)


@router.post("/library/elsewhere/forget")
async def forget_elsewhere() -> Dict[str, Any]:
    """The copies under no folder of the library ("Elsewhere"): the same
    plan and question."""
    from main import scan_forget
    return await scan_forget(None)


class PhantomPrefs(BaseModel):
    enabled:        Optional[bool] = None
    radio_streamed: Optional[bool] = None


@router.get("/phantoms")
def get_phantom_state() -> Dict[str, Any]:
    """Read for the Streaming library screen. Its own endpoint rather than a
    field on /library: the counts cost ~0.4 s and the library screen wakes on
    every scan/enrich progress tick, which would pay for them a hundred times
    over for blocks that no longer live there."""
    return _phantom_section()


@router.put("/phantoms")
def put_phantom_prefs(req: PhantomPrefs) -> Dict[str, Any]:
    if req.radio_streamed is not None:
        # Read at each radio batch: a station playing takes it at its next fill
        _write("radio.streamed", bool(req.radio_streamed))
    if req.enabled is None:
        return _phantom_section()
    _write("discovery.phantom_layer", bool(req.enabled))
    # Streaming follows the library: its providers, yt-dlp's build and the
    # refresh loop start and stop here
    from config import settings as app_settings
    from streaming import service as streaming_service
    streaming_service.follow_switch(app_settings)
    if req.enabled:
        # Imported scrobbles waiting on names only the phantom layer can place.
        from canon import scrobbles
        scrobbles.wake()
    return _phantom_section()


@router.post("/phantoms/prune")
def phantom_prune() -> Dict[str, Any]:
    """Remove the phantom album/track layer in the background — the owner's
    explicit, confirmed action; nothing else ever starts it."""
    with _phantom_lock:
        if _phantom_state["running"]:
            raise HTTPException(status_code=409,
                                detail="Phantom layer removal already running")
        _phantom_state.update(running=True, cancel_requested=False,
                              progress="Starting…", pct=None, error=None)
    threading.Thread(target=_phantom_worker, daemon=True,
                     name="phantom-prune").start()
    notify_library_subscribers()
    return {"success": True}


@router.post("/phantoms/prune/cancel")
def phantom_prune_cancel() -> Dict[str, Any]:
    with _phantom_lock:
        if not _phantom_state["running"]:
            raise HTTPException(status_code=409,
                                detail="No phantom layer removal running")
        _phantom_state["cancel_requested"] = True
    notify_library_subscribers()
    return {"success": True}


# ============================================================
# Library actions (thin passthroughs for the Settings buttons)
# ============================================================

@router.post("/library/scan")
async def trigger_scan(prune: bool = False) -> Dict[str, Any]:
    """Start a library scan from the Settings screen.

    Delegates to the existing /scan/start endpoint logic so we don't
    duplicate the scan-worker setup. `prune=true` (Rescan) also
    looks for DB rows whose files have disappeared from the music
    folder and ends with a plan of what it would remove — the
    removal waits for the answer below (the default is add-only)."""
    from main import scan_start
    return await scan_start(prune=prune)


@router.post("/library/away/remove")
async def remove_away() -> Dict[str, Any]:
    """Plan the removal of the copies that are away; the Library asks it."""
    from main import scan_away
    return await scan_away()


@router.post("/library/scan/prune")
async def answer_prune(plan_id: str, remove: bool) -> Dict[str, Any]:
    """The owner's answer to a Rescan's plan of removals."""
    from main import scan_prune
    return await scan_prune(plan_id=plan_id, remove=remove)


@router.post("/library/scan/cancel")
async def cancel_scan() -> Dict[str, Any]:
    from main import scan_cancel
    return await scan_cancel()


class InstrumentsReq(BaseModel):
    enabled: bool


@router.put("/library/instruments")
async def set_instruments(req: InstrumentsReq) -> Dict[str, Any]:
    """The owner's "Recognise instruments" switch. On, the analysis run's
    instrument pass follows its sonic pass and streams are tagged as they
    are analysed; off, neither, and nothing stored is removed."""
    import hardware_profile
    if not hardware_profile.resolve().ml_available:
        raise HTTPException(status_code=400, detail="No ML runtime on this node")
    _write("enrichment.instruments", req.enabled)
    notify_library_subscribers()
    return {"success": True, "enabled": instruments_on()}


@router.post("/library/enrich")
async def trigger_enrich() -> Dict[str, Any]:
    """Start enrichment for items missing bios / audio analysis."""
    from main import enrich_start
    # default args; the existing endpoint sets sensible options
    return await enrich_start()  # type: ignore[call-arg]


@router.post("/library/enrich/cancel")
async def cancel_enrich() -> Dict[str, Any]:
    from main import enrich_cancel
    return await enrich_cancel()


@router.post("/library/hqp-sync")
def trigger_hqp_sync(force: bool = False, confirm: bool = False,
                     endpoint_id: Optional[int] = None) -> Dict[str, Any]:
    """Bring an HQPlayer's library into the catalogue (hqp_library.sync):
    files known here are touched, new ones imported, nothing removed.
    `force` ignores the stored library hash; `endpoint_id` names a library
    listed in Settings instead of the configured endpoint.

    The FIRST import of an endpoint is answered with a preview — which
    HQPlayer, how many files it lists, how many are new here — until the
    caller repeats the request with `confirm`. An HQPlayer on this machine
    is refused outright: it reads this node's own library, and importing
    that brought every album in as a copy once (2026-09-27)."""
    import hqp_library
    host, port = _hqp_target(endpoint_id)
    if not confirm:
        if not hqp_library.has_own_library(host):
            ep = hqp_library.endpoint_by_address(host, port) or {}
            return {"success": False, "refused": "here", "endpoint": f"{host}:{port}",
                    "name": hqp_library.label(ep.get("name"), ep.get("product"))}
        try:
            ep, how, facts = hqp_library.resolve_endpoint(host, port)
        except OSError as e:
            raise HTTPException(status_code=503, detail=f"HQPlayer at {host}:{port} is not answering: {e}")
        if ep is None or ep["library_hash"] is None:
            # a library never imported: the decision names the HQPlayer and
            # the size of what it would bring (its own files excluded)
            entries, counts = hqp_library.parse_library(hqp_library.fetch_library(host, port))
            known = hqp_library._known_paths(ep["id"]) if ep else {}
            return {"success": False, "preview": {
                "endpoint": f"{host}:{port}", "name": facts["hqp_name"] or host,
                "product": facts["product"], "files": counts["files"],
                "unsupported": counts["unsupported"], "known": len(known),
                "new": sum(1 for _, md in entries if md["file_path"] not in known)}}
    if not hqp_library.start_job(host, port, force=force):
        raise HTTPException(status_code=409, detail="An HQPlayer library job is already running")
    return {"success": True}


@router.post("/library/hqp-rescan")
def trigger_hqp_rescan(endpoint_id: Optional[int] = None) -> Dict[str, Any]:
    """The explicit, confirmed rescan: files the HQPlayer library no longer
    lists are forgotten (hqp_library.forget_missing)."""
    import hqp_library
    host, port = _hqp_target(endpoint_id)
    if not hqp_library.start_job(host, port, rescan=True):
        raise HTTPException(status_code=409, detail="An HQPlayer library job is already running")
    return {"success": True}


@router.post("/library/hqp-sync/cancel")
def cancel_hqp_sync() -> Dict[str, Any]:
    import hqp_library
    return {"success": hqp_library.cancel_job()}


@router.post("/library/hqp-forget")
def forget_hqp_library(endpoint_id: int) -> Dict[str, Any]:
    """The explicit, confirmed goodbye to one HQPlayer's library — a
    streamer that went home, a box retired: everything its library alone
    put here goes (hqp_library.forget_endpoint_id), and with its row the
    DSP load measured on it (playback.hqp_load — what that box keeps up
    with); the analysis and the listens of its tracks stay as they do for a
    vanished local rip. Never in the middle of a benchmark run on it."""
    import hqp_library
    from playback import hqp_benchmark
    if hqp_library.job_state()["running"]:
        raise HTTPException(status_code=409, detail="An HQPlayer library job is already running")
    job = hqp_benchmark.state()
    if job["running"] and job["endpoint_id"] == endpoint_id:
        raise HTTPException(status_code=409, detail="The benchmark is measuring this HQPlayer — "
                                                    "cancel it first")
    from playback import hqp_ab
    sitting = hqp_ab.current()          # read once: a close between two reads leaves None
    if sitting is not None and hqp_ab.running() and sitting.ep["id"] == endpoint_id:
        raise HTTPException(status_code=409, detail="A comparison by ear is going on with this "
                                                    "HQPlayer — stop it first")
    if not db_query_one("SELECT 1 AS ok FROM hqp_endpoints WHERE id = %(e)s", {"e": endpoint_id}):
        raise HTTPException(status_code=404, detail="No such HQPlayer library")
    driven = hqp_library.driven_endpoint()
    stats = hqp_library.forget_endpoint_id(endpoint_id)
    if driven is not None and driven["id"] == endpoint_id:
        # The HQPlayer the node drives stays the owner's pick without its
        # library: registered afresh — no library, nothing measured — and
        # the attached backend looks its row up again. Left unregistered,
        # the benchmark refused it until the Output picker was opened
        # (2026-10-03).
        hqp_library.request_sync(*hqp_library.driven())
        from playback.manager import manager
        if manager.active is not None and manager.active.id == "hqplayer":
            manager.active.endpoint_forgotten()
    notify_library_subscribers()
    return {"success": True, **stats}
