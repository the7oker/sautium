"""The node's sealing owner.

One thread, woken by every producer of signable state — the scanner, the
analysis run, the stream enricher, the background pass — that author-signs
at once and Worker-stamps on its own cadence (sign_audio.sign / stamp).

Why one owner: sealing used to be a call each producer remembered to make
(post-scan, post-analysis-run), and the stream enricher — the third
producer — never did, so first-hand analysis of streamed phantoms sat
unsigned, invisible to every peer, until an unrelated scan happened to run
(30 Portico Quartet tracks, 2026-09-06). Producers now emit "signable state
committed" and nothing else is expected of them; the wake is the whole
contract.

Why two cadences: a signature is local and instant, so it lands on every
wake — the row is final from then on and no later uuid rewrite can be
mis-attested (the guard sheds it). A stamp is one Worker call per batch
against a per-IP hourly budget shared with whoever else sits behind the
same address (CGNAT), so wakes inside STAMP_MIN_SPACING_S coalesce into
one stamp at the window's end: an album's worth of streamed tracks is one
root, not twelve. Every deadline here is known when it is set — the loop
waits on its event with the deadline as the timeout, never polls.
"""

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# Minimum gap between two Worker stamps from this node. Sized against the
# Worker's per-IP budget (10/h, shared by a CGNAT cohort): a node listening
# all day makes at most four stamps an hour, and a normal album lands in one.
STAMP_MIN_SPACING_S = 15 * 60
# Worker unreachable, or throttled without a Retry-After: the retry ladder,
# holding at the last rung (the Worker's rate window is an hour).
_RETRY_S = (60, 300, 900, 3600)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class _Notary:
    def __init__(self) -> None:
        self._wake_ev = threading.Event()
        self._stop_ev = threading.Event()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._conn = None
        self._reasons: list = []
        self._full = False
        self._stamp_at: Optional[float] = None
        self._last_stamp = 0.0
        self._failures = 0
        self._state = {
            "running": False,
            "last_sign_at": None, "last_signed": 0,
            "last_stamp_at": None, "last_stamped": 0,
            "next_stamp_at": None, "last_error": None,
        }

    # ---- public ----------------------------------------------------------
    def start(self) -> bool:
        from config import settings
        from p2p_identity import load_signing_key
        if load_signing_key(settings) is None:
            logger.info("notary: no signing identity — sealing disabled")
            return False
        if self._thread and self._thread.is_alive():
            return True
        self._stop_ev.clear()
        self._thread = threading.Thread(target=self._loop, name="notary", daemon=True)
        self._thread.start()
        self._set(running=True)
        logger.info("notary started")
        return True

    def stop(self) -> None:
        self._stop_ev.set()
        self._wake_ev.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=5)
        self._drop_conn()
        self._set(running=False)

    def wake(self, reason: str, full: bool = False) -> None:
        """Something signable was committed (any thread). `full` asks for the
        album layer rescanned end to end — a canon pass shed seals, a
        reconcile minted tracklists under already-signed tracks — instead of
        scoped to the tracks whose analysis just landed."""
        if not (self._thread and self._thread.is_alive()):
            return
        with self._lock:
            self._reasons.append(reason)
            self._full = self._full or full
        self._wake_ev.set()

    def status(self) -> dict:
        with self._lock:
            return dict(self._state)

    # ---- loop ------------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop_ev.is_set():
            with self._lock:
                due = self._stamp_at
            woke = self._wake_ev.wait(None if due is None
                                      else max(0.0, due - time.time()))
            if self._stop_ev.is_set():
                break
            with self._lock:
                self._wake_ev.clear()
                reasons, self._reasons = self._reasons, []
                full, self._full = self._full, False
            try:
                if woke:
                    self._sign(reasons, full)
                self._stamp_if_due()
            except Exception as e:
                # A DB hiccup (restart, dropped connection): let the
                # connection go and come back on the retry rung — the
                # signatures and the unstamped rows are all in the database,
                # nothing is lost with the pass.
                logger.warning("notary pass failed: %s", e)
                self._drop_conn()
                self._defer_stamp(self._backoff())
                self._set(last_error=str(e)[:200])

    def _sign(self, reasons: list, full: bool) -> None:
        import sign_audio
        conn = self._connect()
        total = 0
        while True:
            n = sign_audio.sign(conn, full=full)
            total += n
            if n < sign_audio.MAX_RECORDS_PER_BATCH:
                break
        if total:
            logger.info("notary: signed %d record(s) on wake (%s)",
                        total, ", ".join(reasons))
        self._set(last_sign_at=_now_iso(), last_signed=total)

    def _stamp_if_due(self) -> None:
        import sign_audio
        now = time.time()
        with self._lock:
            due = self._stamp_at
            since_last = now - self._last_stamp
        if due is not None and now < due:
            return
        if due is None and since_last < STAMP_MIN_SPACING_S:
            self._defer_stamp(STAMP_MIN_SPACING_S - since_last)
            return
        conn = self._connect()
        try:
            n = sign_audio.stamp(conn)
        except sign_audio.NotaryThrottled as e:
            wait = e.retry_after or self._backoff()
            logger.warning("notary: Worker throttled — next stamp in %ds", wait)
            self._defer_stamp(wait)
            self._set(last_error="Worker throttled")
            return
        with self._lock:
            self._stamp_at = None
            self._failures = 0
            if n:
                self._last_stamp = time.time()
        self._set(next_stamp_at=None, last_error=None)
        if n:
            self._set(last_stamp_at=_now_iso(), last_stamped=n)
            if n >= sign_audio.MAX_RECORDS_PER_BATCH:
                # A capped backlog (a full re-seal) continues at once: each
                # batch is one Worker call, a handful in a row is within budget.
                self._defer_stamp(0)

    # ---- helpers ---------------------------------------------------------
    def _defer_stamp(self, seconds: float) -> None:
        at = time.time() + max(0.0, seconds)
        with self._lock:
            self._stamp_at = at
        self._set(next_stamp_at=datetime.fromtimestamp(at, tz=timezone.utc).isoformat())

    def _backoff(self) -> int:
        with self._lock:
            self._failures += 1
            return _RETRY_S[min(self._failures, len(_RETRY_S)) - 1]

    def _connect(self):
        if self._conn is None or self._conn.closed:
            import sign_audio
            self._conn = sign_audio.connect()
        return self._conn

    def _drop_conn(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None and not conn.closed:
            conn.close()

    def _set(self, **fields) -> None:
        with self._lock:
            self._state.update(fields)


_notary = _Notary()
start = _notary.start
stop = _notary.stop
wake = _notary.wake
status = _notary.status
