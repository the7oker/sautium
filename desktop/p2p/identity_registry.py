"""Identity registry — every certified identity this node has met, and the
one-time proof verification behind identity-gated decisions.

`p2p_identities` is this node's LOCAL ledger of certified peers: the
certificate fields, `first_seen_at` (the anchor of witnessed age — a
hoarded certificate ripens in nobody's drawer), the last contact, and the
verification status of the proof-of-work. It is never shared: history is
per-node, banlists are local (docs/design/P2P-SYNC-INTEGRITY.md
§§ "Proof-of-work certificates", "Defense strategy").

Verification is LAZY. Seeing an identity costs nothing but an upsert; the
~1.5 s Argon2id evaluation runs the first time the identity claims
something identity-gated (today: accepting an invite token that requires
a certificate; later: the identity lane, relay vouchers, preferred
sources). A stranger who never asks for anything is never verified — and
a flood of forged proofs is never evaluated unless its author invests in
coming back. `IdentityGate.admit` is that decision point: shape + authority
signature (µs) → ban list → registry rules → for `method: pow` one
evaluation under a process-wide semaphore of ONE (the 2 GiB working set
beside a resident ML stack is the real limit, not CPU) with an available-
memory guard, `HashingError` = "busy, retry", a per-address failure
backstop (a forged proof is a once-per-lifetime event for an honest peer,
so five failures an hour from one place is a WAF-grade signal). A proof
that verifies marks the identity `verified` for good; a proof that does not
is deterministic evidence: `failed` + `p2p_node_bans`.

Registry rules for a pubkey we already know: the authority anchor
`issued_at` must match (idempotent issuance — anything else is an anomaly
and the update is refused); `method` only ever moves pow → email (a device
holding a superseded pow certificate for an email identity is stale, not
suspicious); `email_token` may change (a changed mailbox); `first_seen_at`
and a `verified` status survive every update — the work was done.

Ripening (`T_min`) is a separate, computed property (`is_ripe`): the
identity lane will require it; token acceptance does not — a newborn
must be able to befriend the master minutes after birth, ripening buys
nothing on a social bit and would only delay every newcomer.

Shared by the launcher (aiohttp sync server) and the Docker peer surface
(FastAPI) through the bind mount — one implementation of "verified".
"""

import asyncio
import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from desktop.p2p import identity_pow, identity_proof

logger = logging.getLogger(__name__)

RIPENING_SECONDS = 3600            # T_min — see is_ripe()
IDENTITY_ROWS_CAP = 50_000         # state-growth cap: oldest unverified evicted
BAN_ROWS_CAP = 10_000              # pow_failed rows kept (keys are free)
FAILS_PER_ADDR_HOUR = 5
VERIFY_WAIT_SECONDS = 20.0         # queue wait before answering "busy"
RETRY_BUSY_SECONDS = 30
RETRY_RATE_LIMITED_SECONDS = 3600

SCHEMA_SQL = (
    """DO $$ BEGIN
         CREATE TYPE p2p_cert_method AS ENUM ('pow', 'email');
       EXCEPTION WHEN duplicate_object THEN NULL; END $$""",
    """DO $$ BEGIN
         CREATE TYPE p2p_email_class AS ENUM ('major', 'other', 'disposable');
       EXCEPTION WHEN duplicate_object THEN NULL; END $$""",
    """DO $$ BEGIN
         CREATE TYPE p2p_identity_status AS ENUM ('unverified', 'verified', 'failed');
       EXCEPTION WHEN duplicate_object THEN NULL; END $$""",
    """CREATE TABLE IF NOT EXISTS p2p_identities (
         pubkey          TEXT PRIMARY KEY,
         cert_v          SMALLINT NOT NULL,
         method          p2p_cert_method NOT NULL,
         issued_at       TIMESTAMPTZ NOT NULL,
         difficulty      BIGINT NOT NULL,
         params_version  SMALLINT NOT NULL,
         email_token     TEXT,
         email_class     p2p_email_class,
         issuer          TEXT NOT NULL,
         cert_sig        TEXT NOT NULL,
         proof_nonce     TEXT,
         status          p2p_identity_status NOT NULL DEFAULT 'unverified',
         fail_reason     TEXT,
         first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
         verified_at     TIMESTAMPTZ,
         last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
         contacts        BIGINT NOT NULL DEFAULT 1,
         first_addr      UUID,
         last_addr       UUID
       )""",
    """CREATE INDEX IF NOT EXISTS idx_p2p_identities_email_token
         ON p2p_identities (email_token) WHERE email_token IS NOT NULL""",
    """CREATE INDEX IF NOT EXISTS idx_p2p_identities_last_seen
         ON p2p_identities (last_seen_at)""",
    """CREATE INDEX IF NOT EXISTS idx_p2p_identities_issued
         ON p2p_identities (issued_at)""",
)


