"""Gate pool — Mode B of the admission gate: the by-product economy
(P2P-SYNC-INTEGRITY.md § "Admission gate" → "Mode B — two-class pool").

Every first-party evaluation the gate performs — an R-sample, an audit,
even a check that fails garbage — leaves a true (task, answer) pair the
server computed in order to compare. Those pairs are the pool:

- **gold** — first-party truth. Handed to a stranger inside its packet it
  costs the client w and the server one memcmp: an O(1) prefilter that
  garbage cannot pass. Single-use (retired on redemption). Entries minted
  while FAILING a packet are the cleanest — their author never solved
  them, so no self-redemption is possible.
- **seed gold** — minted by the node itself while idle (one 64 MiB task per
  load-meter sample below a small target): no author, no recipient keys,
  eligible for everyone. It exists so the O(1) prefilter is armed from the
  very first packet — otherwise a garbage flood in an empty pool costs us
  R·w per packet while gold minted from that garbage can never be issued
  back to its author (self-redemption rule) and the flood is free to run.
  Not the "pre-mined pool" the design rejects for peers (that was about
  scarcity at 1:1 parity); this is a bootstrap of the filter, bounded and
  idle-only.
- **silver** — the unchecked claims from packets that passed their sample
  (`not_verified`, never ground truth). Mixed into future packets as free
  cross-validation probes: a match is a quorum vote (M votes from distinct
  recipients → promoted, still audited on mismatch), a mismatch triggers
  a w-audit that mints the truth and exposes the liar deterministically.
  A silver mismatch alone rejects nobody — the audit decides.

Policy: retire on use; an abandoned lease retires the entry (a free
by-product returned to the pool would let its author fish for it);
never reissue an entry to a recipient sharing a hard axis with anyone who
already held it (`recipient_keys`: pubkey, subnet, email domain — an
exclusion, not a ranking, so the lookup is one indexed query); expiry and
a row cap bound the state. Local, never shared.

Answers are 32-byte Argon2id outputs (admission.solve); inputs 32 bytes.
"""

import logging
from typing import Optional, Sequence

logger = logging.getLogger(__name__)

SILVER_QUORUM = 3                 # M distinct matching recipients → promoted
SILVER_PER_PACKET_MINT = 6        # of the K−R unchecked claims, how many to keep
ENTRY_TTL_DAYS = 7
POOL_ROWS_CAP = 10_000

SCHEMA_SQL = (
    """DO $$ BEGIN
         CREATE TYPE p2p_pool_class AS ENUM ('gold', 'silver');
       EXCEPTION WHEN duplicate_object THEN NULL; END $$""",
    """DO $$ BEGIN
         CREATE TYPE p2p_pool_origin AS ENUM ('sample', 'audit', 'garbage', 'claim', 'seed');
       EXCEPTION WHEN duplicate_object THEN NULL; END $$""",
    """ALTER TYPE p2p_pool_origin ADD VALUE IF NOT EXISTS 'seed'""",
    """CREATE TABLE IF NOT EXISTS p2p_gate_pool (
         id              BIGSERIAL PRIMARY KEY,
         task_input      BYTEA NOT NULL,
         answer          BYTEA NOT NULL,
         class           p2p_pool_class NOT NULL,
         origin          p2p_pool_origin NOT NULL,
         params_version  SMALLINT NOT NULL,
         source_pubkey   TEXT NOT NULL,
         source_nonce    TEXT NOT NULL,
         votes           SMALLINT NOT NULL DEFAULT 0,
         use_count       INTEGER NOT NULL DEFAULT 0,
         recipient_keys  TEXT[] NOT NULL DEFAULT '{}',
         leased_nonce    TEXT,
         leased_at       TIMESTAMPTZ,
         lease_deadline  TIMESTAMPTZ,
         created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
         expires_at      TIMESTAMPTZ NOT NULL,
         retired_at      TIMESTAMPTZ,
         retire_reason   TEXT
       )""",
    """CREATE INDEX IF NOT EXISTS idx_p2p_gate_pool_free
         ON p2p_gate_pool (class, expires_at) WHERE retired_at IS NULL AND leased_nonce IS NULL""",
    """CREATE INDEX IF NOT EXISTS idx_p2p_gate_pool_lease
         ON p2p_gate_pool (leased_nonce) WHERE leased_nonce IS NOT NULL""",
    """CREATE INDEX IF NOT EXISTS idx_p2p_gate_pool_recipients
         ON p2p_gate_pool USING GIN (recipient_keys)""",
)

_COLS = ("id", "task_input", "answer", "class", "origin", "params_version", "source_pubkey",
         "source_nonce", "votes", "use_count", "recipient_keys", "leased_nonce")


def ensure_schema(conn) -> None:
    with conn.cursor() as cur:
        for stmt in SCHEMA_SQL:
            cur.execute(stmt)
    conn.commit()


def _row(r) -> dict:
    d = dict(zip(_COLS, r))
    d["task_input"] = bytes(d["task_input"])
    d["answer"] = bytes(d["answer"])
    return d


