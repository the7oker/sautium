"""Similarity — a price coefficient, not a verdict (P2P-SYNC-INTEGRITY.md
§ "Similarity: a price coefficient, not a verdict"; plan Ф14).

One axis fails both ways: CGNAT neighbours share an address (false
positive), cheap proxies unshare it (false negative). A CONJUNCTION of
axes inverts both — honest neighbours collide on the address alone (births
scattered over months, mailboxes on different domains), a fleet born from
one process collides on several axes at once. So a pair's score is the sum
of the axes it coincides on, and it is ZERO unless at least two axes hit;
the one exception is the mailbox token — Worker-peppered, equality means
the same origin, a hard link that needs no second witness.

Axes and v0 weights (placeholders in SHADOW — the honest distributions
that set them are measured after launch, plan § "Точки повернення" T2):

    mailbox   email_token equal                              4.0  hard link
    birth     issued_at within 10 min (Worker-signed)        2.0
              same UTC day                                   0.5
    wave      both born above the base difficulty within     1.0
              10 min — the notary itself priced that wave (Ф2b hint;
              an axis, never a verdict: they PAID more, they are not
              punished for it)
    addr      an exact address in common (first/last)        1.5
    subnet    a /24 (IPv6 /48) in common                     1.0
    domain    same mailbox domain when sharing it means      1.5
              something (email_domains.informative: a populous
              provider — no; a disposable service or a domain the
              table has no opinion about — yes)

Behaviour and taste (targets, schedule) carry no identity weight on
purpose — "economic ballast": a bot fills them with noise for free, and in
a priced world the noise is what gets taxed. They are not computed here.

Deterministic evidence bans; statistics only price. A cluster is a
COLLECTIVE PRICE MULTIPLIER anchored on a member that was caught for real
(`p2p_node_bans` / status failed): sim_mult(X) = 1 + gain × Σ score(X, Y)
over the banned members Y of X's cluster, capped. A cluster with nobody
caught pays nothing extra — two honest people in one flat share IP, near
births, similar taste, and the worst that can happen to them is "entered
a bit more expensively", never "excluded".

Second consumer of the same metric: relay diversity. A node behind CGNAT
holds K peer relays; picking the most DISSIMILAR ones (distinct subnets
first — the eclipse axis we can read off the address itself — then the
registry pairs) is a direct eclipse defence.

Cost model: `sim_mult` is called on the priced path, so it never blocks —
a cached value or 1.0 with a background refresh; the candidate query is
one indexed SELECT (token, domain, address, subnet, birth window) plus
Python scoring of a few rows. Shared by both surfaces through the bind
mount; `install()/current()` like the load meter and the pricer.
"""

import ipaddress
import logging
import queue
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional, Sequence

logger = logging.getLogger(__name__)

W_MAILBOX = 4.0
W_BIRTH_10MIN = 2.0
W_BIRTH_DAY = 0.5
W_WAVE = 1.0
W_ADDR = 1.5
W_SUBNET = 1.0
W_DOMAIN = 1.5
BIRTH_NEAR_SECONDS = 600
CONJUNCTION_MIN_AXES = 2
CLUSTER_THRESHOLD = 2.0            # pair score that makes Y part of X's cluster
SCORE_CAP = 6.0                    # one anchored member's contribution is bounded
ANCHOR_GAIN = 0.5
SIM_MULT_MAX = 4.0
POW_BASE_DIFFICULTY = 32           # MIRRORS worker/verify.js POW_DIFFICULTY (the golden-age base)
CANDIDATE_LIMIT = 2000
CACHE_TTL_S = 600.0
CACHE_CAP = 10_000
RELAY_SAME_SUBNET_SCORE = 4.0      # relays in one /24 are as bad as a hard link for eclipse purposes

_current: Optional["SimilarityIndex"] = None


def install(index: "SimilarityIndex") -> "SimilarityIndex":
    global _current
    _current = index
    return index


def current() -> Optional["SimilarityIndex"]:
    return _current


# ----------------------------------------------------------------------------
# The pure part
# ----------------------------------------------------------------------------

def _ts(value) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).timestamp()
    return float(value)