def ensure_schema(conn) -> None:
    """Idempotent DDL — a node updated in place gains the registry without a
    manual migration (desktop/migrations/001_initial.sql carries the same)."""
    with conn.cursor() as cur:
        for stmt in SCHEMA_SQL:
            cur.execute(stmt)
    conn.commit()


def parse_issued_at(iso: str) -> datetime:
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def is_ripe(cert: dict, now: Optional[datetime] = None) -> bool:
    now = now or datetime.now(timezone.utc)
    return (now - parse_issued_at(cert["issued_at"])).total_seconds() >= RIPENING_SECONDS


def addr_uuid(host: Optional[str]) -> Optional[str]:
    if not host:
        return None
    from desktop.p2p.mb_slice_queries import addr_uuid as _addr_uuid
    return _addr_uuid(host)


_ROW_COLUMNS = ("pubkey", "cert_v", "method", "issued_at", "difficulty",
                "params_version", "email_token", "email_class", "issuer",
                "cert_sig", "proof_nonce", "status", "fail_reason",
                "first_seen_at", "verified_at", "last_seen_at", "contacts",
                "first_addr", "last_addr")


def get(conn, pubkey: str) -> Optional[dict]:
    with conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(_ROW_COLUMNS)} FROM p2p_identities WHERE pubkey = %s",
                    (pubkey.lower(),))
        row = cur.fetchone()
    return dict(zip(_ROW_COLUMNS, row)) if row else None


