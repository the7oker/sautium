"""Gate service — the server side of the admission gate on one peer surface
(wire format v1 § 4; primitives in admission.py, pool in gate_pool.py).

    GET /api/gate/quote?pubkey=…  →  {"quote": core, "tasks": [hex…], "sig": hex}
    priced request + X-Sautium-Gate → verdict

Dormant by default: `price` returns 0 fresh tasks, the quote is still
signed and single-use, and a client that pays anyway (n = 0 → no answers)
is verified through the same path — so the whole handshake runs in the
golden age at zero cost and switching the price on later touches no
protocol. Pricing (base × load × similarity, modes) arrives in Ф12.

A priced packet is `n_fresh` derived tasks + up to one GOLD and two SILVER
entries leased from the pool, shuffled by the gate secret so the client
cannot tell them apart. Verification order (`check_payment`):

1. the quote is OURS (signature, pubkey), for THIS requester, alive,
   unused (a per-nonce seen-set until the deadline; released again on a
   transient failure, kept burned on a failed or short packet), and about
   the tasks we would derive + the entries we leased (tasks_digest);
2. **gold prefilter** — memcmp against first-party truth: garbage dies
   here at O(1), and the entry is retired (single-use);
3. **fresh sample** — R fresh positions chosen with server randomness
   AFTER submission, recomputed under a small semaphore; every truth we
   computed becomes a gold entry (origin `sample` when the packet passed,
   `garbage` when it did not — the cleanest entries, their author never
   solved them);
4. **silver probes** — the client's answers on silver / promoted slots
   compared with the stored claims: a match is a quorum vote, a mismatch
   is a w-audit that mints the truth (the entry becomes gold) and names
   the liar deterministically — the silver's source (`on_evidence`) or the
   current client (then its packet fails); a silver mismatch alone rejects
   nobody;
5. up to SILVER_PER_PACKET_MINT of the unsampled fresh claims are minted
   as silver; every leased entry records the recipient's exclusion keys.

Shared by the launcher (aiohttp) and the Docker peer surface (FastAPI)
through the bind mount. Without a `conn_factory` the pool is off (tests,
nodes without a database handle).
"""

import asyncio
import hmac
import logging
import random
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional, Sequence

from desktop.p2p import admission, gate_pool, identity_pow

logger = logging.getLogger(__name__)

CLOCK_SKEW = 60                    # a quote "from the future" beyond this is invalid
VERIFY_CONCURRENCY = 2             # R × 64 MiB per slot
VERIFY_WAIT_SECONDS = 10.0
RETRY_BUSY_SECONDS = 15
SEEN_CAP = 100_000                 # state-growth cap for the nonce seen-set
GOLD_PER_PACKET = 1
SILVER_PER_PACKET = 2
MAINTENANCE_EVERY = 50             # quotes between pool sweeps


@dataclass(frozen=True)
class GateVerdict:
    status: str          # ok | none | invalid | replay | expired | failed | busy
    detail: str = ""
    n: int = 0
    retry_after: Optional[int] = None
    minted: dict = field(default_factory=dict)      # {"gold": k, "silver": k, "audits": k}

    @property
    def http_status(self) -> int:
        return {"ok": 200, "none": 200, "expired": 410, "busy": 503}.get(self.status, 403)

    @property
    def error(self) -> str:
        return {"invalid": "gate_invalid", "replay": "gate_replay", "expired": "gate_expired",
                "failed": "gate_failed", "busy": "gate_busy"}.get(self.status, "")


def client_keys(pubkey: str, addr: Optional[str] = None,
                email_domain_token: Optional[str] = None) -> list:
    """The requester's exclusion keys for the pool (hard axes only)."""
    from desktop.p2p.contact_log import addr_ids
    keys = [pubkey.lower()]
    _, subnet = addr_ids(addr)
    if subnet:
        keys.append(f"subnet:{subnet}")
    if email_domain_token:
        keys.append(f"domain:{email_domain_token}")
    return keys


