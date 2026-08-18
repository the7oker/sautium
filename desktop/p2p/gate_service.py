"""Gate service — the server side of the admission gate on one peer surface
(wire format v1 § 4; primitives in admission.py).

    GET /api/gate/quote?pubkey=…  →  {"quote": core, "tasks": [hex…], "sig": hex}
    priced request + X-Sautium-Gate → verdict

Dormant by default: `price` returns 0 tasks, the quote is still signed
and single-use, and a client that pays anyway (n = 0 → no answers) is
verified through the same path — so the whole handshake runs in the
golden age at zero cost and switching the price on later touches no
protocol. Pricing (base × load × similarity, modes off/shadow/enforce)
arrives in Ф12; the gold/silver pool that fills the packet arrives in Ф11
— `_pool_tasks()` is its seam.

Verification of a payment (`check_payment`): the quote must be OURS (our
signature, our pubkey), for THIS requester (client == request signer),
alive (issued − skew ≤ now ≤ deadline), unused (a per-nonce seen-set that
lives until the deadline — bounded by rate × TTL) and about the tasks we
would derive (tasks_digest recomputed from the gate secret); then R
answers are recomputed under a small semaphore (R × 64 MiB) with server
randomness drawn after submission. A packet that fails is deterministic
evidence: the nonce is consumed and the request is refused; a packet we
could not check (busy, out of memory) is a 503 with Retry-After — the
quote stays valid.

Shared by the launcher (aiohttp) and the Docker peer surface (FastAPI)
through the bind mount.
"""

import asyncio
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from desktop.p2p import admission, identity_pow

logger = logging.getLogger(__name__)

CLOCK_SKEW = 60                    # a quote "from the future" beyond this is invalid
VERIFY_CONCURRENCY = 2             # R × 64 MiB per slot
VERIFY_WAIT_SECONDS = 10.0
RETRY_BUSY_SECONDS = 15
SEEN_CAP = 100_000                 # state-growth cap for the nonce seen-set


@dataclass(frozen=True)
class GateVerdict:
    status: str          # ok | none | invalid | replay | expired | failed | busy
    detail: str = ""
    n: int = 0
    retry_after: Optional[int] = None

    @property
    def http_status(self) -> int:
        return {"ok": 200, "none": 200, "expired": 410, "busy": 503}.get(self.status, 403)

    @property
    def error(self) -> str:
        return {"invalid": "gate_invalid", "replay": "gate_replay", "expired": "gate_expired",
                "failed": "gate_failed", "busy": "gate_busy"}.get(self.status, "")


