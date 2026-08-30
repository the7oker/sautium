"""
HQPlayer connection layer: the two client singletons (command + status),
reconnect + circuit-breaker logic, and the resilient playlist-add
primitives every HQPlayer queue write goes through.

Two independent HQPlayer connections so the background status poller and
user-initiated commands never contend for the same socket / lock:

  * `_hqp_status_client` + `_hqp_status_lock`
      Owned exclusively by the status poller. Fast socket timeout so a
      lagging HQPlayer is detected quickly without freezing the rest of
      the request flow.

  * `_hqp_client` + `_hqp_lock`
      Owned by all command endpoints (play / pause / next / play-track /
      /api/player/playlist etc). 5 s timeout for write operations that
      may take longer to acknowledge (e.g. play-album loads many URIs).

HQPlayer's control API accepts multiple concurrent TCP clients, so the
two sockets co-exist cleanly on the HQP side.
"""

import logging
import threading
import time
from typing import Optional

from config import settings
from hqplayer_client import HQPlayerClient, PlaybackState, file_path_to_uri

from playback import queue as queue_mod
from playback.base import Capabilities, PlaybackStatus, PlayerBackend, ReorderPlan
from playback.queue import CanonicalQueue, QueueItem

logger = logging.getLogger(__name__)

_hqp_client: Optional[HQPlayerClient] = None
_hqp_lock = threading.Lock()  # cmd commands

_hqp_status_client: Optional[HQPlayerClient] = None
_hqp_status_lock = threading.Lock()  # status poller


def _make_client(timeout: float) -> HQPlayerClient:
    return HQPlayerClient(
        host=settings.hqplayer_host,
        port=settings.hqplayer_port,
        timeout=timeout,
    )


# Circuit breaker for a stalled HQPlayer control port. A control-port stall
# (HQPlayer accepts the TCP connect but never replies, or stops accepting
# connects) makes every reconnect block for the full socket timeout. Without
# this, one play action fans out into rotate + stop + add × retries = tens of
# seconds of stacked connect timeouts while holding _hqp_lock, which also
# blocks every other request queued behind that lock. After a failed connect
# we "open" the breaker for a short cooldown: further reconnects fail fast
# instead of stacking timeouts. The next successful connect (e.g. the status
# poller once HQPlayer answers again) closes it.
_hqp_unreachable_until: float = 0.0
HQP_CIRCUIT_COOLDOWN = 6.0


def _ensure_connected(client: Optional[HQPlayerClient], timeout: float, label: str
                      ) -> HQPlayerClient:
    """Return a healthy HQPlayer client; reconnect if the cached one is stale.

    Caller must hold the appropriate lock. `client` is the previous instance
    (may be None). Returns the (possibly new) instance. Raises ConnectionError
    if the connection cannot be established.
    """
    need_reconnect = client is None or not client.is_connected()

    # Detect remote-side close by peeking the socket.
    if not need_reconnect and client and client.socket:
        import select
        try:
            ready = select.select([client.socket], [], [], 0)
            if ready[0]:
                peek = client.socket.recv(1, 0x02)  # MSG_PEEK
                if not peek:
                    logger.info(f"HQPlayer ({label}) closed by remote, reconnecting...")
                    need_reconnect = True
        except Exception:
            need_reconnect = True

    if need_reconnect:
        global _hqp_unreachable_until
        # Breaker open — fail fast rather than eat another connect timeout.
        if time.monotonic() < _hqp_unreachable_until:
            raise ConnectionError("HQPlayer unreachable (circuit open)")
        if client:
            try:
                client.disconnect()
            except Exception:
                pass
        client = _make_client(timeout=timeout)
        if not client.connect():
            _hqp_unreachable_until = time.monotonic() + HQP_CIRCUIT_COOLDOWN
            raise ConnectionError(
                f"Cannot connect to HQPlayer at {settings.hqplayer_host}:{settings.hqplayer_port}"
            )
        _hqp_unreachable_until = 0.0  # connected — close the breaker
        logger.info(f"HQPlayer ({label}) connected")
    return client


def _get_hqp() -> HQPlayerClient:
    """Get or create HQPlayer command client. Must be called inside _hqp_lock."""
    global _hqp_client
    _hqp_client = _ensure_connected(_hqp_client, timeout=5.0, label="cmd")
    return _hqp_client