def pair_score(a: dict, b: dict) -> tuple:
    """(score, hits) for two registry-shaped rows (issued_at, email_token,
    email_class, email_domain_token, difficulty, first/last_addr,
    first/last_subnet). Zero unless ≥ 2 axes hit, except the mailbox hard
    link. Symmetric."""
    hits = []
    score = 0.0
    if a.get("email_token") and a.get("email_token") == b.get("email_token"):
        hits.append("mailbox")
        score += W_MAILBOX
    ta, tb = _ts(a.get("issued_at")), _ts(b.get("issued_at"))
    near_birth = False
    if ta is not None and tb is not None:
        if abs(ta - tb) <= BIRTH_NEAR_SECONDS:
            hits.append("birth")
            score += W_BIRTH_10MIN
            near_birth = True
        elif datetime.fromtimestamp(ta, timezone.utc).date() == datetime.fromtimestamp(tb, timezone.utc).date():
            hits.append("birthday")
            score += W_BIRTH_DAY
    if (near_birth and int(a.get("difficulty") or 0) > POW_BASE_DIFFICULTY
            and int(b.get("difficulty") or 0) > POW_BASE_DIFFICULTY):
        hits.append("wave")
        score += W_WAVE
    addrs_a = {str(x) for x in (a.get("first_addr"), a.get("last_addr")) if x}
    addrs_b = {str(x) for x in (b.get("first_addr"), b.get("last_addr")) if x}
    if addrs_a & addrs_b:
        hits.append("addr")
        score += W_ADDR
    else:
        subs_a = {str(x) for x in (a.get("first_subnet"), a.get("last_subnet")) if x}
        subs_b = {str(x) for x in (b.get("first_subnet"), b.get("last_subnet")) if x}
        if subs_a & subs_b:
            hits.append("subnet")
            score += W_SUBNET
    domain = a.get("email_domain_token")
    if domain and domain == b.get("email_domain_token") and _domain_informative(domain, a, b):
        hits.append("domain")
        score += W_DOMAIN
    if "mailbox" not in hits and len(hits) < CONJUNCTION_MIN_AXES:
        return 0.0, []
    return score, hits


def _domain_informative(domain_token: str, a: dict, b: dict) -> bool:
    """The node-side table decides (email_domains); when it has no opinion,
    the Worker's issuance-time class still rules out a populous provider —
    the two lists may drift, and a false "rare domain" is the costlier
    mistake."""
    from desktop.p2p import email_domains
    if email_domains.tier_of(domain_token) is not None:
        return email_domains.informative(domain_token)
    return a.get("email_class") != "major" and b.get("email_class") != "major"


def cluster_from_rows(row: dict, candidates: Sequence[dict]) -> list:
    """[{pubkey, score, hits, anchored}] — the candidates that pass the
    cluster threshold against `row`; `anchored` = banned or failed."""
    members = []
    for c in candidates:
        if c.get("pubkey") == row.get("pubkey"):
            continue
        score, hits = pair_score(row, c)
        if score >= CLUSTER_THRESHOLD:
            members.append({"pubkey": c["pubkey"], "score": score, "hits": hits,
                            "anchored": bool(c.get("banned")) or c.get("status") == "failed"})
    members.sort(key=lambda m: -m["score"])
    return members


def sim_mult_from_cluster(members: Sequence[dict]) -> float:
    anchored = sum(min(SCORE_CAP, m["score"]) for m in members if m.get("anchored"))
    return min(SIM_MULT_MAX, 1.0 + ANCHOR_GAIN * anchored)


def subnet_of(host: str) -> Optional[str]:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    return str(ipaddress.ip_network(f"{host}/24" if ip.version == 4 else f"{host}/48", strict=False))


def relay_order(candidates: Sequence[dict], held: Sequence[dict],
                pair: Callable[[str, str], float] = lambda a, b: 0.0) -> list:
    """Order relay candidates ({ip, pubkey, known}) for recruitment: least
    similar to what is already held (and to each other, greedily) first —
    a shared /24 counts like a hard link — then previously used relays,
    then the caller's (shuffled) order. `pair(a, b)` is the registry pair
    score for two pubkeys (unknown pubkeys score 0)."""
    chosen = list(held)
    remaining = list(candidates)
    ordered = []
    while remaining:
        def similarity(c):
            worst = 0.0
            for h in chosen:
                s = 0.0
                if c.get("ip") and h.get("ip") and subnet_of(c["ip"]) and subnet_of(c["ip"]) == subnet_of(h["ip"]):
                    s = RELAY_SAME_SUBNET_SCORE
                if c.get("pubkey") and h.get("pubkey"):
                    s = max(s, pair(c["pubkey"], h["pubkey"]))
                worst = max(worst, s)
            return worst
        best_i = min(range(len(remaining)),
                     key=lambda i: (round(similarity(remaining[i]) * 2) / 2, 0 if remaining[i].get("known") else 1, i))
        pick = remaining.pop(best_i)
        ordered.append(pick)
        chosen.append(pick)
    return ordered


