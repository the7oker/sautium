"""Who holds a live wake stream to a relay — the one registry both relay
surfaces use (backend/routers/peer_chat.py, desktop/p2p/sync_server.py).

A stream belongs to a NODE, not to a key. An account runs on every machine
its owner signs into, all under one key, and the registry used to keep one
stream per key: a second machine's subscription closed the first's, which
came back five seconds later and closed it in turn — two nodes of one
account took turns every ~11 s for as long as both ran, each turn a history
pull against the relay's per-IP window, and a forwarded message reached
whichever held the slot (2026-09-24). Each subscription now names its node
with `instance`, a token the node draws once per process, and supersedes
only that node's previous stream. What the relay has for a key goes to
every stream of it: a wake is a hint each node follows with its own history
pull, a forwarded envelope is stored by each (the first receipt answers the
sender, a later one finds no forward waiting), a support warrant is
deduplicated by id on each and fulfilled by the first bundle.

The instance is not a credential and is not signed: the subscription's
signature proves the key, and nothing a stream carries can be read or
answered without it. A subscriber that sends none holds the empty instance —
one slot per key, which is how nodes from before 2026-09-24 still behave.

Thread-safe: the backend signals streams from its LISTEN thread.
"""

import asyncio
import re
import threading
from collections import deque
from typing import Iterator, Optional

MAX_PER_IP = 20
# Nodes of one account. Past it the OLDEST stream yields to the newcomer —
# a stream whose node died without closing it is the oldest of its key.
MAX_PER_KEY = 8
# Forwarded envelopes waiting on one stream.
QUEUE_MAX = 100

_INSTANCE = re.compile(r"[0-9a-f]{0,32}")


def valid_instance(instance: str) -> bool:
    return _INSTANCE.fullmatch(instance) is not None


class WakeSub:
    """One live wake stream. The relay's handler waits on `evt`, takes what
    arrived with WakeRegistry.drain and writes it down the stream."""
    __slots__ = ("evt", "loop", "ip", "kinds", "envelopes", "frames", "closed")

    def __init__(self, loop: asyncio.AbstractEventLoop, ip: str):
        self.evt = asyncio.Event()
        self.loop = loop
        self.ip = ip
        self.kinds: set = set()
        # A queue, not a set: each envelope is a distinct message, and
        # order is the sender's.
        self.envelopes: deque = deque()
        # Typed frames beyond deliver/wake — the support warrant
        # (backend/routers/peer_diag.py).
        self.frames: deque = deque()
        self.closed = False

    def signal(self) -> None:
        self.loop.call_soon_threadsafe(self.evt.set)


class WakeRegistry:
    def __init__(self):
        self._subs: dict[str, dict[str, WakeSub]] = {}   # key -> instance -> stream, oldest first
        self._lock = threading.Lock()

    def _all(self) -> Iterator[WakeSub]:
        for slots in self._subs.values():
            yield from slots.values()

    def _open(self, pubkey: str) -> list[WakeSub]:
        return [s for s in self._subs.get(pubkey, {}).values() if not s.closed]

    @staticmethod
    def _close(sub: WakeSub) -> None:
        sub.closed = True
        sub.signal()

    def register(self, pubkey: str, instance: str, ip: str) -> Optional[WakeSub]:
        """A stream for node `instance` of `pubkey`, superseding that node's
        previous one; None when `ip` already holds MAX_PER_IP streams."""
        sub = WakeSub(asyncio.get_running_loop(), ip)
        with self._lock:
            slots = self._subs.get(pubkey, {})
            old = slots.pop(instance, None)
            if old is not None:
                self._close(old)
            else:
                if sum(1 for s in self._all() if s.ip == ip) >= MAX_PER_IP:
                    return None
                if len(slots) >= MAX_PER_KEY:
                    self._close(slots.pop(next(iter(slots))))
            slots[instance] = sub
            self._subs[pubkey] = slots
        return sub

    def unregister(self, pubkey: str, instance: str, sub: WakeSub) -> bool:
        """Drop `sub` unless a newer stream took its slot. True when no
        stream of `pubkey` is left — where a relay client's announce ends."""
        with self._lock:
            slots = self._subs.get(pubkey)
            if slots is None:
                return True
            if slots.get(instance) is sub:
                del slots[instance]
            if slots:
                return False
            del self._subs[pubkey]
            return True

    def drain(self, sub: WakeSub) -> tuple[list, list, list]:
        """(frames, envelopes, wake kinds) queued on `sub` since the last
        drain — written in that order: an instruction, the payload, then
        only a hint to go looking."""
        with self._lock:
            frames, envelopes, kinds = list(sub.frames), list(sub.envelopes), sorted(sub.kinds)
            sub.frames.clear()
            sub.envelopes.clear()
            sub.kinds.clear()
        return frames, envelopes, kinds

    def ping(self, pubkey: Optional[str], kind: str = "message") -> None:
        """Wake every stream of `pubkey` — of every key when None."""
        with self._lock:
            subs = list(self._all()) if pubkey is None else self._open(pubkey)
            for sub in subs:
                sub.kinds.add(kind)
                sub.signal()

    def push_frame(self, pubkey: str, frame: dict) -> bool:
        """Queue a typed frame on every stream of `pubkey`; False when the
        key holds none."""
        with self._lock:
            subs = self._open(pubkey)
            for sub in subs:
                sub.frames.append(frame)
                sub.signal()
        return bool(subs)

    def queue_envelope(self, pubkey: str, envelope: dict) -> Optional[str]:
        """Queue a forwarded envelope on every stream of `pubkey` with room.
        None when queued, else the refusal: "not connected" or "busy"."""
        with self._lock:
            subs = self._open(pubkey)
            if not subs:
                return "not connected"
            room = [s for s in subs if len(s.envelopes) < QUEUE_MAX]
            if not room:
                return "busy"
            for sub in room:
                sub.envelopes.append(envelope)
                sub.signal()
        return None

    def close(self, pubkey: str) -> None:
        """End every stream of `pubkey` — a relay that stops relaying."""
        with self._lock:
            for sub in self._subs.get(pubkey, {}).values():
                self._close(sub)

    def keys(self) -> list[str]:
        with self._lock:
            return list(self._subs)
