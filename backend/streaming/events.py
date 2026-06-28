"""Process-wide bare-ping pub/sub for the phantom-preview UI.

Publishers are background threads (the proxy fetch loop, the enricher); the
subscriber is the /preview-events SSE endpoint. We deliberately send NO payload
— a ping just says "preview state changed". The open album page re-fetches its
OWN /api/albums/{id} (a single consistent snapshot of features + buffering), so
there is no split-source state to race. Cross-thread delivery via the
subscriber loop's call_soon_threadsafe."""
import asyncio
import logging
import threading

logger = logging.getLogger(__name__)


class PreviewEvents:
    def __init__(self) -> None:
        self._subs: list = []          # list of (asyncio.Queue, loop)
        self._lock = threading.Lock()

    def subscribe(self):
        loop = asyncio.get_running_loop()   # called from the async SSE endpoint
        q: asyncio.Queue = asyncio.Queue(maxsize=64)
        with self._lock:
            self._subs.append((q, loop))
        return q

    def unsubscribe(self, q) -> None:
        with self._lock:
            self._subs[:] = [(qq, ll) for qq, ll in self._subs if qq is not q]

    def ping(self) -> None:
        """Signal 'preview state changed' to every subscriber (thread-safe)."""
        with self._lock:
            subs = list(self._subs)
        for q, loop in subs:
            try:
                loop.call_soon_threadsafe(_offer, q)
            except Exception:
                pass


def _offer(q) -> None:
    # Coalesce: if the queue already holds an unread ping, drop this one — the
    # subscriber will re-fetch the latest state once anyway.
    try:
        q.put_nowait(None)
    except asyncio.QueueFull:
        pass


preview_events = PreviewEvents()
