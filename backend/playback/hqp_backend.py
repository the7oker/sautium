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

Where HQPlayer runs decides how a track reaches it, read off its address
(`_stream_mode`, since 2026-09-28 — nothing to configure): an HQPlayer on
THIS machine (loopback, the Docker host, one of our own addresses) shares
the disks and opens an owned file as a file:// URI at the stored path; one
anywhere else — a Desktop on another computer, an HQPlayer Embedded box on
the LAN — gets every owned file as an http URL on the media proxy, as a
DLNA renderer does, with no path in common with this node. CUE slices, m4a
transcodes and phantom previews are http URLs either way. Media URLs name
the address HQPlayer can reach us at (streaming.media_host), never a fixed
one.
"""

import logging
import re
import socket
import threading
import time
from contextlib import contextmanager, nullcontext
from typing import Optional
from urllib.parse import urlsplit

import psycopg2

from config import settings
from hqplayer_client import (HQPlayerClient, PlaybackState, file_path_to_uri,
                             redact_uri, uri_to_file_path)

from playback import hqp_diagnostics as diag
from playback import hqp_load
from playback import hqp_meter
from playback import queue as queue_mod
from playback.base import Capabilities, PlaybackStatus, PlayerBackend, ReorderPlan
from playback.queue import CanonicalQueue, QueueItem

logger = logging.getLogger(__name__)

_hqp_client: Optional[HQPlayerClient] = None
_hqp_lock = threading.Lock()  # cmd commands

_hqp_status_client: Optional[HQPlayerClient] = None
_hqp_status_lock = threading.Lock()  # status poller

# The attached backend, whose playback trace hears every answer on the command
# client (HqpBackend._on_outcome) — what was actually sent, whoever sent it.
_listener: Optional["HqpBackend"] = None


def _route_outcome(outcome) -> None:
    b = _listener
    if b is not None:
        b._on_outcome(outcome)


def _notify_notices() -> None:
    from db_pool import db_execute
    db_execute("NOTIFY sautium_notices")


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
    _hqp_client.on_outcome = _route_outcome
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


def _uri_in_playlist(uri: str) -> bool:
    """Best-effort: is `uri` already in HQPlayer's playlist? Used to avoid
    re-adding (duplicating) a track whose add LANDED but whose response was lost
    to a slow read-timeout. Returns False if the playlist can't be read. Call
    while holding `_hqp_lock`."""
    try:
        return any(t.get("uri") == uri for t in (_get_hqp().get_playlist() or []))
    except Exception:
        return False


def _poll_interval(failures: int) -> float:
    """Seconds to the next status read: 1 while HQPlayer answers, then 2, 4,
    8, 15. The exponent stops where the cap does: a float power overflows
    past 2**1023, and a night with HQPlayer switched off — a miss every
    ~17 s — got there and killed the poller (OverflowError, 2026-10-04)."""
    return 1.0 if failures <= 0 else min(2.0 ** min(failures, 4), 15.0)


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
    refused = 0         # adds HQPlayer answered, and did not take
    unanswered = False
    gave_up = False
    for i, uri in enumerate(uris):
        clear = clear_first and i == 0
        ok = False
        result, refusal = None, ""
        for attempt in (1, 2):
            silent = False
            try:
                hqp = _get_hqp()
                ok = hqp.playlist_add(uri, clear=clear)
                if not ok:
                    # Read now: the playlist check below replaces it.
                    result, refusal = _refusal(hqp)
                    silent = hqp.timed_out
                    unanswered = silent or not hqp.is_connected()
            except (BrokenPipeError, ConnectionError, OSError) as e:
                ok, unanswered = False, True
                result, refusal = "refused", str(e)
            if ok:
                unanswered = False
                break
            if silent:
                break       # the check and the retry would wait out the same silence
            if attempt == 1:
                # The add may have LANDED but its response was lost (slow
                # HQPlayer read-timeout); re-adding an append would DUPLICATE the
                # track. Verify first — preview URIs are unique, so a present URI
                # means the first add took.
                if not clear and _uri_in_playlist(uri):
                    ok = True
                    break
                _reset_hqp()  # force a fresh socket before the single retry
        diag.note_add(uri, ok, None if ok else result, "" if ok else refusal)
        if ok:
            added += 1
        else:
            logger.warning("playlist_add failed after reconnect: %s — %s",
                           redact_uri(uri), refusal)
            if unanswered:
                # Not a refusal: HQPlayer did not answer, and every add after
                # this one would wait out the same silence — a Pi 5 playing
                # a setting it could not keep up with answered nothing for
                # minutes, and a queue of 19 took five of them to fail.
                gave_up = True
                break
            refused += 1
    # Each reason the batch is short is told: the silence that stopped it,
    # and what HQPlayer refused before it
    if gave_up:
        logger.warning("HQPlayer is not answering — %d of %d tracks were not handed over; "
                       "the queue is mirrored again on the next play",
                       len(uris) - added, len(uris))
    if refused and _stream_mode():
        logger.warning(
            "HQPlayer refused %d of %d media URLs — an HQPlayer elsewhere "
            "fetches them from us, so that usually means it cannot reach %s:%d (a firewall, or the wrong "
            "LAN address in MEDIA_PROXY_ADVERTISED_HOST / SAUTIUM_HOST_IPS)",
            refused, len(uris), hqp_media_host(), settings.media_proxy_port)
    elif refused:
        # HQPlayer refuses a file it cannot open — a dropped music mount
        # looks exactly like this; the notices channel re-derives.
        _notify_notices()
    return added


def _refusal(hqp: HQPlayerClient) -> tuple:
    """(result, reason) of the client's latest refusal, as the ledger keeps it."""
    return (hqp.last_error.result if hqp.last_error else None), hqp.refusal()


def _add_one(hqp: HQPlayerClient, uri: str, *, clear: bool = False) -> bool:
    """A PlaylistAdd whose answer is final as it stands (the rebuild and
    reorder paths do not retry), kept in the hand-over ledger."""
    ok = hqp.playlist_add(uri, clear=clear)
    result, message = (None, "") if ok else _refusal(hqp)
    diag.note_add(uri, ok, result, message)
    return ok


def _hqp_safe(action, command: str) -> bool:
    """Run one HQPlayer command (stop / play / clear / select_track)
    tolerantly inside an existing `_hqp_lock`: one reconnect-and-retry,
    never raises. Frames a resilient multi-add so a churning control port
    can't abort the whole operation at its stop()/play() bookends before
    the add even runs. Returns whether HQPlayer took it: one it did not is
    logged in its words, and a mirror step it did not take leaves its
    playlist no mirror — the caller says so. A command the retry could not
    send either is a step of the play intent it served — it never reached
    HQPlayer, so its client heard nothing."""
    for attempt in (1, 2):
        try:
            hqp = _get_hqp()
            if action(hqp):
                return True
            logger.warning("HQPlayer did not take %s: %s", command,
                           diag.redact_text(hqp.refusal()))
            return False
        except (BrokenPipeError, ConnectionError, OSError) as e:
            if attempt == 1:
                _reset_hqp()
            elif _listener is not None:
                _listener._refused(command, str(e))
    return False


def _slot_key(item: QueueItem) -> tuple:
    """What a slot is, as the hand-over ledger indexes it (a QueueItem is no
    dict key): the track, the file, and the way this output opens it."""
    src = item.opener()
    return (item.track_id, item.media_file_id,
            src.get("path") or src.get("token") or src.get("uri"), src.get("cue_start"))


_TOKEN = re.compile(r"/(?:file|preview)/([^?/#]+)")


def _token_of(uri: str) -> Optional[str]:
    m = _TOKEN.search(uri)
    return m.group(1) if m else None


def _same_entry(a: str, b: str) -> bool:
    """Do two playlist URIs name the same entry — HQPlayer reports a
    file:// URI with its own escapes, a media URL by the host it was given."""
    if a.startswith("file://") and b.startswith("file://"):
        return _uri_file_path(a) == _uri_file_path(b)
    ta, tb = _token_of(a), _token_of(b)
    if ta or tb:
        return ta == tb
    return a == b


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