# ----------------------------------------------------------------------------
# The DB part
# ----------------------------------------------------------------------------

_CANDIDATE_SQL = """
    SELECT i.pubkey, i.issued_at, i.email_token, i.email_class, i.email_domain_token,
           i.difficulty, i.first_addr::text, i.last_addr::text, i.first_subnet::text, i.last_subnet::text,
           i.status::text,
           EXISTS (SELECT 1 FROM p2p_node_bans b WHERE b.pubkey = i.pubkey) AS banned
      FROM p2p_identities i
     WHERE i.pubkey <> %(pubkey)s
       AND ((%(token)s IS NOT NULL AND i.email_token = %(token)s)
            OR (%(domain)s IS NOT NULL AND i.email_domain_token = %(domain)s)
            OR (i.first_addr IS NOT NULL AND i.first_addr::text = ANY(%(addrs)s))
            OR (i.last_addr IS NOT NULL AND i.last_addr::text = ANY(%(addrs)s))
            OR (i.first_subnet IS NOT NULL AND i.first_subnet::text = ANY(%(subnets)s))
            OR (i.last_subnet IS NOT NULL AND i.last_subnet::text = ANY(%(subnets)s))
            OR i.issued_at BETWEEN %(issued)s - interval '1 day' AND %(issued)s + interval '1 day')
     LIMIT %(limit)s
"""
_CANDIDATE_COLS = ("pubkey", "issued_at", "email_token", "email_class", "email_domain_token",
                   "difficulty", "first_addr", "last_addr", "first_subnet", "last_subnet",
                   "status", "banned")


