"""
HQPlayer output: the playback backend that drives one HQPlayer, and the
resilient playlist-add primitives every HQPlayer queue write goes through.

Everything this node says to that HQPlayer goes through its link
(playback.hqp_link): one connection, one request on it at a time — the
status poller's questions and the commands alike — the command lock that
refuses a person's command while HQPlayer builds a setting, the build watch,
and the intent clock that drops an item a newer intent superseded. The
backend takes the selected HQPlayer's link at construction; another address
is another link.

HQPlayer's playlist is the canonical queue without its gaps, in the same
order: a gap is a slot this HQPlayer does not hold — nothing it can open (a
file held only in another HQPlayer's library, one that left the library), or
a file it refused while it took the rest of its batch. A gap stays in the
queue, named in its playlist, and is never handed to HQPlayer; it belongs to
the slot on this output, read again at every mirroring. A queue slot and
HQPlayer's entry are told apart by type (Entry), and the layout (_Layout) is
the one translation between them.

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
import threading
import time
from bisect import bisect_left, bisect_right
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlsplit

import psycopg2

from config import settings
from hqplayer_client import (DSP_KNOBS, STATUS_READINGS, HQPlayerClient, PlaybackState,
                             file_path_to_uri, redact_uri, uri_to_file_path)

from playback import hqp_diagnostics as diag
from playback import hqp_link
from playback import hqp_load
from playback import hqp_meter
from playback import queue as queue_mod
from playback.base import Capabilities, PlaybackStatus, PlayerBackend, ReorderPlan
from playback.hqp_benchmark import _building
from playback.hqp_link import (NOT_ANSWERING, QUESTION_WAIT_S, UNFINISHED, Building, Dropped,
                               GaveUp, HqpLink, Item, NotAnswering, background, through)
from playback.queue import CanonicalQueue, QueueItem

logger = logging.getLogger(__name__)


def _notify_notices() -> None:
    from db_pool import db_execute
    db_execute("NOTIFY sautium_notices")


def change_dsp(knobs: Dict[str, Any]) -> Dict[str, Any]:
    """The one DSP change path — the HQPlayer screen's /config and the
    assistant's DSP tools — on the selected HQPlayer's link, as one intent
    (dsp). Each knob's Set*, in DSP_KNOBS' order, then a <Status/> held on
    the same connection until HQPlayer answers: a build ends there —
    HQPlayer answers a Set* before it builds and falls silent on the command
    after it, or holds the Set*'s own answer — and the next knob goes only
    then. A refused knob is reported in HQPlayer's words and the rest still
    go; a held Status HQPlayer did not answer — a build it did not finish, a
    connection that broke — an output lent meanwhile, and a newer intent that
    supersedes the rest of the change (another DSP change, a switch away:
    SUPERSEDED_BY) end the change there, the knobs after it reported as not
    sent. When HQPlayer builds past BUILDING_AFTER_S the change answers at
    once: what was taken and refused so far, what is still to go, and the
    build; the rest is carried out behind the answer and its outcome rides
    the player status (BuildWatch.report). Built, a change made while
    HQPlayer played resumes the spot it played if HQPlayer came back
    stopped, and leaves one that plays on alone; another controller's entry
    — slot 0 on the status — is not ours to resume. Raises OutputHeld while
    the output is lent, Building while HQPlayer builds."""
    from playback.manager import manager
    pre = manager.latest_status
    entry = (manager.queue.item_at(int(pre.get("track_index") or 0))
             if pre.get("state") == "playing" else None)
    spot = ((entry, int(pre.get("position") or 0), manager.queue.generation)
            if entry is not None else None)
    return _DspChange(hqp_link.selected(), {k: v for k, v in knobs.items() if v is not None},
                      spot).answer()


class _DspChange:
    """change_dsp's run: a thread of its own, so the caller can be answered
    the moment a build begins (BuildWatch.waking)."""

    def __init__(self, link: HqpLink, knobs: Dict[str, Any], spot: Optional[tuple]):
        self.link = link
        # The attachment the change is made in: its outcome is shown there only
        self.attachment = link.attachment
        self.knobs = knobs
        self.spot = spot
        self.n: Optional[int] = None
        self.applied: Dict[str, Any] = {}
        self.failed: Dict[str, str] = {}
        self.error: Optional[Exception] = None
        self.done = False
        self.ready = threading.Event()

    def answer(self) -> Dict[str, Any]:
        threading.Thread(target=self._run, daemon=True, name="hqp-dsp-change").start()
        self.ready.wait()
        if self.done and self.error is not None:
            raise self.error
        if self.done:
            return {"ok": not self.failed, "applied": dict(self.applied),
                    "failed": dict(self.failed), "building": None, "pending": []}
        return {"ok": None, "applied": dict(self.applied), "failed": dict(self.failed),
                "building": self.link.build.state, "pending": self._pending()}

    def _pending(self) -> list:
        return [k for k in DSP_KNOBS if k in self.knobs
                and k not in self.applied and k not in self.failed]

    def _not_sent(self, why: str) -> None:
        self.failed.update({k: f"not sent: {why}" for k in self._pending()})

    def _run(self) -> None:
        from playback.manager import OutputHeld, manager
        link = self.link
        status = None
        try:
            with link.command("dsp", item="dsp_knob") as self.n, link.build.waking(self.ready):
                # Checked under the lock the hold detaches the backend under:
                # a change that passed it is through before a run sends
                # anything — unless the detach went on through a build of
                # the change's own, asked again after each knob
                if manager.held is not None:
                    raise OutputHeld(manager.held)
                hqp = link.client
                try:
                    for knob, taken, refusal in hqp.set_knobs(self.knobs):
                        if taken is None:
                            self.failed[knob] = refusal
                        else:
                            self.applied.update(taken)
                        status = hqp.get_status(wait=hqp_link.BUILD_LIMIT_S, watched=True)
                        why = (UNFINISHED if status is None and hqp.timed_out
                               else f"HQPlayer did not answer ({hqp.refusal()})" if status is None
                               else str(OutputHeld(manager.held)) if manager.held is not None
                               else None)
                        if why is not None:
                            self._not_sent(why)
                            break
                except (Building, Dropped) as e:
                    # Its own build HQPlayer did not finish, or a newer intent
                    # took over the knobs still to go
                    status = None
                    self._not_sent(str(e))
        except Exception as e:      # the caller's to answer: refused, held, or HQPlayer away
            self.error = e
        finally:
            built = self.ready.is_set()
            self.done = True
            self.ready.set()
        if self.error is not None:
            if built:
                logger.warning("HQPlayer DSP change ended after a build: %s", self.error)
            return
        if built:
            link.build.report(dict(self.applied), dict(self.failed), self.attachment)
        if self.spot is not None and status is not None and status.state == PlaybackState.STOPPED:
            self._resume(*self.spot)

    def _resume(self, entry: QueueItem, position: int, generation: int) -> None:
        """HQPlayer came back stopped from a change made while it played (an
        HQPlayer that stops the transport for a build and never restarts it):
        back to the spot, as an item of the change (resume) — dropped once a
        newer intent supersedes it, or the queue it was made for was
        replaced, or the output is no longer this HQPlayer. Behind the
        change's answer: a build begun meanwhile (the owner's next change) is
        waited out."""
        from playback.manager import manager
        link = self.link
        backend = manager.active
        item = link.item("resume", self.n, queue=manager.queue, generation=generation)

        def needed() -> Optional[str]:
            if manager.active is not backend or getattr(backend, "_link", None) is not link:
                return "the output changed"
            return link.superseded(item)
        # Nobody to raise to: its attempt (the playback trace) holds what went wrong
        if background("playback not resumed after a DSP change",
                      lambda: backend.resume_after_rebuild(item, entry, position),
                      needed=needed, until=lambda: needed() is not None):
            logger.info("resumed playback after a DSP change (%s at %d s)", entry.title, position)


def _uri_in_playlist(hqp: HQPlayerClient, uri: str) -> bool:
    """Is `uri` already in HQPlayer's playlist? Used to avoid re-adding
    (duplicating) a track whose add LANDED but whose response was lost to a
    slow read-timeout. False when the playlist can't be read; the link's own
    refusals end the sequence it serves. On a held connection."""
    try:
        return any(t.get("uri") == uri for t in (hqp.get_playlist() or []))
    except (Building, Dropped):
        raise
    except (BrokenPipeError, ConnectionError, OSError):
        return False


def _poll_interval(failures: int) -> float:
    """Seconds to the next status read: 1 while HQPlayer answers, then 2, 4,
    8, 15. The exponent stops where the cap does: a float power overflows
    past 2**1023, and a night with HQPlayer switched off — a miss every
    ~17 s — got there and killed the poller (OverflowError, 2026-10-04)."""
    return 1.0 if failures <= 0 else min(2.0 ** min(failures, 4), 15.0)


def _add_uris_with_retry(link: HqpLink, uris: list[str], *,
                         clear_first: bool = False) -> tuple[list, int]:
    """Append URIs to the HQPlayer playlist, one PlaylistAdd each. MUST be
    called inside a command sequence on `link` (HqpLink.command); its
    refusals — a build, an item superseded, a link let go — end the batch
    there and are raised.

    `playlist_add` returns False (it does not raise) when the control socket
    is down, so a plain `for` loop silently drops tracks while the endpoint
    still reports success — exactly the failure that made a Queue action add
    only the one track that landed before HQPlayer stalled. Here every add
    is accounted for: taken; refused in HQPlayer's words — final, the same
    file is refused again, and its connection stays; or lost with a
    connection that broke — then the playlist is looked at first, since an
    add that LANDED with its answer lost must not be added twice, and it is
    sent once more on a fresh one. A silence ends the batch: every add after
    it would wait out the same silence (a Pi 5 playing a setting it could not
    keep up with answered nothing for minutes, and a queue of 19 took five
    of them to fail).

    Returns one flag per URI, in order — taken or not — and where the batch
    stopped because HQPlayer did not answer or could not be reached: the
    URIs from that index on were not handed over (len(uris) when none was
    left; a refusal is no such stop — what is False before it was refused).

    `clear_first=True` issues clear=True on the first URI (replace-queue
    semantics), and on each after it until HQPlayer takes one: a file it
    refuses clears nothing, and what follows must not land behind the
    playlist it held. Once one is taken the rest append, so a mid-batch
    reconnect never re-clears already-added tracks.
    """
    taken: list = []
    refused = 0
    unsent = len(uris)
    for i, uri in enumerate(uris):
        clear = clear_first and not any(taken)
        ok = answered = False
        result, refusal = None, ""
        for attempt in (1, 2):
            silent = False
            hqp = link.client
            try:
                ok = hqp.playlist_add(uri, clear=clear)
                if not ok:
                    # Read now: the playlist check below replaces it.
                    result, refusal = _refusal(hqp)
                    answered, silent = hqp.answered, hqp.timed_out
            except (Building, Dropped):
                raise
            except NotAnswering as e:
                ok, silent = False, True
                result, refusal = "lost", str(e)
            except (BrokenPipeError, ConnectionError, OSError) as e:
                ok = False
                result, refusal = "refused", str(e)
            if ok or answered or silent:
                break
            if attempt == 1 and not clear and _uri_in_playlist(hqp, uri):
                # Its connection broke under it, yet it LANDED: a re-add
                # would DUPLICATE the track — preview URIs are unique, so a
                # present URI means the first add took
                ok = True
                break
        diag.note_add(uri, ok, None if ok else result, "" if ok else refusal)
        taken.append(ok)
        if ok:
            continue
        logger.warning("playlist_add failed: %s — %s", redact_uri(uri), refusal)
        if not answered:
            # Not a refusal: HQPlayer did not answer, or could not be reached
            unsent = i
            break
        refused += 1
    # Each reason the batch is short is told: the silence that stopped it,
    # and what HQPlayer refused before it
    if unsent < len(uris):
        logger.warning("HQPlayer is not answering — %d of %d tracks were not handed over",
                       len(uris) - unsent, len(uris))
    if refused and _stream_mode():
        logger.warning(
            "HQPlayer refused %d of %d media URLs — an HQPlayer elsewhere "
            "fetches them from us, so that usually means it cannot reach %s:%d (a firewall, or the wrong "
            "LAN address in MEDIA_PROXY_ADVERTISED_HOST / SAUTIUM_HOST_IPS)",
            refused, len(uris), hqp_media_host(), settings.media_proxy_port)
    return taken[:unsent] + [False] * (len(uris) - unsent), unsent


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


def _hqp_safe(link: HqpLink, action, command: str) -> bool:
    """Run one HQPlayer command (stop / play / clear / remove) of a command
    sequence on `link` tolerantly: nothing is raised but the link's own
    refusals — a build, an item a newer intent superseded, a link let go —
    which end the sequence; a connection it could not have is HQPlayer not
    taking it. Frames a resilient multi-add so a churning control port can't
    abort the whole operation at its stop()/play() bookends before the add
    even runs. Returns whether HQPlayer took it: one it did not is logged in
    its words, and a mirror step it did not take leaves its playlist no
    mirror — the caller says so. A command that could not be sent is a step
    of the play intent it served — it never reached HQPlayer, so its client
    heard nothing."""
    try:
        hqp = link.client
        if action(hqp):
            return True
        logger.warning("HQPlayer did not take %s: %s", command,
                       diag.redact_text(hqp.refusal()))
        return False
    except (Building, Dropped):
        raise
    except (BrokenPipeError, ConnectionError, OSError) as e:
        attached = link.attached
        if attached is not None:
            attached._refused(command, str(e))
        return False


def _slot_key(item: QueueItem) -> tuple:
    """What a slot is, as the hand-over ledger indexes it (a QueueItem is no
    dict key): the track, the file, and the way this output opens it."""
    src = item.opener()
    return (item.track_id, item.media_file_id,
            src.get("path") or src.get("token") or src.get("uri"), src.get("cue_start"))


# Why a slot is a gap when HQPlayer refused its file while it took the rest
# of the batch: it stays one until the next full mirror hands it over again
REFUSED_HERE = "this HQPlayer did not open it"


@dataclass(frozen=True, order=True)
class Entry:
    """A place in HQPlayer's playlist, 1-based — never a queue slot: a slot
    HQPlayer does not hold (a gap) has no entry, and the entries after it sit
    that much earlier. One comes from a slot through the layout
    (_Layout.entry), or from what HQPlayer says of its own playlist — the
    entry its status reads, a place in what PlaylistGet listed; only _select
    and _remove hand one to the client."""
    n: int

    def __add__(self, k: int) -> "Entry":
        return Entry(self.n + k)

    @staticmethod
    def read(status) -> Optional["Entry"]:
        """The entry HQPlayer's status names, if it names one."""
        return Entry(status.track_index) if status.track_index >= 1 else None