def _stream_mode() -> bool:
    """Streams, unless HQPlayer runs on this very machine (auth_hmac's
    own-address test): a mounted share is an extra setup on the HQPlayer
    side for bytes the proxy hands over just the same, so it is not a mode."""
    from auth_hmac import is_own_address
    return not is_own_address(settings.hqplayer_host)


def hqp_media_host() -> str:
    """The address HQPlayer is handed inside media URLs — picked for the
    machine it runs on (streaming.media_host), never a fixed advertised
    host: an HQPlayer Embedded box on the LAN cannot reach 127.0.0.1."""
    from streaming.media_host import media_host_for_name
    return media_host_for_name(settings.hqplayer_host)


def _library_uri(db_path: str) -> str:
    """The file:// URI HQPlayer opens for a stored path — the path itself:
    an HQPlayer handed file:// URIs runs on this machine."""
    return file_path_to_uri(db_path)


def _library_db_path(uri: str) -> Optional[str]:
    """Inverse of `_library_uri`: the stored media_files.file_path behind a
    file:// URI HQPlayer reports — percent-escapes undone (HQPlayer escapes
    brackets in the URIs it returns). None for anything that is not a file."""
    if not uri.startswith("file://"):
        return None
    return uri_to_file_path(uri).replace("\\", "/")


def _uri_file_path(uri: str) -> Optional[str]:
    """The path behind a file:// URI as HQPlayer reports it, escapes undone,
    forward slashes — the identity of a file held in HQPlayer's own library
    (no library-root remap: that path IS where HQPlayer opens it)."""
    if not uri.startswith("file://"):
        return None
    return uri_to_file_path(uri).replace("\\", "/")


def _http_served(item: QueueItem) -> bool:
    """Does this slot reach HQPlayer as an http URL rather than a file it
    opens itself? Then HQPlayer's own tags for it are not authoritative (it
    may know the track only as 'HTTP stream') and its playlist URI carries a
    token, not a path."""
    src = item.source
    if src["kind"] == "proxy":
        return True
    if src["kind"] != "file":
        return src.get("uri", "").startswith("http")
    if src.get("cue_start") is not None:
        return True
    from streaming.local import TRANSCODE_FORMATS
    if (src.get("format") or "").upper() in TRANSCODE_FORMATS:
        return True
    return _stream_mode()


def _register_owned(item: QueueItem, proxy) -> str:
    """Register the owned bytes behind `item` with the media proxy and
    return the token /file/ serves them under — a CUE slice as its cut, a
    plain file as itself. Idempotent (the tokens are deterministic), so the
    busy attach calls it for a restored queue and the playlist a running
    HQPlayer still holds resolves again after a backend restart."""
    from streaming import transcode
    from streaming.proxy import MIME_BY_FORMAT
    src = item.source
    container_path = settings.translate_to_local_path(src["path"])
    if src.get("cue_start") is not None:
        return proxy.register_file(
            container_path, transcode.FLAC_MIME,
            start=src["cue_start"], end=src.get("cue_end"),
            tags={"title": item.title, "artist": item.artist,
                  "album": item.album, "track": item.track_number})
    mime = MIME_BY_FORMAT.get((src.get("format") or "").upper(),
                              "application/octet-stream")
    return proxy.register_file(container_path, mime)


def _slot_identity(uri: str, proxy) -> Optional[tuple]:
    """(db_path, cue_start) behind a URI in HQPlayer's playlist: a file://
    path through the library-root remap, an owned /file/{token} through the
    proxy's registry. None for previews, transcodes and foreign items."""
    if uri.startswith("file://"):
        path = _library_db_path(uri)
        return (path, None) if path else None
    if proxy is not None and "/file/" in uri:
        token = uri.rsplit("/file/", 1)[-1].split("?", 1)[0]
        entry = proxy.file_entry(token)
        if entry is None:
            return None
        return (settings.translate_to_host_path(entry.path), entry.start)
    return None


def _handed(uri: str) -> Optional[tuple]:
    """("track", track_id, media_file_id) for a URI Sautium handed over for a
    track — still the slot's track after the slot was re-bound to another
    copy. HQPlayer reports a file as file://E:/…, so the form it was handed
    in is asked too; an entry with no track (a pass-through, or one
    queue_insert_next re-appended from HQPlayer's own playlist) says nothing."""
    keys = [uri]
    if uri.startswith("file://"):
        keys.append(file_path_to_uri(_uri_file_path(uri)))
    for key in keys:
        e = diag.handover(key)
        if e is not None and (e["track_id"] is not None or e["media_file_id"] is not None):
            return ("track", e["track_id"], e["media_file_id"])
    return None


def _reading(uri: str, proxy, ours: str) -> Optional[tuple]:
    """What a URI names by itself: ("file", path, cue_start) for a file://
    path or an owned /file/ token, ("track", track_id, media_file_id) for a
    /preview/ stream (a phantom's, or an m4a transcoded from its file), and
    ("uri", uri, None) for any other address — another node's media proxy or
    a NAS path among them: a token counts only at `ours`, the address this
    backend hands HQPlayer. None for a token at ours this process does not
    know (a stream of an earlier session): nothing can be said."""
    if uri.startswith("file://"):
        return ("file", _uri_file_path(uri), None)
    token = _token_of(uri)
    if token is None or proxy is None or urlsplit(uri).netloc != ours:
        return ("uri", uri, None)
    if "/file/" in uri:
        entry = proxy.file_entry(token)
        return ("file", settings.translate_to_host_path(entry.path), entry.start) if entry else None
    meta = proxy.preview_meta(token)
    return ("track", meta["track_id"], meta["media_file_id"]) if meta else None


def _reads_item(reading: tuple, item: QueueItem) -> bool:
    """Is what a reading names this queue item: its track, by the track's
    UUID or the file a transcode was made from, or the file it opens here — a
    CUE slice by its start, the whole image when the cut failed?"""
    kind, a, b = reading
    if kind == "track":
        return (a is not None and a == item.track_id) or (
            b is not None and b == item.media_file_id)
    src = item.opener()
    if kind == "uri":
        return src["kind"] == "uri" and _same_entry(src["uri"], a)
    if src["kind"] in ("file", "hqp"):
        path = src["path"]
    elif src["kind"] == "uri" and src["uri"].startswith("file://"):
        path = _uri_file_path(src["uri"])
    else:
        return False
    return path == a and (b is None or b == src.get("cue_start"))


def _outward(idx: int, n: int):
    """The slots 1..n, nearest to `idx` first."""
    for d in range(max(idx - 1, n - idx) + 1):
        for j in ((idx - d, idx + d) if d else (idx,)):
            if 1 <= j <= n:
                yield j


def _owned_play_uri(item: QueueItem, host: str) -> str:
    """HQPlayer URI for an owned track.

    A CUE slice always rides the media proxy as a cached FLAC cut — the raw
    path would play the whole disc image; the only fallback is that whole
    image, loudly logged. An m4a (MP4 container: HQPlayer decodes neither AAC
    nor ALAC) is transcoded to FLAC in memory and served from /preview/,
    displayed as owned, falling back to the raw file when streaming is off
    or the transcode fails. Everything else is a file:// URI at the path an
    HQPlayer on this machine opens itself, or /file/{token} on the media
    proxy for one anywhere else. `host` is the proxy address HQPlayer
    reaches."""
    global _local_provider
    src = item.opener()
    media_file_id, db_path, file_format = (
        src.get("media_file_id", item.media_file_id), src["path"], src.get("format"))

    def whole_file_uri() -> str:
        """The file itself — for a CUE slice, the raw image (cut failed)."""
        if not _stream_mode():
            return _library_uri(db_path)
        from streaming import service as streaming_service
        from streaming.proxy import MIME_BY_FORMAT
        proxy = streaming_service.ensure_proxy()
        mime = MIME_BY_FORMAT.get((file_format or "").upper(),
                                  "application/octet-stream")
        tok = proxy.register_file(settings.translate_to_local_path(db_path), mime)
        return proxy.file_url(tok, host=host)

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
            return whole_file_uri()
        return proxy.file_url(_register_owned(item, proxy), host=host)

    from streaming.local import TRANSCODE_FORMATS
    if (file_format or "").upper() not in TRANSCODE_FORMATS:
        return whole_file_uri()

    from streaming import service as streaming_service
    proxy = streaming_service.get_proxy() if streaming_service.is_enabled() else None
    if proxy is None:
        return whole_file_uri()

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
        return whole_file_uri()
    return proxy.url_for(tokens[0], host=host)

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
# in HQPlayer's own GUI are logged, never reconciled. What plays is not the
# canary's to say: every tick checks the entry HQPlayer reads (_playing).
DRIFT_CHECK_EVERY = 30