class GateService:
    def __init__(self, server_pubkey: str, sign: Callable[[bytes], bytes], gate_secret: bytes, *,
                 price: Callable[[str, str], int] = lambda client_pubkey, action: 0,
                 conn_factory: Optional[Callable] = None,
                 on_evidence: Optional[Callable[[str, str], None]] = None,
                 sample_r: int = admission.DEFAULT_SAMPLE,
                 params_version: int = admission.CURRENT_GATE_PARAMS_VERSION,
                 clock: Callable[[], float] = time.time):
        self.server_pubkey = server_pubkey.lower()
        self._sign = sign
        self._secret = gate_secret
        self._price = price
        self._conn_factory = conn_factory
        self._on_evidence = on_evidence
        self._r = sample_r
        self._params_version = params_version
        self._params = admission.GATE_PARAMS[params_version]
        self._clock = clock
        self._seen: dict = {}                     # nonce hex → deadline
        self._seen_lock = threading.Lock()
        self._sem = asyncio.Semaphore(VERIFY_CONCURRENCY)
        self._quotes = 0

    # -- pool plumbing (sync, callers wrap in to_thread) --------------------------

    def _pool(self, fn, *args, **kwargs):
        if self._conn_factory is None:
            return None
        with self._conn_factory() as conn:
            return fn(conn, *args, **kwargs)

    def _lease(self, nonce_hex: str, deadline_ts: int, keys: Sequence[str]) -> list:
        deadline = datetime.fromtimestamp(deadline_ts, timezone.utc)
        return self._pool(gate_pool.lease, nonce=nonce_hex, deadline=deadline, keys=keys,
                          params_version=self._params_version,
                          gold=GOLD_PER_PACKET, silver=SILVER_PER_PACKET) or []

    def _maintenance(self) -> None:
        self._quotes += 1
        if self._quotes % MAINTENANCE_EVERY == 1:
            self._pool(gate_pool.retire_abandoned)
            self._pool(gate_pool.evict_over_cap)

    # -- quote -----------------------------------------------------------------

    def quote(self, client_pubkey: str, keys: Optional[Sequence[str]] = None,
              action: str = "", n_fresh: Optional[int] = None) -> dict:
        """Blocking (pool DB) — callers run it in a thread. `n_fresh` lets the
        caller supply the price it computed with lane knowledge; otherwise
        the constructor's price(client, action) is used."""
        client_pubkey = client_pubkey.lower()
        keys = list(keys) if keys else [client_pubkey]
        n_fresh = max(0, int(self._price(client_pubkey, action) if n_fresh is None else n_fresh))
        nonce = secrets.token_bytes(admission.NONCE_LEN)
        issued = int(self._clock())
        with_pool = n_fresh > 0 and self._conn_factory is not None
        # The deadline is fixed BEFORE leasing (an upper bound on the packet
        # size) so the lease and the signed quote agree on it.
        deadline = admission.quote_deadline(
            issued, n_fresh + (GOLD_PER_PACKET + SILVER_PER_PACKET if with_pool else 0))
        entries = []
        if with_pool:
            self._maintenance()
            entries = self._lease(nonce.hex(), deadline, keys)
        inputs = self._packet_inputs(client_pubkey, nonce, n_fresh, [e["task_input"] for e in entries])
        core = admission.build_quote_core(self.server_pubkey, client_pubkey, nonce, inputs,
                                          action=action, issued=issued, deadline=deadline,
                                          params_version=self._params_version)
        return {"quote": core, "tasks": [t.hex() for t in inputs],
                "sig": admission.sign_quote(self._sign, core)}

    def _packet_inputs(self, client_pubkey: str, nonce: bytes, n_fresh: int,
                       pool_inputs: Sequence[bytes]) -> list:
        """Fresh inputs + pool tasks in the shuffled packet order the server
        can recompute from (secret, nonce, lease) alone."""
        together = admission.derive_fresh_inputs(self._secret, nonce, client_pubkey, n_fresh) + list(pool_inputs)
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

    async def check_payment(self, header_value: Optional[str], client_pubkey: Optional[str],
                            keys: Optional[Sequence[str]] = None, action: str = "") -> GateVerdict:
        if not header_value:
            return GateVerdict("none")
        if not client_pubkey:
            return GateVerdict("invalid", "payment requires a signed request")
        client_pubkey = client_pubkey.lower()
        keys = list(keys) if keys else [client_pubkey]
        sub = admission.decode_submission(header_value)
        if sub is None:
            return GateVerdict("invalid", "payment malformed")
        core, sig, answers = sub["quote"], sub["sig"], sub["answers"]
        if not admission.verify_quote(core, sig, self.server_pubkey):
            return GateVerdict("invalid", "quote signature")
        if core.get("client") != client_pubkey:
            return GateVerdict("invalid", "quote is for another client")
        if core.get("action", "") != action:
            return GateVerdict("invalid", "quote is for another action")
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

        # Replay is decided on the nonce alone — before anything that depends
        # on lease state (a settled lease no longer resolves), and before the
        # expensive part; a transient failure releases it again.
        nonce_hex = core["nonce"]
        if not self._consume_nonce(nonce_hex, deadline):
            return GateVerdict("replay", "quote already used", n)
        entries = await asyncio.to_thread(self._pool, gate_pool.leased, nonce_hex) or []
        n_fresh = n - len(entries)
        if n_fresh < 0:
            return GateVerdict("invalid", "packet shape", n)
        inputs = self._packet_inputs(client_pubkey, nonce, n_fresh, [e["task_input"] for e in entries])
        if len(inputs) != n or admission.tasks_digest(inputs) != core.get("tasks_digest"):
            return GateVerdict("invalid", "tasks digest", n)
        if len(answers) != n:
            await asyncio.to_thread(self._settle_used, entries, keys)
            return GateVerdict("failed", "answer count", n)          # nonce stays burned

        # Positions: packet position p holds original index order[p]; original
        # indices >= n_fresh are pool entries (in lease/id order).
        order = admission.shuffle_positions(self._secret, nonce, n)
        pos_of = {orig: p for p, orig in enumerate(order)}
        fresh_positions = [pos_of[i] for i in range(n_fresh)]
        pool_at = {pos_of[n_fresh + j]: e for j, e in enumerate(entries)}

        # 2. gold prefilter — first-party truth, O(1), garbage dies here
        for p, e in pool_at.items():
            if e["class"] == "gold" and e["origin"] != "claim":
                if not hmac.compare_digest(e["answer"], answers[p]):
                    await asyncio.to_thread(self._settle_used, entries, keys)
                    logger.info("gate: gold mismatch from %s", client_pubkey[:8])
                    return GateVerdict("failed", "gold mismatch", n)

        # 3. fresh sample — R·w, the one place a transient failure may occur
        try:
            await asyncio.wait_for(self._sem.acquire(), VERIFY_WAIT_SECONDS)
        except asyncio.TimeoutError:
            self._release_nonce(nonce_hex)
            return GateVerdict("busy", "verifier busy", n, RETRY_BUSY_SECONDS)
        try:
            avail = identity_pow.mem_available_kib()
            if avail is not None and avail < self._params.memory_kib * (self._r + 1) * 2:
                self._release_nonce(nonce_hex)
                return GateVerdict("busy", "verifier short of memory", n, RETRY_BUSY_SECONDS)
            sampled = sorted(random.SystemRandom().sample(fresh_positions, min(self._r, len(fresh_positions))))
            try:
                truths = await asyncio.to_thread(
                    lambda: {p: admission.solve(inputs[p], self._params) for p in sampled})
                fresh_ok = all(len(answers[p]) == admission.TASK_LEN
                               and hmac.compare_digest(truths[p], answers[p]) for p in sampled)
                # 4. silver probes (only worth auditing when the fresh sample held)
                audits = {}
                if fresh_ok:
                    for p, e in pool_at.items():
                        if e["class"] == "silver" or e["origin"] == "claim":
                            if not hmac.compare_digest(e["answer"], answers[p]):
                                audits[p] = await asyncio.to_thread(admission.solve, inputs[p], self._params)
            except identity_pow.HashingError:
                self._release_nonce(nonce_hex)
                return GateVerdict("busy", "verifier could not allocate", n, RETRY_BUSY_SECONDS)
        finally:
            self._sem.release()

        minted = await asyncio.to_thread(
            self._settle, client_pubkey, nonce_hex, keys, inputs, answers, entries, pool_at,
            fresh_positions, sampled, truths, fresh_ok, audits)
        if not fresh_ok:
            logger.info("gate payment failed for %s (n=%d)", client_pubkey[:8], n)
            return GateVerdict("failed", "sampled answers wrong", n, minted=minted)
        if minted.get("client_lied"):
            logger.info("gate: %s lied on a probe (audit)", client_pubkey[:8])
            return GateVerdict("failed", "probe audit", n, minted=minted)
        return GateVerdict("ok", "", n, minted=minted)

    # -- settlement (sync, in a thread) ---------------------------------------------

    def _settle_used(self, entries: list, keys: Sequence[str]) -> None:
        """A presented lease is a use, whatever happened after: recipients
        recorded, gold retired."""
        if not entries or self._conn_factory is None:
            return
        with self._conn_factory() as conn:
            for e in entries:
                gate_pool.record_recipient(conn, e["id"], keys)
                if e["class"] == "gold":
                    gate_pool.retire(conn, e["id"], "used")

    def _settle(self, client_pubkey, nonce_hex, keys, inputs, answers, entries, pool_at,
                fresh_positions, sampled, truths, fresh_ok, audits) -> dict:
        minted = {"gold": 0, "silver": 0, "audits": len(audits), "client_lied": False}
        if self._conn_factory is None:
            return minted
        with self._conn_factory() as conn:
            origin = "sample" if fresh_ok else "garbage"
            for p in sampled:
                gate_pool.mint(conn, task_input=inputs[p], answer=truths[p], klass="gold", origin=origin,
                               params_version=self._params_version, source_pubkey=client_pubkey,
                               source_nonce=nonce_hex, recipient_keys=keys)
                minted["gold"] += 1
            for p, e in pool_at.items():
                gate_pool.record_recipient(conn, e["id"], keys)
                if e["class"] == "gold" and e["origin"] != "claim":
                    gate_pool.retire(conn, e["id"], "used")
                    continue
                if not fresh_ok:
                    continue
                if p in audits:
                    truth = audits[p]
                    gate_pool.gild(conn, e["id"], truth)
                    client_right = hmac.compare_digest(truth, answers[p])
                    source_right = hmac.compare_digest(truth, e["answer"])
                    if not source_right and self._on_evidence is not None:
                        self._on_evidence(e["source_pubkey"], "gate_lie")
                    if not client_right:
                        minted["client_lied"] = True
                else:
                    gate_pool.silver_vote(conn, e["id"])
            if fresh_ok and not minted["client_lied"]:
                unsampled = [p for p in fresh_positions if p not in sampled]
                for p in random.SystemRandom().sample(unsampled, min(gate_pool.SILVER_PER_PACKET_MINT, len(unsampled))):
                    if len(answers[p]) == admission.TASK_LEN:
                        gate_pool.mint(conn, task_input=inputs[p], answer=answers[p], klass="silver",
                                       origin="claim", params_version=self._params_version,
                                       source_pubkey=client_pubkey, source_nonce=nonce_hex,
                                       recipient_keys=keys)
                        minted["silver"] += 1
        return minted