def _get_hqp_status() -> HQPlayerClient:
    """Get or create HQPlayer status-poller client. Must be called inside _hqp_status_lock."""
    global _hqp_status_client
    # 4s, not 2s: HQPlayer's control thread can lag a few seconds behind a
    # <Status/> while busy rendering. A tight timeout turns a slow-but-alive
    # reply into a needless disconnect + reconnect churn cycle.
    _hqp_status_client = _ensure_connected(_hqp_status_client, timeout=4.0, label="status")
    return _hqp_status_client


def _reset_hqp():
    """Force-close HQPlayer command client so next _get_hqp() reconnects."""
    global _hqp_client
    if _hqp_client:
        try:
            _hqp_client.disconnect()
        except Exception:
            pass
        _hqp_client = None


def _reset_hqp_status():
    """Force-close HQPlayer status client so next _get_hqp_status() reconnects."""
    global _hqp_status_client
    if _hqp_status_client:
        try:
            _hqp_status_client.disconnect()
        except Exception:
            pass
        _hqp_status_client = None


def reset_all_clients() -> None:
    """Public entry point — called by /api/settings/hqplayer after the
    host or port changes so the next status/command call reconnects
    against the new address instead of holding onto the old socket."""
    with _hqp_lock:
        _reset_hqp()
    with _hqp_status_lock:
        _reset_hqp_status()


def _hqp_cmd(func):
    """Execute a function with HQPlayer client under lock. Auto-reconnects on broken pipe."""
    with _hqp_lock:
        try:
            hqp = _get_hqp()
            return func(hqp)
        except (BrokenPipeError, ConnectionError, OSError) as e:
            logger.warning(f"HQPlayer connection lost ({e}), reconnecting...")
            _reset_hqp()
            hqp = _get_hqp()
            return func(hqp)


def _uri_in_playlist(uri: str) -> bool:
    """Best-effort: is `uri` already in HQPlayer's playlist? Used to avoid
    re-adding (duplicating) a track whose add LANDED but whose response was lost
    to a slow read-timeout. Returns False if the playlist can't be read. Call
    while holding `_hqp_lock`."""
    try:
        return any(t.get("uri") == uri for t in (_get_hqp().get_playlist() or []))
    except Exception:
        return False


def _add_uris_with_retry(uris: list[str], *, clear_first: bool = False) -> int:
    """Append URIs to the HQPlayer playlist, surviving a mid-batch
    connection drop. MUST be called while holding `_hqp_lock`.

    `playlist_add` returns False (it does not raise) when the control
    socket is down, so a plain `for` loop silently drops tracks while the
    endpoint still reports success — exactly the failure that made a Queue
    action add only the one track that landed before HQPlayer stalled.
    Here every add is verified: on a falsey result or a dropped socket we
    reset the connection and retry that one URI once. Returns the count
    actually added so the caller can surface a short count instead of a
    fake 'ok'.

    `clear_first=True` issues clear=True on the first URI (replace-queue
    semantics); the rest append. The clear only ever fires on i == 0, so a
    mid-batch reconnect never re-clears already-added tracks.
    """
    added = 0
    for i, uri in enumerate(uris):
        clear = clear_first and i == 0
        ok = False
        for attempt in (1, 2):
            try:
                ok = _get_hqp().playlist_add(uri, clear=clear)
            except (BrokenPipeError, ConnectionError, OSError):
                ok = False
            if ok:
                break
            if attempt == 1:
                # The add may have LANDED but its response was lost (slow
                # HQPlayer read-timeout); re-adding an append would DUPLICATE the
                # track. Verify first — preview URIs are unique, so a present URI
                # means the first add took.
                if not clear and _uri_in_playlist(uri):
                    ok = True
                    break
                _reset_hqp()  # force a fresh socket before the single retry
        if ok:
            added += 1
        else:
            logger.warning(f"playlist_add failed after reconnect: {uri}")
    return added


def _hqp_safe(action) -> None:
    """Run one HQPlayer command (stop / play / clear / select_track)
    tolerantly inside an existing `_hqp_lock`: one reconnect-and-retry,
    never raises. Frames a resilient multi-add so a churning control port
    can't abort the whole operation at its stop()/play() bookends before
    the add even runs."""
    for attempt in (1, 2):
        try:
            action(_get_hqp())
            return
        except (BrokenPipeError, ConnectionError, OSError):
            if attempt == 1:
                _reset_hqp()