class HqpBackend(PlayerBackend):
    """HQPlayer output. Status acquisition = 1 s poll on a dedicated socket —
    the control protocol cannot push; documented boundary exception to the
    no-polling rule (HARDWARE-TIERS §2.6). The canonical queue is mirrored
    one-way INTO HQPlayer's native playlist via the queue_* hooks."""

    id = "hqplayer"
    # What the play-intent gate tells the user when reachable() says no.
    offline_hint = ("start HQPlayer, or pick another output in Settings → "
                    "Audio output (an HQPlayer Embedded in trial mode stops "
                    "every 30 minutes and must be restarted)")

    def __init__(self, emit, queue: CanonicalQueue):
        super().__init__(emit)
        self._queue = queue
        self._endpoint_row: dict = {}
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._failures = 0
        self._drift_logged = False
        # The playlist HQPlayer holds differs from the canonical queue (an
        # external edit, or HQPlayer came back empty after a restart): the
        # slot index names nothing of ours until the next mirror.
        self._drift = False
        # The last verdict of _playing, by (entry, HQPlayer's index, queue
        # version): a foreign entry played for an hour is looked for in the
        # queue once, not every second.
        self._verdict: Optional[tuple] = None
        # Set when a RECONNECT found that difference — HQPlayer restarted (a
        # trial-mode Embedded stops every 30 minutes) and lost the mirror —
        # so the play-intent gate re-attaches and re-mirrors before playing.
        self._mirror_lost = False
        # Lost sight of HQPlayer (a dropped socket, failed polls): its
        # playlist is read on the first answer, and the flag stays raised
        # until that read succeeds, so one failed PlaylistGet right after a
        # restart cannot let an empty playlist pass as an external edit.
        self._verify_mirror = False
        self._gone = False
        # The proxy address HQPlayer reaches, fixed per attach: a DNS + route
        # lookup once, not per queued item.
        self._url_host = hqp_media_host()
        # Slot to select on the first play after an output switch (resume_at).
        self._resume_index: Optional[int] = None
        # The play intent being watched (playback.hqp_diagnostics), and the
        # ones an intent superseded or the owner ended, waiting for the
        # poller to judge them.
        self._attempt: Optional[diag.Attempt] = None
        self._closing: list = []
        self._att_lock = threading.Lock()
        # The intent each thread is carrying out: two can overlap (a Next
        # pressed while a replace still adds), and each one's commands are its
        # own steps — never the owner ending the other.
        self._local = threading.local()
        self._last_closed: Optional[diag.Attempt] = None
        self._last_track = 0
        self._info: dict = {}
        # When a listen leaves a DSP sample (playback.hqp_load).
        self._sampler = hqp_load.Sampler()
        # Set by shutdown under _hqp_lock: from then on no command of this
        # instance reaches HQPlayer (_check_attached).
        self._detached = False
        # A cut-short benchmark run is put back once HQPlayer answers: at the
        # attach if GetInfo did, else at the first status that does
        self._recover_due = False

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        global _listener
        _listener = self
        try:
            with _hqp_status_lock:
                self._info = _get_hqp_status().get_info() or {}
        except (BrokenPipeError, ConnectionError, OSError) as e:
            logger.info("HQPlayer GetInfo at attach failed: %s", e)
        if self._detached:
            return              # switched away while the attach waited on HQPlayer
        self._note_info()
        if self._info:
            self._recover()
        else:
            self._recover_due = True
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
            else:
                self._register_serving()
        self._library_check()
        with _hqp_lock:
            # shutdown() detaches under this lock: past it, a poller started
            # here is one it joins — never one nothing stops
            if self._detached:
                return
            # Where its meter stream lives: the control port + 1 (Signalyst's
            # SDK). Opened only while a page has the meter open.
            hqp_meter.meter.attach(settings.hqplayer_host, settings.hqplayer_port + 1)
            self._running = True
            self._thread = threading.Thread(target=self._poll_loop, daemon=True,
                                            name="hqp-status-poller")
            self._thread.start()
        logger.info("HQPlayer status poller started")

    def _note_info(self) -> None:
        """What HQPlayer said of itself goes on its endpoint row: the build
        a DSP sample is valid for, the machine when it is this one."""
        ep_id = self.endpoint_id
        if ep_id is not None and self._info:
            import hqp_library
            try:
                hqp_library.note_info(ep_id, self._info, here=not _stream_mode())
            except psycopg2.Error as e:
                logger.warning("HQPlayer's GetInfo not recorded: %s", e)

    def _recover(self) -> None:
        """A benchmark run cut short left HQPlayer lowered and on its test
        settings: put back the next time HQPlayer answers — at the attach,
        before anything reads its playlist, and when the poller sees it come
        back. A failure costs the recovery, never the attach or the poll."""
        if self.endpoint_id is None:
            return
        from playback import hqp_benchmark
        try:
            hqp_benchmark.recover(self.endpoint_id)
        except (OSError, RuntimeError, psycopg2.Error) as e:
            logger.warning("benchmark recovery failed: %s", e)

    def _write_sample(self, status, dsp: dict) -> None:
        """A listen's DSP sample. A database that cannot take it costs the
        sample, never the poll: it is not HQPlayer failing. An endpoint that
        took none is looked up again — forgotten meanwhile, or not yet told
        its build."""
        ep_id = self.endpoint_id
        if ep_id is None:
            return
        try:
            if hqp_load.write(hqp_load.Sample.of(status, dsp, endpoint_id=ep_id)) is None:
                self._endpoint_row = {}
        except psycopg2.Error as e:
            logger.warning("DSP sample not written: %s", e)

    def _library_check(self) -> None:
        """An HQPlayer we just attached to or that just came back may have
        rescanned its library: hqp_library re-checks its hash off this
        thread and syncs only an endpoint the owner synced before."""
        import hqp_library
        hqp_library.request_sync(settings.hqplayer_host, settings.hqplayer_port)

    def shutdown(self) -> None:
        global _listener
        with _hqp_lock:
            # A command already on its way finished before the lock was ours;
            # one sent later by whoever took this backend before the switch
            # (a play intent, the DSP resume watcher) is refused.
            self._detached = True
        self._running = False
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=3)
        if _listener is self:
            _listener = None
            hqp_meter.meter.detach()
        with self._att_lock:
            att, self._attempt = self._attempt, None
            if att is not None:
                att.request_end("shutdown")
                self._closing.append(att)
        self._settle(None, None)
        logger.info("HQPlayer status poller stopped")

    def capabilities(self) -> Capabilities:
        return Capabilities(volume=True, volume_kind="db", seek=True, gapless=True)

    def healthy(self) -> bool:
        return not self._gone and not self._mirror_lost

    def reachable(self) -> bool:
        """Protocol-level liveness at play-intent time: connect and get an
        answer to GetInfo within 2 s. A bare TCP connect proves nothing —
        HQPlayer Embedded's trial stop keeps the port open and closes every
        connection at once (seen live), and HQPlayer Desktop listens only
        once its audio engine is up."""
        try:
            with socket.create_connection(
                    (settings.hqplayer_host, settings.hqplayer_port),
                    timeout=2.0) as s:
                s.settimeout(2.0)
                s.sendall(b"<GetInfo/>")
                reply = s.recv(4096)
        except OSError as e:
            self._unreachable(f"GetInfo: {e}")
            return False
        if b"product=" in reply:
            return True
        self._unreachable("GetInfo: the connection closed without an answer" if not reply
                          else "GetInfo: answered without naming a product")
        return False

    def _unreachable(self, message: str) -> None:
        """A play intent the gate stops here never reaches HQPlayer: it is
        an attempt of its own, closed at once."""
        if diag.record_unreachable(message, self._context()):
            _notify_notices()

    def _register_serving(self) -> None:
        """HQPlayer plays on through a backend restart: its playlist is
        left alone, but the restored queue's owned files are registered with
        the proxy again so the /file/ URLs it still holds resolve — the
        tokens are deterministic, so they are the very same ones."""
        from streaming.local import TRANSCODE_FORMATS
        items = [it for it in self._queue.snapshot()
                 if it.source["kind"] == "file" and _http_served(it)
                 and (it.source.get("format") or "").upper() not in TRANSCODE_FORMATS]
        if not items:
            return
        from streaming import service as streaming_service
        try:
            proxy = streaming_service.ensure_proxy()
        except RuntimeError as e:
            logger.warning("owned files not re-registered for HQPlayer: %s", e)
            return
        for it in items:
            _register_owned(it, proxy)
        logger.info("re-registered %d owned file(s) behind HQPlayer's running "
                    "playlist", len(items))

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
        items = _items_from_hqp_tracks(hqp_tracks, self.endpoint_id)
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
            status = None
            playing = None
            reads = None
            sample = None
            reconnected = False
            att = self._attempt
            try:
                hqp_playlist = None
                with _hqp_status_lock:
                    before = _hqp_status_client
                    try:
                        hqp = _get_hqp_status()
                        status = hqp.get_status()
                    except (BrokenPipeError, ConnectionError, OSError):
                        _reset_hqp_status()
                        hqp = _get_hqp_status()
                        status = hqp.get_status()
                    # HQPlayer went away and came back: the socket was
                    # replaced within this tick (it dropped the connection
                    # and answered the immediate retry), or this is the
                    # first answer after failed ticks — it was gone for
                    # longer than one poll (a power cycle, a trial restart
                    # taking its time), and those ticks had already reset
                    # the client, so the socket comparison alone saw
                    # nothing and the empty playlist passed as an external
                    # edit (live, 2026-09-27). Its playlist is read NOW,
                    # not at the next canary tick: a restart drops the
                    # mirror, and the play press must know before it lands
                    # on an empty list.
                    if (before is not None and hqp is not before) or self._failures > 0:
                        self._verify_mirror = True
                        self._library_check()
                        self._info = hqp.get_info() or self._info
                        reconnected = True
                    if status is not None and (self._verify_mirror
                                               or tick % DRIFT_CHECK_EVERY == 0):
                        _, version, pending = self._queue.view()
                        try:
                            hqp_playlist = hqp.get_playlist()
                        except (BrokenPipeError, ConnectionError, OSError) as e:
                            logger.debug("drift check playlist read failed: %s", e)
                        # Judged against the queue it was read with — never a
                        # read made while a mutation was in flight, nor one a
                        # commit overtook — and before the slot below, which
                        # falls back on the verdict.
                        if (hqp_playlist is not None and not pending
                                and self._queue.view()[1:] == (version, False)):
                            if self._check_drift(hqp_playlist) and self._verify_mirror:
                                self._mirror_lost = True
                                logger.warning("HQPlayer came back with a different "
                                               "playlist (restarted?) — the queue is "
                                               "re-mirrored on the next play")
                            self._verify_mirror = False
                    if att is not None and status is not None:
                        att.tick(status, STATE_NAMES.get(status.state, "unknown"))
                        if att.due(time.time()) is not None:
                            # Judged on what HQPlayer holds NOW, read on this
                            # socket — a command thread never takes this lock.
                            reads = self._close_reads(hqp, att)
                    now = time.monotonic()
                    playing = self._playing(status) if status is not None else None
                    if playing is not None and self._sampler.due(
                            status, ours=playing[1] is not None,
                            engine=self._info.get("engine"), now=now,
                            metering=hqp_meter.meter.metering()):
                        dsp = hqp.get_state()
                        if dsp is None:
                            self._sampler.taken(now)    # lost with the read: the next waits its turn
                        elif self._sampler.steady(dsp, now):
                            self._sampler.taken(now)
                            sample = (status, dsp)

                if reconnected:
                    self._note_info()
                    if status is not None:
                        hqp_meter.meter.hqplayer_back()
                # Only when it answered: a trial-stopped Embedded takes the
                # connection and closes it at once, tick after tick.
                if status is not None and (reconnected or self._recover_due):
                    self._recover_due = False
                    self._recover()

                if status is not None and not status.state.known:
                    # A state the SDK does not name (Embedded said 5 while it
                    # started a play): it answered, nothing to act on — the
                    # last good status stands, no listen closes, no miss.
                    self._failures = 0
                elif status is None:
                    # Transient read miss (e.g. HQPlayer stalled past the
                    # socket timeout). Keep serving the last good status.
                    self._register_failure()
                else:
                    self._failures = 0
                    hqp_meter.meter.set_gain(None if status.track_gain is None
                                             else status.volume + status.track_gain)
                    if status.track_index >= 1:
                        self._last_track = status.track_index
                    if playing is not None:
                        # The slot playing on, its position moving and the DSP
                        # keeping up: a failure judged on it explains nothing now.
                        if (status.state == PlaybackState.PLAYING and playing[1] is not None
                                and status.position >= 1.0
                                and (status.process_speed is None or status.process_speed >= 1.0)
                                and diag.note_playing(playing[0], self._queue.generation)):
                            _notify_notices()
                        self._emit(self._status_of(status, playing))
                    if diag.note_answered():
                        _notify_notices()
                    if sample is not None:
                        self._write_sample(*sample)
            except Exception:
                self._register_failure()
            if att is not None and status is None:
                err = _hqp_status_client.last_error if _hqp_status_client else None
                att.miss(err.message if err else "no status answer",
                         silent=bool(_hqp_status_client and _hqp_status_client.timed_out))
            self._settle(att if att is not None and att.due(time.time()) is not None
                         else None, reads)

            # Back off when HQPlayer stops answering. It accepts the TCP
            # connect but doesn't reply to <Status/> (control thread busy
            # under DSP load), so each retry is a connect->read-timeout->
            # disconnect churn cycle that only piles load onto an already-
            # struggling control port. Exponential backoff (2,4,8..15s) lets
            # it recover; a successful poll resets to 1s. Capped at 15 s —
            # the WSL2→Windows hop flaps for seconds at a time, and a 30 s
            # cap kept the UI on "disconnected" long after HQP was back.
            self._wake.wait(timeout=_poll_interval(self._failures))
            self._wake.clear()
            tick += 1

    def _playing(self, status) -> Optional[tuple]:
        """Which queue slot HQPlayer reads: (index, item, foreign), or None —
        no verdict this tick. Its index names our slot only while its playlist
        is our mirror, and the canary compares the two every DRIFT_CHECK_EVERY
        ticks — an entry started in HQPlayer's own window was tracked and
        scrobbled as the queued track at that index until then. So every tick
        checks the entry its status names against one view of the queue: ours
        at that slot, ours at another (the nearest), or foreign; a slot is the
        entry when the hand-over or the URI itself names it.

        No verdict while HQPlayer opens an entry (PLAYING at track 0 and
        length 0 — the slot comes with the next tick), nor for an entry the
        queue does not hold at HQPlayer's index while a mutation is in
        flight: a replace plays its first track before the queue commits, a
        removal shifts HQPlayer's index first — an index read off the queue
        before the commit names another item after it. With nothing that
        names the entry (no URI from this build; a stream token of an earlier
        session) the index stands, external while the canary says the
        playlist differs."""
        idx = status.track_index
        items, version, pending = self._queue.view()
        item = items[idx - 1] if 1 <= idx <= len(items) else None
        if status.state not in (PlaybackState.PLAYING, PlaybackState.PAUSED):
            return idx, item, False
        if idx < 1:
            return None
        readings = self._readings(status.uri)
        if not readings:
            return (0, None, True) if self._drift else (idx, item, False)
        key = (status.uri, idx, version)
        cached = self._verdict
        if cached is not None and cached[0] == key:
            return cached[1]
        found = next((j for j in _outward(idx, len(items))
                      if any(_reads_item(r, items[j - 1]) for r in readings)), None)
        if pending and found != idx:
            return None
        verdict = (found, items[found - 1], False) if found else (0, None, True)
        self._verdict = (key, verdict)
        return verdict

    def _readings(self, uri: str) -> list:
        """What names the entry at `uri`: the hand-over (_handed) and the URI
        itself (_reading) — a CUE image whose cut failed was handed for every
        slice, so neither alone settles the slot."""
        if not uri:
            return []
        from streaming import service as streaming_service
        proxy = streaming_service.get_proxy()
        ours = f"{self._url_host}:{proxy.port}" if proxy is not None else ""
        return [r for r in (_handed(uri), _reading(uri, proxy, ours)) if r is not None]

    def reads_foreign(self, status) -> bool:
        """Does HQPlayer, as this status finds it, read an entry that is not
        in the queue — another controller playing, a file from its own window?"""
        playing = self._playing(status)
        return playing is not None and playing[2]

    def _status_of(self, status, playing: tuple) -> PlaybackStatus:
        """One status tick as the manager reads it, at the slot `_playing`
        found. HQPlayer's own tags are authoritative only for a slot it
        opened as a file; an http-served slot (a preview, a transcode, every
        owned file for an HQPlayer elsewhere) is described by the queue item,
        so those keys are left out and the manager falls back to it. An entry
        that is not ours is reported as external playback (slot 0), so
        nothing is tracked against a queued track."""
        idx, item, foreign = playing
        extra = {"genre": status.genre, "process_speed": status.process_speed,
                 "limited": status.limited, "active_mode": status.active_mode}
        if foreign:
            extra["source"] = "external"
        if item is None or not _http_served(item):
            extra.update(artist=status.artist, album=status.album,
                         song=status.song)
        diagnosis = self._diagnosis(status)
        if diagnosis is not None:
            extra["diagnosis"] = diagnosis
        return PlaybackStatus(
            state=STATE_NAMES.get(status.state, "unknown"),
            position=status.position, length=status.length,
            queue_index=idx, volume=status.volume, extra=extra, item=item)

    def _diagnosis(self, status) -> Optional[dict]:
        """The failed attempt Now Playing offers to explain: this backend's
        latest, while no newer one runs, the queue is the one it was made
        for, HQPlayer is not playing something meanwhile, and it has not
        played that slot since (diag.note_playing). An attempt that never
        reached HQPlayer is left to the play route's own answer."""
        last = self._last_closed
        if (last is None or last.cleared or self._attempt is not None
                or status.state == PlaybackState.PLAYING):
            return None
        v, f = last.verdict or {}, last.facts or {}
        if (v.get("code") in (None, "played", "interrupted", "unreachable")
                or f.get("generation") != self._queue.generation):
            return None
        return {"id": last.id, "code": v["code"], "title": v["title"], "next": v["next"],
                "track_id": (f.get("item") or {}).get("track_id"),
                "failing": diag.failing_run() is not None}

    # -- the playback trace (playback.hqp_diagnostics) ------------------------------

    @contextmanager
    def _intent(self, intent: str, *, slot: Optional[int] = None,
                uris: Optional[list] = None, items: Optional[list] = None):
        """One play intent watched as an attempt. Its commands are steps (the
        command client reports them on the thread that sent them); the exit
        judges whether a final refusal or a command that never left already
        settles it."""
        att = diag.Attempt(intent=intent, slot=slot, uris=uris, items=items)
        with self._att_lock:
            old, self._attempt = self._attempt, att
            if old is not None:
                old.request_end("superseded")
                self._closing.append(old)
        self._local.attempt = att
        exc = None
        try:
            yield att
        except (BrokenPipeError, ConnectionError, OSError) as e:
            exc = e
            raise
        finally:
            self._local.attempt = None
            att.intent_done(exc)
            self.poke()

    def _on_outcome(self, outcome) -> None:
        """An answer on the command client. Sent by an intent, it is a step of
        that intent's attempt (superseded or not); from anywhere else a stop,
        a pause, a DSP change or a replaced playlist HQPlayer took means the
        owner moved on — one it refused changed nothing."""
        mine = getattr(self._local, "attempt", None)
        if mine is not None:
            mine.record(outcome)
            return
        att = self._attempt
        if att is None or outcome.failed:
            return
        a = outcome.attributes
        if (outcome.command in diag.OWNER_COMMANDS
                or (outcome.command == "PlaylistAdd" and a.get("clear") == "1")
                or (outcome.command == "PlaylistRemove"
                    and str(a.get("index")) == str(att.slot))):
            att.request_end("owner")
            self.poke()

    def _refused(self, command: str, message: str) -> None:
        mine = getattr(self._local, "attempt", None)
        if mine is not None:
            mine.refused(command, message)

    def _check_attached(self) -> None:
        """Every command path of this instance calls it under _hqp_lock."""
        if self._detached:
            raise ConnectionError("HQPlayer was detached from playback")

    def _hqp_cmd(self, func):
        """One command on the command client, under _hqp_lock; one reconnect
        on a dropped socket."""
        with _hqp_lock:
            self._check_attached()
            try:
                return func(_get_hqp())
            except (BrokenPipeError, ConnectionError, OSError) as e:
                logger.warning(f"HQPlayer connection lost ({e}), reconnecting...")
                _reset_hqp()
                return func(_get_hqp())

    def _command(self, name: str, fn) -> bool:
        """One command on the command client; one HQPlayer did not take is
        logged in its words."""
        return self._refusal_of(name, fn) is None

    def _refusal_of(self, name: str, fn) -> Optional[str]:
        """`_command`, answering why not: None when HQPlayer took it."""
        def run(h):
            return None if fn(h) else h.refusal()
        refusal = self._hqp_cmd(run)
        if refusal is not None:
            logger.warning("HQPlayer did not take %s: %s", name, diag.redact_text(refusal))
        return refusal

    def _close_reads(self, hqp: HQPlayerClient, att: diag.Attempt) -> dict:
        """What HQPlayer holds as the attempt closes — its playlist (whose
        entries are ours, which one it plays) and, for a slot that never
        played, its matrix profile. Called inside _hqp_status_lock."""
        entries = hqp.get_playlist()
        reads = {"playlist": None if hqp.last_error is not None
                 else [t.get("uri", "") for t in entries]}
        if not att.played_so_far():
            reads["state"] = hqp.get_state()
        return reads

    def _settle(self, due_att: Optional[diag.Attempt], reads: Optional[dict]) -> None:
        """Judge what is ready: the attempts superseded or ended by the owner,
        and the watched one once it is due. Runs on the poller (and at
        shutdown) only."""
        with self._att_lock:
            ready, self._closing = self._closing, []
            if due_att is not None and due_att is self._attempt:
                self._attempt = None
                ready.append(due_att)
        for att in ready:
            self._finalize(att, reads if att is due_att else None)

    def _finalize(self, att: diag.Attempt, reads: Optional[dict]) -> None:
        facts = self._facts(att, reads)
        changed = diag.close(att, facts)
        self._last_closed = att
        v = att.verdict
        if v["code"] not in ("played", "interrupted"):
            logger.info("HQPlayer %s attempt %d: %s — %s", att.intent, att.id, v["code"],
                        diag.redact_text(v["sentence"]))
        if changed:
            _notify_notices()
        if v["code"] in ("not_played", "unknown") or (v["code"] == "rejected"
                                                      and not v.get("quote")):
            # HQPlayer's log names what the protocol did not — read once, now.
            threading.Thread(target=self._attach_log, args=(att, facts.get("uri")),
                             daemon=True, name="hqp-log").start()

    def _attach_log(self, att: diag.Attempt, uri: Optional[str]) -> None:
        source = diag.log_source(settings.hqplayer_host, self._info)
        diag.attach_log(att, diag.read_log(source, self._info, uri))

    @property
    def info(self) -> dict:
        """What HQPlayer said of itself (GetInfo) at attach or its return."""
        return dict(self._info)

    @property
    def drift(self) -> bool:
        """HQPlayer's playlist is not the canonical queue — an edit in its own
        window, or another controller (Roon) driving it."""
        return self._drift

    def _context(self) -> dict:
        return {"hqplayer": f"{settings.hqplayer_host}:{settings.hqplayer_port}",
                "media_url": f"{self._url_host}:{settings.media_proxy_port}",
                "remote": _stream_mode(), "drift": self._drift,
                "endpoint_id": self.endpoint_id, "info": dict(self._info)}

    def _facts(self, att: diag.Attempt, reads: Optional[dict]) -> dict:
        """Everything the verdict weighs, gathered as the attempt closes —
        the shape tests/fixtures/hqp_traces records."""
        from streaming import service as streaming_service
        proxy = streaming_service.get_proxy()
        f = att.snapshot()
        uris = f.pop("uris")
        ticks = f["ticks"]
        explicit = f["slot"] is not None
        slot = f["slot"]
        if slot is None:
            slot = (next((t["track"] for t in ticks if t["state"] == "playing" and t["track"] >= 1), None)
                    or next((t["track"] for t in reversed(ticks) if t["track"] >= 1), None))
        item = expected = None
        if slot is not None:
            if uris and slot <= len(uris):
                expected = uris[slot - 1]
                item = att.items[slot - 1] if att.items and slot <= len(att.items) else None
            else:
                item = self._queue.item_at(slot)
                if item is not None and explicit:
                    expected = diag.handed_uri(_slot_key(item))
        playlist, at = None, None
        listed = (reads or {}).get("playlist")
        if listed is not None:
            refs = self._queue_refs()
            at = listed[slot - 1] if slot and 1 <= slot <= len(listed) else None
            playlist = {
                "read": True, "count": len(listed), "at_slot": at,
                "at_slot_ours": at is not None and self._ours(at, refs, proxy),
                "at_slot_expected": bool(expected and at and _same_entry(at, expected)),
                "expected_present": (any(_same_entry(u, expected) for u in listed)
                                     if expected else None),
                "foreign": sum(1 for u in listed if not self._ours(u, refs, proxy))}
        uri = expected or at or (diag.handed_uri(_slot_key(item)) if item is not None else None)
        handover = diag.handover(uri) or (
            diag.handover_like(lambda handed: _same_entry(handed, uri)) if uri else None)
        if handover:
            uri = handover["uri"]          # as Sautium handed it
        mode = handover["mode"] if handover else (diag.mode_of(uri) if uri else None)
        token = _token_of(uri) if uri else None
        hits = (proxy.hits(token) if proxy is not None and token else []) \
            if mode in diag.HTTP_MODES else None
        if item is not None:
            who = {"track_id": item.track_id, "media_file_id": item.media_file_id,
                   "title": item.title, "artist": item.artist, "album": item.album,
                   "kind": item.opener()["kind"]}
        elif handover:
            who = {k: handover.get(k) for k in ("track_id", "media_file_id", "title", "artist")}
        else:
            who = None
        if who is not None and mode == "preview" and proxy is not None and token:
            who["chain"] = proxy.chain_summary(token)
        state = (reads or {}).get("state") or {}
        return {**f, "closed": time.time(), "slot": slot, "expected_uri": expected,
                "uri": uri, "mode": mode, "token": token, "handover": handover,
                "proxy": hits, "playlist": playlist, "item": who,
                "context": {**self._context(),
                            "dsp": {"matrix_profile": state.get("matrix_profile") or None}},
                "generation": self._queue.generation}

    def _queue_refs(self) -> set:
        """Every path and verbatim URI the queue hands over, as HQPlayer's
        playlist names them — what counts as ours there."""
        refs = set()
        for it in self._queue.snapshot():
            for src in (it.source, it.play or {}):
                if src.get("path"):
                    refs.add(src["path"].replace("\\", "/"))
                if src.get("uri"):
                    refs.add(src["uri"])
        return refs

    @staticmethod
    def _ours(uri: str, refs: set, proxy) -> bool:
        """Is this playlist entry one Sautium handed over? A path of a queued
        file (or of one HQPlayer holds for a slot), a /file/ token the proxy
        registered, a preview it knows — all survive a backend restart."""
        if uri.startswith("file://"):
            return _uri_file_path(uri) in refs
        token = _token_of(uri)
        if token and proxy is not None:
            if "/file/" in uri:
                return proxy.file_entry(token) is not None
            return proxy.preview_meta(token) is not None
        return uri in refs

    def _register_failure(self) -> None:
        """Tolerate a short burst of misses; emit 'disconnected' only past
        the threshold (the manager dedupes repeats)."""
        self._failures += 1
        if self._failures >= STATUS_FAILURE_THRESHOLD:
            self._emit(PlaybackStatus(state="disconnected"))

    def _first_divergence(self, hqp_tracks: list, snapshot: list) -> Optional[int]:
        """The first 1-based slot, over the length the two share, at which
        HQPlayer's entry names another file than the queue's; None when none
        does. Preview and transcode slots are skipped (their tokens are
        re-minted); every other file slot must resolve to the same (path,
        cue_start) — `_slot_identity` undoes HQPlayer's percent-escapes and
        the library-root remap and looks /file/ tokens up in the proxy, so
        raw URI equality is never relied on."""
        from streaming import service as streaming_service
        from streaming.local import TRANSCODE_FORMATS
        proxy = streaming_service.get_proxy()
        for i, (t, it) in enumerate(zip(hqp_tracks, snapshot), start=1):
            src = it.opener()
            if src["kind"] == "hqp":
                # A file in HQPlayer's own library: the URI's path is the
                # identity, no remap.
                if _uri_file_path(t.get("uri") or "") != src["path"]:
                    return i
                continue
            if (src["kind"] != "file"
                    or (src.get("format") or "").upper() in TRANSCODE_FORMATS):
                continue
            if (_slot_identity(t.get("uri") or "", proxy)
                    != (src["path"], src.get("cue_start"))):
                return i
        return None

    def _check_drift(self, hqp_tracks: list) -> bool:
        """Compare HQPlayer's playlist against the canonical queue — the
        lengths, then every slot (_first_divergence). Returns whether they
        differ."""
        snapshot = self._queue.snapshot()
        drift = (len(hqp_tracks) != len(snapshot)
                 or self._first_divergence(hqp_tracks, snapshot) is not None)
        self._drift = drift
        if drift and not self._drift_logged:
            logger.warning(
                "HQPlayer playlist drifted from the canonical queue "
                "(external edit?) — the Sautium queue is authoritative")
            self._drift_logged = True
        elif not drift:
            self._drift_logged = False
        return drift

    # -- transport -----------------------------------------------------------------

    def play(self) -> bool:
        with self._intent("play", slot=self._resume_index):
            if self._resume_index is not None:
                idx, self._resume_index = self._resume_index, None
                logger.info("play: honoring resume slot %d", idx)
                if self._select_play(idx):
                    # A SelectTrack on a STOPPED HQPlayer needs a beat to
                    # register before Play honors it — the resume-watcher's
                    # documented 1s boundary exception (04c46b8), same quirk.
                    time.sleep(1.0)
            ok = self._command("Play", lambda h: h.play())
        self.poke()
        return ok

    def pause(self) -> bool:
        ok = self._command("Pause", lambda h: h.pause())
        self.poke()
        return ok

    def stop(self) -> bool:
        ok = self._command("Stop", lambda h: h.stop())
        self.poke()
        return ok

    def next(self) -> bool:
        self._resume_index = None    # explicit navigation overrides resume
        # Watched only when there is a next slot to land on.
        with (self._intent("next") if 1 <= self._last_track < len(self._queue)
              else nullcontext()):
            ok = self._command("Next", lambda h: h.next())
        self.poke()
        return ok

    def previous(self) -> bool:
        self._resume_index = None    # explicit navigation overrides resume
        with self._intent("previous") if self._last_track > 1 else nullcontext():
            ok = self._command("Previous", lambda h: h.previous())
        self.poke()
        return ok

    def select(self, index: int) -> bool:
        """Load AND play the slot (PlayerBackend contract): HQPlayer's
        SelectTrack alone leaves a STOPPED player stopped, so the Play that
        jump() used to send from the manager follows here, same order."""
        self._resume_index = None    # explicit navigation overrides resume
        with self._intent("select", slot=index):
            ok = self._select_play(index)
        self.poke()
        return ok

    def _select_play(self, index: int) -> bool:
        ok = self._command("SelectTrack", lambda h: h.select_track(index))
        if ok:
            ok = self._command("Play", lambda h: h.play())
        return ok

    def resume_after_rebuild(self, index: int, position: int) -> bool:
        """Back to the slot and second a DSP change stopped — select, seek,
        play as ONE attempt (routers.hqplayer._resume_after_dsp_change)."""
        self._resume_index = None
        with self._intent("resume", slot=index):
            ok = self._select_play(index)
            if position > 0:
                self._command("Seek", lambda h: h.seek(int(position)))
            ok = self._command("Play", lambda h: h.play()) and ok
        self.poke()
        return ok

    def seek(self, seconds: int) -> bool:
        ok = self._command("Seek", lambda h: h.seek(int(seconds)))
        self.poke()
        return ok

    def set_volume(self, level: float) -> bool:
        ok = self._command("Volume", lambda h: h.set_volume(level))
        self.poke()
        return ok

    def volume_up(self) -> bool:
        ok = self._command("VolumeUp", lambda h: h.volume_up())
        self.poke()
        return ok

    def volume_down(self) -> bool:
        ok = self._command("VolumeDown", lambda h: h.volume_down())
        self.poke()
        return ok

    # -- canonical-queue mirror -------------------------------------------------------

    def _endpoint(self) -> dict:
        """The hqp_endpoints row of the HQPlayer this backend drives — empty
        until the picker or the attach registered it (hqp_library.register).
        Looked up on first use and kept once found: a row that lands while
        the output is attached is found on the next ask."""
        if not self._endpoint_row:
            import hqp_library
            self._endpoint_row = hqp_library.endpoint_by_address(
                settings.hqplayer_host, settings.hqplayer_port) or {}
        return self._endpoint_row

    def endpoint_forgotten(self) -> None:
        """Its row was forgotten (and registered afresh): looked up again."""
        self._endpoint_row = {}

    @property
    def endpoint_id(self) -> Optional[int]:
        return self._endpoint().get("id")

    @property
    def label(self) -> str:
        """Which HQPlayer, once there can be several — the one name for it
        everywhere (hqp_library.label)."""
        import hqp_library
        ep = self._endpoint()
        return hqp_library.label(ep.get("name"), ep.get("product")) if ep else "HQPlayer"

    def _uri_for(self, item: QueueItem) -> str:
        """Playable HQPlayer URI for a queue item — an owned file by
        file-access mode (`_owned_play_uri`), a preview by its proxy URL,
        both naming the address HQPlayer reaches us at. May transcode (m4a)
        — call OUTSIDE `_hqp_lock`; a slow transcode must never hold the
        command socket hostage."""
        # What THIS output opens (QueueItem.play, playback.substitute): its
        # own held copy or a rip here for a slot queued elsewhere.
        src = item.opener()
        if src["kind"] == "file":
            uri = _owned_play_uri(item, self._url_host)
            mode = ("path" if uri.startswith("file://")
                    else "transcode" if "/preview/" in uri
                    else "cut" if src.get("cue_start") is not None else "stream")
        elif src["kind"] == "hqp":
            # Held in HQPlayer's own library: it opens the path itself.
            uri, mode = file_path_to_uri(src["path"]), "held"
        elif src["kind"] == "proxy":
            from streaming import service as streaming_service
            uri = streaming_service.get_proxy().url_for(src["token"], host=self._url_host)
            mode = "preview"
        elif src["kind"] in ("pending", "unplayable"):
            # Nothing this HQPlayer can open: the origin's path, which it
            # drops (a file it does not hold) — the slot reads as drift
            # until the queue moves on. No stream is minted for the mirror:
            # HQPlayer fetches an http entry at add time.
            uri, mode = file_path_to_uri(item.source.get("path") or ""), "unplayable"
        else:
            uri, mode = src["uri"], "foreign"
        diag.note_handover(uri, _slot_key(item), item, mode)
        return uri

    def queue_changed(self, kind: str, *, play: bool = False) -> None:
        """Every mutation is mirrored in the queue_* hooks, before its commit.
        A re-bind happens under the queue — files left the library
        (PlaybackManager.rebind_files) — with nothing to mirror first, so
        HQPlayer converges on it here."""
        if kind == "rebind":
            self._converge_mirror()

    def _converge_mirror(self) -> None:
        """Bring HQPlayer's playlist back in step after slots were re-bound to
        another copy of their track: its entries still name files that are
        gone. Only a mirror that was in step is converged — a playlist of
        another length is the drift canary's and the play press's. Stopped,
        HQPlayer takes the queue afresh and the play press resumes at the
        slot it was on. Busy, the slot it reads is never touched while the
        first changed entry lies past it: the entries from there are removed
        and appended again, as queue_insert_next does. One at or before it
        cannot be put back in place — the protocol has no insert — so a
        playing HQPlayer is rebuilt around its position (the gap a reorder
        rebuild costs), and a paused one takes the queue afresh and resumes
        that slot from its start on the play press."""
        snapshot = self._queue.snapshot()
        try:
            with _hqp_lock:
                self._check_attached()
                hqp = _get_hqp()
                status = hqp.get_status()
                playlist = hqp.get_playlist()
        except (BrokenPipeError, ConnectionError, OSError) as e:
            logger.info("re-bound slots: HQPlayer unreachable (%s) — the queue "
                        "is re-mirrored on the next play", e)
            self._mirror_lost = True
            return
        # A state the SDK does not name is HQPlayer starting a play: a replace
        # now would stop it — the next rebind, or the play press, converges
        if status is None or not status.state.known or len(playlist) != len(snapshot):
            return
        first = self._first_divergence(playlist, snapshot)
        if first is None:
            return
        current = status.track_index
        busy = status.state in (PlaybackState.PLAYING, PlaybackState.PAUSED)
        if busy and not 1 <= current <= len(snapshot):
            return          # it reads a slot we cannot place: the canary's
        try:
            if busy and first > current:
                uris = [self._uri_for(it) for it in snapshot[first - 1:]]
                with _hqp_lock:
                    self._check_attached()
                    # The URIs took their time: a playhead that moved on to
                    # a changed entry meanwhile is not removed from under
                    # itself — that entry names a gone file, and the play
                    # press re-mirrors.
                    now = _get_hqp().get_status()
                    converged = now is not None and now.track_index < first
                    # A removal HQPlayer did not take stops it there: what is
                    # appended after it would sit beside what it kept
                    if converged:
                        converged = all(_hqp_safe(lambda h: h.playlist_remove(first),
                                                  "PlaylistRemove") for _ in uris)
                    if converged:
                        converged = _add_uris_with_retry(uris) == len(uris)
                self.poke()
            elif status.state == PlaybackState.PLAYING:
                converged = self._rebuild([self._uri_for(it) for it in snapshot],
                                          current, int(status.position), resume=True)
            else:
                converged = self.queue_replace(snapshot, play=False) == len(snapshot)
                if 1 <= current <= len(snapshot):
                    self._resume_index = current
        except (BrokenPipeError, ConnectionError, OSError) as e:
            logger.warning("re-bound slots: HQPlayer's playlist could not follow "
                           "(%s) — the queue is re-mirrored on the next play", e)
            converged = False
        if converged:
            self._drift = False
            logger.info("HQPlayer's playlist follows the re-bound slots from %d", first)
        else:
            self._mirror_lost = True

    def _rebuild(self, uris: list, index: int, position: int, *, resume: bool) -> bool:
        """Clear and reload HQPlayer's playlist and land on slot `index` at
        `position` — the ~300 ms gap of every change that needs an insert the
        protocol lacks. The URIs come made (_uri_for may transcode; never
        under `_hqp_lock`). hqp.stop() is mandatory — HQPlayer ignores
        PlaylistAdd(clear=True) while playing, silently appending instead,
        which would double every track and land select_track on the wrong
        slot. Returns whether HQPlayer accepted every entry."""
        with self._intent("rebuild", slot=index, uris=uris) if resume else nullcontext():
            with _hqp_lock:
                self._check_attached()
                hqp = _get_hqp()
                hqp.stop()
                accepted = [_add_one(hqp, uris[0], clear=True)]
                accepted += [_add_one(hqp, uri) for uri in uris[1:]]
                hqp.select_track(index)
                if position > 0:
                    hqp.seek(position)
                if resume:
                    hqp.play()
        self.poke()
        return all(accepted)

    def queue_replace(self, items: list, *, play: bool, probe_first: bool = False) -> int:
        uris = [self._uri_for(it) for it in items]
        # Watched with the items themselves: the canonical queue still holds
        # the old ones until the manager commits this replace.
        with (self._intent("replace", slot=1, uris=uris, items=list(items))
              if play and uris else nullcontext()):
            with _hqp_lock:
                self._check_attached()
                _hqp_safe(lambda h: h.stop(), "Stop")
                added = _add_uris_with_retry(uris, clear_first=True)
                if added == len(uris):
                    self._drift = self._mirror_lost = False   # mirrored afresh
                if added and play:
                    _hqp_safe(lambda h: h.play(), "Play")
                    if probe_first:
                        _skip_to_first_playable(added)
        self.poke()
        return added

    def queue_append(self, items: list) -> int:
        uris = [self._uri_for(it) for it in items]
        with _hqp_lock:
            self._check_attached()
            added = _add_uris_with_retry(uris, clear_first=False)
        self.poke()
        return added

    def queue_insert_next(self, items: list, anchor_index: Optional[int]) -> int:
        """Insert right after the playing slot via the seamless remove-after /
        re-append trick — the reading slot is never touched, so audio plays
        through. URI-based so it works for preview streams too. A removal
        HQPlayer does not take ends the removals there, and only what left
        is appended again: never an entry twice, and the playlist is marked
        no mirror (drift) until the next one."""
        uris = [self._uri_for(it) for it in items]
        with _hqp_lock:
            self._check_attached()
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
                removed = 0
                while removed < len(after) and _hqp_safe(
                        lambda h: h.playlist_remove(idx + 1), "PlaylistRemove"):
                    removed += 1
                added = _add_uris_with_retry(uris, clear_first=False)
                back = _add_uris_with_retry(after[:removed], clear_first=False) if removed else 0
                if removed < len(after) or back < removed:
                    self._drift = True
        self.poke()
        return added

    def queue_remove(self, index: int) -> bool:
        """Mirror-first while HQPlayer's playlist mirrors the queue: the slot
        leaves the queue once it left HQPlayer, and a removal HQPlayer refuses
        raises in its words (the slot is still there — the queue did not
        change). A playlist that is no mirror — drift (an external edit, a
        mirror step HQPlayer did not take) or lost with a restart — is not
        edited by index: the removal is the queue's alone, and the next
        mirror carries it."""
        if self._drift or self._mirror_lost:
            return True
        refusal = self._refusal_of("PlaylistRemove", lambda h: h.playlist_remove(index))
        self.poke()
        if refusal is not None:
            raise RuntimeError(f"HQPlayer did not remove it: {refusal}")
        return True

    def queue_clear_after_current(self) -> bool:
        # PlaylistClear keeps the reading slot intact — it erases everything
        # queued around the current track while the seed plays on. One
        # HQPlayer did not take leaves the queue as it is (mirror-first).
        with _hqp_lock:
            self._check_attached()
            cleared = _hqp_safe(lambda h: h.playlist_clear(), "PlaylistClear")
        self.poke()
        return cleared

    def queue_reorder(self, plan: ReorderPlan) -> dict:
        """Execute a validated reorder plan against HQPlayer's append/remove
        primitives (no insert/move in the protocol — see the /reorder
        endpoint docstring for the seamless-feasibility rules). A step
        HQPlayer does not take ends the sequence there and raises in its
        words: the queue keeps its order (PlaybackManager.apply_reorder), and
        the playlist, part-way, is marked no mirror (drift)."""
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
                self._check_attached()
                hqp = _get_hqp()
                taken = (all(hqp.playlist_remove(plan.status_idx + 1) for _ in plan.old_after)
                         and all(_add_one(hqp, uri_by_id[mid]) for mid in plan.new_after))
                cursor = 1
                new_before_remaining = list(plan.new_before)
                for old_track in plan.old_before:
                    if not taken:
                        break
                    if (new_before_remaining
                            and new_before_remaining[0] == old_track):
                        new_before_remaining.pop(0)
                        cursor += 1
                    else:
                        taken = hqp.playlist_remove(cursor)
                refusal = None if taken else hqp.refusal()
            self.poke()
            if refusal is not None:
                self._drift = True
                logger.warning("HQPlayer did not take the new order: %s",
                               diag.redact_text(refusal))
                raise RuntimeError(f"HQPlayer did not take the new order: {refusal}")
            return {
                "removed": len(plan.old_after) + (len(plan.old_before) - len(plan.new_before)),
                "added": len(plan.new_after),
                "interrupted": False,
            }

        # Fallback: clear+rebuild. Required for cases that need an insert
        # (swap inside before, after→before crossing, current right-shift).
        uri_by_id = {mid: self._uri_for(plan.items_by_id[mid])
                     for mid in plan.order}
        self._rebuild([uri_by_id[mid] for mid in plan.order], plan.new_status_idx,
                      plan.position, resume=plan.resume)
        return {
            "removed": len(plan.order),
            "added": len(plan.order),
            "interrupted": True,
        }


