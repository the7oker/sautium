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
from hqplayer_client import HQPlayerClient

logger = logging.getLogger(__name__)

_hqp_client: Optional[HQPlayerClient] = None
_hqp_lock = threading.Lock()  # cmd commands

# Bumped every time a NEW playback queue is started (any clear_first add). A
# background phantom-album filler captures it and stops appending the instant the
# user moves to another queue, so a slow album fill never bleeds tracks into an
# unrelated session. Only ever mutated under _hqp_lock.
_playback_generation = 0

_hqp_status_client: Optional[HQPlayerClient] = None
_hqp_status_lock = threading.Lock()  # status poller


def generation() -> int:
    """Current playback generation — background fillers capture it and stop
    appending the moment a new queue supersedes theirs."""
    return _playback_generation


def bump_generation() -> int:
    """Start a new playback generation (retires any running filler). Caller
    must hold `_hqp_lock` — same rule as the implicit bump inside
    `_add_uris_with_retry(clear_first=True)`."""
    global _playback_generation
    _playback_generation += 1
    return _playback_generation


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
    if clear_first:
        # New queue = new playback generation (retires any phantom filler).
        global _playback_generation
        _playback_generation += 1
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