def mint(conn, *, task_input: bytes, answer: bytes, klass: str, origin: str, params_version: int,
         source_pubkey: str, source_nonce: str, recipient_keys: Sequence[str]) -> int:
    """One entry. gold ← sample|audit|garbage (first-party truth); silver ←
    claim (the client's unchecked answer). The source's own keys start the
    recipient list so the entry can never come back to its author."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO p2p_gate_pool (task_input, answer, class, origin, params_version,
                source_pubkey, source_nonce, recipient_keys, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now() + make_interval(days => %s))
            RETURNING id
        """, (task_input, answer, klass, origin, params_version, source_pubkey.lower(),
              source_nonce, list(recipient_keys), ENTRY_TTL_DAYS))
        entry_id = cur.fetchone()[0]
    conn.commit()
    return entry_id


def lease(conn, *, nonce: str, deadline, keys: Sequence[str], params_version: int,
          gold: int = 1, silver: int = 2) -> list:
    """Free entries for one packet: up to `gold` gold + `silver` silver whose
    recipient_keys share nothing with `keys` (exclusion on hard axes — one
    indexed query, random pick among the eligible). Marks them leased to
    the quote nonce until its deadline. Returns entries in id order."""
    keys = list(keys)
    picked = []
    with conn.cursor() as cur:
        for klass, count in (("gold", gold), ("silver", silver)):
            if count <= 0:
                continue
            # The pick is a MATERIALIZED CTE: evaluated exactly once. As an
            # `id IN (SELECT … ORDER BY random() …)` semi-join the planner may
            # re-run the volatile subquery per candidate row and the LIMIT
            # stops bounding the packet.
            cur.execute(f"""
                WITH picked AS MATERIALIZED (
                     SELECT id FROM p2p_gate_pool
                      WHERE class = %s AND retired_at IS NULL AND leased_nonce IS NULL
                        AND expires_at > now() AND params_version = %s
                        AND NOT (recipient_keys && %s::text[])
                      ORDER BY random() LIMIT %s
                      FOR UPDATE SKIP LOCKED)
                UPDATE p2p_gate_pool AS g
                   SET leased_nonce = %s, leased_at = now(), lease_deadline = %s
                  FROM picked WHERE g.id = picked.id
             RETURNING {', '.join('g.' + c for c in _COLS)}
            """, (klass, params_version, keys, count, nonce, deadline))
            picked.extend(_row(r) for r in cur.fetchall())
    conn.commit()
    return sorted(picked, key=lambda e: e["id"])


def free_seed_gold(conn, params_version: int) -> int:
    """How many authorless gold entries are on offer right now."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT count(*) FROM p2p_gate_pool
             WHERE class = 'gold' AND origin = 'seed' AND retired_at IS NULL
               AND leased_nonce IS NULL AND expires_at > now() AND params_version = %s
        """, (params_version,))
        return cur.fetchone()[0]


def leased(conn, nonce: str) -> list:
    with conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(_COLS)} FROM p2p_gate_pool WHERE leased_nonce = %s ORDER BY id",
                    (nonce,))
        return [_row(r) for r in cur.fetchall()]


def record_recipient(conn, entry_id: int, keys: Sequence[str]) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE p2p_gate_pool
               SET recipient_keys = (SELECT array_agg(DISTINCT k) FROM unnest(recipient_keys || %s::text[]) AS k),
                   use_count = use_count + 1, leased_nonce = NULL, leased_at = NULL, lease_deadline = NULL
             WHERE id = %s
        """, (list(keys), entry_id))
    conn.commit()


def retire(conn, entry_id: int, reason: str) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE p2p_gate_pool
               SET retired_at = now(), retire_reason = %s, leased_nonce = NULL
             WHERE id = %s AND retired_at IS NULL
        """, (reason, entry_id))
    conn.commit()


def silver_vote(conn, entry_id: int) -> int:
    """A recipient's answer matched the claim: one more distinct vote (the
    recipient is distinct by the exclusion at lease time). At the quorum
    the entry is promoted to serve as gold — origin stays `claim`, so a
    later mismatch still audits instead of rejecting."""
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE p2p_gate_pool
               SET votes = votes + 1,
                   class = CASE WHEN votes + 1 >= %s THEN 'gold'::p2p_pool_class ELSE class END
             WHERE id = %s
             RETURNING votes
        """, (SILVER_QUORUM, entry_id))
        votes = cur.fetchone()[0]
    conn.commit()
    return votes


def gild(conn, entry_id: int, truth: bytes) -> None:
    """An audit computed the truth: the entry becomes first-party gold."""
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE p2p_gate_pool
               SET answer = %s, class = 'gold', origin = 'audit'
             WHERE id = %s
        """, (truth, entry_id))
    conn.commit()


