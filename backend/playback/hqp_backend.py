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
from playback.base import (Capabilities, OutputRefused, PlaybackStatus, PlayerBackend,
                           ReorderPlan)
from playback import hqp_benchmark
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
    stopped, and leaves one that plays on alone. The spot is read in the
    change's own command sequence, before its first knob, against the
    queue as it is then (_spot_of): an entry the queue does not hold —
    another controller's, or the old queue's while a replace still adds —
    is not ours to resume. Raises OutputHeld or Dropped while the output is
    lent (or its link let go), Building while HQPlayer builds, NotAnswering
    while it answers nothing past a short silence."""
    return _DspChange(hqp_link.selected(),
                      {k: v for k, v in knobs.items() if v is not None}).answer()


class _DspChange:
    """change_dsp's run: a thread of its own, so the caller can be answered
    the moment a build begins (BuildWatch.waking)."""

    def __init__(self, link: HqpLink, knobs: Dict[str, Any]):
        self.link = link
        # The attachment the change is made in: its outcome is shown there only
        self.attachment = link.attachment
        self.knobs = knobs
        # (queue item, second, queue generation) HQPlayer played when the
        # change began: where it resumes if the change leaves it stopped
        self.spot: Optional[tuple] = None
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
                # The person's change: a run cut short here owes its put-back
                # no more — written where it happens, not only on the link
                hqp_benchmark.left_by_person(link.host, link.port, "a DSP change came first")
                hqp = link.client
                self.spot = self._spot_of(hqp.get_status())
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

    def _spot_of(self, status) -> Optional[tuple]:
        """What HQPlayer plays as the change begins, read in its command
        sequence — a slot of the queue as it is now, named by what its entry
        is (_playing) — with its second and the queue's generation; None
        while it plays nothing of the queue, the output driving it is
        another, or it plays only because a queue mutation holds the
        person's Pause or Stop (halt_owed): no resume over it."""
        from playback.manager import manager
        backend = self.link.attached
        if (status is None or status.state != PlaybackState.PLAYING
                or backend is None or manager.active is not backend
                or backend.halt_owed()):
            return None
        playing = backend._playing(status)
        if playing is None or playing[1] is None or playing[2]:
            return None
        return playing[1], int(status.position), manager.queue.generation

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
    refusals end the sequence it serves. A request of the caller's command
    sequence."""
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
    """A PlaylistAdd whose answer is final as it stands (the seamless reorder
    does not retry), kept in the hand-over ledger."""
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

    def upto(self, q: int) -> int:
        """How many of HQPlayer's entries hold the queue's slots up to `q`,
        `q` included — what goes after slot `q` goes after that many."""
        return bisect_right(self.slots, q)


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
    isn't real audio), skip ahead until one plays. Best-effort, in the replace
    start's command sequence; the link's own refusals end it."""
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


def _still_reads(status, entry: Optional["Entry"]) -> bool:
    """Does HQPlayer, as `status` finds it, still read `entry` — playing or
    paused on it — or rest stopped? Another entry, one it is opening, a stop
    under way or a state the SDK does not name is no reading an edit planned
    on `entry` may go by."""
    return status.state == PlaybackState.STOPPED or (
        status.state in (PlaybackState.PLAYING, PlaybackState.PAUSED)
        and Entry.read(status) == entry)


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
# canonical queue (drift canary). One-way mirror per §2.6 — an edit made in
# HQPlayer's own window marks the playlist drifted and is logged; nothing
# reconciles it on its own, and the next play press hands the queue over
# again. What plays is not the canary's to say: every tick checks the entry
# HQPlayer reads (_playing).
DRIFT_CHECK_EVERY = 30


@dataclass
class _Start:
    """A start a play press — or a queue mutation in flight — holds: a Play,
    a tap, Next, Previous or a Seek made while it runs, or a DSP change's
    resume. `item` is its intent — superseded as any start is
    (SUPERSEDED_BY) — `target` its track by identity, never by slot,
    `position` the second it starts at, `following` the queue from the
    target on as it stood (the target removed meanwhile, the next one still
    queued starts), and `held` the spot it spends once HQPlayer takes it.
    `owed`: a Play asking again for the start a queue mutation owes (the
    replace's first track, the rebuild's track at its second) — that start
    goes as the mutation owes it, numbered as the Play. `steps`: a start
    Next or Previous made counting from a mutation's base, by how many slots
    — counted again on the queue as it stands (a "play next" HQPlayer
    refused whole, a removal it refused, left it as it was), and kept by a
    Play or a Seek made after it."""
    item: Item
    target: QueueItem
    position: int
    following: list
    held: Optional[tuple]
    owed: bool = False
    steps: Optional[int] = None


class _Words:
    """A person's play intents held by the work that carries them out at
    its end — a play press, a queue mutation in flight: each start and each
    Pause and Stop, numbered as it came. At the end the newest one no newer
    intent superseded goes."""

    def __init__(self):
        self.starts: list = []
        # The numbers of the Pauses and Stops made meanwhile
        self.halts: list = []
        # The second a Seek gave the newest word when that word was a Pause
        # or a Stop: the spot left takes it
        self.halt_second: Optional[int] = None

    def newest(self) -> Optional[_Start]:
        """The start of the newest intent — by its number, not by when it
        joined: a DSP change's resume joins a press with the change's own."""
        return max(self.starts, key=lambda s: s.item.n) if self.starts else None

    def live(self, link: HqpLink) -> Optional[_Start]:
        """The newest start, unless a newer intent superseded it — every
        older one a newer start superseded already."""
        start = self.newest()
        return start if start is not None and link.superseded(start.item) is None else None

    def halted(self) -> bool:
        """Was a Pause or a Stop the newest word?"""
        start = self.newest()
        return bool(self.halts) and (start is None or max(self.halts) > start.item.n)


class _Press(_Words):
    """A play press under way on a playlist that is no mirror: HQPlayer
    takes the queue afresh once (`item`, its rebuild — superseded only by a
    new queue or a switch away), and at its end the newest start made
    meanwhile goes, unless a newer intent superseded it."""

    def __init__(self, item: Item, generation: int, spot_gen: int):
        super().__init__()
        self.item = item
        self.generation = generation
        # The spot's generation when it began: a spot it leaves is put only
        # where no start was taken nor a spot set since
        self.spot_gen = spot_gen
        # Its rebuild's Stop went: HQPlayer is stopped whatever follows
        self.stopped = False


class _InFlight(_Words):
    """A queue mutation in flight — a replace, a reorder, a re-bind's or the
    reconverge's rebuild, the radio's clear — while it hands HQPlayer its
    playlist: the person's play intents made meanwhile are held as a play
    press holds them, and carried out once the queue stands (queue_start).
    A word names a track, never a slot, among `items` — what HQPlayer is
    handed, the queue's once committed. `stops`: the mutation stops HQPlayer
    (a replace, a rebuild), else it plays on (a seamless reorder, the
    radio's clear). `base`: the track and second the words count from while
    none has made a start — the start the mutation owes when `owes`."""

    def __init__(self, items: list, *, stops: bool, base: Optional[tuple], owes: bool):
        super().__init__()
        self.items = items
        self.stops = stops
        self.base = base
        self.owes = owes
        # A Seek moved the base's second on a mutation that owes no start:
        # the spot it leaves takes it
        self.base_moved = False
        # The Plays and Seeks made with no start to count from (a mutation
        # that leaves HQPlayer playing, or knows no track it stood on), as
        # (number, command, second): none names a track — they act in order
        # once the queue stands
        self.plain: list = []
        # The newest Pause's or Stop's command: sent once the queue stands
        # unless the mutation's own Stop stopped HQPlayer (`stopped`)
        self.halt_command: Optional[str] = None
        self.stopped = False

    def halted(self) -> bool:
        """Was a Pause or a Stop the newest word — newer than every start and
        every word that names no track?"""
        if not self.halts:
            return False
        start = self.newest()
        return max(self.halts) > max([start.item.n if start is not None else 0]
                                     + [n for n, _, _ in self.plain])

    def index(self, item: QueueItem) -> Optional[int]:
        """`item`'s place (1-based) among what HQPlayer is handed."""
        return next((i for i, it in enumerate(self.items, 1) if it is item), None)


# _Press's first step found HQPlayer's playlist the queue's again: no rebuild
_IN_STEP = object()