def _items_from_hqp_tracks(hqp_tracks: list, endpoint_id: Optional[int]) -> list[QueueItem]:
    """Reverse-map HQPlayer's raw playlist into QueueItems (adopt-on-attach):
    file:// URIs and owned /file/ URLs resolve through media_files by
    (path, cue_start) — `_slot_identity` undoes HQPlayer's escapes, the
    library-root remap and the proxy's token registry; proxy previews
    resolve through the streaming session's metadata; anything else
    becomes a foreign pass-through item with HQPlayer's own labels."""
    from streaming import service as streaming_service
    proxy = streaming_service.get_proxy()

    identities: dict[str, tuple] = {}
    for t in hqp_tracks:
        uri = t.get("uri", "")
        ident = _slot_identity(uri, proxy)
        if ident is not None:
            identities[uri] = ident
    by_span = queue_mod.items_for_file_spans(list(identities.values()))
    # A file:// slot that is no local file may be one HQPlayer holds in its
    # own library (hqp_library.sync) — keyed by the path it opens.
    held_paths = [_uri_file_path(t.get("uri", "")) for t in hqp_tracks
                  if t.get("uri", "").startswith("file://")
                  and by_span.get(identities.get(t.get("uri", ""))) is None]
    by_held = queue_mod.items_for_hqp_paths(
        [h for h in held_paths if h], endpoint_id
    ) if any(held_paths) else {}

    items: list[QueueItem] = []
    for t in hqp_tracks:
        uri = t.get("uri", "")
        item = None
        if uri in identities:
            item = by_span.get(identities[uri])
        if item is None and uri.startswith("file://"):
            item = by_held.get(_uri_file_path(uri))
        if item is None and uri.startswith("http://") and "/preview/" in uri:
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