def retire_abandoned(conn) -> int:
    """Leases whose quote deadline passed without a presentation: the entry
    is retired (never returned to the pool)."""
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE p2p_gate_pool
               SET retired_at = now(), retire_reason = 'abandoned', leased_nonce = NULL
             WHERE leased_nonce IS NOT NULL AND lease_deadline < now() AND retired_at IS NULL
        """)
        n = cur.rowcount
        cur.execute("""
            UPDATE p2p_gate_pool SET retired_at = now(), retire_reason = 'expired'
             WHERE retired_at IS NULL AND expires_at < now()
        """)
        n += cur.rowcount
    conn.commit()
    return n


def evict_over_cap(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM p2p_gate_pool")
        total = cur.fetchone()[0]
        if total <= POOL_ROWS_CAP:
            return 0
        excess = total - POOL_ROWS_CAP
        cur.execute("""
            DELETE FROM p2p_gate_pool WHERE id IN (
                SELECT id FROM p2p_gate_pool
                 ORDER BY (retired_at IS NULL), created_at ASC LIMIT %s)
        """, (excess,))
        n = cur.rowcount
    conn.commit()
    return n


def stats(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT class::text, origin::text, count(*) FILTER (WHERE retired_at IS NULL) AS live,
                   count(*) FILTER (WHERE retired_at IS NOT NULL) AS retired,
                   count(*) FILTER (WHERE leased_nonce IS NOT NULL) AS leased,
                   coalesce(sum(use_count), 0), coalesce(max(votes), 0)
              FROM p2p_gate_pool GROUP BY 1, 2 ORDER BY 1, 2
        """)
        rows = cur.fetchall()
    return {f"{c}/{o}": {"live": live, "retired": ret, "leased": ls, "uses": int(u), "max_votes": mv}
            for c, o, live, ret, ls, u, mv in rows}


# ----------------------------------------------------------------------------
# python -m desktop.p2p.gate_pool --selftest --dsn …
# ----------------------------------------------------------------------------

def _selftest(dsn: str) -> None:
    import os
    from datetime import datetime, timedelta, timezone
    from desktop.p2p.identity_registry import psycopg2_conn_factory
    fut = datetime.now(timezone.utc) + timedelta(minutes=5)
    minted = []
    with psycopg2_conn_factory(dsn) as conn:
        ensure_schema(conn)
        try:
            src = "aa" * 32
            g = mint(conn, task_input=os.urandom(32), answer=os.urandom(32), klass="gold", origin="sample",
                     params_version=1, source_pubkey=src, source_nonce="n1", recipient_keys=[src, "sub:1"])
            s1 = mint(conn, task_input=os.urandom(32), answer=os.urandom(32), klass="silver", origin="claim",
                      params_version=1, source_pubkey=src, source_nonce="n1", recipient_keys=[src, "sub:1"])
            s2 = mint(conn, task_input=os.urandom(32), answer=os.urandom(32), klass="silver", origin="claim",
                      params_version=1, source_pubkey=src, source_nonce="n1", recipient_keys=[src, "sub:1"])
            minted += [g, s1, s2]
            # the author (or its subnet) never gets its own entries
            assert lease(conn, nonce="q0", deadline=fut, keys=[src], params_version=1) == []
            assert lease(conn, nonce="q0", deadline=fut, keys=["bb" * 32, "sub:1"], params_version=1) == []
            got = lease(conn, nonce="q1", deadline=fut, keys=["bb" * 32, "sub:2"], params_version=1)
            assert {e["class"] for e in got} == {"gold", "silver"} and len(got) == 3
            assert leased(conn, "q1") == got
            # a second lease for another client finds nothing free
            assert lease(conn, nonce="q2", deadline=fut, keys=["cc" * 32, "sub:3"], params_version=1) == []
            # settle: gold used → retired; silver voted; recipients recorded
            retire(conn, g, "used")
            for e in got:
                record_recipient(conn, e["id"], ["bb" * 32, "sub:2"])
            assert silver_vote(conn, s1) == 1
            assert silver_vote(conn, s1) == 2
            assert silver_vote(conn, s1) == SILVER_QUORUM
            free = lease(conn, nonce="q3", deadline=fut, keys=["cc" * 32, "sub:3"], params_version=1)
            assert {e["id"] for e in free} == {s1, s2}, free            # promoted s1 now leases as gold
            assert next(e for e in free if e["id"] == s1)["class"] == "gold"
            assert next(e for e in free if e["id"] == s1)["origin"] == "claim"   # promoted, still audited on mismatch
            gild(conn, s2, b"\x01" * 32)
            assert leased(conn, "q3")[1]["class"] == "gold"
            # abandoned lease: pretend the deadline passed
            with conn.cursor() as cur:
                cur.execute("UPDATE p2p_gate_pool SET lease_deadline = now() - interval '1 minute' WHERE leased_nonce = 'q3'")
            conn.commit()
            assert retire_abandoned(conn) >= 2
            assert lease(conn, nonce="q4", deadline=fut, keys=["dd" * 32], params_version=1) == []
            print("stats:", stats(conn))
            print("selftest OK")
        finally:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM p2p_gate_pool WHERE id = ANY(%s)", (minted,))
            conn.commit()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Gate pool (Mode B)")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--dsn", default="postgresql://musicai:supervisor@localhost:5432/music_ai")
    args = ap.parse_args()
    if args.selftest:
        logging.basicConfig(level=logging.INFO)
        _selftest(args.dsn)
    else:
        ap.print_help()