def _skip_to_first_playable(added: int) -> None:
    """If the first queued track didn't start (e.g. a [Vinyl] placeholder path that
    isn't real audio), skip ahead until one plays. Best-effort; holds _hqp_lock."""
    try:
        hqp = _get_hqp()
        time.sleep(0.5)
        status = hqp.get_status()
        if status and status.state == PlaybackState.STOPPED and added > 1:
            logger.warning("play: first track didn't start, skipping ahead")
            for skip_idx in range(2, min(added + 1, 6)):
                hqp.select_track(skip_idx)
                hqp.play()
                time.sleep(0.5)
                status = hqp.get_status()
                if status and status.state != PlaybackState.STOPPED:
                    logger.info(f"play: track {skip_idx} started")
                    break
    except (BrokenPipeError, ConnectionError, OSError):
        pass


_local_provider = None


def _owned_play_uri(item: "QueueItem") -> str:
    """HQPlayer URI for an owned track. Native file:// for formats HQPlayer decodes;
    for an m4a (which it can't — MP4 container, AAC or ALAC) transcode to FLAC in
    memory and return the media-proxy http URL so it plays anyway, displayed as
    owned. Falls back to file:// if streaming/proxy is off or the transcode fails.
    HQPlayer-only workaround: engine-rendered backends decode m4a natively.

    A CUE slice always rides the media proxy as a cached FLAC cut — the raw
    path would play the whole disc image. The only fallback is that whole
    image, loudly logged; there is no silent path."""
    global _local_provider
    src = item.source
    media_file_id, db_path, file_format = (
        item.media_file_id, src["path"], src.get("format"))

    if src.get("cue_start") is not None:
        from streaming import service as streaming_service
        from streaming import transcode
        container_path = settings.translate_to_local_path(db_path)
        tags = {"title": item.title, "artist": item.artist,
                "album": item.album, "track": item.track_number}
        try:
            proxy = streaming_service.ensure_proxy()
            # Produce EAGERLY — _uri_for runs OUTSIDE _hqp_lock by contract,
            # and HQPlayer probes the URI synchronously at ADD time: the cut
            # must exist before the command socket ever sees the URL.
            transcode.flac_slice_path_for_file(
                container_path, src["cue_start"], src.get("cue_end"), tags)
        except Exception as e:
            logger.error("cue slice unavailable for %s [%s, %s) — playing the "
                         "whole image: %s", db_path, src.get("cue_start"),
                         src.get("cue_end"), e)
            return file_path_to_uri(db_path)
        tok = proxy.register_file(container_path, transcode.FLAC_MIME,
                                  start=src["cue_start"], end=src.get("cue_end"),
                                  tags=tags)
        return proxy.file_url(tok)

    from streaming.local import TRANSCODE_FORMATS
    if (file_format or "").upper() not in TRANSCODE_FORMATS:
        return file_path_to_uri(db_path)

    from streaming import service as streaming_service
    proxy = streaming_service.get_proxy() if streaming_service.is_enabled() else None
    if proxy is None:
        return file_path_to_uri(db_path)

    if _local_provider is None:
        from streaming.local import LocalTranscodeProvider
        _local_provider = LocalTranscodeProvider()
    from streaming.base import TrackQuery
    container_path = settings.translate_to_local_path(db_path)
    # artist/title are required but unused for display here — the playlist payload
    # renders this entry from media_file_id (a real owned row), not the query.
    q = TrackQuery(artist="", title="", media_file_id=media_file_id)
    tokens = proxy.add_tracks([(q, [(_local_provider, container_path)])])
    try:
        # front: a local transcode the mirror is waiting on — it must not
        # queue behind a backlog of network phantom fetches.
        e = proxy.wait_ready(tokens[0], front=True)
    except (TimeoutError, KeyError):
        e = None
    if e is None or e.audio is None:
        return file_path_to_uri(db_path)
    return proxy.url_for(tokens[0])


# -- The HQPlayer output backend ------------------------------------------------

STATE_NAMES = {
    PlaybackState.STOPPED: "stopped",
    PlaybackState.PAUSED: "paused",
    PlaybackState.PLAYING: "playing",
    PlaybackState.STOPREQ: "stopping",
}