def _row_of(conn, pubkey: str) -> Optional[dict]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT pubkey, issued_at, email_token, email_class, email_domain_token, difficulty,
                   first_addr::text, last_addr::text, first_subnet::text, last_subnet::text, status::text
              FROM p2p_identities WHERE pubkey = %s
        """, (pubkey.lower(),))
        r = cur.fetchone()
    return dict(zip(_CANDIDATE_COLS[:-1], r)) if r else None


def cluster(conn, pubkey: str) -> Optional[dict]:
    """{pubkey, members, sim_mult} for a known identity, None for an unknown
    one — one indexed candidate query, scoring in Python."""
    row = _row_of(conn, pubkey)
    if row is None:
        return None
    with conn.cursor() as cur:
        cur.execute(_CANDIDATE_SQL, {
            "pubkey": row["pubkey"], "token": row["email_token"], "domain": row["email_domain_token"],
            "addrs": [x for x in (row["first_addr"], row["last_addr"]) if x],
            "subnets": [x for x in (row["first_subnet"], row["last_subnet"]) if x],
            "issued": row["issued_at"], "limit": CANDIDATE_LIMIT,
        })
        candidates = [dict(zip(_CANDIDATE_COLS, r)) for r in cur.fetchall()]
    members = cluster_from_rows(row, candidates)
    return {"pubkey": row["pubkey"], "members": members, "sim_mult": sim_mult_from_cluster(members)}


def pair(conn, pubkey_a: str, pubkey_b: str) -> float:
    a, b = _row_of(conn, pubkey_a), _row_of(conn, pubkey_b)
    return pair_score(a, b)[0] if a and b else 0.0


class SimilarityIndex:
    """Cached sim_mult per pubkey with a background refresh — the priced
    path never waits for the database."""

    def __init__(self, conn_factory: Callable, *, ttl: float = CACHE_TTL_S,
                 cap: int = CACHE_CAP, clock: Callable[[], float] = time.monotonic,
                 background: bool = True):
        self._conn_factory = conn_factory
        self._ttl = ttl
        self._cap = cap
        self._clock = clock
        self._cache: dict = {}                     # pubkey → (mult, computed_at)
        self._lock = threading.Lock()
        self._pending: set = set()
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._thread = None
        if background:
            self._thread = threading.Thread(target=self._run, daemon=True, name="similarity")
            self._thread.start()

    # -- the priced path (never blocks) ---------------------------------------------

    def sim_mult(self, pubkey: Optional[str]) -> float:
        if not pubkey:
            return 1.0
        pubkey = pubkey.lower()
        now = self._clock()
        with self._lock:
            hit = self._cache.get(pubkey)
            fresh = hit is not None and now - hit[1] < self._ttl
            if not fresh and pubkey not in self._pending:
                self._pending.add(pubkey)
                self._queue.put(pubkey)
        if hit is None:
            return 1.0
        return hit[0]

    # -- refresh -----------------------------------------------------------------------

    def refresh(self, pubkey: str) -> Optional[dict]:
        """Blocking recompute (the worker thread, the report, tests)."""
        pubkey = pubkey.lower()
        try:
            with self._conn_factory() as conn:
                result = cluster(conn, pubkey)
        finally:
            with self._lock:
                self._pending.discard(pubkey)
        mult = result["sim_mult"] if result else 1.0
        with self._lock:
            self._cache[pubkey] = (mult, self._clock())
            if len(self._cache) > self._cap:
                for k in sorted(self._cache, key=lambda k: self._cache[k][1])[: len(self._cache) - self._cap]:
                    del self._cache[k]
        if result and result["members"]:
            logger.debug("similarity %s: %d cluster member(s), sim_mult %.2f",
                         pubkey[:8], len(result["members"]), mult)
        return result

    def drain(self) -> int:
        """Run every queued refresh now (tests / shutdown)."""
        n = 0
        while True:
            try:
                pubkey = self._queue.get_nowait()
            except queue.Empty:
                return n
            self.refresh(pubkey)
            n += 1

    def _run(self) -> None:
        while True:
            pubkey = self._queue.get()
            try:
                self.refresh(pubkey)
            except Exception as e:                    # the pricer must never learn about a DB hiccup
                logger.debug("similarity refresh failed for %s: %s", pubkey[:8], e)

    def relay_pair(self, pubkey_a: str, pubkey_b: str) -> float:
        try:
            with self._conn_factory() as conn:
                return pair(conn, pubkey_a, pubkey_b)
        except Exception as e:
            logger.debug("similarity pair lookup failed: %s", e)
            return 0.0

    def stats(self) -> dict:
        with self._lock:
            priced = sum(1 for m, _ in self._cache.values() if m > 1.0)
            return {"cached": len(self._cache), "priced": priced, "pending": len(self._pending)}


# ----------------------------------------------------------------------------
# Report (contact_log --report) and self-test
# ----------------------------------------------------------------------------

def report(conn, limit: int = 2000) -> str:
    """Pairwise picture of the registry (bounded): how many pairs coincide
    on ≥ 2 axes, which axes, how many clusters have an anchor — the
    honest-traffic baseline the weights are calibrated against."""
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT pubkey, issued_at, email_token, email_class, email_domain_token, difficulty,
                   first_addr::text, last_addr::text, first_subnet::text, last_subnet::text, status::text,
                   EXISTS (SELECT 1 FROM p2p_node_bans b WHERE b.pubkey = i.pubkey) AS banned
              FROM p2p_identities i ORDER BY last_seen_at DESC LIMIT %s
        """, (limit,))
        rows = [dict(zip(_CANDIDATE_COLS, r)) for r in cur.fetchall()]
    pairs = 0
    axis_hits: dict = {}
    priced = 0
    scored_pairs = 0
    for i, a in enumerate(rows):
        members = cluster_from_rows(a, rows[i + 1:])
        for m in members:
            scored_pairs += 1
            for h in m["hits"]:
                axis_hits[h] = axis_hits.get(h, 0) + 1
        pairs += len(rows) - i - 1
        if sim_mult_from_cluster(cluster_from_rows(a, rows)) > 1.0:
            priced += 1
    out = [f"similarity ({len(rows)} identities, {pairs} pairs): {scored_pairs} pair(s) on ≥ 2 axes, "
           f"{priced} identit{'y' if priced == 1 else 'ies'} priced above 1×"]
    if axis_hits:
        out.append("  axes on scored pairs: " + ", ".join(f"{k}={v}" for k, v in sorted(axis_hits.items())))
    idx = current()
    if idx is not None:
        out.append(f"  cache: {idx.stats()}")
    return "\n".join(out)