class _Layout:
    """The queue as this HQPlayer holds it — its slots in order, the gaps
    left out — and the one translation between a queue slot and HQPlayer's
    entry."""

    def __init__(self, items: list, gap: Callable[[QueueItem], Optional[str]]):
        self.items = items
        self.slots = [s for s, it in enumerate(items, 1) if gap(it) is None]
        self.handed = [items[s - 1] for s in self.slots]

    def entry(self, slot: Optional[int]) -> Optional[Entry]:
        """HQPlayer's entry for the queue's `slot`; None for a gap."""
        i = bisect_left(self.slots, slot) if slot is not None else len(self.slots)
        return Entry(i + 1) if i < len(self.slots) and self.slots[i] == slot else None

    def slot(self, entry: Optional[Entry]) -> Optional[int]:
        """The queue's slot at HQPlayer's `entry`; None past its last one."""
        return (self.slots[entry.n - 1]
                if entry is not None and 1 <= entry.n <= len(self.slots) else None)

    def near(self, entry: Entry) -> int:
        """The slot to look for HQPlayer's `entry` from: its own, or the last
        one HQPlayer holds when the entry lies past it (1 for none)."""
        return self.slot(Entry(min(entry.n, len(self.slots)))) or 1

    def held(self, slot: int, by: int = 1) -> Optional[int]:
        """The first slot HQPlayer holds from `slot` on, going `by`."""
        while 1 <= slot <= len(self.items):
            if self.entry(slot) is not None:
                return slot
            slot += by
        return None

    def behind(self, q: int) -> list:
        """The slots HQPlayer holds after the queue's slot `q`, in order."""
        return self.handed[bisect_right(self.slots, q):]

    def before(self, q: int) -> list:
        """The slots HQPlayer holds before the queue's slot `q`, in order."""
        return self.handed[:bisect_left(self.slots, q)]


def _select(h: HQPlayerClient, entry: Entry) -> bool:
    """SelectTrack: the one way a place in HQPlayer's playlist reaches it to
    start there — never a queue slot."""
    if not isinstance(entry, Entry):
        raise TypeError(f"SelectTrack takes HQPlayer's entry, not {entry!r}")
    return h.select_track(entry.n)


def _remove(h: HQPlayerClient, entry: Entry) -> bool:
    """PlaylistRemove: the one way a place in HQPlayer's playlist reaches it
    to remove — never a queue slot."""
    if not isinstance(entry, Entry):
        raise TypeError(f"PlaylistRemove takes HQPlayer's entry, not {entry!r}")
    return h.playlist_remove(entry.n)


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