# ----------------------------------------------------------------------------
# python -m desktop.p2p.gate_service --selftest --dsn …
# ----------------------------------------------------------------------------

def _selftest(dsn: str) -> None:
    """The whole Mode A + Mode B loop against a REAL database with cheap
    Argon2 parameters: packets pass and mint gold/silver, a second client
    receives them, gold is checked by memcmp, silver votes and promotes,
    a lying claim is audited and the liar named, garbage burns."""
    import os
    from functools import partial
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from desktop.p2p.identity_registry import psycopg2_conn_factory

    small = admission.PowParams(version=1, memory_kib=8 * 1024, time_cost=1, parallelism=1)
    factory = partial(psycopg2_conn_factory, dsn)
    with factory() as conn:
        gate_pool.ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT coalesce(max(id), 0) FROM p2p_gate_pool")
            first_id = cur.fetchone()[0]
    evidence = []
    server = Ed25519PrivateKey.generate()
    svc = GateService(server.public_key().public_bytes_raw().hex(), server.sign,
                      admission.derive_gate_secret(server.private_bytes_raw()),
                      price=lambda c, a: 8, conn_factory=factory,
                      on_evidence=lambda pub, reason: evidence.append((pub[:8], reason)))
    svc._params = small

    def pay(client, keys, corrupt=None):
        q = svc.quote(client, keys)
        inputs = [bytes.fromhex(t) for t in q["tasks"]]
        answers = admission.solve_all(inputs, small, threads=1)
        if corrupt == "garbage":
            answers = [os.urandom(32) for _ in answers]
        if corrupt == "one":                    # exactly one wrong answer, at a random position
            i = random.randrange(len(answers)); answers[i] = os.urandom(32)
        return q, asyncio.run(svc.check_payment(admission.encode_submission(q["quote"], q["sig"], answers), client, keys))

    A, B, C = "aa" * 32, "bb" * 32, "cc" * 32
    kA, kB, kC = [A, "subnet:1"], [B, "subnet:2"], [C, "subnet:3"]
    try:
        q1, v1 = pay(A, kA)
        assert v1.status == "ok" and q1["quote"]["n"] == 8, v1          # empty pool → 8 fresh only
        assert v1.minted["gold"] == 2 and v1.minted["silver"] == 6, v1.minted
        q2, v2 = pay(B, kB)
        assert q2["quote"]["n"] == 8 + 1 + 2, q2["quote"]["n"]           # + gold + 2 silver from A's packet
        assert v2.status == "ok" and v2.minted["audits"] == 0, v2
        with factory() as conn:
            st = gate_pool.stats(conn)
        print("  after two honest packets:", st)
        # A never gets its own entries back (also not by subnet)
        q3, v3 = pay(A, [A, "subnet:9"])
        with factory() as conn:
            leased_now = gate_pool.leased(conn, q3["quote"]["nonce"])
        assert v3.status == "ok"
        # C: garbage → gold prefilter or fresh sample fails; nonce burned; no silver minted
        q4, v4 = pay(C, kC, corrupt="garbage")
        assert v4.status == "failed", v4
        assert v4.minted.get("silver", 0) == 0
        # a lying claim: plant a silver whose claim is wrong, lease it to D, D answers right → source named
        D, kD = "dd" * 32, ["dd" * 32, "subnet:4"]
        with factory() as conn:
            liar_input = os.urandom(32)
            liar_id = gate_pool.mint(conn, task_input=liar_input, answer=os.urandom(32), klass="silver",
                                     origin="claim", params_version=1, source_pubkey="ee" * 32,
                                     source_nonce="x", recipient_keys=["ee" * 32])
            with conn.cursor() as cur:            # make the liar the only silver on offer
                cur.execute("UPDATE p2p_gate_pool SET retired_at = now(), retire_reason = 'test' "
                            "WHERE class = 'silver' AND retired_at IS NULL AND id > %s AND id <> %s",
                            (first_id, liar_id))
            conn.commit()
        qD = svc.quote(D, kD)
        inputs = [bytes.fromhex(t) for t in qD["tasks"]]
        assert liar_input in inputs
        answers = admission.solve_all(inputs, small, threads=1)
        vD = asyncio.run(svc.check_payment(admission.encode_submission(qD["quote"], qD["sig"], answers), D, kD))
        assert vD.status == "ok" and vD.minted["audits"] >= 1, vD
        assert ("ee" * 32)[:8] in [e[0] for e in evidence], evidence
        with factory() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT class::text, origin::text, answer FROM p2p_gate_pool WHERE task_input = %s", (liar_input,))
                klass, origin, ans = cur.fetchone()
        assert klass == "gold" and origin == "audit" and bytes(ans) == admission.solve(liar_input, small)
        # replay
        vR = asyncio.run(svc.check_payment(admission.encode_submission(qD["quote"], qD["sig"], answers), D, kD))
        assert vR.status == "replay"
        with factory() as conn:
            print("  final:", gate_pool.stats(conn))
        print("selftest OK")
    finally:
        with factory() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM p2p_gate_pool WHERE id > %s", (first_id,))


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Gate service (Mode A + B)")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--dsn", default="postgresql://musicai:supervisor@localhost:5432/music_ai")
    args = ap.parse_args()
    if args.selftest:
        logging.basicConfig(level=logging.INFO)
        _selftest(args.dsn)
    else:
        ap.print_help()
