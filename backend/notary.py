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
against a budget the Worker keeps per identity (Ф20) and per address, so
wakes inside the pace coalesce into one stamp at its end: an album's worth
of streamed tracks is one root, not twelve. The pace is the Worker's
`not_before` when it gave one, STAMP_MIN_SPACING_S otherwise. Every
deadline here is known when it is set — the loop waits on its event with
the deadline as the timeout, never polls.

Witness records (desktop/p2p/witness.py) are the one producer outside this
process — the launcher records from its own — so they wake the notary
through the database: an insert NOTIFYs sautium_witness, the listener here
wakes it with WITNESS, and a pass woken only for them signs them and
nothing else (sign_audio.sign_witness). They ride the next stamp made for
analysis. A node with no analysis to stamp would leave them unstamped
forever, so a backstop allows a stamp of witness rows alone once the oldest
has waited WITNESS_BACKSTOP_S plus this node's own offset. The backstop is a
PERMISSION kept in a field of its own, never the stamp slot: `_stamp_at` is
"not before", and a backstop parked there would hold the node's analysis
stamps back for days, or be erased by the next pace deferral. The loop's
next attempt is the slot when one is set, else the backstop no earlier than
the pace; an attempt carries witness_only once the backstop has passed, and
a failed one keeps the permission and climbs the retry ladder like any.
"""

import hashlib
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from db_pool import Listener

logger = logging.getLogger(__name__)

# The node's own minimum gap between two Worker stamps, for a Worker that
# sends no `not_before`: a node listening all day makes at most four stamps
# an hour, and a normal album lands in one.
STAMP_MIN_SPACING_S = 15 * 60
# Worker unreachable, or throttled without a Retry-After: the retry ladder,
# holding at the last rung.
_RETRY_S = (60, 300, 900, 3600)

# The wake of a recorded sighting (the sautium_witness listener).
WITNESS = "witness"
# A witness row stamps alone once the oldest has waited this long — a week
# of Worker silence at most for a node with nothing else to stamp — plus a
# per-node offset in [0, WITNESS_OFFSET_S): the launch day's households
# would otherwise reach their backstops together, and the Worker's budget
# prices identities that stamp from one address on one day as a cohort.
WITNESS_BACKSTOP_S = 7 * 24 * 3600
WITNESS_OFFSET_S = 48 * 3600


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def backstop_offset(pubkey_hex: str) -> float:
    """This node's constant share of WITNESS_OFFSET_S, from its key — drawn
    afresh at every re-arm it would drift toward the earliest draw."""
    digest = hashlib.blake2b(b"sautium-witness-backstop:" + bytes.fromhex(pubkey_hex),
                             digest_size=8).digest()
    return float(int.from_bytes(digest, "big") % WITNESS_OFFSET_S)


class _Notary:
    def __init__(self) -> None:
        self._wake_ev = threading.Event()
        self._stop_ev = threading.Event()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._conn = None
        self._reasons: list = []
        self._full = False
        self._stamp_at: Optional[float] = None    # a scheduled stamp attempt
        self._earliest = 0.0                      # the pace: no stamp before this
        self._backstop: Optional[float] = None    # from then on witness rows may stamp alone
        self._offset = 0.0                        # this node's share of WITNESS_OFFSET_S
        self._failures = 0
        self._state = {
            "running": False,
            "last_sign_at": None, "last_signed": 0,
            "last_stamp_at": None, "last_stamped": 0,
            "next_stamp_at": None, "last_error": None,
            "witness_backstop_at": None,
        }

    # ---- public ----------------------------------------------------------
    def start(self) -> bool:
        from config import settings
        from p2p_identity import load_signing_key
        key = load_signing_key(settings)
        if key is None:
            logger.info("notary: no signing identity — sealing disabled")
            return False
        if self._thread and self._thread.is_alive():
            return True
        self._offset = backstop_offset(key.public_key().public_bytes_raw().hex())
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
    def _next_attempt(self) -> Optional[float]:
        """Under the lock: when the loop tries a stamp on its own — the
        scheduled attempt when one is set, else the witness backstop, no
        earlier than the pace."""
        if self._stamp_at is not None:
            return self._stamp_at
        if self._backstop is not None:
            return max(self._backstop, self._earliest)
        return None

    def _loop(self) -> None:
        while not self._stop_ev.is_set():
            with self._lock:
                due = self._next_attempt()
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
        # A wake for sightings alone signs them alone: a probe of a new peer
        # must not cost the catalogue scans an analysis wake runs.
        if not full and all(r == WITNESS for r in reasons):
            step = sign_audio.sign_witness
        else:
            def step(c):
                return sign_audio.sign(c, full=full)
        total = 0
        while True:
            n = step(conn)
            total += n
            if n < sign_audio.MAX_RECORDS_PER_BATCH:
                break
        if total:
            logger.info("notary: signed %d record(s) on wake (%s)",
                        total, ", ".join(dict.fromkeys(reasons)))
        self._set(last_sign_at=_now_iso(), last_signed=total)

    def _stamp_if_due(self) -> None:
        import sign_audio
        now = time.time()
        with self._lock:
            due = self._stamp_at
            earliest = self._earliest
            backstop = self._backstop
        if due is not None and now < due:
            return
        if due is None and now < earliest:
            self._defer_stamp(earliest - now)
            return
        conn = self._connect()
        try:
            result = sign_audio.stamp(conn, witness_only=backstop is not None and now >= backstop)
        except sign_audio.NotaryThrottled as e:
            wait = e.retry_after or self._backoff()
            logger.warning("notary: Worker throttled — next stamp in %ds", wait)
            self._defer_stamp(wait)
            self._set(last_error="Worker throttled")
            return
        capped = result.stamped >= sign_audio.MAX_RECORDS_PER_BATCH
        with self._lock:
            self._stamp_at = None
            self._failures = 0
            if result.stamped:
                # The Worker's `not_before` is the budget's lower bound, not
                # a "stamp now": the node's own spacing is what coalesces a
                # listening burst into one root — taking the Worker's word
                # alone stamped after every 5-minute background pass while
                # its bucket had tokens (8 roots in 40 min, 2026-09-06). A
                # capped batch is a known backlog, not a burst to coalesce:
                # it continues as soon as the Worker allows.
                server = float(result.not_before or 0)
                own = 0.0 if capped else time.time() + STAMP_MIN_SPACING_S
                self._earliest = max(server, own)
            self._backstop = (None if result.witness_waiting is None
                              else result.witness_waiting.timestamp()
                              + WITNESS_BACKSTOP_S + self._offset)
            backstop = self._backstop
        self._set(next_stamp_at=None, last_error=None,
                  witness_backstop_at=None if backstop is None
                  else datetime.fromtimestamp(backstop, tz=timezone.utc).isoformat())
        if result.stamped:
            self._set(last_stamp_at=_now_iso(), last_stamped=result.stamped)
            if capped:
                self._defer_stamp(max(0.0, self._earliest - time.time()))

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


def drain_witness() -> int:
    """Sign every unsigned witness row now, on the caller's thread, with the
    key in force — the first step of an identity rotation
    (device_auth.rotate_identity), so a sighting stays signed by the key that
    saw it and no seen_at falls before its signer's issuance. Safe beside the
    notary's own signing (a row is signed once, by whichever comes first);
    the stamps follow at the notary's next pass.

    What it cannot cover: a sighting recorded after the new key is in force
    by a peer surface still bound to the old one (the launcher's until its
    P2P layer restarts on the rotation's NOTIFY, a Docker node's until its
    backend restarts) is signed by the new key, its seen_at before that key's
    certificate — a verifier takes it with the rotation notice that links
    the two keys."""
    import sign_audio
    conn = sign_audio.connect()
    try:
        total = 0
        while True:
            n = sign_audio.sign_witness(conn)
            total += n
            if n < sign_audio.MAX_RECORDS_PER_BATCH:
                return total
    finally:
        conn.close()


def _witness_wake(*_args) -> None:
    _notary.wake(WITNESS)


# Every runtime records into p2p_witness from wherever the sighting landed —
# the launcher from its own process — and the insert trigger NOTIFYs. The
# sweep on every (re)connect wakes the notary for rows recorded while no
# listener was there to hear them.
_witness_listener = Listener("witness-listener", ("sautium_witness",),
                             on_notify=_witness_wake, on_connect=_witness_wake)


def start_witness_listener() -> None:
    _witness_listener.start()


def stop_witness_listener() -> None:
    _witness_listener.stop()
