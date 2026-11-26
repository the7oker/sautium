"""The account's key, derived once, in the background (SEC-S5).

The key costs minutes (node_identity.ARGON2_TIME_COST: about 5 on a desktop,
12 on a 16 GB Pi 5, 26 on a 1 GB board), and nothing else the node does waits
for it: the account exists from its first second (node_identity's account
file), and only what speaks for the node — P2P, the notary's seals, the
email's code — waits for the key. One worker makes it, on every road: the Web
UI's setup and a change of identity (backend/device_auth), the env road's
first start (backend/p2p_identity), the launcher's wizard and its window
(desktop/wizard, desktop/launcher).

One at a time in a process: a start while one runs is refused (False). A
thread, not a process — argon2-cffi releases the GIL, so the event loop and
Tk keep running beside it, and the password never crosses a pipe; a stop of
the process ends it, and the account file says so (no `pubkey`): the next
sign-in, its password checked by the hash, starts it again.

Its progress is one estimate said at the start and one event at its end —
Argon2 has no callback in between, and a tick would be a timer: the page
draws its own clock from `started_at` and `expected_s`.
"""

import datetime
import logging
import threading
import time
from typing import Callable, Optional

from desktop import node_identity

logger = logging.getLogger(__name__)

# The calibration's passes: one short derivation at the key's memory, whose
# time a pass gives the whole one's (linear in t, measured: SECURITY §9.4)
CALIBRATION_TIME_COST = 8


def expected_seconds(cost: Optional[tuple] = None) -> float:
    """About how long the key's derivation takes on this machine now: a
    short one at the same memory, scaled by the passes. Measured, not a
    constant: the same board was 1.9× slower under a memory cap (PI-NODE-2B
    §8.15). `cost`: another derivation's (time, memory, parallelism) — an
    identity file's, whose header keeps the cost it was locked at."""
    from argon2.low_level import Type, hash_secret_raw
    time_cost, memory_cost, parallelism = cost or (
        node_identity.ARGON2_TIME_COST, node_identity.ARGON2_MEMORY_COST,
        node_identity.ARGON2_PARALLELISM)
    t0 = time.monotonic()
    hash_secret_raw(secret=b"calibration", salt=b"sautium-calibration",
                    time_cost=CALIBRATION_TIME_COST, memory_cost=memory_cost,
                    parallelism=parallelism, hash_len=node_identity.ARGON2_HASH_LEN, type=Type.ID)
    took = time.monotonic() - t0
    return took * time_cost / CALIBRATION_TIME_COST


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(
        microsecond=0).isoformat().replace("+00:00", "Z")


class Birth:
    """This process's one derivation of an account's key."""

    def __init__(self):
        self._lock = threading.Lock()
        self._state: Optional[dict] = None

    def state(self) -> Optional[dict]:
        """The derivation running now ({"status": "deriving", "purpose",
        "username", "started_at", "expected_s"}), or None."""
        with self._lock:
            return dict(self._state) if self._state else None

    def start(self, username: str, password: str, *, purpose: str,
              done: Callable[[bytes], None], said: Callable[[dict], None],
              failed: Callable[[BaseException], None],
              slot: Optional[threading.Semaphore] = None) -> bool:
        """Derive (username, password)'s seed in a thread of its own. `said`
        hears the derivation's state once, at its start; `done(seed)` runs in
        that thread at its end; `failed(error)` for a derivation that failed
        (logged). `slot`, held for the whole derivation: the memory bound the
        caller shares with its other Argon2 calls. False when one runs."""
        with self._lock:
            if self._state is not None:
                return False
            self._state = {"status": "deriving", "purpose": purpose, "username": username}

        def run():
            try:
                if slot is not None:
                    slot.acquire()
                try:
                    expected = expected_seconds()
                    with self._lock:
                        self._state.update(started_at=_now(), expected_s=round(expected))
                        state = dict(self._state)
                    logger.info("The account's key is being derived (%s, %s): about %d s",
                                purpose, username, expected)
                    said(state)
                    t0 = time.monotonic()
                    seed = node_identity.derive_seed(username, password)
                    logger.info("The account's key is derived (%s): %.0f s", purpose,
                                time.monotonic() - t0)
                finally:
                    if slot is not None:
                        slot.release()
                with self._lock:
                    self._state = None
                done(seed)
            except BaseException as e:
                with self._lock:
                    self._state = None
                logger.error("The account's key could not be made (%s): %s", purpose, e,
                             exc_info=True)
                failed(e)

        threading.Thread(target=run, name="identity-birth", daemon=True).start()
        return True