# A single status read can stall past the socket timeout — the WSL2→Windows
# hop to HQPlayer routinely does, and container-network blips (DHT announce
# windows) stall it for tens of seconds while the AUDIO stream itself rides
# through on its long-lived connection. Blanking the status tears down the
# whole Now Playing UI mid-music, so the last-known-good status keeps being
# served until this many polls fail in a row (~30 s with the 2,4,8,15 s
# backoff), then "disconnected" is emitted.
STATUS_FAILURE_THRESHOLD = 5

# Every N poll ticks: read HQPlayer's playlist and compare against the
# canonical queue (drift canary). One-way mirror per §2.6 — external edits
# in HQPlayer's own GUI are logged, never reconciled.
DRIFT_CHECK_EVERY = 30


class HqpBackend(PlayerBackend):
    """HQPlayer output. Status acquisition = 1 s poll on a dedicated socket —
    the control protocol cannot push; documented boundary exception to the
    no-polling rule (HARDWARE-TIERS §2.6). The canonical queue is mirrored
    one-way INTO HQPlayer's native playlist via the queue_* hooks."""

    id = "hqplayer"
    label = "HQPlayer"

    def __init__(self, emit, queue: CanonicalQueue):
        super().__init__(emit)
        self._queue = queue
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._failures = 0
        self._drift_logged = False
        # Slot to select on the first play after an output switch (resume_at).
        self._resume_index: Optional[int] = None

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        adopted = self._adopt_hqp_playlist()
        if not adopted and len(self._queue):
            # Output switch with a live queue (§2.6): HQPlayer becomes the
            # mirror of the canonical queue — replace, no autoplay, the
            # user presses play. Without this the switch left HQPlayer's
            # playlist empty and the play button dead. Skipped only while
            # HQPlayer is actively PLAYING/PAUSED (a backend restart
            # mid-listening — clearing would cut live audio); a stopped
            # HQPlayer gets the replace even if it holds a stale playlist.
            try:
                with _hqp_status_lock:
                    st = _get_hqp_status().get_status()
                hqp_busy = st is not None and st.state in (
                    PlaybackState.PLAYING, PlaybackState.PAUSED)
            except Exception:
                hqp_busy = False
            if not hqp_busy:
                try:
                    added = self.queue_replace(self._queue.snapshot(), play=False)
                    logger.info("mirrored %d canonical tracks into HQPlayer "
                                "on attach", added)
                except Exception as e:
                    logger.warning("canonical mirror on attach failed: %s", e)
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True,
                                        name="hqp-status-poller")
        self._thread.start()
        logger.info("HQPlayer status poller started")

    def shutdown(self) -> None:
        self._running = False
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=3)
        logger.info("HQPlayer status poller stopped")

    def capabilities(self) -> Capabilities:
        return Capabilities(volume=True, volume_kind="db", seek=True, gapless=True)

    def resume_at(self, index: int) -> None:
        # HQPlayer owns its playlist pointer, and a select fired straight
        # after the attach mirror does not stick (verified live) — defer it
        # to the play press, where select+play is the proven jump sequence.
        self._resume_index = index

    def poke(self) -> None:
        """Wake the poller for an immediate re-poll after a command."""
        self._wake.set()

    # -- restart resilience -------------------------------------------------------

    def _adopt_hqp_playlist(self) -> None:
        """One-time bootstrap on attach: when OUR queue is empty but HQPlayer
        still holds a playlist (backend restarted mid-listening — a normal
        operation), import it so Now Playing and play tracking resume. This
        is NOT bidirectional sync; after adoption the canonical queue is
        authoritative again."""
        if len(self._queue):
            return
        try:
            with _hqp_status_lock:
                hqp_tracks = _get_hqp_status().get_playlist()
        except Exception as e:
            logger.info("adopt: HQPlayer playlist unavailable (%s)", e)
            return
        if not hqp_tracks:
            return
        items = _items_from_hqp_tracks(hqp_tracks)
        if items:
            self._queue.replace(items)
            logger.info("adopted %d tracks from the running HQPlayer playlist",
                        len(items))
            return True
        return False

    # -- status poll loop ------------------------------------------------------------

    def _poll_loop(self) -> None:
        tick = 0
        while self._running:
            try:
                hqp_playlist = None
                with _hqp_status_lock:
                    try:
                        hqp = _get_hqp_status()
                        status = hqp.get_status()
                    except (BrokenPipeError, ConnectionError, OSError):
                        _reset_hqp_status()
                        hqp = _get_hqp_status()
                        status = hqp.get_status()
                    if status is not None and tick % DRIFT_CHECK_EVERY == 0:
                        try:
                            hqp_playlist = hqp.get_playlist()
                        except (BrokenPipeError, ConnectionError, OSError) as e:
                            logger.debug("drift check playlist read failed: %s", e)

                if status is None:
                    # Transient read miss (e.g. HQPlayer stalled past the
                    # socket timeout). Keep serving the last good status.
                    self._register_failure()
                else:
                    self._failures = 0
                    self._emit(PlaybackStatus(
                        state=STATE_NAMES.get(status.state, "unknown"),
                        position=status.position,
                        length=status.length,
                        queue_index=status.track_index,
                        volume=status.volume,
                        extra={
                            "artist": status.artist,
                            "album": status.album,
                            "song": status.song,
                            "genre": status.genre,
                            "process_speed": status.process_speed,
                        },
                    ))
                    if hqp_playlist is not None:
                        self._check_drift(hqp_playlist)
            except Exception:
                self._register_failure()

            # Back off when HQPlayer stops answering. It accepts the TCP
            # connect but doesn't reply to <Status/> (control thread busy
            # under DSP load), so each retry is a connect->read-timeout->
            # disconnect churn cycle that only piles load onto an already-
            # struggling control port. Exponential backoff (2,4,8..15s) lets
            # it recover; a successful poll resets to 1s. Capped at 15 s —
            # the WSL2→Windows hop flaps for seconds at a time, and a 30 s
            # cap kept the UI on "disconnected" long after HQP was back.
            if self._failures > 0:
                poll_interval = min(2.0 ** self._failures, 15.0)
            else:
                poll_interval = 1.0
            self._wake.wait(timeout=poll_interval)
            self._wake.clear()
            tick += 1

    def _register_failure(self) -> None:
        """Tolerate a short burst of misses; emit 'disconnected' only past
        the threshold (the manager dedupes repeats)."""
        self._failures += 1
        if self._failures >= STATUS_FAILURE_THRESHOLD:
            self._emit(PlaybackStatus(state="disconnected"))

    def _check_drift(self, hqp_tracks: list) -> None:
        """Compare HQPlayer's playlist against the canonical queue. Proxy-
        and transcode-backed slots are skipped (their tokens are re-minted
        across restarts); plain file slots must resolve to the same db path
        (compared through `_uri_db_path` — HQPlayer percent-escapes brackets
        in the URIs it returns, so raw URI equality would false-positive)."""
        snapshot = self._queue.snapshot()
        drift = len(hqp_tracks) != len(snapshot)
        if not drift:
            from streaming.local import TRANSCODE_FORMATS
            for t, it in zip(hqp_tracks, snapshot):
                if (it.source["kind"] != "file"
                        or it.source.get("cue_start") is not None
                        or (it.source.get("format") or "").upper() in TRANSCODE_FORMATS):
                    continue
                uri = t.get("uri") or ""
                if (not uri.startswith("file://")
                        or _uri_db_path(uri) != it.source["path"]):
                    drift = True
                    break
        if drift and not self._drift_logged:
            logger.warning(
                "HQPlayer playlist drifted from the canonical queue "
                "(external edit?) — the Sautium queue is authoritative")
            self._drift_logged = True
        elif not drift:
            self._drift_logged = False

    # -- transport -----------------------------------------------------------------

    def play(self) -> bool:
        if self._resume_index is not None:
            idx, self._resume_index = self._resume_index, None
            logger.info("play: honoring resume slot %d", idx)
            if self.select(idx):
                # A SelectTrack on a STOPPED HQPlayer needs a beat to
                # register before Play honors it — the resume-watcher's
                # documented 1s boundary exception (04c46b8), same quirk.
                time.sleep(1.0)
        ok = _hqp_cmd(lambda h: h.play())
        self.poke()
        return ok

    def pause(self) -> bool:
        ok = _hqp_cmd(lambda h: h.pause())
        self.poke()
        return ok

    def stop(self) -> bool:
        ok = _hqp_cmd(lambda h: h.stop())
        self.poke()
        return ok

    def next(self) -> bool:
        self._resume_index = None    # explicit navigation overrides resume
        ok = _hqp_cmd(lambda h: h.next())
        self.poke()
        return ok

    def previous(self) -> bool:
        self._resume_index = None    # explicit navigation overrides resume
        ok = _hqp_cmd(lambda h: h.previous())
        self.poke()
        return ok

    def select(self, index: int) -> bool:
        """Load AND play the slot (PlayerBackend contract): HQPlayer's
        SelectTrack alone leaves a STOPPED player stopped, so the Play that
        jump() used to send from the manager follows here, same order."""
        self._resume_index = None    # explicit navigation overrides resume
        ok = _hqp_cmd(lambda h: h.select_track(index))
        if ok:
            ok = _hqp_cmd(lambda h: h.play())
        self.poke()
        return ok

    def seek(self, seconds: int) -> bool:
        ok = _hqp_cmd(lambda h: h.seek(int(seconds)))
        self.poke()
        return ok

    def set_volume(self, level: float) -> bool:
        ok = _hqp_cmd(lambda h: h.set_volume(level))
        self.poke()
        return ok

    def volume_up(self) -> bool:
        ok = _hqp_cmd(lambda h: h.volume_up())
        self.poke()
        return ok

    def volume_down(self) -> bool:
        ok = _hqp_cmd(lambda h: h.volume_down())
        self.poke()
        return ok

    # -- canonical-queue mirror -------------------------------------------------------

    def _uri_for(self, item: QueueItem) -> str:
        """Playable HQPlayer URI for a queue item. May transcode (m4a) —
        call OUTSIDE `_hqp_lock`; a slow transcode must never hold the
        command socket hostage."""
        src = item.source
        if src["kind"] == "file":
            return _owned_play_uri(item)
        if src["kind"] == "proxy":
            from streaming import service as streaming_service
            return streaming_service.get_proxy().url_for(src["token"])
        return src["uri"]

    def queue_replace(self, items: list, *, play: bool, probe_first: bool = False) -> int:
        uris = [self._uri_for(it) for it in items]
        with _hqp_lock:
            _hqp_safe(lambda h: h.stop())
            added = _add_uris_with_retry(uris, clear_first=True)
            if added and play:
                _hqp_safe(lambda h: h.play())
                if probe_first:
                    _skip_to_first_playable(added)
        self.poke()
        return added

    def queue_append(self, items: list) -> int:
        uris = [self._uri_for(it) for it in items]
        with _hqp_lock:
            added = _add_uris_with_retry(uris, clear_first=False)
        self.poke()
        return added

    def queue_insert_next(self, items: list, anchor_index: Optional[int]) -> int:
        """Insert right after the playing slot via the seamless remove-after /
        re-append trick — the reading slot is never touched, so audio plays
        through. URI-based so it works for preview streams too."""
        uris = [self._uri_for(it) for it in items]
        with _hqp_lock:
            try:
                raw = _get_hqp().get_playlist() or []
            except Exception:
                raw = []
            idx = anchor_index or 0
            if idx < 1 or idx > len(raw):
                added = _add_uris_with_retry(uris, clear_first=False)
            else:
                # Remove the after-segment (always slot idx+1, which the rest
                # shift down into), then re-append it behind the new tracks.
                after = [t.get("uri") for t in raw[idx:] if t.get("uri")]
                for _ in range(len(after)):
                    _hqp_safe(lambda h: h.playlist_remove(idx + 1))
                added = _add_uris_with_retry(uris, clear_first=False)
                if after:
                    _add_uris_with_retry(after, clear_first=False)
        self.poke()
        return added

    def queue_remove(self, index: int) -> bool:
        ok = _hqp_cmd(lambda h: h.playlist_remove(index))
        self.poke()
        return ok

    def queue_clear_after_current(self) -> bool:
        # PlaylistClear keeps the reading slot intact — it erases everything
        # queued around the current track while the seed plays on.
        with _hqp_lock:
            _hqp_safe(lambda h: h.playlist_clear())
        self.poke()
        return True

    def queue_reorder(self, plan: ReorderPlan) -> dict:
        """Execute a validated reorder plan against HQPlayer's append/remove
        primitives (no insert/move in the protocol — see the /reorder
        endpoint docstring for the seamless-feasibility rules)."""
        if plan.seamless:
            # Sequence (current at slot K = status_idx throughout, then
            # shifts left as we shrink the before-segment in step 3):
            #
            # 1. Empty the after-segment by repeatedly removing slot K+1.
            #    K never changes: every remove targets a slot above K.
            # 2. Append new_after in target order. All appends land at the
            #    very end, after the current slot, so K stays put.
            # 3. Walk old_before slot by slot. Tracks present in new_before
            #    (matched in subsequence order) stay; the rest are removed
            #    by remove(slot). When the last "drop" track is removed, K
            #    has shifted down by exactly (len(old_before) -
            #    len(new_before)), landing on new_status_idx as required.
            # URIs come from _uri_for (outside the lock — it may transcode):
            # a raw file_path_to_uri would hand HQPlayer the whole disc image
            # for a CUE slice and an unplayable path for an m4a.
            uri_by_id = {mid: self._uri_for(plan.items_by_id[mid])
                         for mid in plan.new_after}
            with _hqp_lock:
                hqp = _get_hqp()
                for _ in range(len(plan.old_after)):
                    hqp.playlist_remove(plan.status_idx + 1)
                for mid in plan.new_after:
                    hqp.playlist_add(uri_by_id[mid])
                cursor = 1
                new_before_remaining = list(plan.new_before)
                for old_track in plan.old_before:
                    if (new_before_remaining
                            and new_before_remaining[0] == old_track):
                        new_before_remaining.pop(0)
                        cursor += 1
                    else:
                        hqp.playlist_remove(cursor)
            self.poke()
            return {
                "removed": len(plan.old_after) + (len(plan.old_before) - len(plan.new_before)),
                "added": len(plan.new_after),
                "interrupted": False,
            }

        # Fallback: clear+rebuild. Required for cases that need an insert
        # (swap inside before, after→before crossing, current right-shift).
        # hqp.stop() is mandatory — HQPlayer ignores PlaylistAdd(clear=True)
        # while playing, silently appending instead, which would double every
        # track and land select_track on the wrong slot.
        uri_by_id = {mid: self._uri_for(plan.items_by_id[mid])
                     for mid in plan.order}
        with _hqp_lock:
            hqp = _get_hqp()
            hqp.stop()
            hqp.playlist_add(uri_by_id[plan.order[0]], clear=True)
            for mid in plan.order[1:]:
                hqp.playlist_add(uri_by_id[mid])
            hqp.select_track(plan.new_status_idx)
            if plan.position > 0:
                hqp.seek(plan.position)
            if plan.resume:
                hqp.play()
        self.poke()
        return {
            "removed": len(plan.order),
            "added": len(plan.order),
            "interrupted": True,
        }