def _selftest(dsn: str) -> None:
    """A synthetic fleet next to an honest CGNAT yard in a REAL database
    (throwaway rows, cleaned up): the fleet clusters and prices once one
    member is caught; the yard never does."""
    import os
    from functools import partial
    from datetime import timedelta
    from desktop.p2p import identity_registry
    from desktop.p2p.identity_registry import psycopg2_conn_factory
    factory = partial(psycopg2_conn_factory, dsn)
    created = []
    with factory() as conn:
        identity_registry.ensure_schema(conn)
        base = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)
        addr_ids = identity_registry.addr_uuid, identity_registry.subnet_uuid

        def insert(pubkey, issued, host, token=None, klass=None, domain=None, difficulty=32):
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO p2p_identities (pubkey, cert_v, method, issued_at, difficulty, params_version,
                        email_token, email_class, email_domain_token, issuer, cert_sig, first_addr, last_addr,
                        first_subnet, last_subnet)
                    VALUES (%s, 4, %s, %s, %s, 1, %s, %s, %s, 'test', 'test', %s, %s, %s, %s)
                """, (pubkey, "email" if token else "pow", issued, difficulty, token, klass, domain,
                      addr_ids[0](host), addr_ids[0](host), addr_ids[1](host), addr_ids[1](host)))
            created.append(pubkey)

        try:
            fleet = [os.urandom(32).hex() for _ in range(6)]
            for k, pk in enumerate(fleet):            # one process: same minute, one /24, one odd domain
                insert(pk, base + timedelta(seconds=10 * k), f"198.51.100.{20 + k}",
                       token=os.urandom(32).hex(), klass="other", domain="dd" * 32)
            yard = [os.urandom(32).hex() for _ in range(6)]
            for k, pk in enumerate(yard):             # honest neighbours: one /24, births months apart, gmail
                insert(pk, base - timedelta(days=30 * (k + 1)), f"203.0.113.{40 + k}",
                       token=os.urandom(32).hex(), klass="major", domain="ee" * 32)
            conn.commit()
            c = cluster(conn, fleet[0])
            assert len(c["members"]) == 5 and c["sim_mult"] == 1.0, c        # a cluster, but nobody caught → 1×
            assert all({"birth", "addr", "domain"} <= set(m["hits"]) or {"birth", "subnet", "domain"} <= set(m["hits"])
                       for m in c["members"]), c["members"]
            y = cluster(conn, yard[0])
            assert y["members"] == [] and y["sim_mult"] == 1.0, y             # single axis: nothing
            identity_registry.mark_failed(conn, fleet[5], "198.51.100.25", "gate_lie")   # one member caught
            c2 = cluster(conn, fleet[0])
            assert c2["sim_mult"] > 1.0, c2
            identity_registry.mark_failed(conn, yard[5], "203.0.113.45", "gate_lie")     # a caught neighbour
            assert cluster(conn, yard[0])["sim_mult"] == 1.0                              # changes nothing for the yard
            idx = SimilarityIndex(factory, background=False)
            assert idx.sim_mult(fleet[1]) == 1.0                                          # first ask: unknown yet
            assert idx.drain() == 1 and idx.sim_mult(fleet[1]) > 1.0                       # refreshed
            assert idx.sim_mult(yard[1]) == 1.0 and idx.drain() == 1 and idx.sim_mult(yard[1]) == 1.0
            print("  fleet:", round(c2["sim_mult"], 2), [m["hits"] for m in c2["members"][:2]])
            print(report(conn))
            print("selftest OK")
        finally:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM p2p_identities WHERE pubkey = ANY(%s)", (created,))
                cur.execute("DELETE FROM p2p_node_bans WHERE pubkey = ANY(%s)", (created,))
            conn.commit()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Similarity (Ф14)")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--dsn", default="postgresql://musicai:supervisor@localhost:5432/music_ai")
    args = ap.parse_args()
    if args.selftest:
        logging.basicConfig(level=logging.INFO)
        _selftest(args.dsn)
    else:
        ap.print_help()