def touch(conn, pubkey: str, addr: Optional[str] = None) -> Optional[dict]:
    """A signed request from an already-known identity: bump the contact
    counters, return the row (None when the identity never introduced
    itself here — the caller answers `X-Sautium-Peer-Identity: unknown`)."""
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE p2p_identities
               SET last_seen_at = now(), contacts = contacts + 1,
                   last_addr = COALESCE(%s, last_addr)
             WHERE pubkey = %s
        """, (addr_uuid(addr), pubkey.lower()))
        hit = cur.rowcount
    conn.commit()
    return get(conn, pubkey) if hit else None


def lane_for(row: Optional[dict], now: Optional[datetime] = None) -> str:
    """anonymous | stranger | identity — the identity lane needs a verified
    AND ripe identity (standing joins the condition later)."""
    from desktop.p2p.peer_auth import LANE_ANONYMOUS, LANE_IDENTITY, LANE_STRANGER
    if row is None:
        return LANE_ANONYMOUS
    now = now or datetime.now(timezone.utc)
    ripe = (now - row["issued_at"]).total_seconds() >= RIPENING_SECONDS
    return LANE_IDENTITY if row["status"] == "verified" and ripe else LANE_STRANGER


def is_banned(conn, pubkey: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM p2p_node_bans WHERE pubkey = %s LIMIT 1", (pubkey.lower(),))
        return cur.fetchone() is not None


def observe(conn, cert: dict, *, proof: Optional[dict] = None,
            addr: Optional[str] = None) -> dict:
    """Upsert a certified identity under the registry rules (module doc).
    `cert` must already be authority-verified by the caller. Returns the
    row after the update; `row['anomaly']` is True when the update was
    refused because the authority anchor changed."""
    pubkey = cert["pubkey"].lower()
    issued_at = parse_issued_at(cert["issued_at"])
    addr_id = addr_uuid(addr)
    proof_nonce = proof["nonce"] if identity_proof.proof_binds(proof, cert) else None
    email_status = "verified" if cert["method"] == "email" else "unverified"
    existing = get(conn, pubkey)
    with conn.cursor() as cur:
        if existing is None:
            cur.execute("""
                INSERT INTO p2p_identities (pubkey, cert_v, method, issued_at, difficulty,
                    params_version, email_token, email_class, issuer, cert_sig, proof_nonce,
                    status, verified_at, first_addr, last_addr)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        CASE WHEN %s = 'verified' THEN now() END, %s, %s)
            """, (pubkey, cert["v"], cert["method"], issued_at, cert["difficulty"],
                  cert["params_version"], cert.get("email_token"), cert.get("email_class"),
                  cert["issuer"], cert["sig"], proof_nonce, email_status, email_status,
                  addr_id, addr_id))
            _evict_if_over_cap(cur)
            conn.commit()
            row = get(conn, pubkey)
            row["anomaly"] = False
            return row

        if existing["issued_at"] != issued_at:
            logger.warning("identity %s presented a certificate with a different issued_at "
                           "(%s vs known %s) — refusing the update",
                           pubkey[:8], cert["issued_at"], existing["issued_at"].isoformat())
            existing["anomaly"] = True
            return existing

        stale = existing["method"] == "email" and cert["method"] == "pow"
        if stale:
            cur.execute("""
                UPDATE p2p_identities
                   SET last_seen_at = now(), contacts = contacts + 1,
                       last_addr = COALESCE(%s, last_addr)
                 WHERE pubkey = %s
            """, (addr_id, pubkey))
        else:
            upgrade = existing["method"] == "pow" and cert["method"] == "email"
            cur.execute("""
                UPDATE p2p_identities
                   SET cert_v = %s, method = %s, difficulty = %s, params_version = %s,
                       email_token = %s, email_class = %s, issuer = %s, cert_sig = %s,
                       proof_nonce = COALESCE(%s, proof_nonce),
                       status = CASE WHEN %s AND status <> 'failed' THEN 'verified'::p2p_identity_status
                                     ELSE status END,
                       verified_at = CASE WHEN %s AND status <> 'failed' AND verified_at IS NULL
                                          THEN now() ELSE verified_at END,
                       last_seen_at = now(), contacts = contacts + 1,
                       last_addr = COALESCE(%s, last_addr)
                 WHERE pubkey = %s
            """, (cert["v"], cert["method"], cert["difficulty"], cert["params_version"],
                  cert.get("email_token"), cert.get("email_class"), cert["issuer"], cert["sig"],
                  proof_nonce, upgrade, upgrade, addr_id, pubkey))
    conn.commit()
    row = get(conn, pubkey)
    row["anomaly"] = False
    return row


def _evict_if_over_cap(cur) -> None:
    cur.execute("SELECT count(*) FROM p2p_identities")
    if cur.fetchone()[0] <= IDENTITY_ROWS_CAP:
        return
    cur.execute("""
        DELETE FROM p2p_identities WHERE pubkey IN (
            SELECT pubkey FROM p2p_identities WHERE status = 'unverified'
             ORDER BY last_seen_at ASC LIMIT 1000)
    """)


def mark_verified(conn, pubkey: str, proof_nonce: str) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE p2p_identities
               SET status = 'verified', verified_at = COALESCE(verified_at, now()),
                   proof_nonce = %s, fail_reason = NULL
             WHERE pubkey = %s
        """, (proof_nonce, pubkey.lower()))
    conn.commit()