def _uri_db_path(uri: str) -> str:
    """file:// URI → the stored media_files.file_path form (forward slashes,
    HQPlayer's %5B/%5D bracket escapes undone)."""
    p = uri[8:] if uri.startswith("file:///") else uri[7:]
    p = p.replace("\\", "/")
    return p.replace("%5B", "[").replace("%5D", "]")


def _items_from_hqp_tracks(hqp_tracks: list) -> list[QueueItem]:
    """Reverse-map HQPlayer's raw playlist into QueueItems (adopt-on-attach):
    file:// URIs resolve through media_files, proxy URLs through the
    streaming session's metadata, anything else becomes a foreign
    pass-through item with HQPlayer's own labels."""
    from streaming import service as streaming_service

    paths = [_uri_db_path(t["uri"]) for t in hqp_tracks
             if t.get("uri", "").startswith("file://")]
    by_path = queue_mod.items_for_file_paths(paths)

    items: list[QueueItem] = []
    for t in hqp_tracks:
        uri = t.get("uri", "")
        item = None
        if uri.startswith("file://"):
            item = by_path.get(_uri_db_path(uri))
        elif uri.startswith("http://") and "/preview/" in uri:
            token = uri.rsplit("/preview/", 1)[-1].split("?", 1)[0]
            try:
                item = queue_mod.item_for_proxy_token(token)
            except Exception:
                item = None
        if item is None:
            item = QueueItem(
                track_id=None, media_file_id=None,
                source={"kind": "uri", "uri": uri},
                title=t.get("song") or "", artist=t.get("artist") or "")
        items.append(item)
    return items