class HqpBackend(PlayerBackend):
    """HQPlayer output: the selected HQPlayer, driven through its link
    (playback.hqp_link), taken once at construction. Status acquisition = 1 s
    poll, asked on the link's one connection like every other request — the
    control protocol cannot push; documented boundary exception to the
    no-polling rule (HARDWARE-TIERS §2.6). The canonical queue is mirrored
    one-way INTO HQPlayer's native playlist via the queue_* hooks."""

    id = "hqplayer"
    holds_words = True
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
        self._lost_by: Optional[str] = None
        self._reconverge_due = False
        # The losses so far (a play press holding the queue meanwhile none)
        self.mirror_losses = 0
        # Every change of the mirror's state goes under this lock and counts
        # (_move_mirror): a verdict read at one count is never applied over a
        # change made since — the canary's stale "in step" undid a loss
        self._mirror_lock = threading.Lock()
        self._mirror_moves = 0
        self._drift_logged = False
        # The entry not ours (its URI, the queue's version) the canary last
        # judged at once (_foreign_in_step)
        self._foreign_judged: Optional[tuple] = None
        # Every URI this attachment handed HQPlayer since its last full mirror
        # (a clear: what it held before is gone): an entry it reads that is
        # one of them is ours at a switch away (_reads_any)
        self._given: set = set()
        # The words held while a queue mutation hands HQPlayer its playlist
        # (_InFlight): set as its hand-over begins, let go as the mutation
        # ends (mutation); under _press_lock
        self._inflight: Optional[_InFlight] = None
        # The number of a Pause or a Stop a queue mutation held, until it
        # went (halt_owed); under _press_lock
        self._halt_n: Optional[int] = None
        # The last verdict of _playing, by (the URI HQPlayer names, its
        # entry, the slot the layout gives it, the queue's version, playing or
        # paused or neither): a foreign entry played for an hour is looked
        # for in the queue once, not every second.
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
        # Moves on with every spot set and every start taken: a start that
        # did not go puts its spot back only if nothing moved it since it was
        # owed (_put_back)
        self._spot_gen = 0
        # A start HQPlayer answered nothing to past the build limit, given up
        # on: (its track, the spot's generation then), judged at HQPlayer's
        # next answer (_judge_start)
        self._unjudged: Optional[tuple] = None
        # HQPlayer was found stopped at the attach (its queue mirrored), so
        # the slot an output switch keeps is a spot (resume_at)
        self._stopped_at_attach = False
        # The play press under way (_press): every play intent made meanwhile
        # is one of its starts, the newest one going at its end
        self._pressed: Optional[_Press] = None
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
            # here is one it stops — never one nothing stops
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
        backend restart mid-listening — clearing would cut live audio): its
        playlist is judged instead (_judge_at_attach); a
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
            self._judge_at_attach(st)
            return False
        try:
            return self._remirror()
        except Building:
            self._verify_mirror = True
        except (BrokenPipeError, ConnectionError, OSError) as e:
            logger.warning("canonical mirror on attach failed: %s", e)
        return False

    def _judge_at_attach(self, status) -> None:
        """HQPlayer plays or pauses at the attach — on through a backend
        restart, or for another controller: what it holds is the canary's to
        judge, now, before anything of this attachment uses it. A tap landing
        ahead of the poller's first answer selected into a playlist that was
        not the queue's (another controller's: its entry played). Unread, the
        poller reads it at its first answer."""
        try:
            with self._link.asking() as hqp:
                playlist = hqp.get_playlist()
                read = hqp.last_error is None
        except Building:
            read = False
        except (BrokenPipeError, ConnectionError, OSError) as e:
            logger.info("HQPlayer's playlist at attach: %s", e)
            read = False
        if not read or not self._judge_mirror(playlist, status, expect=self._mirror_moves):
            self._verify_mirror = True

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
        try:
            hqp_benchmark.recover(self.endpoint_id, self._link, gave=self.gave,
                                  again=self._recover_later)
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

    def readdressed(self) -> None:
        """The link dials another alias of this HQPlayer (HqpLink.readdress):
        the meter stream, at the control port + 1, goes there too."""
        hqp_meter.meter.attach(self._link.host, self._link.port + 1)

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
            # ours — but never waited out through a build, nor the Stop a
            # switch away owes: that goes on watched and ends on its own, and
            # what its sequence still had to send is dropped by what made the
            # detach — a person's switch away (release) or the hold's lend; on
            # the play gate's re-attach it sends its rest (the gate re-attaches
            # only after a probe HQPlayer left unanswered). A sequence begun
            # later by whoever took this backend before the switch (a play
            # intent) is refused (_detached).
            self._detached = True
        # The poller stops: a question of its still waiting for the
        # connection is not asked, and one HQPlayer has yet to answer is not
        # waited for — whatever it hears acts on nothing (_poll_loop)
        self._running = False
        self._wake.set()
        link.wake_askers()
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
        """A person switched away from it: HQPlayer stops if it plays or
        pauses an entry this node gave it, at once, or once it answers
        (HqpLink.release): the switch waits a moment at most — believed off
        the network, it gets no Stop, the switch away said on its link all
        the same. Returns what the person is told when the Stop stays
        owed."""
        given = self._layout()
        told = self._link.release(None if self._gone else
                                  lambda status: self._reads_any(status, given))
        # A run cut short here owes its put-back no more: written where the
        # person switched, so no later answer or restart puts it back
        hqp_benchmark.left_by_person(self._link.host, self._link.port,
                                     "the output was switched away")
        return told

    def _reads_any(self, status, given: _Layout) -> bool:
        """Does HQPlayer read an entry this node gave it — one this
        attachment handed it since its last full mirror (_handed_here: a slot
        removed since from a playlist that is no mirror is still ours), or a
        slot of the queue as it stands at the switch, never as it is once the
        Stop goes? With nothing that names the entry (no URI from this
        build) its place stands while the playlist was our mirror."""
        if self._handed_here(status.uri):
            return True
        readings = self._readings(status.uri)
        if not readings:
            reads = Entry.read(status)
            return self._mirror == "step" and given.slot(reads) is not None
        return any(_reads_item(r, it) for r in readings for it in given.handed)

    def gave(self, uri: str) -> bool:
        """Is `uri` an entry this node gave HQPlayer — one this attachment
        handed it, a stream at this node's own media proxy (one of an
        earlier session too, its token no longer registered: no one else
        hands that address), or one that names a slot of the queue as it
        stands?"""
        if self._handed_here(uri):
            return True
        from streaming import service as streaming_service
        proxy = streaming_service.get_proxy()
        if (proxy is not None and _token_of(uri) is not None
                and urlsplit(uri).netloc == f"{self._url_host}:{proxy.port}"):
            return True
        handed = self._layout().handed
        return any(_reads_item(r, it) for r in self._readings(uri) for it in handed)

    def _handed_here(self, uri: Optional[str]) -> bool:
        """Did this attachment hand this HQPlayer `uri` since its last full
        mirror — in HQPlayer's own escapes too? Never the process's ledger of
        every file ever handed: one handed before a new queue, and played now
        by another controller, is not ours to stop."""
        if not uri:
            return False
        keys = [uri]
        if uri.startswith("file://"):
            keys.append(file_path_to_uri(_uri_file_path(uri)))
        return any(k in self._given for k in keys)

    def gap(self, item: QueueItem) -> Optional[str]:
        """Why this HQPlayer does not hold the slot — nothing here it can
        open, as the slot's way in says (playback.substitute: held in another
        HQPlayer's library, its file gone), or it refused the file while it
        took the rest of the batch (REFUSED_HERE) — or None: it holds it."""
        src = item.opener()
        if src["kind"] == "unplayable":
            return src["reason"]
        if any(item is it and key == _slot_key(item) for it, key in self._link.refusals):
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
        False, said in the playback trace either way. While this backend's
        play press drives HQPlayer, or a queue mutation hands it its
        playlist, it is reachable: a question would only wait behind their
        Stop and adds."""
        if self._inflight is not None or self._live(self._pressed) is not None:
            return True
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
            self._spot_gen += 1

    def _put_back(self, spot: tuple, gen: int) -> None:
        """Where a start that did not go would have started (`spot`): the
        play press resumes there — unless a start was taken or a spot set
        since it was owed (`gen`, the spot's generation then)."""
        with self._spot_lock:
            if self._spot_gen == gen:
                self._spot = spot

    def _play_spending(self, h: HQPlayerClient, held: Optional[tuple]) -> bool:
        """A plain Play: a start taken spends the spot (a start owed before it
        and dropped by it puts none back under what plays)."""
        if not h.play():
            return False
        self._spent(held)
        return True

    def _spent(self, held: Optional[tuple]) -> None:
        """A start HQPlayer took spends the spot it would have resumed — the
        one it read (`held`), any when it read none — and a start dropped
        before it puts none back. Called under the command lock the start
        held: a dropped start's put-back comes after it."""
        with self._spot_lock:
            self._spot_gen += 1
            if held is None or self._spot is held:
                self._spot = None

    def _given_up(self, target: Optional[QueueItem]) -> None:
        """HQPlayer answered nothing to a start past the build limit: whether
        it took it is judged at its next answer (_judge_start)."""
        if target is not None:
            with self._spot_lock:
                self._unjudged = (target, self._spot_gen)

    def _judge_start(self, status, playing: Optional[tuple]) -> None:
        """HQPlayer's first answer since a start it was given up on: playing
        or paused on that start's track, it took it — the spot it would have
        spent, or the one put back for it, is spent, unless a start was taken
        or a spot set since; anything else, it did not, and the spot stays."""
        with self._spot_lock:
            unjudged = self._unjudged
            if unjudged is None or playing is None:
                return              # no verdict this tick (an opening one): the next
            self._unjudged = None
            if (playing[1] is not unjudged[0]
                    or status.state not in (PlaybackState.PLAYING, PlaybackState.PAUSED)):
                return
            if self._spot_gen == unjudged[1]:
                self._spot = None
            self._spot_gen += 1

    def _spot_at(self) -> Optional[tuple]:
        """The spot as (its slot now, its second); None when there is none
        — or its item left the queue, which takes the spot with it."""
        held = self._spot
        if held is None:
            return None
        idx = self._queue.index_of(held[0])
        if idx is None:
            with self._spot_lock:
                if self._spot is held:          # one set since stays
                    self._spot = None
            return None
        return idx, held[1]

    def poke(self) -> None:
        """Wake the poller for an immediate re-poll after a command."""
        self._wake.set()

    def _move_mirror(self, state: str, *, by: Optional[str] = None, only_from: tuple = (),
                     expect: Optional[int] = None) -> bool:
        """Every change of the mirror's state (_mirror), under one lock: to
        `state` — `by`, what a lost one was lost through; a reconverge is due
        after a silence — from one of `only_from` when given, and with
        `expect`, the count of changes a verdict was read at, never over a
        change made since (compare-and-set). Returns whether it moved."""
        with self._mirror_lock:
            if expect is not None and expect != self._mirror_moves:
                return False
            if only_from and self._mirror not in only_from:
                return False
            self._mirror, self._lost_by = state, by
            self._reconverge_due = state == "lost" and by == "silence"
            self._mirror_moves += 1
            if state == "lost" and by != "press":
                self.mirror_losses += 1
            return True

    def lose_mirror(self, why: str, *, by: str = "silence",
                    expect: Optional[int] = None) -> bool:
        """HQPlayer's playlist is no mirror of the queue any more: queue edits
        are the queue's alone from now on. Lost through a silence (an
        unanswered add, a filler whose wait gave up, a restart) HQPlayer gets
        the queue back when it answers again (_reconverge, once per loss);
        lost through a refusal of everything — it answered, and took nothing
        — the next play press tries again. `expect`: see _move_mirror.
        Returns whether it was lost here."""
        if not self._move_mirror("lost", by=by, expect=expect):
            return False
        logger.warning("%s — %s", why, "HQPlayer gets the queue back once it answers again"
                       if by == "silence" else "the next play press hands it over again")
        self.poke()
        return True

    def _in_step(self, *, expect: Optional[int] = None) -> bool:
        return self._move_mirror("step", expect=expect)

    def _drifted(self, why: str, *, expect: Optional[int] = None) -> None:
        """HQPlayer's playlist differs from the queue by an edit made outside
        this node, or a mirror step it did not take: never reconciled on its
        own (a lost mirror stays lost)."""
        self._move_mirror("drift", only_from=("step", "tail"), expect=expect)
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
        answer leaves the mirror lost — through a silence, due again at its
        next answer; a play
        press under way takes the queue to HQPlayer itself — one a new queue
        overtook takes none."""
        if self._live(self._pressed) is not None:
            return
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
        # The same file at more than one slot it holds: which one HQPlayer
        # reads is no verdict to repair on, nor a spot to resume at
        twice = len(self._named(status, layout.handed)) > 1
        if status.state in (PlaybackState.PLAYING, PlaybackState.PAUSED):
            reads = Entry.read(status)
            if (playing is None or playing[2] or twice or reads is None
                    or reads.n > len(playlist)):
                return              # not one entry of ours: the play press re-mirrors
            self._repair_tail(reads, playing[0], playlist, layout)
        elif status.state == PlaybackState.STOPPED:
            self._remirror(at=playing[1] if playing is not None and not playing[2]
                           and not twice else None)

    def _named(self, status, items: list) -> list:
        """Every one of `items` the entry HQPlayer's status names is — one
        file queued twice is two. Given what HQPlayer holds (the layout's
        `handed`): a gap is never an entry it reads, though its track may be
        queued at another slot too."""
        readings = self._readings(status.uri)
        return [s for s, it in enumerate(items, 1) if any(_reads_item(r, it) for r in readings)]

    def _repair_tail(self, reads: Entry, q: int, playlist: list, layout: _Layout) -> None:
        """HQPlayer reads our slot `q` at its entry `reads`: what lies behind
        that entry is made what HQPlayer holds of the queue behind the slot —
        removed from the first entry that differs and added again, as
        play-next does: the entry read is never touched, the sound never
        stops — read again in the repair's own sequence, so an HQPlayer that
        moved on meanwhile is repaired at its next answer instead. In step
        when the part before it was the queue's too, else a tail mirror
        (§ _mirror). Under the queue lock."""
        after, want = playlist[reads.n:], layout.behind(q)
        first = self._first_divergence(after, want)
        keep = min(len(after), len(want)) if first is None else first - 1
        items = want[keep:]
        uris = [self._uri_for(it) for it in items]

        def repair(hq: HQPlayerClient):
            # The URIs took their time: HQPlayer may have moved on meanwhile,
            # and the entry it moved on to is never removed from under it
            now = hq.get_status()
            if now is None or Entry.read(now) != reads:
                return "moved"
            unanswered = False

            def remove(c: HQPlayerClient) -> bool:
                nonlocal unanswered
                took = _remove(c, reads + (keep + 1))
                unanswered = not took and c.timed_out and not c.answered
                return took
            for _ in range(len(after) - keep):
                if not _hqp_safe(self._link, remove, "PlaylistRemove"):
                    # Unanswered, HQPlayer removes it once its silence is over
                    return "silent" if unanswered else None
            return _add_uris_with_retry(self._link, uris)
        out = self._cmd(repair)
        self.poke()
        if out == "silent" and self._lost_by != "silence":
            # A drifted playlist a Play's repair met a silence on: HQPlayer
            # carries the removal out once it is free — lost through that
            # silence, it gets the queue back at its next answer
            self.lose_mirror("HQPlayer did not answer the repair behind the entry it reads")
        elif out in ("moved", "silent"):
            # judged again at HQPlayer's next answer
            self._reconverge_due = self._lost_by == "silence"
        elif out is None:
            self._drifted("HQPlayer did not take a removal behind the entry it reads")
        elif self._took(items, *out, "behind the entry it reads"):
            before = layout.before(q)
            head = (reads == layout.entry(q)
                    and self._first_divergence(playlist[:reads.n - 1], before) is None)
            if head:
                self._in_step()
            else:
                self._move_mirror("tail")
            logger.info("HQPlayer follows the queue behind the entry it reads (%s)",
                        "in step" if head else "a tail mirror")

    def _remirror(self, *, at: Optional[QueueItem] = None, position: int = 0) -> bool:
        """A stopped HQPlayer takes the queue afresh (a rebuild): each slot's
        way in read again, every slot it holds handed over — a slot it
        refuses is a gap until the next full mirror (_took). The spot, when
        none was kept, is `at` — the slot it stood on — at `position`. Under
        the queue lock. Returns whether HQPlayer holds the queue again. The
        play intents made from its first step on — the URIs it makes (a cut,
        a transcode) included — are held, counted from `at`, and newer than
        it (_hold_words)."""
        self._ways_in()
        self._link.refusals = []
        layout = self._layout()
        with self._handing(layout.items, stops=True,
                           base=(at, position) if at is not None else None, owes=False,
                           intent="rebuild", yields=True) as n:
            if n is None:
                return False        # a play press asked meanwhile takes the queue to it
            taken, unsent, _ = self._afresh([self._uri_for(it) for it in layout.handed],
                                            "rebuild", number=n)
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
        while it took others of the batch is a gap of this output, in the
        queue still, until the next full mirror hands it over again; cut short
        by a silence, the mirror is lost (the queue keeps them); refused
        whole, lost by the refusal and no gap — the next play press hands the
        queue over again (every slot a gap would leave nothing to hand, and an
        empty playlist would read as the queue). One notice for what it
        refused — a batch it took whole ends the last one. Returns whether
        HQPlayer holds them now."""
        sent, answered = items[:unsent], taken[:unsent]
        self._note_refused(sent, answered)
        if any(answered):
            self._link.refusals.extend((it, _slot_key(it))
                                       for it, t in zip(sent, answered) if not t)
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
            left_state = left_lists = None
            left_gen = 0
            check_due = False
            miss = None         # (what was said, whether HQPlayer kept the connection silent)
            came_back = learned = False
            att = self._attempt
            try:
                hqp_playlist = None
                with link.asking(until=lambda: not self._running) as hqp:
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
                    foreign = self._foreign_in_step(status) if status is not None else None
                    if status is not None and (self._verify_mirror
                                               or tick % DRIFT_CHECK_EVERY == 0
                                               or foreign is not None):
                        _, version, pending = self._queue.view()
                        mirror = self._mirror_moves
                        hqp_playlist = hqp.get_playlist()
                        # Judged against the queue and the mirror's state it
                        # was read with — never a read made while a mutation
                        # was in flight or a play press takes the queue
                        # again, nor one HQPlayer did not answer (an empty
                        # list), and its verdict never applied over a change
                        # of the mirror made since (a loss: a replace
                        # HQPlayer refused whole, its Stop gone) —
                        # and before the slot below, which falls back on it.
                        # An entry not ours is spent only once judged.
                        if (hqp.last_error is None and not pending and self._pressed is None
                                and self._queue.view()[1:] == (version, False)):
                            self._foreign_judged = foreign or self._foreign_judged
                            if self._judge_mirror(hqp_playlist, status, expect=mirror):
                                self._verify_mirror = False
                    # Stopped while a benchmark run's measured mode is
                    # unchecked: judged at this rest (hqp_benchmark.check_measured)
                    check_due = (status is not None and status.state == PlaybackState.STOPPED
                                 and self.endpoint_id is not None
                                 and hqp_benchmark.unchecked_run(self.endpoint_id) is not None)
                    # Both asked, never one for the other: each fills its own
                    # cache, and a point stands beside a left run's notice too
                    if status is not None and self.endpoint_id is not None and any([
                            hqp_benchmark.left_pending(self.endpoint_id),
                            hqp_benchmark.point_pending(self.endpoint_id)]):
                        # A benchmark run left to the owner: what HQPlayer
                        # still holds of its test settings, for its notice;
                        # a check that left HQPlayer in another mode: whether
                        # it is back where it was found (the owner's switch)
                        left_gen = hqp_benchmark.watch_gen()
                        left_state = hqp.get_state()
                        if left_state is not None and hqp_benchmark.wants_lists(
                                self.endpoint_id, left_state["mode"]):
                            left_lists = {"modes": hqp.get_modes(), "rates": hqp.get_rates(),
                                          "filters": hqp.get_filters(),
                                          "shapers": hqp.get_shapers()}
                    if att is not None and status is not None:
                        att.tick(status, STATE_NAMES.get(status.state, "unknown"))
                        if att.due(time.time()) is not None:
                            # Judged on what HQPlayer holds NOW, read in the
                            # same tick.
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

                if not self._running:
                    break               # detached meanwhile: what it heard acts on nothing
                if left_state is not None:
                    hqp_benchmark.note_left(self.endpoint_id, left_state, left_lists)
                    hqp_benchmark.note_point(self.endpoint_id, left_state, left_gen)
                if check_due:
                    hqp_benchmark.check_measured(self.endpoint_id, self._link, status,
                                                 gave=self.gave)
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
                    self._judge_start(status, playing)
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
            if not self._running:
                break
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
        # A gap is never the entry HQPlayer reads, though it may be the same
        # track as a slot HQPlayer holds
        found = next((j for j in _outward(slot or layout.near(entry), len(items))
                      if layout.entry(j) is not None
                      and any(_reads_item(r, items[j - 1]) for r in readings)), None)
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

    def _foreign_in_step(self, status) -> Optional[tuple]:
        """Does HQPlayer read an entry of no queue slot while its playlist is
        said to be the queue's? Then the canary judges it at this answer, not
        DRIFT_CHECK_EVERY ticks later: a tap meanwhile selected into a
        playlist that was not the queue's (another controller's entry
        played). Its key — (entry, queue version) — or None: once judged
        (the poller spends it when the canary ran), one the canary found the
        queue's after all waits for its turn; a tick whose canary did not
        run asks again."""
        if self._mirror != "step" or not self.reads_foreign(status):
            return None
        key = (status.uri, self._queue.version)
        return None if key == self._foreign_judged else key

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
        if self._pressed is not None:
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
        step the link did not send (Dropped), or a wait for a build given up
        because the step was no longer wanted (GaveUp, not failed), ends it
        as superseded."""
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
            if isinstance(exc, Dropped) or (isinstance(exc, GaveUp) and not exc.failed):
                # The link did not send it — a newer intent came first, the
                # output was switched away or lent, the link let go: nothing
                # of HQPlayer's to judge, never "HQPlayer did not answer"
                att.request_end("superseded")
                att.intent_done(None)
                self.poke()
            elif exc is not None or owed is None or owed[0] is not att:
                att.intent_done(exc)
                self.poke()

    def _owe_start(self, att: Optional[diag.Attempt], item: Item,
                   start: Callable[[Item], Any], *, spot: Optional[tuple] = None,
                   patient: bool = False) -> None:
        """A mirror step under the queue lock leaves its start — the Play of a
        replace, the resume of a rebuild — to queue_start, once the lock is
        let go: HQPlayer may build the start for minutes, and no queue edit
        may wait behind it. `start(item)` sends it, `item` its intent
        (HqpLink.item): a newer intent that supersedes it drops it — a Play
        made while the mutation ran asks for it again, numbered as itself
        (_carry_out). `spot`: (queue item, second), where the play press
        resumes if the start does not go — unless a start was taken or a
        spot set since it was owed (_put_back); `patient`: the start is
        background work (a re-bind), so one that fails is logged, never
        raised."""
        self._local.start = (att, item, start, spot, patient, self._spot_gen)

    def queue_start(self) -> Optional[str]:
        """The start a mirror step left (_owe_start), sent now. One that
        meets someone else's build is not refused: the start of an accepted
        intent waits for the build's end on the link, in the background
        (_start_after_build), and the caller is answered at once — the
        status says HQPlayer builds. A newer intent that superseded it
        meanwhile drops it, its spot left for the play press. The words a
        person made while the mutation handed HQPlayer its playlist are
        carried out first, the queue committed or not (_carry_out). Returns
        what of them could not be carried out, in words, or None."""
        words = getattr(self._local, "words", None)
        self._local.words = None
        said = (self._carry_out(words, committed=self._local.committed)
                if words is not None else None)
        owed = getattr(self._local, "start", None)
        if owed is None:
            return said
        self._local.start = None
        att, item, start, spot, patient, gen = owed
        item.queue, item.generation = self._queue, self._queue.generation
        later = False
        try:
            with self._steps(att):
                try:
                    start(item)
                except Building:
                    later = True
                    self._local.start = owed        # the attempt stays open for it
        except Dropped as e:
            # Where it does not go, the play press resumes — as after a build
            if spot is not None:
                self._put_back(spot, gen)
            logger.info("HQPlayer's start not sent: %s", e)
        except (BrokenPipeError, ConnectionError, OSError) as e:
            if spot is not None:
                self._put_back(spot, gen)
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
        return said

    @contextmanager
    def mutation(self):
        """A queue mutation on this thread — a replace, a reorder, a re-bind,
        the radio's clear, the reconverge — until the queue commits it or
        gives it up: the person's play intents made while it hands HQPlayer
        its playlist are held (_hold_words), and queue_start carries them
        out once the queue lock is let go — on the queue it left, when it
        gave up."""
        self._local.mutating = True
        committed = False
        try:
            yield
            committed = True
        finally:
            self._local.mutating = False
            with self._press_lock:
                words, self._inflight = self._inflight, None
            self._local.words, self._local.committed = words, committed

    def _hold_words(self, items: list, *, stops: bool, base: Optional[tuple],
                    owes: bool, intent: Optional[str] = None,
                    yields: bool = False) -> Optional[int]:
        """A hand-over of a queue mutation (mutation) begins: from now until
        the mutation ends, a person's play intents are held, not sent
        (_InFlight). Outside a mutation — an attach taking the queue — they
        act as they come. `intent` — a new queue, a rebuild — is numbered in
        the same step (HqpLink.ask_early): every word held is newer than the
        mutation. Returns its number, for the sequence that hands over —
        _handing owns it, withdrawn on any exit before that sequence holds.
        `yields`: the reconverge's rebuild, which a play press asked before
        it overtakes — it hands HQPlayer the queue itself — so none begins:
        None, nothing numbered or held. In the same step as a press that
        registers, so neither misses the other."""
        mutating = getattr(self._local, "mutating", False)
        with self._press_lock:
            if yields and self._live(self._pressed) is not None:
                return None
            n = self._link.ask_early(intent) if intent is not None else None
            if mutating:
                self._inflight = _InFlight(items, stops=stops, base=base, owes=owes)
        return n

    @contextmanager
    def _handing(self, items: list, *, stops: bool, base: Optional[tuple], owes: bool,
                 intent: str, yields: bool = False):
        """A hand-over numbered as it begins holding words (_hold_words): its
        early number — None when it yields to a play press — is published
        once its sequence holds the command lock (HqpLink.command) and
        withdrawn on any exit before that, a failure making its URIs
        included: no early number outlives the hand-over that asked it."""
        n = self._hold_words(items, stops=stops, base=base, owes=owes, intent=intent,
                             yields=yields)
        try:
            yield n
        finally:
            if n is not None:
                self._link.withdraw(n)

    def _hold_around(self, items: list) -> None:
        """A seamless edit's hand-over — a "play next", a removal — HQPlayer
        playing on through it: the person's play intents are held, counted
        from the track it plays (_hold_words), translated against `items`,
        the queue as the edit leaves it — HQPlayer's entries move under a
        slot meanwhile."""
        last = self._last_tick
        playing = self._queue.item_at(last.queue_index) if last is not None else None
        self._hold_words(items, stops=False,
                         base=(playing, int(last.position)) if playing is not None else None,
                         owes=False)

    def _held_from(self, items: list, playing: QueueItem) -> None:
        """A seamless edit's own read named the track HQPlayer plays (a "play
        next", in step or on a tail mirror): the words held meanwhile count
        from it — HQPlayer may have moved on since the status the hold began
        with, past the anchor (the tracks placed behind what it reads) or into
        a "play next" run the tracks extend — translated against `items`, the
        queue as the edit leaves it. A base a held word moved itself (a step
        at a spot, which no read of HQPlayer shows) stays."""
        with self._press_lock:
            words = self._inflight
            if words is not None:
                words.items = items
                if not words.base_moved:
                    words.base = (playing, 0)

    def _carry_out(self, words: _InFlight, *, committed: bool) -> Optional[str]:
        """The words held while this thread's mutation handed HQPlayer its
        playlist, now that the queue stands — the mutation's, or, given up,
        the one it left: the newest one no newer intent superseded, once,
        as the same word would go now. A start goes at its track, found by
        identity in the queue as it stands: at its slot's entry while the
        mirror is in step, else as a play press of its track at its second
        (an entry of HQPlayer's is never a slot elsewhere); a Play that
        asked again for the start a committed mutation owes goes as owed,
        numbered as itself. A Pause or a Stop drops the starts before it and
        always goes: sent, unless the mutation's own Stop stopped HQPlayer —
        then nothing starts and the spot is left at the newest start's track
        (at the second a Seek after the halt gave it). With no start made,
        the words that name no track made since the last halt act in order —
        after a Seek that moved the halt's second, else after the last Seek
        of what plays made before the halt; on a mutation that owes no start
        the spot it leaves takes a Seek's second. A start Next or Previous
        made counts again from the base on the queue as it stands — a "play
        next" or a removal HQPlayer refused left it as it was — and so does a
        Play or a Seek made after it. Starts older than a word that names no
        track — a tap that waited its turn while that word was made on what
        played (_held_tap) — yield to it: the words go as if they never
        came. Returns what could not go, in words: a start whose track the
        queue does not hold (a new queue that did not commit)."""
        owed = getattr(self._local, "start", None)
        newest = words.newest()
        if newest is not None and any(n > newest.item.n for n, _, _ in words.plain):
            newest = None
        last = max(words.halts, default=0)
        # A Seek of what plays, made before the halt with no start to move,
        # still holds
        sought = [] if newest is not None else [
            w for w in words.plain if w[1] == "Seek" and w[0] < last][-1:]
        if words.halted():
            if not words.stopped:
                self._send_words(sought + [(last, words.halt_command, None)] + (
                    [(last, "Seek", words.halt_second)] if words.halt_second is not None else []))
                return None
            self._halt_went(last)                   # the mutation's own Stop stopped HQPlayer
            at = (newest.target, newest.position) if newest is not None else words.base
            spot = self._queued(at[0]) if at is not None else None
            if spot is not None:
                self._set_spot(spot, at[1] if words.halt_second is None else words.halt_second)
            return None
        if newest is None:
            spot = self._queued(words.base[0]) if words.base_moved else None
            if spot is not None:
                self._set_spot(spot, words.base[1])
            after = [w for w in words.plain if w[0] > last]
            if after and words.halt_second is not None:
                after = [(last, "Seek", words.halt_second)] + after
            elif after:
                after = sought + after
            self._send_words(after)
            return None
        start = words.live(self._link)
        if start is None:
            return None             # superseded by an intent of another kind
        if committed and start.owed and owed is not None:
            att, _, send, spot, patient, gen = owed
            self._local.start = (att, start.item, send, spot, patient, gen)
            return None
        target = self._queued(start.target)
        if start.steps is not None and words.base is not None:
            # A held step counts on the queue as it stands from what HQPlayer
            # played: the layout it was handed once the mutation changed the
            # queue, the queue it left when it did not
            stepped = self._stepped(words.base[0], start.steps)
            if stepped is not None and stepped is not target:
                target = stepped
                start = replace(start, target=target,
                                following=self._queue.snapshot()[self._queue.index_of(target) - 1:])
        if target is None:
            said = (f"{start.target.title or 'the track'}, played meanwhile, is not in the queue "
                    "— not played")
            logger.info("%s", said)
            return said
        if self._mirror != "step":
            self._press(target, start.position, item=start.item)
            return None
        self._local.start = (owed[0] if owed is not None else self._watch("play"), start.item,
                             lambda it: self._start_item(target, start.position, it),
                             (target, start.position), True, self._spot_gen)
        return None

    def _queued(self, item: QueueItem) -> Optional[QueueItem]:
        """`item` while the queue holds it, else the first slot that holds
        its track — a word names a track, never an entry of a queue gone —
        or None."""
        if self._queue.index_of(item) is not None:
            return item
        return next((it for it in self._queue.snapshot()
                     if item.track_id is not None and it.track_id == item.track_id), None)

    def _stepped(self, base: QueueItem, steps: int) -> Optional[QueueItem]:
        """The slot `steps` slots this HQPlayer holds after `base` — before
        it, when negative — on the queue as it stands: None when the queue
        does not hold `base`'s track, or past either end."""
        origin = self._queued(base)
        index = self._queue.index_of(origin) if origin is not None else None
        layout, by = self._layout(), 1 if steps > 0 else -1
        for _ in range(abs(steps)):
            if index is None:
                return None
            index = layout.held(index + by, by)
        return self._queue.item_at(index) if index is not None else None

    def _halt_went(self, n: int) -> None:
        """The Pause or the Stop a mutation held — numbered up to `n` — went,
        or the mutation's own Stop stopped HQPlayer: none is owed any more."""
        with self._press_lock:
            if self._halt_n is not None and self._halt_n <= n:
                self._halt_n = None

    def halt_owed(self) -> bool:
        """Is a Pause or a Stop a queue mutation held still to go, and the
        newest of the person's play words? HQPlayer then plays only because it
        was held: a DSP change made meanwhile resumes nothing (_DspChange)."""
        with self._press_lock:
            n = self._halt_n
        return n is not None and self._link.newest("transport", "queue", "rebuild", "start") == n

    def _send_words(self, words: list) -> None:
        """Held words that name no track — (number, command, second): a
        Play, a Pause, a Stop, a Seek — sent now, in order, as the same words
        would be on the queue as it stands (a playlist that is no mirror
        gets a play press), under the number they were held with: no intent
        of their own — numbered again now, they would be newer than a word
        the person made after them that waits its turn. Their outcome is
        their own, never the mutation's that held them: overtaken by an
        intent newer than the newest of them (a person's word made once the
        mutation ended) they do not go; the word that meets a build goes,
        with those after it, once it is done — in the background, as a start
        does; one HQPlayer does not take is logged in its words."""
        if not words:
            return
        # Superseded as a start is: by a newer word, a new queue, a rebuild,
        # a switch away
        item = Item("start", max(n for n, _, _ in words))
        link, rest = self._link, list(words)
        what = "the words made while HQPlayer took the queue"
        say = {"Seek": lambda s: (self._seek_pressed(s, word=item)
                                  or self._command("Seek", lambda h: h.seek(s), word=item)),
               "Play": lambda _: self._play(item),
               "Pause": lambda _: self._command("Pause", lambda h: h.pause(), word=item),
               "Stop": lambda _: self._command("Stop", lambda h: h.stop(), word=item)}

        def send() -> None:
            while rest:
                # A newer intent drops what is left — a Play a press would
                # carry out included, which no request gate sees first
                why = link.superseded(item)
                if why is not None:
                    raise Dropped(why)
                _, command, second = rest[0]
                say[command](second)
                if command in ("Pause", "Stop"):
                    self._halt_went(item.n)
                rest.pop(0)
        try:
            send()
        except Building:
            threading.Thread(target=background, daemon=True, name="hqp-start",
                             args=(f"{what} not sent after the build", send),
                             kwargs={"needed": lambda: link.superseded(item),
                                     "until": lambda: link.superseded(item) is not None}
                             ).start()
        except Dropped as e:
            logger.info("%s not sent: %s", what, e)
        except (BrokenPipeError, ConnectionError, OSError) as e:
            logger.warning("HQPlayer did not take %s: %s", what, e)

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
        att, item, start, spot, _, gen = owed
        link = self._link
        try:
            with self._steps(att):
                through(lambda: start(item), needed=lambda: link.superseded(item),
                        until=lambda: link.superseded(item) is not None)
        except (GaveUp, Dropped) as e:
            if spot is not None:
                self._put_back(spot, gen)
            (logger.warning if getattr(e, "failed", False) else logger.info)(
                "HQPlayer's start not sent after the build: %s", e)
        except (BrokenPipeError, ConnectionError, OSError) as e:
            if spot is not None:
                self._put_back(spot, gen)
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

    def _cmd(self, func, intent: Optional[str] = None, *, item: Any = None,
             number: Optional[int] = None):
        """One command sequence of this backend: `func(client)` under the
        link's command lock, each request taking the connection for itself
        (HqpLink.command) —
        `intent` the person's or this node's (`number`: the early one a
        mutation took as it began holding words), `item` what it serves."""
        with self._link.command(intent, item=item, number=number):
            self._check_attached()
            return func(self._link.client)

    def _command(self, name: str, fn, why=None, intent: Optional[str] = "transport", *,
                 word: Optional[Item] = None) -> bool:
        """One command; one HQPlayer did not take is logged in its words.
        `word`: a held word carried out (_send_words) — it serves that item,
        no intent of its own."""
        return self._refusal_of(name, fn, why, intent, word=word) is None

    def _refusal_of(self, name: str, fn, why=None,
                    intent: Optional[str] = "transport", *,
                    word: Optional[Item] = None) -> Optional[str]:
        """`_command`, answering why not — `why(client)`, its refusal by
        default: None when HQPlayer took it. Kept for refusal()."""
        def run(h):
            return None if fn(h) else (why or HQPlayerClient.refusal)(h)
        refusal = self._local.refusal = (self._cmd(run, intent) if word is None
                                         else self._cmd(run, item=word))
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
        played, its matrix profile. Questions of the poller's tick."""
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

    def _judge_mirror(self, playlist: list, status, *, expect: int) -> bool:
        """The canary: HQPlayer's playlist against the queue without its gaps,
        read together while the mirror's state had changed `expect` times. The
        same, it is in step. Otherwise, back from a restart (_verify_mirror)
        the mirror is lost and HQPlayer gets the queue back; a tail mirror
        whose tail holds stays one; any other difference is drift — an edit
        made outside this node — never reconciled on its own (a lost mirror
        stays lost). A verdict a change of the mirror overtook is not
        applied. Returns whether the verdict stands."""
        layout = self._layout()
        if (len(playlist) == len(layout.handed)
                and self._first_divergence(playlist, layout.handed) is None):
            was = self._mirror
            if not self._in_step(expect=expect):
                return False
            if was != "step":
                logger.info("HQPlayer's playlist is the queue again")
            self._drift_logged = False
        elif self._verify_mirror:
            return self.lose_mirror("HQPlayer came back with a different playlist (restarted?)",
                                    expect=expect)
        elif self._mirror == "tail" and self._tail_holds(playlist, layout, status):
            return True
        elif self._mirror in ("step", "tail"):
            self._drifted("HQPlayer's playlist drifted from the canonical queue (an external edit?)",
                          expect=expect)
        return True

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
        return self._play(None)

    def _play(self, word: Optional[Item]) -> bool:
        """Play — or, `word`, a held Play carried out under the number it was
        held with (_send_words): no intent of its own."""
        if word is None and self._held_play():
            return True
        if self._mirror != "step" or self._live(self._pressed) is not None:
            return self._press_play(word)
        held, spot = self._spot, self._spot_at()
        start = self._start_point(*spot) if spot is not None else None
        with self._intent("play", slot=start[0].n if start is not None else None):
            if spot is not None:
                logger.info("play: honoring resume slot %d at %d s", *spot)
                ok = self._start_at(*spot, fallback=True,
                                    intent="transport" if word is None else None,
                                    item="start" if word is None else word, spends=held)
            else:
                ok = self._command("Play", lambda h: self._play_spending(h, held), word=word)
        self.poke()
        return ok

    def pause(self) -> bool:
        if self._halt("Pause"):
            return True
        ok = self._command("Pause", lambda h: h.pause())
        self.poke()
        return ok

    def stop(self) -> bool:
        if self._halt("Stop"):
            return True
        ok = self._command("Stop", lambda h: h.stop())
        self.poke()
        return ok

    def _halt(self, command: str) -> bool:
        """A Pause or a Stop while a play press runs, or a queue mutation
        hands HQPlayer its playlist: numbered as it comes, it drops the start
        the work leads to — and every start made before it — while the work
        goes on: the work starts nothing, and the next Play starts where it
        was asked to start. Nothing is sent now: a press that rebuilds
        nothing stops HQPlayer at its end, and a mutation that leaves
        HQPlayer playing sends it once the queue stands (_carry_out) — until
        then HQPlayer plays only because it is held (halt_owed). False when
        neither runs."""
        with self._press_lock:
            words = self._inflight
            if words is not None:
                n = self._link.accept("transport")
                words.halts.append(n)
                words.halt_command = command
                self._halt_n = n
            else:
                press = self._live(self._pressed)
                if press is None:
                    return False
                press.halts.append(self._link.accept("transport"))
        self.poke()
        return True

    def _held_play(self) -> bool:
        """Play while a queue mutation hands HQPlayer its playlist: the newest
        start's track and second again, numbered as it comes — before any,
        the start the mutation owes, or on a mutation that stops HQPlayer
        its base; after a Pause or a Stop, at the second a Seek after it
        gave; with no track to count from (HQPlayer plays on, or the
        mutation knows none it stood on) a Play once the queue stands.
        False when no mutation runs."""
        with self._press_lock:
            words = self._inflight
            if words is None:
                return False
            moved = words.halt_second if words.halted() else None
            n = self._link.accept("transport")
            newest = words.newest()
            if newest is not None:
                words.starts.append(_Start(
                    self._link.item("start", n), newest.target,
                    newest.position if moved is None else moved, newest.following, None,
                    owed=newest.owed and moved is None, steps=newest.steps))
            elif words.stops and words.base is not None:
                target, second = words.base
                words.starts.append(_Start(
                    self._link.item("start", n), target, second if moved is None else moved,
                    words.items[words.index(target) - 1:], None,
                    owed=words.owes and moved is None))
            else:
                words.plain.append((n, "Play", None))
        self.poke()
        return True

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
        """Next and Previous. While a play press runs they move its target,
        counted from the newest one — the slot after or before that this
        HQPlayer holds, at its start: a start of their own (_press); at
        either end of the queue it stays. Stopped at the spot, they only
        move it, as far: no command, no sound (at either end of the queue it
        stays). Otherwise HQPlayer's own Next or Previous — onto the entry
        beside, the slot it holds beside — a start taken that spends a spot.
        On a playlist that is no mirror the slot beside is started by a play
        press, and at either end of the queue nothing is sent: HQPlayer's own
        Next would move it in a playlist that is not the queue's."""
        with self._press_lock:
            if self._inflight is not None:
                return self._held_step(self._inflight, by)
        layout = self._layout()
        with self._press_lock:
            press = self._live(self._pressed)
            newest = press.newest() if press is not None else None
        if newest is not None:
            now = self._resolved(newest)
            index = self._queue.index_of(now[0]) if now is not None else None
            target = self._queue.item_at(layout.held(index + by, by)) if index else None
            if target is None:
                self.poke()
                return True                     # the queue's end: the target stays
            return self._press(target, 0)
        held, spot = self._spot, self._spot_at()
        last = self._last_tick
        if spot is not None and last is not None and last.state == "stopped":
            target = self._queue.item_at(layout.held(spot[0] + by, by))
            if target is not None:
                with self._spot_lock:
                    if self._spot is held:
                        self._spot = (target, 0)
                        self._spot_gen += 1
            self.poke()
            return True
        reading = last.item if last is not None and self._mirror != "step" else None
        index = self._queue.index_of(reading) if reading is not None else None
        if index is not None:
            target = self._queue.item_at(layout.held(index + by, by))
            if target is None:
                self.poke()
                return True                     # the queue's end: nowhere to go
            return self._press(target, 0)

        def step(h: HQPlayerClient) -> bool:
            if not send(h):
                return False
            self._spent(held)
            return True
        with self._intent(name.lower()) if watched else nullcontext():
            ok = self._command(name, step)
        self.poke()
        return ok

    def _held_step(self, words: _InFlight, by: int) -> bool:
        """Next or Previous while a queue mutation hands HQPlayer its playlist:
        a start at the slot after or before the newest start's track — the
        base's before any — that this HQPlayer holds among what it is handed,
        numbered as it comes; at either end the newest word stays. With no
        start made, on a mutation that leaves HQPlayer at a spot (a rebuild
        that does not resume) or while it stands at one, it only moves that
        spot, as _step does there: nothing is sent. With nothing to count
        from — no start, no base, no spot (HQPlayer back from a restart, its
        queue handed afresh) — it moves nothing. Under the press lock, with
        the words read: none goes to a mutation that ended meanwhile."""
        newest = words.newest()
        last = self._last_tick
        spot = self._spot
        at_spot = newest is None and not words.owes and (
            words.stops or (spot is not None and last is not None and last.state == "stopped"))
        if at_spot:
            ref = (words.base[0] if words.base is not None and (words.stops or words.base_moved)
                   else spot[0] if spot is not None else None)
        else:
            ref = newest.target if newest is not None else (
                words.base[0] if words.base else None)
        i = words.index(ref) if ref is not None else None
        slot = _Layout(words.items, self.gap).held(i + by, by) if i is not None else None
        if slot is not None and at_spot:
            words.base, words.base_moved = (words.items[slot - 1], 0), True
        elif slot is not None:
            steps = (None if newest is not None and newest.steps is None
                     else by + (newest.steps if newest is not None else 0))
            words.starts.append(_Start(
                self._link.item("start", self._link.accept("transport")),
                words.items[slot - 1], 0, words.items[slot - 1:], None, steps=steps))
        self.poke()
        return True

    def select(self, index: int, track_id: Optional[str] = None) -> bool:
        """Load AND play the slot (PlayerBackend contract). SelectTrack
        alone already starts a STOPPED HQPlayer on the slot it names (its
        log: "GoTo 1", then "Play (1/0)"; Desktop 6.2.4, 2026-10-11) — so no
        path sends it just to ready a slot on a stopped HQPlayer (_rebuild
        leaves that to the play press). The Play after it, which jump()
        used to send from the manager, finds it playing then; a slot
        HQPlayer refuses gets no Play. On a playlist that is no mirror, or
        while a play press runs, the tap is a play press (_press) of its own
        track, from its start. A slot this HQPlayer does not hold (a gap) is
        refused in the gap's words, nothing sent. The tap names the track
        the person saw in the slot (`track_id`): while a queue mutation hands
        HQPlayer its playlist it is held as a start of its own (_held_tap);
        otherwise it plays where the queue holds that track now — one it no
        longer holds is refused in words. What the checks before the command
        lock let through is decided again as the start goes, in its command
        sequence: a mutation that began meanwhile holds the tap, one that
        committed meanwhile moved its track's slot (found by its track, as
        the first check finds it), and a playlist that is no mirror by then
        makes it a play press. Whatever it becomes is numbered as the tap
        was asked — its number, taken as it asks for the command lock, holds
        only once it goes, so a tap refused there supersedes nothing and a
        word made after it stays newer. The attempt is watched from the
        start the tap itself sends (a held one is the mutation's to
        judge)."""
        with self._press_lock:
            if self._inflight is not None:
                return self._held_tap(self._inflight, index, track_id)
        index = self._queue.slot_of(index, track_id)
        item = self._queue.item_at(index) if index is not None else None
        if item is None:
            self._local.refusal = "it is no longer in the queue"
            return False
        why = self.gap(item)
        if why is not None:
            self._local.refusal = f"it is not on this HQPlayer — {why}"
            return False
        if self._mirror != "step" or self._live(self._pressed) is not None:
            return self._press(item, 0)     # never behind the command lock
        held, tid = self._spot, item.track_id
        n = self._link.reserve()

        def tap(h: HQPlayerClient) -> Optional[bool]:
            with self._press_lock:
                words = self._inflight
                if words is not None:
                    return self._held_tap(words, index, tid, number=n)
            if self._mirror != "step" or self._live(self._pressed) is not None:
                return None         # the press, off this command sequence
            slot = self._queue.slot_of(index, tid)
            if slot is None:
                self._local.refusal = "it is no longer in the queue"
                return False
            start = self._start_point(slot, 0)
            if start is None:
                return self._started(None, f"nothing from slot {slot} on is on this HQPlayer")
            self._link.hold_as("transport", n, "start")
            with self._intent("select", slot=start[0].n):
                return self._started(start[0], self._start_held(h, *start, fallback=False,
                                                                spends=held))
        ok = self._cmd(tap)
        if ok is None:
            slot = self._queue.slot_of(index, tid)
            target = self._queue.item_at(slot) if slot is not None else None
            if target is None:
                self._local.refusal = "it is no longer in the queue"
                return False
            self._link.publish("transport", n)
            return self._press(target, 0, item=self._link.item(
                "start", n, queue=self._queue, generation=self._queue.generation))
        self.poke()
        return ok

    def _held_tap(self, words: _InFlight, index: int, track_id: Optional[str], *,
                  number: Optional[int] = None) -> bool:
        """A tap while a queue mutation hands HQPlayer its playlist: the track
        the person saw in the slot — `track_id`, else the slot's track as the
        queue stands, the sheet's — wherever the mutation puts it among what
        HQPlayer is handed, from its start, numbered as it comes — or, held
        in its own command sequence, as it was asked (`number`). A track it
        is not handed is refused in words, numbered nothing; the words before
        stand. Under the press lock, with the words read: none goes to a
        mutation that ended meanwhile."""
        shown = self._queue.item_at(index)
        tid = track_id if track_id is not None else getattr(shown, "track_id", None)
        target = (shown if shown is not None and shown.track_id == tid
                  and words.index(shown) is not None
                  else next((it for it in words.items if tid is not None and it.track_id == tid),
                            None))
        if target is None:
            self._local.refusal = "it is not in the queue being loaded"
            return False
        why = self.gap(target)
        if why is not None:
            self._local.refusal = f"it is not on this HQPlayer — {why}"
            return False
        if number is None:
            number = self._link.accept("transport")
        else:
            self._link.publish("transport", number)
        words.starts.append(_Start(self._link.item("start", number), target, 0,
                                   words.items[words.index(target) - 1:], None))
        self.poke()
        return True

    def _press_play(self, word: Optional[Item] = None) -> bool:
        """Play while a play press runs: its newest target again, at the
        second it was asked for — after a Pause or a Stop, at the one a Seek
        after it gave — a start of its own (_press); a press a new queue
        overtook is none (_live). Otherwise Play
        on a playlist that is no mirror (lost, drifted, a tail): on a pause
        it only resumes — the sound never stops for a rebuild — and what lies
        behind the entry HQPlayer reads is repaired as the reconverge does.
        From a stop HQPlayer takes the queue afresh and starts at the spot,
        else at the slot it stopped on — named by what its entry is, as a
        Play after a Stop replays the entry it stopped on — else at the
        first (_press). `word`: a held Play carried out (_play)."""
        with self._press_lock:
            press = self._live(self._pressed)
            newest = press.newest() if press is not None else None
            moved = press.halt_second if press is not None and press.halted() else None
        if newest is not None:
            target, position = self._resolved(newest) or (newest.target, newest.position)
            if moved is not None and target is newest.target:
                position = moved                # the spot a Seek after the halt gave
            return self._press(target, position, item=word)
        last = self._last_tick
        if last is not None and last.state in ("paused", "playing"):
            ok = self._command("Play", lambda h: h.play(), word=word)
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
            return self._command("Play", lambda h: h.play(), word=word)
        return self._press(target, spot[1] if spot is not None else 0, item=word)

    def _press(self, target: QueueItem, position: int, *, item: Optional[Item] = None) -> bool:
        """A play press on a playlist that is no mirror (lost, drifted, a
        tail mirror): HQPlayer takes the queue afresh — once — and starts at
        `target` — at `position` while it took that very track, else at the
        next it took, from 0 s — as background work through the link: the
        caller is answered at once, the status saying the queue is being
        taken again (pressing names the track). Every play intent made while
        it runs — a Play, a tap, Next, Previous, a DSP change's resume
        (`item`) — is one more start of the same press, numbered as it comes
        on the intent clock: at its end the newest one no newer intent
        superseded goes. One asked while a queue mutation holds words is a
        start among them, its track found among what HQPlayer is handed (one
        not there is refused in words). No retry; no route waits for it."""
        items = self._queue.snapshot()
        at = next((i for i, it in enumerate(items) if it is target), None)
        following = items[at:] if at is not None else [target]
        with self._press_lock:
            words = self._inflight
            if words is not None:
                held = (target if words.index(target) is not None else
                        next((it for it in words.items if target.track_id is not None
                              and it.track_id == target.track_id), None))
                if held is None:
                    self._local.refusal = "it is not in the queue being loaded"
                    return False
                words.starts.append(_Start(
                    item or self._link.item("start", self._link.accept("transport")), held,
                    position, words.items[words.index(held) - 1:], None))
                self.poke()
                return True
            if item is None:
                item = self._link.item("start", self._link.accept("transport"),
                                       queue=self._queue, generation=self._queue.generation)
            press = self._live(self._pressed)
            begun = press is None
            if begun:
                press = self._pressed = _Press(
                    self._link.item("press", item.n, queue=self._queue,
                                    generation=self._queue.generation),
                    self._queue.generation, self._spot_gen)
            press.starts.append(_Start(item, target, position, following, self._spot))
        if begun:
            threading.Thread(target=self._press_run, args=(press,), daemon=True,
                             name="hqp-press").start()
        self.poke()
        return True

    def pressing(self) -> Optional[dict]:
        with self._press_lock:
            words = self._inflight
            if words is not None:
                # What HQPlayer is handed: the index is the slot the track
                # takes once the mutation commits
                start = words.live(self._link)
                if start is not None:
                    at = (start.target, start.position)
                elif words.halted() or words.starts:
                    return {}
                elif words.owes and words.base is not None:
                    at = words.base
                else:
                    return None
                return {"index": words.index(at[0]), "track_id": at[0].track_id,
                        "position": at[1]}
            press = self._live(self._pressed)
            start = press.live(self._link) if press is not None else None
        if press is None:
            return None
        at = self._resolved(start) if start is not None else None
        if at is None:
            return {}
        return {"index": self._queue.index_of(at[0]), "track_id": at[0].track_id,
                "position": at[1]}

    def _press_run(self, press: _Press) -> None:
        try:
            background("HQPlayer did not take the queue for the play press",
                       lambda: self._press_once(press),
                       needed=lambda: "HQPlayer was detached" if self._detached else None,
                       until=lambda: self._detached)
        finally:
            self._press_ended(press)
            self.poke()

    def _press_once(self, press: _Press) -> None:
        """The press's work — from its first step again after a build it
        waited out (through). The queue is read under the queue lock, its
        URIs made and handed over off it: a queue edit meanwhile is the
        queue's alone and never waits for the rebuild. Then the end, under
        the lock again, and the start it owes once the lock is let go. A
        press none of whose starts is live as its rebuild would go, holding
        no Pause or Stop — a word made before it began, without it, came
        first (a tap that waited its turn) — rebuilds nothing, and is let go
        as it decides: a word made from then on goes as on a playlist that
        is no mirror, the playlist stays no mirror, and HQPlayer gets the
        queue back at its next answer without a stop."""
        from playback.manager import manager
        taking = manager.mirror_locked(self, lambda: self._press_begin(press))
        if taking is None:
            return
        if taking is _IN_STEP:
            manager.mirror_locked(self, lambda: self._press_end(press, None, taking, None))
        else:
            layout, _ = taking
            uris = [self._uri_for(it) for it in layout.handed]
            att = self._watch("rebuild", uris=uris)
            with self._steps(att):
                with self._link.command(item=press.item):
                    self._check_attached()
                    with self._press_lock:
                        dead = press.live(self._link) is None and not press.halted()
                        if dead and self._pressed is press:
                            # Never joined again: a press over in all but its
                            # end would take a word and act on nothing, and the
                            # reconverge the loss below arms would leave the
                            # playlist to it
                            self._pressed = None
                    if not dead:
                        self._given = set(uris)
                        if _hqp_safe(self._link, lambda h: h.stop(), "Stop"):
                            press.stopped = True
                        taken, unsent = _add_uris_with_retry(self._link, uris,
                                                             clear_first=True)
                if dead:
                    att.request_end("superseded")
                    self.lose_mirror("a word made before the play press began came first")
                    return
                self.poke()
                manager.mirror_locked(self, lambda: self._press_end(press, att, taking,
                                                                    (taken, unsent)))
        self.queue_start()

    def _press_begin(self, press: _Press):
        """The press's first step, under the queue lock. A new queue since
        (a replace, the radio) took HQPlayer itself: nothing (None). In step
        again meanwhile — a reconverge, the canary — nothing is rebuilt
        (_IN_STEP). Otherwise each slot's way in is read again and HQPlayer
        is handed the queue as it stands; the queue's edits are its own until
        the press ends (the mirror lost to it). Returns the layout handed
        over and each slot's way in as it was (_slot_key)."""
        if self._queue.generation != press.generation:
            return None
        if self._mirror == "step":
            return _IN_STEP
        self._ways_in()
        self._link.refusals = []
        layout = self._layout()
        self._move_mirror("lost", by="press")
        return layout, [_slot_key(it) for it in layout.handed]

    def _press_end(self, press: _Press, att: Optional[diag.Attempt], taking, out) -> None:
        """The press's last step, under the queue lock. A new queue since
        took HQPlayer itself. Otherwise what HQPlayer took is judged (_took)
        and the queue's edits made meanwhile followed (_follow_edits), and
        the press ends: the newest start no newer intent superseded is owed
        (_owe_start) — at its target, the target gone at the next track still
        queued; on a playlist the edits left no mirror, at HQPlayer's own
        entry for it — HQPlayer, stopped by the hand-over, resting at the
        start's spot until it goes. None goes — a Pause, a Stop or a newer
        intent came last: HQPlayer stays stopped where the press was asked to
        start, and a Pause or a Stop stops it there when the press rebuilt
        nothing."""
        void = (self._queue.generation != press.generation
                or self._link.superseded(press.item) is not None)
        took, holds = not void, None
        if not void and taking is not _IN_STEP:
            layout, keys = taking
            taken, unsent = out
            took = self._took(layout.handed, taken, unsent, "of the queue for the play press")
            if took:
                holds = self._follow_edits(press, layout.handed, keys, taken)
        with self._press_lock:
            if self._pressed is press:
                self._pressed = None
            start = press.live(self._link) if took else None
            halted = press.halted()
        if start is not None:
            go = self._press_start(start, holds)
            if go is not None:
                spot, send = go
                held = start.held
                if taking is not _IN_STEP:
                    # Taken afresh while stopped, HQPlayer rests at the start's
                    # spot until the start goes (as _rebuild leaves one): a
                    # Play landing first starts there, and spends it
                    with self._spot_lock:
                        self._spot = held = spot
                        self._spot_gen += 1
                self._owe_start(att or self._watch("play"), start.item, lambda _: send(held),
                                spot=spot)
                return
        if att is not None and (took or void):
            att.request_end("superseded")
        if void:
            return
        if taking is _IN_STEP and halted:
            self._cmd(lambda h: _hqp_safe(self._link, lambda c: c.stop(), "Stop"),
                      item=press.item)
        if press.stopped or (taking is _IN_STEP and halted):
            at = self._left_at(press)
            if at is not None:
                self._put_back(at, press.spot_gen)

    def _press_start(self, start: _Start, holds: Optional[list]) -> Optional[tuple]:
        """Where the press's start goes and how: (its spot, the send — given
        the spot it spends). On the mirror, at its track's slot as it stands
        when it is sent; on a playlist the queue's edits left no mirror, at
        the entry HQPlayer holds it at — the first track from the target on
        that is still queued and HQPlayer took. None when nothing is left to
        start."""
        if self._mirror == "step":
            at = self._resolved(start)
            if at is None:
                return None
            return at, lambda held: self._start_item(at[0], at[1], start.item, held)
        for it in start.following:
            n = next((i for i, h in enumerate(holds or [], 1) if h is it), None)
            if n is None or self._queue.index_of(it) is None:
                continue
            second = start.position if it is start.target else 0
            return (it, second), lambda held: self._start_entry(
                Entry(n), second, it, fallback=True, item=start.item, spends=held)
        return None

    def _follow_edits(self, press: _Press, handed: list, keys: list, taken: list) -> list:
        """The queue's edits made while HQPlayer took it for the press: none,
        or tracks appended behind what it took — those follow it, and it is
        in step. Any other — a removal, a new order, a play-next, a slot
        re-bound — leaves its playlist no mirror (drift): the press starts at
        HQPlayer's own entry, and the next play press hands the queue over
        again. Returns what HQPlayer holds, in order."""
        holds = [it for it, t in zip(handed, taken) if t]
        ways = [k for k, t in zip(keys, taken) if t]
        now = self._layout().handed
        if len(now) < len(holds) or any(a is not it or _slot_key(a) != k
                                         for a, it, k in zip(now, holds, ways)):
            self._move_mirror("drift")
            logger.warning("the queue changed while HQPlayer took it for the play press — "
                           "the next play press hands it over again")
            return holds
        rest = now[len(holds):]
        if rest:
            uris = [self._uri_for(it) for it in rest]
            added, unsent = self._cmd(lambda h: _add_uris_with_retry(self._link, uris),
                                      item=press.item)
            self.poke()
            if not self._took(rest, added, unsent, "queued while HQPlayer took the queue"):
                return holds
            holds += [it for it, t in zip(rest, added) if t]
        self._in_step()
        return holds

    def _press_ended(self, press: _Press) -> None:
        """A press whose end never came — HQPlayer silent or gone, a build it
        did not finish, a new queue, the output changed — is over all the
        same. A playlist it left half taken is no mirror: HQPlayer gets the
        queue back at its next answer (_reconverge); once its Stop went,
        HQPlayer stays stopped where the press was asked to start."""
        with self._press_lock:
            if self._pressed is not press:
                return
            self._pressed = None
        if self._mirror == "lost" and self._lost_by == "press":
            self.lose_mirror("HQPlayer did not take the queue for the play press")
        if press.stopped:
            at = self._left_at(press)
            if at is not None:
                self._put_back(at, press.spot_gen)

    def _live(self, press: Optional[_Press]) -> Optional[_Press]:
        """`press` while it still leads somewhere — no new queue since, and
        nothing superseded its hand-over (a new queue's intent, a switch
        away); None for one overtaken: no play intent joins it, and it hands
        HQPlayer no queue the reconverge should wait for."""
        if (press is None or press.generation != self._queue.generation
                or self._link.superseded(press.item) is not None):
            return None
        return press

    def _left_at(self, press: _Press) -> Optional[tuple]:
        """Where a press that started nothing leaves the spot: its newest
        start's track, at the second a Seek after a Pause or a Stop gave it
        (a target gone gives way to the next track, from its start)."""
        newest = press.newest()
        at = self._resolved(newest)
        if at is not None and press.halt_second is not None and at[0] is newest.target:
            return at[0], press.halt_second
        return at

    def _resolved(self, start: _Start) -> Optional[tuple]:
        """(the track, its second) a press's start goes at now: its target
        while the queue holds it, else the next track of `following` still
        queued, from its start; None when none is left."""
        if self._queue.index_of(start.target) is not None:
            return start.target, start.position
        nxt = next((it for it in start.following[1:] if self._queue.index_of(it) is not None),
                   None)
        return (nxt, 0) if nxt is not None else None

    def _start_point(self, slot: int, position: int) -> Optional[tuple]:
        """Where a start at the queue's `slot` lands on this HQPlayer: the
        entry the layout names, at `position`, and the slot's item there —
        for a slot it does not hold (a gap), the next one it holds, from its
        start. None when none follows."""
        layout = self._layout()
        held = layout.held(slot)
        if held is None:
            return None
        return layout.entry(held), (position if held == slot else 0), layout.items[held - 1]

    def _start_at(self, slot: int, position: int, *, fallback: bool,
                  intent: Optional[str] = None, item: Any = None,
                  spends: Optional[tuple] = None) -> bool:
        """The queue's slot, from `position` on — where it lands on this
        HQPlayer (_start_point) — as one command sequence serving `item`: a
        person's jump or play press (intent transport, whose Seek and Play
        after the SelectTrack are its start), or a start made earlier.
        `spends`: the spot it resumes (_start_held). Returns whether HQPlayer
        took the start."""
        start = self._start_point(slot, position)
        if start is None:
            return self._started(None, f"nothing from slot {slot} on is on this HQPlayer")
        return self._start_entry(*start, fallback=fallback, intent=intent, item=item,
                                 spends=spends)

    def _start_entry(self, entry: Entry, position: int, target: QueueItem, *, fallback: bool,
                     intent: Optional[str] = None, item: Any = None,
                     spends: Optional[tuple] = None) -> bool:
        """The start at HQPlayer's `entry` — the queue's `target` — as one
        command sequence (_start_held) serving `item`."""
        return self._started(entry, self._cmd(
            lambda h: self._start_held(h, entry, position, target, fallback=fallback,
                                       spends=spends),
            intent, item=item))

    def _start_held(self, h: HQPlayerClient, entry: Entry, position: int,
                    target: QueueItem, *, fallback: bool,
                    spends: Optional[tuple]) -> Optional[str]:
        """_start_on, judged in its own command sequence: taken, the
        spot it resumes is spent (_spent) — the one it read (`spends`), any
        when it read none — before a start dropped meanwhile could put one
        back; given up on — HQPlayer answered nothing past the build limit —
        whether it took it is judged at HQPlayer's next answer
        (_judge_start). Returns HQPlayer's refusal, or None."""
        try:
            refusal = self._start_on(h, entry, position, fallback=fallback)
        except Building:
            if h.timed_out:
                self._given_up(target)
            raise
        if refusal is None:
            self._spent(spends)
        elif h.timed_out:
            self._given_up(target)
        return refusal

    @staticmethod
    def _start_on(h: HQPlayerClient, entry: Entry, position: int, *,
                  fallback: bool) -> Optional[str]:
        """The start as one command sequence, under its command lock:
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
        resume — its state read first, in the same command sequence: an
        HQPlayer that plays on is left alone (False, and no attempt). On a
        playlist that is no mirror the queue's slot names no entry of
        HQPlayer's: the resume is a start of a play press there (_press),
        carrying `item` — a newer DSP change supersedes it as it would here —
        and the press starts it (False: nothing resumed yet). Returns
        whether it resumed."""
        index = self._queue.index_of(entry)
        if index is None:
            return False            # its track left the queue meanwhile
        mirrored = self._mirror == "step" and self._live(self._pressed) is None
        start = self._start_point(index, position)
        if start is None:
            return False            # nothing from it on is on this HQPlayer
        at, second, target = start

        def resume(h: HQPlayerClient) -> Optional[bool]:
            now = h.get_status()
            if now is None or now.state != PlaybackState.STOPPED:
                return False
            if not mirrored:
                return None         # the press, off this command sequence
            with self._intent("resume", slot=at.n):
                return self._started(at, self._start_held(h, at, second, target, fallback=True,
                                                          spends=None))
        ok = self._cmd(resume, item=item)
        if ok is None:
            self._press(entry, position, item=item)
            return False
        self.poke()
        return ok

    def seek(self, seconds: int) -> bool:
        if self._held_seek(int(seconds)) or self._seek_pressed(int(seconds)):
            self.poke()
            return True
        ok = self._command("Seek", lambda h: h.seek(int(seconds)))
        self.poke()
        return ok

    def _held_seek(self, seconds: int) -> bool:
        """A Seek while a queue mutation hands HQPlayer its playlist, as one
        during a play press (_seek_pressed): the newest word's second — a
        start's (a start of its own, the same track), after a Pause or a Stop
        the spot's; before any start, on a mutation that stops HQPlayer and
        owes one a start of its own at the base's track, on one that owes
        none the spot it leaves; with no track to count from a Seek once the
        queue stands. False when no mutation runs."""
        with self._press_lock:
            words = self._inflight
            if words is None:
                return False
            if words.halted():
                # The halt's second, as a press's Seek after a Pause or a
                # Stop: no word of its own — it goes with the halt
                words.halt_second = seconds
                return True
            n = self._link.accept("transport")
            newest = words.newest()
            if newest is not None:
                words.starts.append(_Start(self._link.item("start", n), newest.target, seconds,
                                           newest.following, None, steps=newest.steps))
            elif words.stops and words.base is not None and words.owes:
                target = words.base[0]
                words.starts.append(_Start(self._link.item("start", n), target, seconds,
                                           words.items[words.index(target) - 1:], None))
            elif words.stops and words.base is not None:
                words.base, words.base_moved = (words.base[0], seconds), True
            else:
                words.plain.append((n, "Seek", seconds))
        return True

    def _seek_pressed(self, seconds: int, *, word: Optional[Item] = None) -> bool:
        """A Seek while a play press runs decides nothing about playing: it
        changes the second of the newest word. A start (Play, a tap, Next or
        Previous): a start of its own, numbered as it comes, the same target
        at the new second. A Pause or a Stop: only the spot the press leaves
        takes the second — nothing starts, the next Play starts there. A
        target HQPlayer refused gives way to the next track it took, at 0 s,
        as before. `word`: a held Seek carried out (_send_words) after the
        held Play its batch made a press of — no intent of its own: that
        start takes the second, unless a start made after the Seek leads the
        press by then (the Seek is dropped, as a request a newer intent
        superseded is). False when no press runs."""
        with self._press_lock:
            press = self._live(self._pressed)
            if press is None:
                return False
            if press.halted():
                press.halt_second = seconds
                return True
            newest = press.newest()
            if word is not None:
                if newest.item.n <= word.n:
                    press.starts[press.starts.index(newest)] = replace(newest, position=seconds)
                return True
            item = self._link.item("start", self._link.accept("transport"),
                                   queue=self._queue, generation=self._queue.generation)
            press.starts.append(_Start(item, newest.target, seconds, newest.following,
                                       self._spot))
        return True

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
        """Playable HQPlayer URI for a queue item (_uri_of), noted in the
        hand-over ledger as handed. May transcode (m4a) — call OUTSIDE a
        command sequence; a slow transcode must never hold the connection
        hostage. Only for a slot HQPlayer holds: a gap is never handed over
        (_Layout.handed)."""
        uri, mode = self._uri_of(item)
        diag.note_handover(uri, _slot_key(item), item, mode)
        self._given.add(uri)
        return uri

    def _uri_of(self, item: QueueItem) -> tuple:
        """(the URI, how it is handed) for a queue item — an owned file by
        file-access mode (`_owned_play_uri`), a preview by its proxy URL,
        both naming the address HQPlayer reaches us at."""
        # What THIS output opens (QueueItem.play, playback.substitute): its
        # own held copy or a rip here for a slot queued elsewhere.
        src = item.opener()
        if src["kind"] == "file":
            uri = _owned_play_uri(item, self._url_host)
            return uri, ("path" if uri.startswith("file://")
                         else "transcode" if "/preview/" in uri
                         else "cut" if src.get("cue_start") is not None else "stream")
        if src["kind"] == "hqp":
            # Held in HQPlayer's own library: it opens the path itself.
            return file_path_to_uri(src["path"]), "held"
        if src["kind"] == "proxy":
            from streaming import service as streaming_service
            return streaming_service.get_proxy().url_for(src["token"], host=self._url_host), "preview"
        return src["uri"], "foreign"

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

        def read(h: HQPlayerClient) -> tuple:
            status = h.get_status()
            playlist = h.get_playlist()
            return status, (playlist if h.last_error is None else None)
        try:
            status, playlist = self._cmd(read)
        except Building:
            raise
        except (BrokenPipeError, ConnectionError, OSError) as e:
            self.lose_mirror(f"re-bound slots: HQPlayer unreachable ({e})")
            return
        # No reading is no empty playlist: HQPlayer gets the queue back at its
        # next answer
        if status is None or playlist is None:
            self.lose_mirror("re-bound slots: HQPlayer did not say what it holds")
            return
        # A state the SDK does not name is HQPlayer starting a play: a replace
        # now would stop it — the next rebind, or the play press, converges
        if not status.state.known:
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
                 what: str, patient: bool = False,
                 reading: Optional[Entry] = None) -> Optional[bool]:
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
        `target` once the queue lock is let go (_owe_start, _start_at),
        wherever the target is by then — `patient` for background work,
        whose failure is only logged; a start that does not go leaves its
        spot to the play press. The second holds only for the track it was
        taken on: a target HQPlayer does not hold, or refused, gives way to
        the next track it took, at 0 s. Not resuming it lands nowhere —
        SelectTrack would start a stopped HQPlayer (select) — and the spot
        stays with `target` at `position` (a gap too: the start resolves it
        when it goes), where the play press resumes. Numbered as it is asked: the play
        intents made while it hands HQPlayer the queue are held, the start it
        owes their base (_hold_words). `reading`: the entry HQPlayer played as
        the rebuild was planned — read again in its sequence before the Stop:
        moved on since, nothing is sent and what plays never stops — the
        mirror is lost, HQPlayer gets what lies behind the entry it reads
        once it answers again — and None is returned. Under the queue lock.
        Returns whether HQPlayer holds the queue now — then it is in step."""
        self._ways_in()
        self._link.refusals = []
        layout = self._layout(items)
        uris = [self._uri_for(it) for it in layout.handed]
        from_target = items[next((i for i, it in enumerate(items) if it is target), 0):]
        onward = {id(it) for it in from_target}
        first = next((i for i, it in enumerate(layout.handed, 1) if id(it) in onward), 1)
        att = self._watch("rebuild", slot=first, uris=uris) if resume else None
        with self._handing(items, stops=True, base=(target, position), owes=resume,
                           intent="rebuild") as n, self._steps(att):
            fresh = self._afresh(uris, "rebuild", number=n, reading=reading)
            if fresh is None:
                if att is not None:
                    # Nothing went: HQPlayer's own move came first — nothing
                    # of HQPlayer's to judge, never a failure to explain
                    att.request_end("superseded")
                self.lose_mirror("HQPlayer does not read the slot a new order was planned on")
                return None
            taken, unsent, n = fresh
            took = {id(it) for it, t in zip(layout.handed, taken) if t}
            begin = next((it for it in from_target if id(it) in took), None)
            at = position if begin is target else 0
            if begin is None or unsent < len(uris):
                pass        # nothing of ours to start at, or HQPlayer stopped answering
            elif resume:
                self._owe_start(att, self._link.item("start", n),
                                lambda it: self._start_item(begin, at, it),
                                spot=(begin, at), patient=patient)
            else:
                self._set_spot(target, position)
        self.poke()
        if not self._took(layout.handed, taken, unsent, what):
            return False
        self._in_step()
        return True

    def _start_item(self, item: QueueItem, position: int, start: Item,
                    held: Optional[tuple] = None) -> bool:
        """A start owed for later — a rebuild's, a play press's — at `item`'s
        slot as it stands now, spending the spot it read (`held`; any when
        none)."""
        index = self._queue.index_of(item)
        if index is None:
            raise Dropped("its track left the queue")
        return self._start_at(index, position, fallback=True, item=start, spends=held)

    def queue_replace(self, items: list, *, play: bool, probe_first: bool = False) -> list:
        """HQPlayer's playlist becomes what it holds of `items` — a new queue
        (intent queue), each slot's way in read for it — and the queue all of
        them (mirror-first): a slot it cannot open, or refuses while it takes
        others, is a gap (_took); refused whole, or silent before it took
        any, nothing commits, and since its Stop went (a file it refuses
        clears nothing) the mirror is lost — the next Play gives it the queue
        as it was. Nothing of `items`
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
            raise OutputRefused(f"none of these tracks is on {self.label} — {self.gap(items[0])}")
        uris = [self._uri_for(it) for it in layout.handed]
        att = (self._watch("replace", slot=1, uris=uris, items=list(layout.handed))
               if play and uris else None)
        with self._handing(layout.items, stops=True,
                           base=(layout.handed[0], 0) if layout.handed else None, owes=play,
                           intent="queue") as n, self._steps(att):
            taken, unsent, n = self._afresh(uris, "queue", number=n)
            if uris and not any(taken):
                if unsent < len(uris):
                    why = "HQPlayer took nothing of a new queue before it fell silent"
                    self.lose_mirror(why)
                else:
                    why = f"HQPlayer refused all {len(uris)} tracks of a new queue"
                    self.lose_mirror(why, by="refusal")
                    self._note_refused(layout.handed, taken)
                self._local.refusal = why
                accepted = []
            else:
                self._in_step()
                self._link.refusals = []
                self._took(layout.handed, taken, unsent, "of a new queue")
                accepted = list(items)
                if play:
                    self._owe_start(att, self._link.item("start", n),
                                    lambda it: self._play_replaced(it, sum(taken), probe_first))
        self.poke()
        return accepted

    def _afresh(self, uris: list, intent: str, *, number: Optional[int] = None,
                reading: Optional[Entry] = None) -> Optional[tuple]:
        """HQPlayer's playlist taken afresh, as `intent` (a new queue, a
        rebuild): stopped, cleared, `uris` added — `number`: the one the
        mutation took as it began holding words (_hold_words), superseding
        from then. `reading`: the entry HQPlayer must still play, read first
        — another, or none said (_still_reads), and nothing is sent (None).
        Returns (taken, unsent) as _add_uris_with_retry does, and the
        intent's number."""
        with self._link.command(intent, number=number) as n:
            self._check_attached()
            if reading is not None:
                now = self._link.client.get_status()
                if now is None or not _still_reads(now, reading):
                    return None
            self._given = set(uris)
            if _hqp_safe(self._link, lambda h: h.stop(), "Stop"):
                with self._press_lock:
                    if self._inflight is not None:
                        self._inflight.stopped = True
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
        handed over (_queue_takes). With the mirror lost the append is the
        queue's alone: lost through a silence, HQPlayer gets the queue back
        when it answers again (_reconverge); lost through a refusal, or to a
        play press under way, the press hands it over."""
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

    def queue_insert_next(self, items: list, anchor_index: Optional[int]) -> tuple:
        """Insert right after the playing slot via the seamless remove-after /
        re-append trick — the reading slot is never touched, so audio plays
        through. URI-based so it works for preview streams too. The new
        tracks go after the last entry HQPlayer holds at or before the anchor
        slot — or the slot HQPlayer reads, read in the insert's own sequence,
        when it moved on past the anchor; a spot on a gap, or a run's earlier
        track HQPlayer does not hold, has none of its own; in a tail mirror
        counted from the entry that reads the playing slot. An anchor
        HQPlayer's playlist cannot place (_place_after) is no place to edit
        by: the insert is the queue's alone and the playlist is marked no
        mirror, never added at its end. A removal HQPlayer does not take ends
        the removals there, and only what left is appended again: never an
        entry twice, and the playlist is marked no mirror (drift) until the
        next one. A slot this
        HQPlayer cannot open is never handed over (_queue_takes). With the
        mirror lost or drifted — a playlist that is no mirror is not edited by
        the queue's places — or HQPlayer not answering, the insert is the
        queue's alone, as an append on a lost mirror is. A person's play
        intents made while HQPlayer takes it are held (_hold_around).
        Returns what the queue takes and the slot it goes behind: the one
        HQPlayer read as it took the tracks, when it moved on past the
        anchor."""
        self._ways_in(items)
        if self._mirror in ("lost", "drift"):
            return list(items), anchor_index
        handed = _Layout(items, self.gap).handed
        uris = [self._uri_for(it) for it in handed]
        anchor = self._queue.item_at(anchor_index) if anchor_index else None
        queue = self._queue.snapshot()
        at = anchor_index or len(queue)
        self._hold_around(queue[:at] + list(items) + queue[at:])
        unplaced = None
        behind = anchor_index

        def insert(h: HQPlayerClient) -> tuple:
            nonlocal unplaced, behind
            raw = h.get_playlist()
            if h.last_error is not None:
                # No reading — a silence, a refusal — is no shorter playlist:
                # the insert is the queue's, HQPlayer given the queue back
                # once it answers (never drift, which nothing mends)
                why = f"HQPlayer's playlist was not read for a play next ({h.refusal()})"
                unplaced = lambda: self.lose_mirror(why)
                return [], 0, True
            placed, unplaced = self._place_after(h, raw, anchor)
            if placed is None:
                return [], 0, True
            place, behind, reads = placed
            if reads is not None:
                self._held_from(queue[:behind] + list(items) + queue[behind:], reads)
            # Remove the after-segment (always the entry after `place`, which
            # the rest shift down into), then re-append it behind the new
            # tracks.
            after = [t.get("uri") for t in raw[place:] if t.get("uri")]
            removed = 0
            while removed < len(after) and _hqp_safe(
                    self._link, lambda c: _remove(c, Entry(place + 1)), "PlaylistRemove"):
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
        if unplaced is not None:
            unplaced()
            return list(items), anchor_index
        return self._queue_takes(items, handed, taken, unsent, "tracks to play next"), behind

    def _place_after(self, h: HQPlayerClient, raw: list,
                     item: Optional[QueueItem]) -> tuple:
        """How many of HQPlayer's entries the tracks played after the queue's
        `item` go behind — every entry holding a slot up to `item`'s, `item`
        a gap or not, or up to the slot HQPlayer reads, read again here, when
        it moved on past `item`; in a tail mirror counted from the entry that
        reads the playing slot. No anchor: behind them all. Returns ((that
        number, the queue slot they go behind, the queue's track HQPlayer
        reads as read here — None with no anchor), None),
        or (None, what marks the playlist) when HQPlayer's playlist cannot
        place it: shorter than the queue it holds, reading no slot of the
        queue's — another controller's entry; in step also an entry it is
        opening, a stop under way or a state the SDK does not name, which
        gives no verdict (_still_reads) — or reading a file queued at more
        than one slot it holds (no verdict which)."""
        if item is None:
            return (len(raw), None, None), None
        layout = self._layout()
        slot = self._queue.index_of(item)
        if self._mirror != "tail":
            # The slot it plays, read now: moved on past the anchor since the
            # status the anchor came from, the tracks go behind what it reads
            now = h.get_status()
            if now is None:
                return None, lambda: self.lose_mirror(
                    "HQPlayer did not say what it reads for a play next")
            named = now.state in (PlaybackState.STOPPED, PlaybackState.PAUSED,
                                  PlaybackState.PLAYING)
            playing = self._playing(now, mutating=True) if named else None
            if playing is None:
                return None, lambda: self.lose_mirror(
                    "HQPlayer gave no verdict on what it reads for a play next")
            if playing[2]:
                return None, lambda: self.lose_mirror(
                    "HQPlayer reads an entry that is not the queue's for a play next")
            if playing[0] > slot:
                slot = playing[0]
            place = layout.upto(slot)
            if place > len(raw):
                return None, lambda: self._drifted(
                    "HQPlayer's playlist is shorter than the queue it holds")
            return (place, slot, playing[1]), None
        read, why = self._tail_read(h, layout)
        if read is None:
            return None, lambda: self.lose_mirror(why)
        playing, reads = read
        if reads.n > len(raw):
            return None, lambda: self.lose_mirror("HQPlayer reads past its own playlist")
        if slot < playing:
            return None, lambda: None       # the part before the entry read: the queue's
        return (reads.n + layout.upto(slot) - layout.upto(playing), slot,
                self._queue.item_at(playing)), None

    def _tail_read(self, h: HQPlayerClient, layout: _Layout) -> tuple:
        """In a tail mirror, the queue slot HQPlayer reads and its entry, read
        inside the edit's own sequence: ((slot, entry), None) — or (None, why)
        when the entry names no slot of the queue's (stopped on an entry not
        the queue's included: slot 0), or a file queued at more than one slot
        HQPlayer holds (no verdict which one it reads: an edit behind a guess
        would land on the wrong copy)."""
        now = h.get_status()
        playing = self._playing(now, mutating=True) if now is not None else None
        reads = Entry.read(now) if now is not None else None
        if playing is None or playing[2] or not playing[0] or reads is None:
            return None, "HQPlayer reads no slot of the queue's"
        if len(self._named(now, layout.handed)) > 1:
            return None, "HQPlayer reads a file queued at more than one slot"
        return (playing[0], reads), None

    def queue_remove(self, index: int) -> bool:
        """Mirror-first while HQPlayer's playlist mirrors the queue: the slot
        leaves the queue once it left HQPlayer — or once HQPlayer left the
        removal unanswered within its wait: the mirror lost, it removes the
        entry once its silence is over — and a removal HQPlayer refuses
        raises in its words (the slot is still there — the queue did not
        change). A slot HQPlayer does not hold (a gap) leaves the queue alone.
        In a tail mirror a slot behind the playing one is removed at the
        entry it has behind the entry read, one before it from the queue
        alone. A playlist that is no mirror — drift (an external edit, a
        mirror step HQPlayer did not take) or lost — is not edited by index:
        the removal is the queue's alone, and the next mirror carries it. A
        person's play intents made while HQPlayer takes it are held
        (_hold_around)."""
        if self._mirror in ("lost", "drift"):
            return True
        layout = self._layout()
        if layout.entry(index) is None:
            return True                                 # a gap: never handed over
        queue = self._queue.snapshot()
        self._hold_around(queue[:index - 1] + queue[index:])
        if self._mirror == "tail":
            unread = None

            def at_offset(h: HQPlayerClient) -> Optional[Entry]:
                nonlocal unread
                read, unread = self._tail_read(h, layout)
                if read is None or index <= read[0]:
                    return None                         # the part before: the queue's
                # Behind the entry read, HQPlayer holds the queue's slots in order
                return read[1] + (layout.upto(index) - layout.upto(read[0]))
            target = self._cmd(at_offset, intent=None)
            if unread is not None:
                self.lose_mirror(unread)
            if target is None:
                return True
        else:
            target = layout.entry(index)
        unanswered = False

        def why(h: HQPlayerClient) -> str:
            nonlocal unanswered
            unanswered = h.timed_out and not h.answered
            return h.refusal()
        refusal = self._refusal_of("PlaylistRemove", lambda h: _remove(h, target), why,
                                   intent=None)
        self.poke()
        if refusal is not None and unanswered:
            # HQPlayer carries out after its silence what was sent into it: the
            # slot leaves the queue, and HQPlayer gets the queue back once it
            # answers again
            self.lose_mirror(f"HQPlayer did not answer a removal ({refusal})")
            return True
        if refusal is not None:
            raise OutputRefused(f"HQPlayer did not remove it: {refusal}")
        return True

    def queue_clear_after_current(self, archive: Callable[[], None]) -> bool:
        """Radio start. PlaylistClear keeps the reading slot intact — it
        erases everything queued around the current track while the seed
        plays on. A new queue: what was left to start is superseded. The
        session rotation (`archive`) runs in the same command sequence,
        before the clear: busy or lent, the sequence is refused before
        either; a rotation that fails clears nothing; a clear HQPlayer
        refuses leaves the queue as it is (mirror-first), and the radio
        follows it, history rotated; one it leaves unanswered within its wait
        is the queue's — the radio goes on, the mirror lost, and HQPlayer
        clears once its silence is over. Playing or paused on the slot the
        queue keeps, HQPlayer holds the queue after it — in step, a tail
        mirror too; moved on from that slot meanwhile, the mirror is lost.
        Stopped, HQPlayer reads no entry its clear would keep: what it left
        is no mirror of the queue's seed, and it gets the queue back at its
        next answer (_reconverge)."""
        def clear(h: HQPlayerClient) -> tuple:
            archive()
            now = h.get_status()
            playing = self._playing(now, mutating=True) if now is not None else None
            unanswered = False

            def clear_it(c: HQPlayerClient) -> bool:
                nonlocal unanswered
                took = c.playlist_clear()
                unanswered = not took and c.timed_out and not c.answered
                return took
            took = _hqp_safe(self._link, clear_it, "PlaylistClear")
            return took, unanswered, now, playing
        last = self._last_tick
        seed = self._queue.item_at(last.queue_index) if last is not None else None
        with self._handing([seed] if seed is not None else [], stops=False,
                           base=(seed, int(last.position)) if seed is not None else None,
                           owes=False, intent="queue") as n:
            cleared, unanswered, now, playing = self._cmd(clear, "queue", number=n)
        if unanswered:
            # HQPlayer clears once its silence is over: the radio goes on, and
            # HQPlayer gets the queue back once it answers again
            self.lose_mirror("HQPlayer did not answer the radio's clear")
            self.poke()
            return True
        busy = now is not None and now.state in (PlaybackState.PLAYING, PlaybackState.PAUSED)
        # The queue keeps the slot its last status named (clear_for_radio)
        kept = self._last_tick.queue_index if self._last_tick is not None else None
        if cleared and busy and playing is not None and not playing[2] and playing[0] == kept:
            # It kept the entry it reads, the slot the queue keeps: whatever
            # the mirror was (a tail), HQPlayer holds the queue now
            self._in_step()
        elif cleared and busy and playing is not None and not playing[2]:
            self.lose_mirror("a radio began while HQPlayer moved on from the slot it keeps")
        elif cleared and not busy:
            self.lose_mirror("a radio began while HQPlayer read no entry to keep")
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
        offset, read in the sequence; any other new order — or one behind an
        entry that gives no verdict (no slot of the queue's, a file queued
        twice, another slot than the plan's) — is the queue's alone, and the
        mirror is lost: HQPlayer gets what lies behind its entry at its next
        answer. In step the entry is read in the sequence too: moved on since
        the plan's status, the new order is the queue's and the mirror lost
        the same way — the entry it plays is never removed, nor restarted by
        a rebuild — and so is a step HQPlayer leaves unanswered (it carries it
        out once its silence is over).
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
        # Stopped at a spot on a gap, HQPlayer holds no entry for the slot the
        # seamless sequence pivots on — and has no sound to keep: a rebuild
        if plan.seamless and (tail or self._layout().entry(plan.status_idx) is not None):
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
            def held(items: list) -> list:
                return [it for it in items if self.gap(it) is None]
            old_after, new_after = held(plan.old_after), held(plan.new_after)
            old_before, new_before = held(plan.old_before), held(plan.new_before)
            uris = [self._uri_for(it) for it in new_after]
            current = self._layout().entry(plan.status_idx)

            unplaced = None

            def reorder(hqp: HQPlayerClient) -> Optional[str]:
                nonlocal unplaced
                k = current
                if tail:
                    read, unplaced = self._tail_read(hqp, self._layout())
                    if read is None:
                        return None
                    if read[0] != plan.status_idx:
                        unplaced = "HQPlayer reads another entry than the playing slot"
                        return None
                    k = read[1]
                else:
                    # In step too, the entry HQPlayer reads now: it may have
                    # moved on since the status the plan was made on
                    now = hqp.get_status()
                    if now is None:
                        unplaced = "HQPlayer did not say what it reads for a new order"
                        return None
                    if not _still_reads(now, k):
                        unplaced = "HQPlayer does not read the slot a new order was planned on"
                        return None
                if k is None:
                    return "HQPlayer holds no entry for the playing slot"
                taken = (all(_remove(hqp, k + 1) for _ in old_after)
                         and all(_add_one(hqp, uri) for uri in uris))
                cursor = Entry(1)
                new_before_remaining = list(new_before)
                for old_track in old_before:
                    if not taken:
                        break
                    if (new_before_remaining
                            and new_before_remaining[0] is old_track):
                        new_before_remaining.pop(0)
                        cursor = cursor + 1
                    else:
                        taken = _remove(hqp, cursor)
                if taken:
                    return None
                if hqp.timed_out and not hqp.answered:
                    # HQPlayer carries the step out once its silence is over
                    unplaced = "HQPlayer did not answer a step of the new order"
                    return None
                return hqp.refusal()
            # HQPlayer plays on through it: the play intents made meanwhile
            # are held, counted from the playing track
            last = self._last_tick
            self._hold_words(plan.order, stops=False,
                             base=(self._queue.item_at(plan.status_idx),
                                   int(last.position) if last is not None else 0),
                             owes=False)
            refusal = self._cmd(reorder)
            self.poke()
            if unplaced is not None:
                # No entry of HQPlayer's to edit by — never one guessed: the
                # new order is the queue's, and HQPlayer gets what lies behind
                # the entry it reads once it answers again
                self.lose_mirror(unplaced)
                return unchanged
            if refusal is not None:
                self._drifted("HQPlayer did not take the new order")
                logger.warning("HQPlayer did not take the new order: %s",
                               diag.redact_text(refusal))
                raise OutputRefused(f"HQPlayer did not take the new order: {refusal}")
            return {
                "removed": len(plan.old_after) + (len(plan.old_before) - len(plan.new_before)),
                "added": len(plan.new_after),
                "interrupted": False,
            }

        # Fallback: clear+rebuild. Required for cases that need an insert
        # (swap inside before, after→before crossing, current right-shift).
        items = plan.order
        if self._rebuild(items, items[plan.new_status_idx - 1],
                         spot[1] if spot is not None and not plan.resume else plan.position,
                         resume=plan.resume, what="in the new order",
                         reading=self._layout().entry(plan.status_idx) if plan.resume
                         else None) is None:
            return unchanged
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