def mark_failed(conn, pubkey: str, addr: Optional[str], reason: str) -> None:
    """Deterministic evidence: the identity is `failed` here and its pubkey
    goes on the local ban list (never shared)."""
    pubkey = pubkey.lower()
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE p2p_identities SET status = 'failed', fail_reason = %s
             WHERE pubkey = %s
        """, (reason, pubkey))
        cur.execute("""
            INSERT INTO p2p_node_bans (pubkey, addr, reason) VALUES (%s, %s, %s)
        """, (pubkey, addr_uuid(addr), reason))
        cur.execute("SELECT count(*) FROM p2p_node_bans WHERE reason = %s", (reason,))
        if cur.fetchone()[0] > BAN_ROWS_CAP:
            cur.execute("""
                DELETE FROM p2p_node_bans WHERE id IN (
                    SELECT id FROM p2p_node_bans WHERE reason = %s
                     ORDER BY created_at ASC LIMIT 500)
            """, (reason,))
    conn.commit()


@dataclass(frozen=True)
class Admission:
    status: str                    # verified | invalid | proof_required | failed | banned | busy | rate_limited
    detail: str = ""
    retry_after: Optional[int] = None

    @property
    def http_status(self) -> int:
        return {"verified": 200, "busy": 503, "rate_limited": 429}.get(self.status, 403)


class IdentityGate:
    """The identity-gated admission of one process (module doc)."""

    def __init__(self, conn_factory: Callable, verify_certificate: Callable[[dict], bool], *,
                 mem_available_kib: Callable[[], Optional[int]] = identity_pow.mem_available_kib,
                 mem_guard_kib: int = identity_proof.MEM_GUARD_KIB):
        """`conn_factory()` returns a context manager yielding a psycopg2
        connection; `verify_certificate` is the surface's authority mirror."""
        self._conn_factory = conn_factory
        self._verify_certificate = verify_certificate
        self._mem_available_kib = mem_available_kib
        self._mem_guard_kib = mem_guard_kib
        self._sem = asyncio.Semaphore(1)
        self._fails: dict = {}
        self._fails_lock = threading.Lock()

    def _addr_rate_limited(self, addr: Optional[str]) -> bool:
        if not addr:
            return False
        now = time.time()
        with self._fails_lock:
            stamps = [t for t in self._fails.get(addr, ()) if now - t < 3600]
            self._fails[addr] = stamps
            return len(stamps) >= FAILS_PER_ADDR_HOUR

    def _note_failure(self, addr: Optional[str]) -> None:
        if not addr:
            return
        with self._fails_lock:
            self._fails.setdefault(addr, []).append(time.time())
            if len(self._fails) > 10_000:
                self._fails = {a: s for a, s in self._fails.items() if s and time.time() - s[-1] < 3600}

    def _db(self, fn):
        with self._conn_factory() as conn:
            return fn(conn)

    async def admit(self, pubkey: str, cert: dict, proof: Optional[dict],
                    addr: Optional[str]) -> Admission:
        pubkey = pubkey.lower()
        if not (isinstance(cert, dict) and self._verify_certificate(cert)
                and cert.get("pubkey", "").lower() == pubkey):
            return Admission("invalid", "identity certificate invalid")

        def _observe(conn):
            if is_banned(conn, pubkey):
                return None
            return observe(conn, cert, proof=proof, addr=addr)

        row = await asyncio.to_thread(self._db, _observe)
        if row is None:
            return Admission("banned", "identity banned")
        if row["anomaly"]:
            return Admission("invalid", "certificate anchor mismatch")
        if row["status"] == "failed":
            return Admission("failed", "identity proof previously failed")
        if row["status"] == "verified":
            return Admission("verified")

        # method: pow, not yet verified — the one-time evaluation
        if not identity_proof.proof_binds(proof, cert):
            return Admission("proof_required", "identity proof required")
        if self._addr_rate_limited(addr):
            return Admission("rate_limited", "too many failed proofs from this address",
                             RETRY_RATE_LIMITED_SECONDS)
        try:
            await asyncio.wait_for(self._sem.acquire(), VERIFY_WAIT_SECONDS)
        except asyncio.TimeoutError:
            return Admission("busy", "verifier busy", RETRY_BUSY_SECONDS)
        try:
            avail = self._mem_available_kib()
            if avail is not None and avail < self._mem_guard_kib:
                return Admission("busy", "verifier short of memory", RETRY_BUSY_SECONDS)
            try:
                ok = await asyncio.to_thread(identity_proof.verify_proof, proof, cert)
            except identity_pow.HashingError:
                return Admission("busy", "verifier could not allocate", RETRY_BUSY_SECONDS)
        finally:
            self._sem.release()

        if ok:
            await asyncio.to_thread(self._db, lambda c: mark_verified(c, pubkey, proof["nonce"]))
            logger.info("identity %s verified (E=%s)", pubkey[:8], cert["difficulty"])
            return Admission("verified")
        await asyncio.to_thread(self._db, lambda c: mark_failed(c, pubkey, addr, "pow_failed"))
        self._note_failure(addr)
        logger.warning("identity %s presented an invalid proof — banned", pubkey[:8])
        return Admission("failed", "identity proof invalid")


@contextmanager
def psycopg2_conn_factory(dsn: str):
    """A conn_factory for surfaces without a pool (the launcher): one
    short-lived autocommit connection per call."""
    import psycopg2
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


# ----------------------------------------------------------------------------
# python -m desktop.p2p.identity_registry --selftest --dsn postgresql://...
# ----------------------------------------------------------------------------