def _skip_to_first_playable(hqp: HQPlayerClient, added: int) -> None:
    """If the first queued track didn't start (e.g. a [Vinyl] placeholder path that
    isn't real audio), skip ahead until one plays. Best-effort, on a held
    connection; the link's own refusals end it."""
    try:
        time.sleep(0.5)
        status = hqp.get_status()
        if status and status.state == PlaybackState.STOPPED and added > 1:
            logger.warning("play: first track didn't start, skipping ahead")
            for skip_idx in range(2, min(added + 1, 6)):
                _select(hqp, Entry(skip_idx))
                hqp.play()
                time.sleep(0.5)
                status = hqp.get_status()
                if status and status.state != PlaybackState.STOPPED:
                    logger.info(f"play: track {skip_idx} started")
                    break
    except (Building, Dropped):
        raise
    except (BrokenPipeError, ConnectionError, OSError) as e:
        logger.info("play: skipping ahead stopped: %s", e)


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
            # Produce EAGERLY — _uri_for runs OUTSIDE a command sequence by contract,
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
    """HQPlayer output: the selected HQPlayer, driven through its link
    (playback.hqp_link), taken once at construction. Status acquisition = 1 s
    poll, asked on the link's one connection like every other request — the
    control protocol cannot push; documented boundary exception to the
    no-polling rule (HARDWARE-TIERS §2.6). The canonical queue is mirrored
    one-way INTO HQPlayer's native playlist via the queue_* hooks."""

    id = "hqplayer"
    # What the play-intent gate tells the user when reachable() says no.
    offline_hint = ("start HQPlayer, or pick another output in Settings → "
                    "Audio output (an HQPlayer Embedded in trial mode stops "
                    "every 30 minutes and must be restarted)")

    def __init__(self, emit, queue: CanonicalQueue):
        super().__init__(emit)
        self._link: HqpLink = hqp_link.selected()
        self._queue = queue
        self._endpoint_row: dict = {}
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._failures = 0
        # Silences past a build HQPlayer did not finish: no failures, but the
        # poller backs off on them all the same
        self._silent_misses = 0
        # The connections HQPlayer had ended (the link's client) when the
        # poller last had an answer: one more since is HQPlayer back — a
        # restart drops the connection — never a reconnect of our own
        self._losses = 0
        # What HQPlayer's playlist is to the canonical queue:
        #   "step"  its mirror;
        #   "tail"  its mirror from the entry it reads to its end, the part
        #           before differing — what follows goes to HQPlayer, a
        #           person's next action that picks a slot rebuilds it;
        #   "lost"  the queue took what HQPlayer did not get — through a
        #           silence (`_lost_by`), HQPlayer gets the queue back at its
        #           next answer (_reconverge, once per loss); through a
        #           refusal of everything, the next play press tries again;
        #   "drift" edited outside this node, or a mirror step HQPlayer did
        #           not take: never reconciled on its own — the play press
        #           re-mirrors.
        # Its entry names our slot through the layout only "in step";
        # elsewhere what the entry it reads is does (_playing).
        self._mirror = "step"
        # The files HQPlayer refused while it took the rest of their batch,
        # as (slot, the way in it refused — _slot_key): gaps of this output,
        # never of the slot (`gap`), until the next full mirror hands them
        # over again — or the slot is re-bound to another copy
        self._refusals: list = []
        self._lost_by: Optional[str] = None
        self._reconverge_due = False
        self.mirror_losses = 0
        self._drift_logged = False
        # The last verdict of _playing, by (entry, HQPlayer's index, queue
        # version): a foreign entry played for an hour is looked for in the
        # queue once, not every second.
        self._verdict: Optional[tuple] = None
        # Lost sight of HQPlayer (a dropped socket, failed polls): its
        # playlist is read on the first answer, and the flag stays raised
        # until that read succeeds, so one failed PlaylistGet right after a
        # restart cannot let an empty playlist pass as an external edit.
        self._verify_mirror = False
        self._gone = False
        # The proxy address HQPlayer reaches, fixed per attach: a DNS + route
        # lookup once, not per queued item.
        self._url_host = hqp_media_host()
        # (queue item, second) the next play press starts at: after an output
        # switch that found HQPlayer stopped (resume_at), or once HQPlayer
        # took the queue afresh while not playing (_converge_mirror,
        # _rebuild) — a paused HQPlayer is left stopped there, never played
        # to be paused again. It names the item, not a slot (_spot_at): an
        # insert or a removal before it moves its index, an append after it
        # nothing, and it goes with its item — the queue keeps item objects
        # through a reorder, a re-bind and a radio clear. A start taken
        # erases it; a command refused, as busy or by HQPlayer, leaves it.
        self._spot: Optional[tuple] = None
        self._spot_lock = threading.Lock()
        # HQPlayer was found stopped at the attach (its queue mirrored), so
        # the slot an output switch keeps is a spot (resume_at)
        self._stopped_at_attach = False
        # A play press is taking the queue afresh (_press): one more joins it
        self._pressing = False
        self._press_lock = threading.Lock()
        # Which attachment of the link this backend is (HqpLink.attach): a DSP
        # change's outcome is shown only to the attachment it was made in
        self._attachment: Optional[int] = None
        # The latest status emitted: what the poller says again, with the
        # build, while HQPlayer is building and answers nothing.
        self._last_tick: Optional[PlaybackStatus] = None
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
        self._last_entry: Optional[Entry] = None
        self._info: dict = {}
        # When a listen leaves a DSP sample (playback.hqp_load).
        self._sampler = hqp_load.Sampler()
        # Set by shutdown: from then on no command of this instance reaches
        # HQPlayer (_check_attached, under the command lock), and no poller
        # starts (start, under _life).
        self._detached = False
        self._life = threading.Lock()
        # A cut-short benchmark run is put back once HQPlayer answers: at the
        # attach if GetInfo did, else at the first status that does
        self._recover_due = False
        # What HQPlayer says of itself is asked with the poller's first
        # answer when the attach could not ask it (a build)
        self._info_due = False

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        link = self._link
        # Attached: the Stop a switch away owed HQPlayer is superseded
        self._attachment = link.attach(self)
        if link.state == "closed":
            return              # the node is stopping (hqp_link.close_all): nothing to start
        # HQPlayer builds and answers nothing: no question is put to it now —
        # each would wait out its timeout — and what it holds is read once
        # it answers (the poller's mirror check), never replaced in its
        # silence. GetInfo then comes with its first answer.
        building = link.build.silent() is not None
        if not building:
            try:
                with link.asking() as hqp:
                    self._info = hqp.get_info(wait=QUESTION_WAIT_S) or {}
            except Building:
                building = True
            except (BrokenPipeError, ConnectionError, OSError) as e:
                logger.info("HQPlayer GetInfo at attach failed: %s", e)
        if self._detached:
            return              # switched away while the attach waited on HQPlayer
        self._note_info()
        if self._info:
            self._recover()
        else:
            self._recover_due = True
            self._info_due = building
        if building:
            self._verify_mirror = True
        else:
            self._stopped_at_attach = self._mirror_on_attach()
        self._library_check()
        with self._life:
            # shutdown() detaches under this lock: past it, a poller started
            # here is one it joins — never one nothing stops
            if self._detached:
                return
            # Where its meter stream lives: the control port + 1 (Signalyst's
            # SDK). Opened only while a page has the meter open.
            hqp_meter.meter.attach(link.host, link.port + 1)
            self._losses = link.client.losses
            self._running = True
            self._thread = threading.Thread(target=self._poll_loop, daemon=True,
                                            name="hqp-status-poller")
            self._thread.start()
        logger.info("HQPlayer status poller started")

    def _mirror_on_attach(self) -> bool:
        """Output switch with a live queue (§2.6): HQPlayer becomes the
        mirror of the canonical queue — afresh, no autoplay, the user presses
        play; what it refuses of it follows the mirror's rules (_remirror).
        Without this the switch left HQPlayer's playlist empty and the play
        button dead. Skipped while HQPlayer is actively PLAYING/PAUSED (a
        backend restart mid-listening — clearing would cut live audio); a
        stopped HQPlayer gets the queue even if it holds a stale playlist.
        One that leaves the Status unanswered, or builds meanwhile, is left
        to the poller's mirror check at its first answer, as at an attach
        during a build — never cleared into its silence. Runs off any queue
        mutation: the output change and the play gate take the queue lock
        before the attach. Returns whether HQPlayer was found stopped and
        took the queue."""
        if self._adopt_hqp_playlist() or not len(self._queue):
            return False
        try:
            with self._link.asking() as hqp:
                st = hqp.get_status(wait=QUESTION_WAIT_S)
        except Building:
            st = None
        except (BrokenPipeError, ConnectionError, OSError) as e:
            logger.info("HQPlayer's Status at attach: %s", e)
            st = None
        if st is None or not st.state.known:
            self._verify_mirror = True
            return False
        if st.state in (PlaybackState.PLAYING, PlaybackState.PAUSED):
            self._register_serving()
            return False
        try:
            return self._remirror()
        except Building:
            self._verify_mirror = True
        except (BrokenPipeError, ConnectionError, OSError) as e:
            logger.warning("canonical mirror on attach failed: %s", e)
        return False

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
        settings: put back on this link the next time HQPlayer answers — at
        the attach, before anything reads its playlist, when the poller sees
        it come back, and at the poller's next answer after a put-back
        HQPlayer did not take (hqp_benchmark.recover). A failure costs the
        recovery, never the attach or the poll."""
        if self.endpoint_id is None:
            return
        from playback import hqp_benchmark
        try:
            hqp_benchmark.recover(self.endpoint_id, self._link, again=self._recover_later)
        except (OSError, RuntimeError, psycopg2.Error) as e:
            logger.warning("benchmark recovery failed: %s", e)

    def _recover_later(self) -> None:
        self._recover_due = True

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
        hqp_library.request_sync(self._link.host, self._link.port)

    def shutdown(self) -> None:
        link = self._link
        with link.lock.detaching(), self._life:
            # A command already on its way is answered before the lock is
            # ours — but never waited out through a build: that goes on
            # watched and ends on its own, and what its sequence still had to
            # send is superseded by the detach (HqpLink.detach). One sent
            # later by whoever took this backend before the switch (a play
            # intent) is refused.
            self._detached = True
        self._running = False
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=3)
        if link.attached is self:
            hqp_meter.meter.detach()
        link.detach(self)
        with self._att_lock:
            att, self._attempt = self._attempt, None
            if att is not None:
                att.request_end("shutdown")
                self._closing.append(att)
        self._settle(None, None)
        logger.info("HQPlayer status poller stopped")

    def release(self) -> Optional[str]:
        """Switched away from: HQPlayer stops if it plays or pauses an entry
        of what it was given — the queue as it stands at the switch, never
        as it is once the Stop goes — at once, or once it answers
        (HqpLink.release): the switch waits a moment at most. Returns what
        the person is told when the Stop stays owed."""
        given = self._layout()
        return self._link.release(lambda status: self._reads_any(status, given))

    def _reads_any(self, status, given: _Layout) -> bool:
        """Does HQPlayer read an entry of what it was given? With nothing
        that names the entry (no URI from this build) its place stands while
        the playlist was our mirror."""
        readings = self._readings(status.uri)
        if not readings:
            reads = Entry.read(status)
            return self._mirror == "step" and given.slot(reads) is not None
        return any(_reads_item(r, it) for r in readings for it in given.handed)

    def gap(self, item: QueueItem) -> Optional[str]:
        """Why this HQPlayer does not hold the slot — nothing here it can
        open, as the slot's way in says (playback.substitute: held in another
        HQPlayer's library, its file gone), or it refused the file while it
        took the rest of the batch (REFUSED_HERE) — or None: it holds it."""
        src = item.opener()
        if src["kind"] == "unplayable":
            return src["reason"]
        if any(item is it and key == _slot_key(item) for it, key in self._refusals):
            return REFUSED_HERE
        return None

    def _layout(self, items: Optional[list] = None) -> _Layout:
        """The queue — or `items`, a new order of it — as HQPlayer holds it."""
        return _Layout(self._queue.snapshot() if items is None else items, self.gap)

    def _ways_in(self, items: Optional[list] = None) -> None:
        """How this HQPlayer opens each slot, read again at every mirroring
        (playback.substitute.native_plays): a copy that came back is handed
        over, one that left is a gap — the queue's own slots, under the queue
        lock, or `items` it does not hold yet (a new queue, an append)."""
        from playback.substitute import native_plays

        def resolve(its: list) -> list:
            return native_plays(its, self.id, self.endpoint_id)
        if items is None:
            self._queue.reresolve(resolve)
            return
        for it, play in zip(items, resolve(items)):
            it.play = play

    def capabilities(self) -> Capabilities:
        return Capabilities(volume=True, volume_kind="db", seek=True, gapless=True)

    def healthy(self) -> bool:
        """Believed on the network. A playlist that is no mirror is no
        health matter: the play press rebuilds it (_press), and a switch
        away still owes the Stop."""
        return not self._gone

    def reachable(self) -> bool:
        """The play gate, on the link (HqpLink.probe): HQPlayer answered
        GetInfo naming a product. Busy is raised — a build (Building), a
        connection taken and left unanswered (NotAnswering) — and offline is
        False, said in the playback trace either way."""
        try:
            why = self._link.probe()
        except NotAnswering as e:
            self._unreachable(f"GetInfo: {e}")
            raise
        if why is None:
            return True
        self._unreachable(f"GetInfo: {why}")
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
        # Only for an HQPlayer the attach found stopped: one that plays or
        # builds keeps its own place.
        item = self._queue.item_at(index)
        if self._stopped_at_attach and item is not None:
            self._set_spot(item, 0)

    def _set_spot(self, item: QueueItem, position: int) -> None:
        with self._spot_lock:
            self._spot = (item, position)

    def _spot_at(self) -> Optional[tuple]:
        """The spot as (its slot now, its second); None when there is none
        — or its item left the queue, which takes the spot with it."""
        held = self._spot
        if held is None:
            return None
        idx = self._queue.index_of(held[0])
        if idx is None:
            self._take_spot(held)
            return None
        return idx, held[1]

    def _take_spot(self, held: Optional[tuple] = None) -> Optional[tuple]:
        """Erase the spot — only if it is still `held`, when given: one a
        later action set stays — and return what it was."""
        with self._spot_lock:
            spot = self._spot
            if held is None or spot is held:
                self._spot = None
            return spot

    def poke(self) -> None:
        """Wake the poller for an immediate re-poll after a command."""
        self._wake.set()

    def lose_mirror(self, why: str, *, by: str = "silence") -> None:
        """HQPlayer's playlist is no mirror of the queue any more: queue edits
        are the queue's alone from now on. Lost through a silence (an
        unanswered add, a filler whose wait gave up, a restart) HQPlayer gets
        the queue back when it answers again (_reconverge, once per loss);
        lost through a refusal of everything — it answered, and took nothing
        — the next play press tries again."""
        self._mirror, self._lost_by = "lost", by
        self._reconverge_due = by == "silence"
        self.mirror_losses += 1
        logger.warning("%s — %s", why, "HQPlayer gets the queue back once it answers again"
                       if by == "silence" else "the next play press hands it over again")
        self.poke()

    def _in_step(self) -> None:
        self._mirror, self._lost_by, self._reconverge_due = "step", None, False

    def _drifted(self, why: str) -> None:
        """HQPlayer's playlist differs from the queue by an edit made outside
        this node, or a mirror step it did not take: never reconciled on its
        own (a lost mirror stays lost)."""
        if self._mirror in ("step", "tail"):
            self._mirror = "drift"
        if not self._drift_logged:
            logger.warning("%s — the Sautium queue is authoritative", why)
            self._drift_logged = True

    def _reconverge(self, press: bool = False) -> None:
        """HQPlayer answers again while its playlist is no mirror: it gets the
        queue back, under the queue lock, as background work (a build met is
        waited out). Playing or paused on an entry of ours, what lies behind
        it is replaced, the sound never stopped; stopped, it takes the queue
        afresh and the play press resumes its slot; on an entry not ours it
        stays lost, and the play press re-mirrors. After a Play that resumed
        a pause (`press`) a drifted playlist is repaired the same way."""
        from playback.manager import manager
        try:
            background("HQPlayer did not get the queue back",
                       lambda: manager.mirror_step(self, lambda: self._follow_queue(press)),
                       needed=lambda: "HQPlayer was detached" if self._detached else None,
                       until=lambda: self._detached)
        finally:
            self.poke()

    def _follow_queue(self, press: bool = False) -> None:
        """_reconverge's step, under the queue lock. A read HQPlayer does not
        answer leaves the mirror lost, due again at its next answer."""
        if self._mirror != "lost" and not (press and self._mirror == "drift"):
            return              # in step again meanwhile (a replace, the canary)
        layout = self._layout()

        def read(h: HQPlayerClient) -> tuple:
            status = h.get_status()
            playlist = h.get_playlist()
            return status, (playlist if h.last_error is None else None)
        status, playlist = self._cmd(read)
        if status is None or playlist is None or not status.state.known:
            self._reconverge_due = self._lost_by == "silence"
            return
        if (len(playlist) == len(layout.handed)
                and self._first_divergence(playlist, layout.handed) is None):
            self._in_step()                             # it holds the queue as it is
            return
        playing = self._playing(status, mutating=True)
        if status.state in (PlaybackState.PLAYING, PlaybackState.PAUSED):
            reads = Entry.read(status)
            if playing is None or playing[2] or reads is None or reads.n > len(playlist):
                return              # an entry not ours: the play press re-mirrors
            self._repair_tail(reads, playing[0], playlist, layout)
        elif status.state == PlaybackState.STOPPED:
            self._remirror(at=playing[1] if playing is not None and not playing[2] else None)

    def _repair_tail(self, reads: Entry, q: int, playlist: list, layout: _Layout) -> None:
        """HQPlayer reads our slot `q` at its entry `reads`: what lies behind
        that entry is made what HQPlayer holds of the queue behind the slot —
        removed from the first entry that differs and added again, as
        play-next does: the entry read is never touched, the sound never
        stops. In step when the part before it was the queue's too, else a
        tail mirror (§ _mirror). Under the queue lock."""
        after, want = playlist[reads.n:], layout.behind(q)
        first = self._first_divergence(after, want)
        keep = min(len(after), len(want)) if first is None else first - 1
        items = want[keep:]
        uris = [self._uri_for(it) for it in items]

        def repair(hq: HQPlayerClient):
            for _ in range(len(after) - keep):
                if not _hqp_safe(self._link, lambda c: _remove(c, reads + (keep + 1)),
                                 "PlaylistRemove"):
                    return None
            return _add_uris_with_retry(self._link, uris)
        out = self._cmd(repair)
        self.poke()
        if out is None:
            self._drifted("HQPlayer did not take a removal behind the entry it reads")
        elif self._took(items, *out, "behind the entry it reads"):
            before = layout.before(q)
            head = (reads == layout.entry(q)
                    and self._first_divergence(playlist[:reads.n - 1], before) is None)
            if head:
                self._in_step()
            else:
                self._mirror, self._lost_by, self._reconverge_due = "tail", None, False
            logger.info("HQPlayer follows the queue behind the entry it reads (%s)",
                        "in step" if head else "a tail mirror")

    def _remirror(self, *, at: Optional[QueueItem] = None, position: int = 0) -> bool:
        """A stopped HQPlayer takes the queue afresh (a rebuild): each slot's
        way in read again, every slot it holds handed over — a slot it
        refuses is a gap until the next full mirror (_took). The spot, when
        none was kept, is `at` — the slot it stood on — at `position`. Under
        the queue lock. Returns whether HQPlayer holds the queue again."""
        self._ways_in()
        self._refusals = []
        layout = self._layout()
        taken, unsent, _ = self._afresh([self._uri_for(it) for it in layout.handed], "rebuild")
        if not self._took(layout.handed, taken, unsent, "of the queue"):
            return False
        self._in_step()
        if self._spot_at() is None and at is not None and self._queue.index_of(at) is not None:
            self._set_spot(at, position)
        logger.info("HQPlayer took the queue afresh (%d of %d tracks)", sum(taken),
                    len(layout.items))
        return True

    def _took(self, items: list, taken: list, unsent: int, what: str) -> bool:
        """The outcome of a mirror batch — `items` the slots handed over, which
        the queue holds already — under the queue lock: what HQPlayer refused
        is a gap of this output, in the queue still, until the next full
        mirror hands it over again; cut short by a silence, the mirror is lost
        (the queue keeps them); refused whole, lost by the refusal. One notice
        for what it refused — a batch it took whole ends the last one.
        Returns whether HQPlayer holds them now."""
        sent, answered = items[:unsent], taken[:unsent]
        self._note_refused(sent, answered)
        self._refusals.extend((it, _slot_key(it)) for it, t in zip(sent, answered) if not t)
        if unsent < len(items):
            self.lose_mirror(f"HQPlayer took {sum(taken)} of the {len(items)} tracks {what}")
            return False
        if items and not any(taken):
            self.lose_mirror(f"HQPlayer refused all {len(items)} tracks {what}", by="refusal")
            return False
        return True

    def _note_refused(self, items: list, taken: list) -> None:
        """One notice for a batch HQPlayer refused files of: what (the album,
        or the first track, and how many) — the channel words its causes. A
        batch it took whole ends the last one."""
        if not items:
            return
        refused = [it for it, t in zip(items, taken) if not t]
        if (diag.note_refused(refused, len(items), stream=_stream_mode()) if refused
                else diag.note_taken()):
            _notify_notices()

    def _lost_sight(self) -> None:
        """A build's held answer broke — HQPlayer went away or restarted
        mid-build (BuildWatch): its playlist is read on its first answer,
        and the poller, woken, asks again."""
        self._verify_mirror = True

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
            with self._link.asking() as hqp:
                hqp_tracks = hqp.get_playlist()
        except (BrokenPipeError, ConnectionError, OSError) as e:
            logger.info("adopt: HQPlayer playlist unavailable (%s)", e)
            return False
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
        link = self._link
        tick = 0
        said_unfinished = said_silent = None
        while self._running:
            silent = link.silent_since
            if silent is not None and silent != said_silent:
                # HQPlayer answered nothing past a short silence's wait (a
                # Stop's teardown): said at once, then asked with the
                # back-off of any silence — its answer ends it
                said_silent = silent
                self._emit_silence({"not_answering": NOT_ANSWERING})
            building = link.build.state
            if building is not None and building["silent"]:
                # HQPlayer answers nothing while it builds: said from the
                # watch, not asked — the watch wakes the poller at the end.
                # One it did not finish is said once, then asked: HQPlayer
                # may be back, and its answer ends it (_register_failure
                # says it again on each silence, an HQPlayer gone is counted).
                unfinished = building.get("unfinished")
                if not unfinished or building is not said_unfinished:
                    try:
                        if building.get("not_answering"):
                            # The Status a switch away holds: no build of ours
                            self._emit_silence({"not_answering": NOT_ANSWERING})
                        else:
                            self._emit_building(building)
                    except Exception:
                        logger.exception("HQPlayer's build was not said on the status")
                    if unfinished:
                        said_unfinished = building
                if not unfinished:
                    self._wake.wait()
                    self._wake.clear()
                    continue
            status = None
            playing = None
            reads = None
            sample = None
            miss = None         # (what was said, whether HQPlayer kept the connection silent)
            came_back = learned = False
            att = self._attempt
            try:
                hqp_playlist = None
                with link.asking() as hqp:
                    status = hqp.get_status(wait=QUESTION_WAIT_S)
                    if status is None and not hqp.timed_out:
                        # The connection broke under the read: once more, on
                        # a fresh one — HQPlayer restarted answers it
                        status = hqp.get_status(wait=QUESTION_WAIT_S)
                    if status is None:
                        miss = (hqp.last_error.message if hqp.last_error else "no status answer",
                                hqp.timed_out)
                    else:
                        # HQPlayer went away and came back — it ended a
                        # connection since the last answer (closed, reset or
                        # refused it: a restart drops the connection), or
                        # this is the first answer after failed ticks — it
                        # was gone for longer than one poll (a power cycle, a
                        # trial restart taking its time). Never a reconnect of
                        # our own after a silence. Its playlist is read NOW,
                        # not at the next canary tick: a restart drops the
                        # mirror, and the play press must know before it
                        # lands on an empty list (live, 2026-09-27). A tick it
                        # did not answer is no return.
                        came_back = hqp.losses != self._losses or self._failures > 0
                        self._losses = hqp.losses
                        if came_back:
                            self._verify_mirror = True
                            self._library_check()
                        if came_back or self._info_due:
                            # Back — or picked while it built, its attach
                            # asking nothing: what it says of itself
                            self._info_due = False
                            info = hqp.get_info(wait=QUESTION_WAIT_S)
                            learned = bool(info)
                            self._info = info or self._info
                    if status is not None and (self._verify_mirror
                                               or tick % DRIFT_CHECK_EVERY == 0):
                        _, version, pending = self._queue.view()
                        mirror = (self._mirror, self.mirror_losses)
                        hqp_playlist = hqp.get_playlist()
                        # Judged against the queue and the mirror's state it
                        # was read with — never a read made while a mutation
                        # was in flight, nor one a commit or a loss of the
                        # mirror overtook (its stale "in step" would undo the
                        # loss: a replace HQPlayer refused whole, its Stop and
                        # clear gone), nor one HQPlayer did not answer (an
                        # empty list) — and before the slot below, which
                        # falls back on the verdict.
                        if (hqp.last_error is None and not pending
                                and self._queue.view()[1:] == (version, False)
                                and (self._mirror, self.mirror_losses) == mirror):
                            self._judge_mirror(hqp_playlist, status)
                            self._verify_mirror = False
                    if att is not None and status is not None:
                        att.tick(status, STATE_NAMES.get(status.state, "unknown"))
                        if att.due(time.time()) is not None:
                            # Judged on what HQPlayer holds NOW, read on the
                            # same hold of the connection.
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

                if came_back or learned:
                    self._note_info()
                if came_back:
                    hqp_meter.meter.hqplayer_back()
                # Only when it answered: a trial-stopped Embedded takes the
                # connection and closes it at once, tick after tick.
                if status is not None and (came_back or self._recover_due):
                    self._recover_due = False
                    self._recover()

                if status is not None and not status.state.known:
                    # A state the SDK does not name (Embedded said 5 while it
                    # started a play): it answered, nothing to act on — the
                    # last good status stands, no listen closes, no miss.
                    self._failures = 0
                elif status is not None:
                    self._failures = self._silent_misses = 0
                    if self._reconverge_due:
                        # Once per loss: HQPlayer answers again, and gets the queue back
                        self._reconverge_due = False
                        threading.Thread(target=self._reconverge, daemon=True,
                                         name="hqp-reconverge").start()
                    link.build.answering(_building(status) and self._embedded())
                    hqp_meter.meter.set_gain(None if status.track_gain is None
                                             else status.volume + status.track_gain)
                    self._last_entry = Entry.read(status) or self._last_entry
                    if playing is not None:
                        # The slot playing on, its position moving and the DSP
                        # keeping up: a failure judged on it explains nothing now.
                        if (status.state == PlaybackState.PLAYING and playing[1] is not None
                                and status.position >= 1.0
                                and (status.process_speed is None or status.process_speed >= 1.0)
                                and diag.note_playing(playing[0], self._queue.generation)):
                            _notify_notices()
                        self._last_tick = self._status_of(status, playing)
                        self._emit(self._last_tick)
                    if diag.note_answered():
                        _notify_notices()
                    if sample is not None:
                        self._write_sample(*sample)
            except Building:
                # A command's answer turned into a build while this tick
                # waited for the connection: said at the top
                continue
            except Dropped:
                # The link was let go or lent: this poller is being stopped
                pass
            except (BrokenPipeError, ConnectionError, OSError) as e:
                miss = (str(e), False)
            except Exception as e:
                logger.exception("HQPlayer status tick failed")
                miss = (str(e), False)
            if miss is not None:
                self._register_failure(silent=miss[1])
                if att is not None:
                    att.miss(miss[0], silent=miss[1])
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
            self._wake.wait(timeout=_poll_interval(max(self._failures, self._silent_misses)))
            self._wake.clear()
            tick += 1

    def _playing(self, status, *, mutating: bool = False) -> Optional[tuple]:
        """Which queue slot HQPlayer reads: (index, item, foreign), or None —
        no verdict this tick. Its entry names our slot through the layout
        only while its playlist is our mirror, and the canary compares the
        two every DRIFT_CHECK_EVERY ticks — an entry started in HQPlayer's
        own window was tracked and scrobbled as the queued track at that
        place until then. So every tick checks the entry its status names
        against one view of the queue: ours at the slot the layout names,
        ours at another (the nearest), or not ours; a slot is the entry when
        the hand-over or the URI itself names it — playing, paused or
        stopped.

        No verdict while HQPlayer opens an entry (PLAYING at track 0 and
        length 0 — the slot comes with the next tick), nor for an entry the
        queue does not hold at that slot while a mutation is in flight: a
        replace plays its first track before the queue commits, a removal
        shifts HQPlayer's entries first — a slot read off the queue before
        the commit names another item after it. A mirror step of this backend
        asks `mutating`: the mutation in flight is its own, under the queue
        lock, and the queue it judges is the one it acts on. With nothing
        that names the entry (no URI from this build; a stream token of an
        earlier session) the layout's slot stands while the playlist is our
        mirror; elsewhere a busy entry is not ours, a stopped one no slot.

        Stopped where the play press resumes (a spot), HQPlayer names the
        slot of the spot: it is the one Now Playing shows and a queue edit
        anchors on, whatever entry HQPlayer's own pointer rests on."""
        items, version, pending = self._queue.view()
        busy = status.state in (PlaybackState.PLAYING, PlaybackState.PAUSED)
        if not busy:
            spot = self._spot_at() if status.state == PlaybackState.STOPPED else None
            if spot is not None:
                return spot[0], items[spot[0] - 1], False
        entry = Entry.read(status)
        if entry is None:
            return None if busy else (0, None, False)
        layout = _Layout(items, self.gap)
        slot = layout.slot(entry)
        readings = self._readings(status.uri)
        if not readings:
            if self._mirror == "step" and slot is not None:
                return slot, items[slot - 1], False
            return 0, None, busy
        key = (status.uri, entry, slot, version, busy)
        cached = self._verdict
        if cached is not None and cached[0] == key:
            return cached[1]
        found = next((j for j in _outward(slot or layout.near(entry), len(items))
                      if any(_reads_item(r, items[j - 1]) for r in readings)), None)
        if pending and not mutating and found != slot:
            return None
        verdict = (found, items[found - 1], False) if found else (0, None, busy)
        if not pending:
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
                 "limited": status.limited, "active_mode": status.active_mode,
                 **{k: getattr(status, k) for k in STATUS_READINGS}}
        if foreign:
            extra["source"] = "external"
        if item is None or not _http_served(item):
            extra.update(artist=status.artist, album=status.album,
                         song=status.song)
        diagnosis = self._diagnosis(status)
        if diagnosis is not None:
            extra["diagnosis"] = diagnosis
        watch = self._link.build
        if watch.state is not None and watch.state.get("not_answering"):
            extra["not_answering"] = NOT_ANSWERING
        elif watch.state is not None:
            extra["building"] = watch.state
        change = watch.last_change
        if change is not None and change["attachment"] == self._attachment:
            extra["dsp_change"] = change
        spot = self._spot_at() if status.state == PlaybackState.STOPPED else None
        if spot is not None:
            # Where the play press resumes: a paused HQPlayer whose playlist
            # was taken afresh is left stopped, not played to be paused again
            extra["resume"] = {"index": spot[0], "position": spot[1]}
        if self._pressing:
            extra["rebuilding"] = True      # a play press: HQPlayer takes the queue again
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

    def _intent(self, intent: str, *, slot: Optional[int] = None,
                uris: Optional[list] = None, items: Optional[list] = None):
        """One play intent watched as an attempt (_watch), its commands its
        steps (_steps)."""
        return self._steps(self._watch(intent, slot=slot, uris=uris, items=items))

    def _watch(self, intent: str, *, slot: Optional[int] = None,
               uris: Optional[list] = None, items: Optional[list] = None) -> diag.Attempt:
        """A new play intent's attempt: the one watched now, superseding the
        one before."""
        att = diag.Attempt(intent=intent, slot=slot, uris=uris, items=items)
        with self._att_lock:
            old, self._attempt = self._attempt, att
            if old is not None:
                old.request_end("superseded")
                self._closing.append(old)
        return att

    @contextmanager
    def _steps(self, att: Optional[diag.Attempt]):
        """The commands this thread sends in the block are `att`'s steps (the
        link's client reports them on the thread that sent them). The exit
        judges whether a final refusal or a command that never left already
        settles it — unless the block left the intent a start still to send
        (_owe_start, or one waiting out a build): its sender ends it then. A
        step the link did not send (Dropped) ends it as superseded."""
        if att is None:
            yield None
            return
        self._local.attempt = att
        exc = None
        try:
            yield att
        except (BrokenPipeError, ConnectionError, OSError) as e:
            exc = e
            raise
        finally:
            self._local.attempt = None
            owed = getattr(self._local, "start", None)
            if exc is not None and owed is not None and owed[0] is att:
                self._local.start = None
            if isinstance(exc, Dropped):
                # The link did not send it — a newer intent came first, the
                # output was switched away or lent, the link let go: nothing
                # of HQPlayer's to judge, never "HQPlayer did not answer"
                att.request_end("superseded")
                att.intent_done(None)
                self.poke()
            elif exc is not None or owed is None or owed[0] is not att:
                att.intent_done(exc)
                self.poke()

    def _owe_start(self, att: Optional[diag.Attempt], item: Item, start: Callable[[], Any], *,
                   spot: Optional[tuple] = None, patient: bool = False) -> None:
        """A mirror step under the queue lock leaves its start — the Play of a
        replace, the resume of a rebuild — to queue_start, once the lock is
        let go: HQPlayer may build the start for minutes, and no queue edit
        may wait behind it. `item` is the start (HqpLink.item): a newer intent
        that supersedes it drops it. `spot`: (queue item, second), where the
        play press resumes if the start does not go; `patient`: the start is
        background work (a re-bind), so one that fails is logged, never
        raised."""
        self._local.start = (att, item, start, spot, patient)

    def queue_start(self) -> None:
        """The start a mirror step left (_owe_start), sent now. One that
        meets someone else's build is not refused: the start of an accepted
        intent waits for the build's end on the link, in the background
        (_start_after_build), and the caller is answered at once — the
        status says HQPlayer builds. A newer intent that superseded it
        meanwhile drops it, its spot left for the play press."""
        owed = getattr(self._local, "start", None)
        if owed is None:
            return
        self._local.start = None
        att, item, start, spot, patient = owed
        item.queue, item.generation = self._queue, self._queue.generation
        later = False
        try:
            with self._steps(att):
                try:
                    start()
                except Building:
                    later = True
                    self._local.start = owed        # the attempt stays open for it
        except Dropped as e:
            # Where it does not go, the play press resumes — as after a build
            if spot is not None:
                self._set_spot(*spot)
            logger.info("HQPlayer's start not sent: %s", e)
        except (BrokenPipeError, ConnectionError, OSError) as e:
            if spot is not None:
                self._set_spot(*spot)
            if not patient:
                raise
            logger.warning("HQPlayer did not start again after its playlist followed the "
                           "queue: %s", e)
        finally:
            self._local.start = None
            self.poke()
        if later:
            threading.Thread(target=self._start_after_build, args=(owed,), daemon=True,
                             name="hqp-start").start()

    def queue_abandon(self) -> None:
        """The replace this thread just mirrored was not committed: its start
        is not sent, its attempt ends, and HQPlayer's playlist — the queue
        that did not commit — is no mirror (_reconverge gives it the queue
        back)."""
        owed = getattr(self._local, "start", None)
        self._local.start = None
        if owed is not None and owed[0] is not None:
            owed[0].intent_done(RuntimeError("the queue was not committed"))
        self.lose_mirror("HQPlayer took a queue that was not committed")

    def _start_after_build(self, owed: tuple) -> None:
        """A start that met a build, sent once the build is done (through) —
        unless a newer intent superseded it meanwhile, the queue it was made
        for was replaced, or the link was let go; a build HQPlayer did not
        finish gives it up. Where it does not go, the play press resumes."""
        att, item, start, spot, _ = owed
        link = self._link
        try:
            with self._steps(att):
                through(start, needed=lambda: link.superseded(item),
                        until=lambda: link.superseded(item) is not None)
        except (GaveUp, Dropped) as e:
            if spot is not None:
                self._set_spot(*spot)
            (logger.warning if getattr(e, "failed", False) else logger.info)(
                "HQPlayer's start not sent after the build: %s", e)
        except (BrokenPipeError, ConnectionError, OSError) as e:
            if spot is not None:
                self._set_spot(*spot)
            logger.warning("HQPlayer's start after the build failed: %s", e)
        finally:
            self.poke()

    def _on_outcome(self, outcome) -> None:
        """An answer on the link's client. Sent by an intent, it is a step of
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
        """Every command sequence of this instance calls it under the
        command lock: a detached backend sends nothing more — dropped, as an
        item a newer intent superseded is."""
        if self._detached:
            raise Dropped("HQPlayer was detached from playback")

    def _cmd(self, func, intent: Optional[str] = None, *, item: Any = None):
        """One command sequence of this backend: `func(client)` under the
        link's command lock, each request taking the connection for itself
        (HqpLink.command) —
        `intent` the person's or this node's, `item` what it serves."""
        with self._link.command(intent, item=item):
            self._check_attached()
            return func(self._link.client)

    def _command(self, name: str, fn, why=None, intent: Optional[str] = "transport") -> bool:
        """One command; one HQPlayer did not take is logged in its words."""
        return self._refusal_of(name, fn, why, intent) is None

    def _refusal_of(self, name: str, fn, why=None,
                    intent: Optional[str] = "transport") -> Optional[str]:
        """`_command`, answering why not — `why(client)`, its refusal by
        default: None when HQPlayer took it. Kept for refusal()."""
        def run(h):
            return None if fn(h) else (why or HQPlayerClient.refusal)(h)
        refusal = self._local.refusal = self._cmd(run, intent)
        if refusal is not None:
            logger.warning("HQPlayer did not take %s: %s", name, diag.redact_text(refusal))
        return refusal

    def refusal(self) -> str:
        """Why the last command this thread sent was not taken, in
        HQPlayer's words — what the assistant quotes."""
        return getattr(self._local, "refusal", None) or "HQPlayer did not confirm it"

    def _close_reads(self, hqp: HQPlayerClient, att: diag.Attempt) -> dict:
        """What HQPlayer holds as the attempt closes — its playlist (whose
        entries are ours, which one it plays) and, for a slot that never
        played, its matrix profile. On the poller's hold of the connection."""
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
        source = diag.log_source(self._link.host, self._info)
        diag.attach_log(att, diag.read_log(source, self._info, uri))

    @property
    def link(self) -> HqpLink:
        """The link of the HQPlayer this backend drives."""
        return self._link

    @property
    def info(self) -> dict:
        """What HQPlayer said of itself (GetInfo) at attach or its return."""
        return dict(self._info)

    @property
    def drift(self) -> bool:
        """HQPlayer's playlist is not the canonical queue by an edit made
        outside this node — its own window, another controller (Roon) — or a
        mirror step it did not take."""
        return self._mirror == "drift"

    def _context(self) -> dict:
        return {"hqplayer": f"{self._link.host}:{self._link.port}",
                "media_url": f"{self._url_host}:{settings.media_proxy_port}",
                "remote": _stream_mode(), "mirror": self._mirror,
                "endpoint_id": self.endpoint_id, "info": dict(self._info)}

    def _facts(self, att: diag.Attempt, reads: Optional[dict]) -> dict:
        """Everything the verdict weighs, gathered as the attempt closes —
        the shape tests/fixtures/hqp_traces records."""
        from streaming import service as streaming_service
        proxy = streaming_service.get_proxy()
        f = att.snapshot()
        uris = f.pop("uris")
        ticks = f["ticks"]
        # The attempt speaks HQPlayer's entries (its SelectTrack, its ticks);
        # the facts name the queue's slot too, as Now Playing does
        explicit = f["slot"] is not None
        entry = f["slot"]
        if entry is None:
            entry = (next((t["track"] for t in ticks if t["state"] == "playing" and t["track"] >= 1), None)
                     or next((t["track"] for t in reversed(ticks) if t["track"] >= 1), None))
        item = expected = None
        if entry is not None:
            if uris and entry <= len(uris):
                expected = uris[entry - 1]
                item = att.items[entry - 1] if att.items and entry <= len(att.items) else None
            else:
                item = self._queue.item_at(self._layout().slot(Entry(entry)))
                if item is not None and explicit:
                    expected = diag.handed_uri(_slot_key(item))
        playlist, at = None, None
        listed = (reads or {}).get("playlist")
        if listed is not None:
            refs = self._queue_refs()
            at = listed[entry - 1] if entry and 1 <= entry <= len(listed) else None
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
        slot = (self._queue.index_of(item) if item is not None
                else self._layout().slot(Entry(entry)) if entry else None)
        return {**f, "closed": time.time(), "slot": slot, "entry": entry, "expected_uri": expected,
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

    def _register_failure(self, *, silent: bool) -> None:
        """Tolerate a short burst of misses, then say what they are — the
        manager dedupes repeats. A miss in a build is the build's. Past a
        build HQPlayer did not finish, a silence — it took the connection
        and said nothing — is that build still. With no build of ours behind
        it, a silence is HQPlayer not answering: busy, not gone. An
        HQPlayer that refuses or drops the connection is away:
        'disconnected'."""
        state = self._link.build.state
        if state is not None and state["silent"]:
            if not state.get("unfinished"):
                return
            if silent:
                # Asked again with the same back-off as an HQPlayer away: no
                # new connection every few seconds into a silence
                self._silent_misses += 1
                try:
                    self._emit_building(state)
                except Exception:
                    logger.exception("HQPlayer's build was not said on the status")
                return
        self._failures += 1
        if self._failures < STATUS_FAILURE_THRESHOLD:
            return
        if silent:
            self._emit_silence({"not_answering": NOT_ANSWERING})
        else:
            self._emit(PlaybackStatus(state="disconnected"))

    def _emit_building(self, state: dict) -> None:
        """The last status again, with the build HQPlayer is in."""
        self._emit_silence({"building": state})

    def _emit_silence(self, why: dict) -> None:
        """The last status again — HQPlayer said nothing new — with why."""
        last = self._last_tick
        self._emit(replace(last, extra={**last.extra, **why}) if last is not None
                   else PlaybackStatus(state="unknown", extra=why))

    def _embedded(self) -> bool:
        import hqp_library
        return hqp_library.is_embedded(self._info.get("product"))

    def _first_divergence(self, hqp_tracks: list, snapshot: list) -> Optional[int]:
        """The first 1-based place, over the length the two share, at which
        HQPlayer's entry names another file than the queue's slot it is
        compared with (what HQPlayer holds of the queue: _Layout.handed);
        None when none does. Preview and transcode slots are skipped (their tokens are
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

    def _judge_mirror(self, playlist: list, status) -> None:
        """The canary: HQPlayer's playlist against the queue without its gaps,
        read together. The same, it is in step. Otherwise, back from a
        restart (_verify_mirror) the mirror is lost and HQPlayer gets the
        queue back; a tail mirror whose tail holds stays one; any other
        difference is drift — an edit made outside this node — never
        reconciled on its own (a lost mirror stays lost)."""
        layout = self._layout()
        if (len(playlist) == len(layout.handed)
                and self._first_divergence(playlist, layout.handed) is None):
            if self._mirror != "step":
                logger.info("HQPlayer's playlist is the queue again")
            self._in_step()
            self._drift_logged = False
        elif self._verify_mirror:
            self.lose_mirror("HQPlayer came back with a different playlist (restarted?)")
        elif self._mirror == "tail" and self._tail_holds(playlist, layout, status):
            return
        elif self._mirror in ("step", "tail"):
            self._drifted("HQPlayer's playlist drifted from the canonical queue (an external edit?)")

    def _tail_holds(self, playlist: list, layout: _Layout, status) -> bool:
        """A tail mirror's invariant: from the entry HQPlayer reads to its
        end, its playlist is what it holds of the queue from the slot it
        reads. One that reads nothing holds it as it was: the play press
        rebuilds."""
        if status.state not in (PlaybackState.PLAYING, PlaybackState.PAUSED):
            return True
        playing = self._playing(status)
        reads = Entry.read(status)
        if playing is None or playing[2] or reads is None:
            return False
        want = [playing[1]] + layout.behind(playing[0])
        return (len(playlist) - reads.n + 1 == len(want)
                and self._first_divergence(playlist[reads.n - 1:], want) is None)

    # -- transport -----------------------------------------------------------------

    def play(self) -> bool:
        if self._mirror != "step":
            return self._press_play()
        held, spot = self._spot, self._spot_at()
        start = self._start_point(*spot) if spot is not None else None
        with self._intent("play", slot=start[0].n if start is not None else None):
            if spot is not None:
                logger.info("play: honoring resume slot %d at %d s", *spot)
                ok = self._start_at(*spot, fallback=True, intent="transport", item="start")
                if ok:
                    self._take_spot(held)
            else:
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
        # Watched only when there is a next entry to land on.
        last = self._last_entry
        return self._step(1, "Next", lambda h: h.next(),
                          last is not None and last < Entry(len(self._layout().slots)))

    def previous(self) -> bool:
        last = self._last_entry
        return self._step(-1, "Previous", lambda h: h.previous(),
                          last is not None and last > Entry(1))

    def _step(self, by: int, name: str, send, watched: bool) -> bool:
        """Next and Previous. Stopped at the spot, they only move it — to the
        slot after or before that this HQPlayer holds, at its start: no
        command, no sound (at either end of the queue it stays). Otherwise
        HQPlayer's own Next or Previous — onto the entry beside, the slot it
        holds beside — a start taken that erases a spot."""
        held, spot = self._spot, self._spot_at()
        last = self._last_tick
        layout = self._layout()
        if spot is not None and last is not None and last.state == "stopped":
            target = self._queue.item_at(layout.held(spot[0] + by, by))
            if target is not None:
                with self._spot_lock:
                    if self._spot is held:
                        self._spot = (target, 0)
            self.poke()
            return True
        reading = last.item if last is not None and self._mirror != "step" else None
        if reading is not None:
            index = self._queue.index_of(reading)
            target = (self._queue.item_at(layout.held(index + by, by))
                      if index is not None else None)
            if target is not None:
                return self._press(target, 0)
        with self._intent(name.lower()) if watched else nullcontext():
            ok = self._command(name, send)
        if ok:
            self._take_spot(held)
        self.poke()
        return ok

    def select(self, index: int) -> bool:
        """Load AND play the slot (PlayerBackend contract). SelectTrack
        alone already starts a STOPPED HQPlayer on the slot it names (its
        log: "GoTo 1", then "Play (1/0)"; Desktop 6.2.4, 2026-10-11) — so no
        path sends it just to ready a slot on a stopped HQPlayer (_rebuild
        leaves that to the play press). The Play after it, which jump()
        used to send from the manager, finds it playing then; a slot
        HQPlayer refuses gets no Play. On a playlist that is no mirror the
        tap is a play press (_press). A slot this HQPlayer does not hold (a
        gap) is refused in the gap's words, nothing sent."""
        item = self._queue.item_at(index)
        why = self.gap(item) if item is not None else None
        if why is not None:
            self._local.refusal = f"it is not on this HQPlayer — {why}"
            return False
        if self._mirror != "step" and item is not None:
            return self._press(item, 0)
        held = self._spot
        entry = self._layout().entry(index)
        with self._intent("select", slot=entry.n if entry is not None else None):
            ok = self._start_at(index, 0, fallback=False, intent="transport", item="start")
        if ok:
            self._take_spot(held)   # a start taken elsewhere: the play press resumes nothing
        self.poke()
        return ok

    def _press_play(self) -> bool:
        """Play on a playlist that is no mirror (lost, drifted, a tail). On a
        pause it only resumes — the sound never stops for a rebuild — and
        what lies behind the entry HQPlayer reads is repaired as the
        reconverge does. From a stop HQPlayer takes the queue afresh and
        starts at the spot, else at the slot it stopped on — named by what
        its entry is, as a Play after a Stop replays the entry it stopped on
        — else at the first (_press)."""
        last = self._last_tick
        if last is not None and last.state in ("paused", "playing"):
            ok = self._command("Play", lambda h: h.play())
            if ok and last.state == "paused" and self._mirror in ("lost", "drift"):
                threading.Thread(target=self._reconverge, kwargs={"press": True}, daemon=True,
                                 name="hqp-reconverge").start()
            self.poke()
            return ok
        spot = self._spot_at()
        stood = last.item if last is not None and last.state == "stopped" else None
        target = (self._queue.item_at(spot[0]) if spot is not None
                  else stood if stood is not None and self._queue.index_of(stood) is not None
                  else self._queue.item_at(1))
        if target is None:
            return self._command("Play", lambda h: h.play())
        return self._press(target, spot[1] if spot is not None else 0)

    def _press(self, target: QueueItem, position: int) -> bool:
        """A play press on a playlist that is no mirror (lost, drifted, a
        tail mirror): HQPlayer takes the queue afresh and starts at
        `target` — at `position` while it took that very track, else at the
        next it took, from 0 s — as background work through the link: the
        caller is answered at once, the status saying the queue is being
        taken again. One attempt per press, no retry; a press while one is
        under way joins it."""
        with self._press_lock:
            if self._pressing:
                return True
            self._pressing = True
        # The press is the person's transport intent as of now: a Pause the
        # moment after it supersedes the start it leads to
        n = self._link.accept("transport")
        threading.Thread(target=self._press_run, args=(target, position, n), daemon=True,
                         name="hqp-press").start()
        self.poke()
        return True

    def _press_run(self, target: QueueItem, position: int, n: int) -> None:
        from playback.manager import manager
        try:
            background("HQPlayer did not take the queue for the play press",
                       lambda: manager.mirror_step(self,
                                                   lambda: self._press_step(target, position, n)),
                       needed=lambda: "HQPlayer was detached" if self._detached else None,
                       until=lambda: self._detached)
        finally:
            with self._press_lock:
                self._pressing = False
            self.poke()

    def _press_step(self, target: QueueItem, position: int, n: int) -> None:
        """_press's rebuild, under the queue lock: its start, made for the
        press (`n`), goes once the lock is let go (PlaybackManager.mirror_step)
        unless a newer intent — a Pause meanwhile — supersedes it."""
        snapshot = self._queue.snapshot()
        if self._queue.index_of(target) is None:
            return          # its track left the queue meanwhile
        self._rebuild(snapshot, target, position, resume=True, pressed=n,
                      what="of the queue for the play press")

    def _start_point(self, slot: int, position: int) -> Optional[tuple]:
        """Where a start at the queue's `slot` lands on this HQPlayer: the
        entry the layout names, at `position` — for a slot it does not hold
        (a gap), the next one it holds, from its start. None when none
        follows."""
        layout = self._layout()
        held = layout.held(slot)
        if held is None:
            return None
        return layout.entry(held), (position if held == slot else 0)

    def _start_at(self, slot: int, position: int, *, fallback: bool,
                  intent: Optional[str] = None, item: Any = None) -> bool:
        """The queue's slot, from `position` on — where it lands on this
        HQPlayer (_start_point) — as one command sequence (_start_on) serving
        `item`: a person's jump or play press (intent transport, whose Seek
        and Play after the SelectTrack are its start), or a start made
        earlier. Returns whether HQPlayer took the start."""
        start = self._start_point(slot, position)
        if start is None:
            return self._started(None, f"nothing from slot {slot} on is on this HQPlayer")
        entry, at = start
        return self._started(entry, self._cmd(
            lambda h: self._start_on(h, entry, at, fallback=fallback), intent, item=item))

    @staticmethod
    def _start_on(h: HQPlayerClient, entry: Entry, position: int, *,
                  fallback: bool) -> Optional[str]:
        """The start as one command sequence, on a held connection:
        SelectTrack starts a stopped HQPlayer, the Seek lands in the start's
        opening run of silent frames before anything is heard, and the Play
        starts what SelectTrack did not. Each waits for its own answer
        through the build the start sets off (the link's command waits). A
        slot HQPlayer refuses: with `fallback` the Play goes all the same —
        it plays where it stands, as a plain Play would (the play press) —
        without it, nothing more (a jump to a slot it does not hold).
        Returns HQPlayer's refusal, or None when it took the start."""
        if _select(h, entry):
            refusal = None
            if position > 0 and not h.seek(int(position)):
                logger.warning("HQPlayer did not seek the resumed slot: %s",
                               diag.redact_text(h.refusal()))
        else:
            refusal = h.refusal()
            if not fallback:
                return refusal
        return refusal if h.play() else h.refusal()

    def _started(self, entry: Optional[Entry], refusal: Optional[str]) -> bool:
        self._local.refusal = refusal
        if refusal is not None:
            logger.warning("HQPlayer did not take the start at entry %s: %s",
                           entry.n if entry is not None else "-", diag.redact_text(refusal))
        return refusal is None

    def resume_after_rebuild(self, item: Item, entry: QueueItem, position: int) -> bool:
        """Back to the queue's `entry` at the second a build left HQPlayer
        stopped at, as ONE attempt (change_dsp) serving `item`, the change's
        resume — its state read first, on the same hold of the connection: an
        HQPlayer that plays on is left alone (False, and no attempt). On a
        playlist that is no mirror the queue's slot names no entry of
        HQPlayer's: the resume is a play press there (_press). Returns
        whether it resumed."""
        index = self._queue.index_of(entry)
        if index is None:
            return False            # its track left the queue meanwhile
        mirrored = self._mirror == "step"
        start = self._start_point(index, position)
        if start is None:
            return False            # nothing from it on is on this HQPlayer
        at, second = start

        def resume(h: HQPlayerClient) -> Optional[bool]:
            now = h.get_status()
            if now is None or now.state != PlaybackState.STOPPED:
                return False
            if not mirrored:
                return None         # the press, off this hold of the connection
            with self._intent("resume", slot=at.n):
                return self._started(at, self._start_on(h, at, second, fallback=True))
        ok = self._cmd(resume, item=item)
        if ok is None:
            return self._press(entry, position)
        if ok:
            self._take_spot()
        self.poke()
        return ok

    def seek(self, seconds: int) -> bool:
        ok = self._command("Seek", lambda h: h.seek(int(seconds)))
        self.poke()
        return ok

    def set_volume(self, level: float) -> bool:
        ok = self._command("Volume", lambda h: h.set_volume(level), HQPlayerClient.volume_refusal,
                           intent=None)
        self.poke()
        return ok

    def volume_up(self) -> bool:
        ok = self._command("VolumeUp", lambda h: h.volume_up(), HQPlayerClient.volume_refusal,
                           intent=None)
        self.poke()
        return ok

    def volume_down(self) -> bool:
        ok = self._command("VolumeDown", lambda h: h.volume_down(), HQPlayerClient.volume_refusal,
                           intent=None)
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
                self._link.host, self._link.port) or {}
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
        — call OUTSIDE a command sequence; a slow transcode must never hold
        the connection hostage. Only for a slot HQPlayer holds: a gap is
        never handed over (_Layout.handed)."""
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
        else:
            uri, mode = src["uri"], "foreign"
        diag.note_handover(uri, _slot_key(item), item, mode)
        return uri

    def queue_changed(self, kind: str, *, play: bool = False) -> None:
        """Every mutation is mirrored in the queue_* hooks, before its commit.
        A re-bind happens under the queue — files left the library
        (PlaybackManager.rebind_files) — with nothing to mirror first, so
        HQPlayer converges on it here; while it builds and answers nothing,
        once the build is done (_converge_after_build)."""
        if kind == "rebind":
            try:
                self._converge_mirror()
            except Building:
                threading.Thread(target=self._converge_after_build, daemon=True,
                                 name="hqp-rebind").start()

    def _converge_after_build(self) -> None:
        """A re-bind HQPlayer could not follow while it built: waited out off
        the queue lock, then converged under it (PlaybackManager.mirror_step)
        against the queue and the playlist as they stand by then — unless
        this backend was detached meanwhile or its link let go. A build
        HQPlayer did not finish, or went away during, loses the mirror: it
        gets the queue back once it answers again."""
        from playback.manager import manager
        try:
            through(lambda: manager.mirror_step(self, self._converge_mirror),
                    needed=lambda: "HQPlayer was detached" if self._detached else None,
                    until=lambda: self._detached)
        except GaveUp as e:
            if e.failed:
                self.lose_mirror(f"re-bound slots: HQPlayer's playlist could not follow ({e})")
        except Dropped as e:
            logger.info("re-bound slots: HQPlayer's playlist not converged: %s", e)

    def _converge_mirror(self) -> None:
        """Bring HQPlayer's playlist back in step after slots were re-bound to
        another copy of their track: its entries still name files that are
        gone, and a slot whose file left with no copy this HQPlayer opens is
        a gap now. Only a mirror that was in step is converged — any other is
        the reconverge's or the play press's. Stopped, HQPlayer takes the
        queue afresh (a rebuild) and the play press resumes at the slot it
        was on — the spot it held, if one (a re-bind moves no slot). Busy,
        the entry it reads is never touched while the first changed entry
        lies past it: the entries from there are removed and what it holds
        of the queue from there appended again, as queue_insert_next does.
        One at or before it cannot be put back in place — the protocol has no
        insert — so a playing HQPlayer is rebuilt around its position (the
        gap a reorder rebuild costs; its start goes once the queue lock is
        let go, waiting out a build it meets), and a paused one takes the
        queue afresh, stopped, and resumes that slot where it was on the play
        press. Raises Building while HQPlayer builds: the caller follows up
        once it is done."""
        if self._mirror != "step":
            return
        layout = self._layout()
        try:
            status, playlist = self._cmd(lambda h: (h.get_status(), h.get_playlist()))
        except Building:
            raise
        except (BrokenPipeError, ConnectionError, OSError) as e:
            self.lose_mirror(f"re-bound slots: HQPlayer unreachable ({e})")
            return
        # A state the SDK does not name is HQPlayer starting a play: a replace
        # now would stop it — the next rebind, or the play press, converges
        if status is None or not status.state.known:
            return
        # An entry that names no slot of the queue is an edit made elsewhere:
        # the canary's to judge, never converged over
        if not all(any(_reads_item(r, it) for r in self._readings(t.get("uri") or "")
                       for it in layout.items) for t in playlist):
            return
        first = self._first_divergence(playlist, layout.handed)
        if first is None:
            if len(playlist) == len(layout.handed):
                return
            first = min(len(playlist), len(layout.handed)) + 1
        reads = Entry.read(status)
        busy = status.state in (PlaybackState.PLAYING, PlaybackState.PAUSED)
        playing = self._playing(status, mutating=True)
        if busy and (reads is None or reads.n > len(playlist)
                     or playing is None or playing[2]):
            return          # it reads an entry we cannot place: the canary's
        try:
            if busy and first > reads.n:
                items = layout.handed[first - 1:]
                uris = [self._uri_for(it) for it in items]
                gone = len(playlist) - first + 1

                def follow(h: HQPlayerClient):
                    # The URIs took their time: a playhead that moved on to
                    # a changed entry meanwhile is not removed from under
                    # itself — that entry names a gone file, and the play
                    # press re-mirrors.
                    now = h.get_status()
                    moved = Entry.read(now) if now is not None else None
                    # A removal HQPlayer did not take stops it there: what is
                    # appended after it would sit beside what it kept
                    if now is None or (moved is not None and moved.n >= first) or not all(
                            _hqp_safe(self._link, lambda c: _remove(c, Entry(first)),
                                      "PlaylistRemove") for _ in range(gone)):
                        return None
                    return _add_uris_with_retry(self._link, uris)
                out = self._cmd(follow)
                self.poke()
                converged = out is not None and self._took(items, *out, "of the re-bound slots")
            elif status.state == PlaybackState.PLAYING:
                converged = self._rebuild(layout.items, playing[1], int(status.position),
                                          resume=True, patient=True, what="of the re-bound queue")
            else:
                converged = self._remirror(
                    at=playing[1] if playing is not None and not playing[2] else None,
                    position=int(status.position) if status.state == PlaybackState.PAUSED else 0)
                self.poke()
        except Building:
            raise
        except (BrokenPipeError, ConnectionError, OSError) as e:
            logger.warning("re-bound slots: HQPlayer's playlist could not follow (%s)", e)
            converged = None
        if converged:
            logger.info("HQPlayer's playlist follows the re-bound slots from %d", first)
        elif converged is None or self._mirror == "step":
            self.lose_mirror("HQPlayer's playlist did not follow the re-bound slots")

    def _rebuild(self, items: list, target: QueueItem, position: int, *, resume: bool,
                 what: str, patient: bool = False, pressed: Optional[int] = None) -> bool:
        """Clear and reload HQPlayer's playlist with `items` — the queue, or
        a new order of it — for `target` at `position`: the ~300 ms gap of
        every change that needs an insert the protocol lacks, as a rebuild
        intent: what was left to start before it is superseded. A full
        mirror: each slot's way in read again, every slot it holds handed
        over, what it refuses a gap until the next one (_took). hqp.stop()
        is mandatory — HQPlayer ignores PlaylistAdd(clear=True) while
        playing, silently appending instead, which would double every track
        and land select_track on the wrong slot. The URIs are made before
        the sequence (_uri_for may transcode). With `resume` it starts at
        `target` once the queue lock is let go (_owe_start: the play press's
        own start, _start_at), wherever the target is by then — `patient` for
        background work, whose failure is only logged; a start that does not
        go leaves its spot to the play press. Not resuming it lands nowhere:
        SelectTrack would start a stopped HQPlayer (select), and the play
        press resumes `target` at `position`. The second holds only for the
        track it was taken on: a target HQPlayer does not hold, or refused,
        gives way to the next track it took, at 0 s. A play press's rebuild
        (`pressed`, the press's intent) takes no intent of its own: its start
        is the press's. Under the queue lock. Returns whether HQPlayer holds
        the queue now — then it is in step."""
        self._ways_in()
        self._refusals = []
        layout = self._layout(items)
        uris = [self._uri_for(it) for it in layout.handed]
        from_target = items[next((i for i, it in enumerate(items) if it is target), 0):]
        onward = {id(it) for it in from_target}
        first = next((i for i, it in enumerate(layout.handed, 1) if id(it) in onward), 1)
        att = self._watch("rebuild", slot=first, uris=uris) if resume else None
        with self._steps(att):
            with self._link.command(None if pressed is not None else "rebuild") as n:
                self._check_attached()
                _hqp_safe(self._link, lambda c: c.stop(), "Stop")
                taken, unsent = _add_uris_with_retry(self._link, uris, clear_first=True)
            n = pressed if pressed is not None else n
            took = {id(it) for it, t in zip(layout.handed, taken) if t}
            begin = next((it for it in from_target if id(it) in took), None)
            at = position if begin is target else 0
            if begin is None or unsent < len(uris):
                pass        # nothing of ours to start at, or HQPlayer stopped answering
            elif resume:
                start = self._link.item("start", n)
                self._owe_start(att, start, lambda: self._start_item(begin, at, start),
                                spot=(begin, at), patient=patient)
            else:
                self._set_spot(begin, at)
        self.poke()
        if not self._took(layout.handed, taken, unsent, what):
            return False
        self._in_step()
        return True

    def _start_item(self, item: QueueItem, position: int, start: Item) -> bool:
        """The start of a rebuild, at `item`'s slot as it stands now."""
        index = self._queue.index_of(item)
        if index is None:
            raise Dropped("its track left the queue")
        return self._start_at(index, position, fallback=True, item=start)

    def queue_replace(self, items: list, *, play: bool, probe_first: bool = False) -> list:
        """HQPlayer's playlist becomes what it holds of `items` — a new queue
        (intent queue), each slot's way in read for it — and the queue all of
        them (mirror-first): a slot it cannot open, or refuses while it takes
        others, is a gap (_took); refused whole, or silent before it took
        any, nothing commits, and since its Stop and clear went the mirror is
        lost — the next Play gives it the queue as it was. Nothing of `items`
        it could open: refused before anything is sent, in the gap's words.
        Cut short by a silence, the queue takes them all and HQPlayer gets
        the rest once it answers again. With `play` it starts once the
        manager has committed the replace and let the queue lock go
        (queue_start) — as one attempt with the mirror, watched with what it
        was handed: the canonical queue holds the old ones until the commit.
        The spot goes with its item when the queue is replaced, and stays
        when nothing commits."""
        self._ways_in(items)
        layout = _Layout(items, self.gap)
        if items and not layout.handed:
            raise RuntimeError(f"none of these tracks is on {self.label} — {self.gap(items[0])}")
        uris = [self._uri_for(it) for it in layout.handed]
        att = (self._watch("replace", slot=1, uris=uris, items=list(layout.handed))
               if play and uris else None)
        with self._steps(att):
            taken, unsent, n = self._afresh(uris, "queue")
            if uris and not any(taken):
                if unsent < len(uris):
                    self.lose_mirror("HQPlayer took nothing of a new queue before it fell silent")
                else:
                    self.lose_mirror(f"HQPlayer refused all {len(uris)} tracks of a new queue",
                                     by="refusal")
                    self._note_refused(layout.handed, taken)
                accepted = []
            else:
                self._in_step()
                self._refusals = []
                self._took(layout.handed, taken, unsent, "of a new queue")
                accepted = list(items)
                if play:
                    item = self._link.item("start", n)
                    self._owe_start(att, item,
                                    lambda: self._play_replaced(item, sum(taken), probe_first))
        self.poke()
        return accepted

    def _afresh(self, uris: list, intent: str) -> tuple:
        """HQPlayer's playlist taken afresh, as `intent` (a new queue, a
        rebuild): stopped, cleared, `uris` added. Returns (taken, unsent) as
        _add_uris_with_retry does, and the intent's number."""
        with self._link.command(intent) as n:
            self._check_attached()
            _hqp_safe(self._link, lambda h: h.stop(), "Stop")
            taken, unsent = _add_uris_with_retry(self._link, uris, clear_first=True)
        return taken, unsent, n

    def _play_replaced(self, item: Item, added: int, probe_first: bool) -> None:
        def start(h: HQPlayerClient) -> None:
            _hqp_safe(self._link, lambda c: c.play(), "Play")
            if probe_first:
                _skip_to_first_playable(h, added)
        self._cmd(start, item=item)

    def _queue_takes(self, items: list, handed: list, taken: list, unsent: int,
                     what: str) -> list:
        """What the queue takes of a batch a person or a filler added —
        `handed` what of it went to HQPlayer: all of it, a slot HQPlayer
        cannot open or refused while it took others a gap of this output
        (_took); cut short by a silence, all of it too, the mirror lost
        (HQPlayer gets it once it answers again). Refused whole: nothing —
        the queue stays as it was, and so does the mirror. One notice for
        what it refused."""
        if handed and unsent == len(handed) and not any(taken):
            self._note_refused(handed, taken)
            return []
        self._took(handed, taken, unsent, what)
        return list(items)

    def queue_append(self, items: list) -> list:
        """Appended behind the last entry — the spot stays with its item —
        each slot's way in read for this HQPlayer, a slot it cannot open never
        handed over (_queue_takes). With the mirror lost the append is the queue's
        alone: HQPlayer gets the queue back when it answers again
        (_reconverge)."""
        self._ways_in(items)
        if self._mirror == "lost":
            return list(items)
        handed = _Layout(items, self.gap).handed
        uris = [self._uri_for(it) for it in handed]
        try:
            taken, unsent = (self._cmd(lambda h: _add_uris_with_retry(self._link, uris))
                             if uris else ([], 0))
        except NotAnswering:
            taken, unsent = [False] * len(uris), 0
        self.poke()
        return self._queue_takes(items, handed, taken, unsent, "appended tracks")

    def queue_insert_next(self, items: list, anchor_index: Optional[int]) -> list:
        """Insert right after the playing slot via the seamless remove-after /
        re-append trick — the reading slot is never touched, so audio plays
        through. URI-based so it works for preview streams too. In a tail
        mirror the playing slot is HQPlayer's entry that reads it. A removal
        HQPlayer does not take ends the removals there, and only what left
        is appended again: never an entry twice, and the playlist is marked
        no mirror (drift) until the next one. A slot this HQPlayer cannot
        open is never handed over (_queue_takes). With the mirror lost, or
        HQPlayer not answering, the insert is the queue's alone, as an
        append is."""
        self._ways_in(items)
        if self._mirror == "lost":
            return list(items)
        handed = _Layout(items, self.gap).handed
        uris = [self._uri_for(it) for it in handed]
        anchor = self._queue.item_at(anchor_index) if anchor_index else None

        def insert(h: HQPlayerClient) -> tuple:
            raw = h.get_playlist() or []
            at = self._entry_of(h, raw, anchor)
            if at is None:
                return _add_uris_with_retry(self._link, uris) + (True,)
            # Remove the after-segment (always the entry after `at`, which
            # the rest shift down into), then re-append it behind the new
            # tracks.
            after = [t.get("uri") for t in raw[at.n:] if t.get("uri")]
            removed = 0
            while removed < len(after) and _hqp_safe(
                    self._link, lambda c: _remove(c, at + 1), "PlaylistRemove"):
                removed += 1
            taken, unsent = _add_uris_with_retry(self._link, uris)
            if unsent < len(uris):
                # The silence ends it: what was removed comes back with the
                # queue once HQPlayer answers again (_queue_takes loses the mirror)
                return taken, unsent, True
            back, back_unsent = (_add_uris_with_retry(self._link, after[:removed])
                                 if removed else ([], 0))
            if back_unsent < removed:
                self.lose_mirror("HQPlayer fell silent while the tracks after the playing "
                                 "one went back")
            elif removed < len(after) or not all(back):
                self._drifted("HQPlayer did not take the tracks after the playing one back")
            return taken, unsent, True
        try:
            taken, unsent, _ = self._cmd(insert) if uris else ([], 0, True)
        except NotAnswering:
            taken, unsent = [False] * len(uris), 0
        self.poke()
        return self._queue_takes(items, handed, taken, unsent, "tracks to play next")

    def _entry_of(self, h: HQPlayerClient, raw: list,
                  item: Optional[QueueItem]) -> Optional[Entry]:
        """HQPlayer's entry for the queue's `item`: the layout's while the
        mirror is in step (or drifted — what it was told), else the entry
        HQPlayer reads when it reads `item` (a tail mirror). None when there
        is none."""
        if item is None:
            return None
        if self._mirror != "tail":
            entry = self._layout().entry(self._queue.index_of(item))
            return entry if entry is not None and entry.n <= len(raw) else None
        now = h.get_status()
        playing = self._playing(now, mutating=True) if now is not None else None
        reads = Entry.read(now) if now is not None else None
        if playing is None or playing[1] is not item or reads is None or reads.n > len(raw):
            return None
        return reads

    def queue_remove(self, index: int) -> bool:
        """Mirror-first while HQPlayer's playlist mirrors the queue: the slot
        leaves the queue once it left HQPlayer, and a removal HQPlayer refuses
        raises in its words (the slot is still there — the queue did not
        change). A slot HQPlayer does not hold (a gap) leaves the queue alone.
        In a tail mirror a slot behind the playing one is removed at the
        entry it has behind the entry read, one before it from the queue
        alone. A playlist that is no mirror — drift (an external edit, a
        mirror step HQPlayer did not take) or lost — is not edited by index:
        the removal is the queue's alone, and the next mirror carries it."""
        if self._mirror in ("lost", "drift"):
            return True
        layout = self._layout()
        if layout.entry(index) is None:
            return True                                 # a gap: never handed over
        if self._mirror == "tail":
            def at_offset(h: HQPlayerClient) -> Optional[Entry]:
                now = h.get_status()
                playing = self._playing(now, mutating=True) if now is not None else None
                reads = Entry.read(now) if now is not None else None
                if playing is None or playing[2] or reads is None or index <= playing[0]:
                    return None                         # the part before: the queue's
                # Behind the entry read, HQPlayer holds the queue's slots in order
                return reads + len([s for s in layout.slots if playing[0] < s <= index])
            target = self._cmd(at_offset, intent=None)
            if target is None:
                return True
        else:
            target = layout.entry(index)
        refusal = self._refusal_of("PlaylistRemove", lambda h: _remove(h, target),
                                   intent=None)
        self.poke()
        if refusal is not None:
            raise RuntimeError(f"HQPlayer did not remove it: {refusal}")
        return True

    def queue_clear_after_current(self, archive: Callable[[], None]) -> bool:
        """Radio start. PlaylistClear keeps the reading slot intact — it
        erases everything queued around the current track while the seed
        plays on. A new queue: what was left to start is superseded. The
        session rotation (`archive`) runs under the same command hold, before
        the clear: busy or lent, the sequence is refused before either; a
        rotation that fails clears nothing; a clear HQPlayer does not take
        leaves the queue as it is (mirror-first), and the radio follows it,
        history rotated."""
        def clear(h: HQPlayerClient) -> bool:
            archive()
            return _hqp_safe(self._link, lambda c: c.playlist_clear(), "PlaylistClear")
        cleared = self._cmd(clear, "queue")
        self.poke()
        return cleared

    def queue_reorder(self, plan: ReorderPlan) -> dict:
        """Execute a validated reorder plan against HQPlayer's append/remove
        primitives (no insert/move in the protocol — see the /reorder
        endpoint docstring for the seamless-feasibility rules). A step
        HQPlayer does not take ends the sequence there and raises in its
        words: the queue keeps its order (PlaybackManager.apply_reorder), and
        the playlist, part-way, is marked no mirror (drift). Stopped where
        the play press resumes (a spot — a reorder made while paused), the
        spot stays with its item at the second it held; a rebuild that stops
        a paused HQPlayer leaves one. A playlist that is no mirror (lost,
        drifted) is not edited by index: the new order is the queue's alone.
        A tail mirror takes a new order behind the playing slot at HQPlayer's
        offset; any other new order is the queue's alone, and the mirror is
        lost — HQPlayer gets what lies behind its entry at its next answer.
        The rebuild is a full mirror: what HQPlayer refuses of it is a gap
        (_took)."""
        spot = self._spot
        unchanged = {"removed": 0, "added": 0, "interrupted": False}
        if self._mirror in ("lost", "drift"):
            return unchanged
        tail = self._mirror == "tail"
        if tail and not (plan.seamless and plan.old_before == plan.new_before):
            self.lose_mirror("a new order the tail mirror cannot carry")
            return unchanged
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
            # In a tail mirror K is HQPlayer's entry that reads the slot,
            # and only steps 1 and 2 have anything to do. A slot HQPlayer
            # does not hold (a gap) has no entry: every step leaves it out.
            # URIs come from _uri_for (outside the sequence — it may
            # transcode): a raw file_path_to_uri would hand HQPlayer the
            # whole disc image for a CUE slice and an unplayable path for an
            # m4a.
            def held(ids: list) -> list:
                return [mid for mid in ids if self.gap(plan.items_by_id[mid]) is None]
            old_after, new_after = held(plan.old_after), held(plan.new_after)
            old_before, new_before = held(plan.old_before), held(plan.new_before)
            uri_by_id = {mid: self._uri_for(plan.items_by_id[mid]) for mid in new_after}
            current = self._layout().entry(plan.status_idx)

            def reorder(hqp: HQPlayerClient) -> Optional[str]:
                k = current
                if tail:
                    now = hqp.get_status()
                    playing = self._playing(now, mutating=True) if now is not None else None
                    if playing is None or playing[0] != plan.status_idx:
                        return "HQPlayer reads another entry than the playing slot"
                    k = Entry.read(now)
                if k is None:
                    return "HQPlayer holds no entry for the playing slot"
                taken = (all(_remove(hqp, k + 1) for _ in old_after)
                         and all(_add_one(hqp, uri_by_id[mid]) for mid in new_after))
                cursor = Entry(1)
                new_before_remaining = list(new_before)
                for old_track in old_before:
                    if not taken:
                        break
                    if (new_before_remaining
                            and new_before_remaining[0] == old_track):
                        new_before_remaining.pop(0)
                        cursor = cursor + 1
                    else:
                        taken = _remove(hqp, cursor)
                return None if taken else hqp.refusal()
            refusal = self._cmd(reorder)
            self.poke()
            if refusal is not None:
                self._drifted("HQPlayer did not take the new order")
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
        items = [plan.items_by_id[mid] for mid in plan.order]
        self._rebuild(items, items[plan.new_status_idx - 1],
                      spot[1] if spot is not None and not plan.resume else plan.position,
                      resume=plan.resume, what="in the new order")
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