class GateService:
    def __init__(self, server_pubkey: str, sign: Callable[[bytes], bytes], gate_secret: bytes, *,
                 price: Callable[[str], int] = lambda client_pubkey: 0,
                 sample_r: int = admission.DEFAULT_SAMPLE,
                 params_version: int = admission.CURRENT_GATE_PARAMS_VERSION,
                 clock: Callable[[], float] = time.time):
        self.server_pubkey = server_pubkey.lower()
        self._sign = sign
        self._secret = gate_secret
        self._price = price
        self._r = sample_r
        self._params_version = params_version
        self._params = admission.GATE_PARAMS[params_version]
        self._clock = clock
        self._seen: dict = {}                     # nonce hex → deadline
        self._seen_lock = threading.Lock()
        self._sem = asyncio.Semaphore(VERIFY_CONCURRENCY)

    # -- quote -----------------------------------------------------------------

    def _pool_tasks(self, client_pubkey: str, n_fresh: int) -> list:
        """Ф11 seam: gold/silver entries leased into this packet. None yet."""
        return []

    def quote(self, client_pubkey: str) -> dict:
        client_pubkey = client_pubkey.lower()
        n = max(0, int(self._price(client_pubkey)))
        nonce = secrets.token_bytes(admission.NONCE_LEN)
        inputs = self._packet_inputs(client_pubkey, nonce, n)
        core = admission.build_quote_core(self.server_pubkey, client_pubkey, nonce, inputs,
                                          issued=int(self._clock()),
                                          params_version=self._params_version)
        return {"quote": core, "tasks": [t.hex() for t in inputs],
                "sig": admission.sign_quote(self._sign, core)}

    def _packet_inputs(self, client_pubkey: str, nonce: bytes, n_fresh: int) -> list:
        """Fresh inputs + pool tasks, in the shuffled packet order the server
        can recompute from (secret, nonce) alone."""
        fresh = admission.derive_fresh_inputs(self._secret, nonce, client_pubkey, n_fresh)
        pool = self._pool_tasks(client_pubkey, n_fresh)
        together = fresh + pool
        order = admission.shuffle_positions(self._secret, nonce, len(together))
        return [together[i] for i in order]

    # -- payment ---------------------------------------------------------------

    def _consume_nonce(self, nonce_hex: str, deadline: int) -> bool:
        """True the first time a nonce is presented, False on replay. Taken
        BEFORE the expensive check so two concurrent copies of one packet
        cannot both pass; a transient failure releases it again."""
        now = self._clock()
        with self._seen_lock:
            if nonce_hex in self._seen:
                return False
            if len(self._seen) >= SEEN_CAP or len(self._seen) % 512 == 0:
                for k in [k for k, d in self._seen.items() if d < now]:
                    self._seen.pop(k, None)
            self._seen[nonce_hex] = deadline
            return True

    def _release_nonce(self, nonce_hex: str) -> None:
        with self._seen_lock:
            self._seen.pop(nonce_hex, None)

    async def check_payment(self, header_value: Optional[str], client_pubkey: Optional[str]) -> GateVerdict:
        if not header_value:
            return GateVerdict("none")
        if not client_pubkey:
            return GateVerdict("invalid", "payment requires a signed request")
        sub = admission.decode_submission(header_value)
        if sub is None:
            return GateVerdict("invalid", "payment malformed")
        core, sig, answers = sub["quote"], sub["sig"], sub["answers"]
        if not admission.verify_quote(core, sig, self.server_pubkey):
            return GateVerdict("invalid", "quote signature")
        if core.get("client") != client_pubkey.lower():
            return GateVerdict("invalid", "quote is for another client")
        try:
            n = int(core["n"])
            nonce = bytes.fromhex(core["nonce"])
            issued, deadline = int(core["issued"]), int(core["deadline"])
            params_version = int(core["params_version"])
        except (KeyError, ValueError, TypeError):
            return GateVerdict("invalid", "quote fields")
        now = self._clock()
        if issued > now + CLOCK_SKEW:
            return GateVerdict("invalid", "quote from the future")
        if now > deadline:
            return GateVerdict("expired", "quote expired", n)
        if params_version != self._params_version:
            return GateVerdict("invalid", "params version", n)
        inputs = self._packet_inputs(client_pubkey, nonce, n)
        if len(inputs) != n or admission.tasks_digest(inputs) != core.get("tasks_digest"):
            return GateVerdict("invalid", "tasks digest", n)
        nonce_hex = core["nonce"]
        if not self._consume_nonce(nonce_hex, deadline):
            return GateVerdict("replay", "quote already used", n)
        if len(answers) != n:
            return GateVerdict("failed", "answer count", n)          # nonce stays burned

        # The packet is well-formed for us; verifying costs R·w. A transient
        # inability to verify releases the nonce — the quote stays valid.
        try:
            await asyncio.wait_for(self._sem.acquire(), VERIFY_WAIT_SECONDS)
        except asyncio.TimeoutError:
            self._release_nonce(nonce_hex)
            return GateVerdict("busy", "verifier busy", n, RETRY_BUSY_SECONDS)
        try:
            avail = identity_pow.mem_available_kib()
            if avail is not None and avail < self._params.memory_kib * self._r * 2:
                self._release_nonce(nonce_hex)
                return GateVerdict("busy", "verifier short of memory", n, RETRY_BUSY_SECONDS)
            indices = admission.sample_indices(n, self._r)
            try:
                ok = await asyncio.to_thread(admission.verify_sampled, inputs, answers, indices, self._params)
            except identity_pow.HashingError:
                self._release_nonce(nonce_hex)
                return GateVerdict("busy", "verifier could not allocate", n, RETRY_BUSY_SECONDS)
        finally:
            self._sem.release()
        if ok:
            return GateVerdict("ok", "", n)
        logger.info("gate payment failed for %s (n=%d)", client_pubkey[:8], n)
        return GateVerdict("failed", "sampled answers wrong", n)