def _selftest(dsn: str) -> None:
    """End-to-end against a REAL database (throwaway keys, cleaned up):
    schema, observe rules, admit paths incl. a real 2 GiB verification."""
    import os
    from functools import partial
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from desktop.p2p import birth_cert

    authority = Ed25519PrivateKey.generate()
    issuer = authority.public_key().public_bytes_raw().hex()
    trusted_verify = partial(birth_cert.verify_certificate, trusted=[issuer])

    def make_cert(method="pow", difficulty=2, issued_at="2026-08-17T10:00:00Z", pubkey=None):
        cert = {"v": 2, "pubkey": pubkey or os.urandom(32).hex(), "issued_at": issued_at,
                "method": method, "difficulty": difficulty, "params_version": 1,
                "email_token": "ab" * 32 if method == "email" else None,
                "email_class": "other" if method == "email" else None, "issuer": issuer}
        cert["sig"] = authority.sign(birth_cert.canonical_payload(cert)).hex()
        return cert

    factory = partial(psycopg2_conn_factory, dsn)
    with factory() as conn:
        ensure_schema(conn)
    gate = IdentityGate(factory, trusted_verify)
    created = []

    async def run():
        # email identity: verified on sight
        e = make_cert("email"); created.append(e["pubkey"])
        assert (await gate.admit(e["pubkey"], e, None, "203.0.113.5")).status == "verified"
        # pow without proof → proof_required, row unverified
        p = make_cert("pow"); created.append(p["pubkey"])
        assert (await gate.admit(p["pubkey"], p, None, "203.0.113.6")).status == "proof_required"
        with factory() as conn:
            assert get(conn, p["pubkey"])["status"] == "unverified"
        # real proof at 2 GiB, E=2 → verified (one evaluation on admit)
        nonce = identity_pow.pow_mine(identity_pow.pow_challenge(p), p["difficulty"],
                                      identity_pow.POW_PARAMS[1])
        proof = identity_proof.make_proof(p, nonce)
        t0 = time.perf_counter()
        assert (await gate.admit(p["pubkey"], p, proof, "203.0.113.6")).status == "verified"
        print(f"  verified real proof in {time.perf_counter() - t0:.2f}s")
        assert (await gate.admit(p["pubkey"], p, proof, "203.0.113.6")).status == "verified"   # cached
        # forged proof → failed + banned
        f = make_cert("pow", difficulty=1 << 40); created.append(f["pubkey"])   # no random nonce meets this
        bad = identity_proof.make_proof(f, os.urandom(16))
        bad_cert = f
        adm = await gate.admit(bad_cert["pubkey"], bad_cert, bad, "203.0.113.7")
        assert adm.status == "failed", adm
        with factory() as conn:
            assert is_banned(conn, bad_cert["pubkey"])
            assert get(conn, bad_cert["pubkey"])["status"] == "failed"
        assert (await gate.admit(bad_cert["pubkey"], bad_cert, bad, "203.0.113.7")).status == "banned"
        # anchor mismatch → invalid; stale pow for an email identity → still verified
        e2 = make_cert("email", issued_at="2026-08-17T11:00:00Z", pubkey=e["pubkey"])
        assert (await gate.admit(e["pubkey"], e2, None, None)).status == "invalid"
        stale = make_cert("pow", pubkey=e["pubkey"])
        assert (await gate.admit(e["pubkey"], stale, None, None)).status == "verified"
        # pow → email upgrade keeps first_seen and becomes verified
        up = make_cert("email", pubkey=p["pubkey"])
        with factory() as conn:
            before = get(conn, p["pubkey"])["first_seen_at"]
        assert (await gate.admit(p["pubkey"], up, None, None)).status == "verified"
        with factory() as conn:
            row = get(conn, p["pubkey"])
            assert row["method"] == "email" and row["first_seen_at"] == before and row["contacts"] == 4
        # per-address backstop
        for _ in range(FAILS_PER_ADDR_HOUR):
            gate._note_failure("198.51.100.1")
        q = make_cert("pow"); created.append(q["pubkey"])
        qp = identity_proof.make_proof(q, os.urandom(16))
        assert (await gate.admit(q["pubkey"], q, qp, "198.51.100.1")).status == "rate_limited"
        # ripening is computed, not stored
        assert not is_ripe(make_cert(issued_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))
        assert is_ripe(p)

    try:
        asyncio.run(run())
        print("selftest OK")
    finally:
        with factory() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM p2p_node_bans WHERE pubkey = ANY(%s)", (created,))
            cur.execute("DELETE FROM p2p_identities WHERE pubkey = ANY(%s)", (created,))


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Identity registry")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--dsn", default="postgresql://musicai:supervisor@localhost:5432/music_ai")
    args = ap.parse_args()
    if args.selftest:
        logging.basicConfig(level=logging.INFO)
        _selftest(args.dsn)
    else:
        ap.print_help()
