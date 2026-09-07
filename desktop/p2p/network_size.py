"""How big is the network — and is it big enough for per-artist keys.

Two DHT layers (dht_service docstring): the node key, which every node
announces, and per-artist keys for a node's rarest held artists. The second
is a strict subset of the first — a per-artist lookup can never name a node
the node key does not already carry — so per-artist keys only pay once the
node key stops being a LIST. libtorrent stores 500 peers per key and answers
a get_peers with 100 of them at random (dht_max_peers / dht_max_peers_reply);
the directory hands out K random volunteers. Under that a sync run
enumerates the node key completely — one holdings call per node answers for
the whole library — and a per-artist key is a traversal spent on a question
the inventory already answered. Over it the node key is a rotating sample: a
holder of one rare artist turns up in a run with probability ~100/N, so at
N=1000 the sample still finds it within ~10 runs (~5 h at the default
cadence); by N=5000 that is a day, and an exact key starts saving real time.

So the per-artist layer switches on the SIZE of the network, both directions
at once: no per-artist announces and no per-artist lookups below RARE_ON,
both above, with hysteresis so a noisy estimate does not flap. Every node
measures the same network, so they flip in the same era without
coordinating; one that flips early spends only its own budget, one that
flips late is still found through node-key samples.

Estimating N. One node-key lookup shows at most a few hundred nodes whatever
N is. What a node CAN see is who it has met: `p2p_nodes_seen` records every
reachable peer by node key (the TLS-verified /health identity), and each
run's DHT sample is a capture-recapture experiment against it — the share of
the sample already known says how much of the population the ledger covers
(Chapman's estimator, which stays finite when the overlap is empty). A
sample under the reply cap was exhaustive and IS the live population. The
Worker directory's fresh-volunteer count, when it comes back with the hints,
is an exact floor. Nodes unseen for SIGHTING_WINDOW_DAYS leave the ledger so
churn does not inflate it forever.

Shared by both runtimes: the launcher runs the sync walk and the whole
estimate; the Docker backend has no walk and gates its tail on the directory
count alone through rare_mode().
"""

from __future__ import annotations

from typing import Iterable

from psycopg2.extras import execute_values

from desktop.p2p.addrs import canon_host

# Nodes. Per-artist keys start earning their traversals at a thousand — see
# the module docstring for the arithmetic — and stop at half that, so the
# estimate has to move by 2× to flip the mode back.
RARE_ON = 1000
RARE_OFF = 500

# libtorrent dht_max_peers_reply: a get_peers reply under this size held
# everything the answering node stored, so the sample was exhaustive.
DHT_REPLY_CAP = 100

SIGHTING_WINDOW_DAYS = 30

Sighting = tuple[str, str, int]      # (pubkey hex, host, port)


def record_sightings(conn, sightings: Iterable[Sighting]) -> None:
    rows = {}
    for pubkey, host, port in sightings:
        if pubkey:
            rows[pubkey.lower()] = (pubkey.lower(), canon_host(host), int(port))
    if not rows:
        return
    with conn.cursor() as cur:
        execute_values(
            cur,
            """INSERT INTO p2p_nodes_seen (pubkey, host, port) VALUES %s
               ON CONFLICT (pubkey) DO UPDATE
                  SET host = EXCLUDED.host, port = EXCLUDED.port,
                      last_seen_at = now(),
                      sightings = p2p_nodes_seen.sightings + 1""",
            list(rows.values()),
        )


def known_addresses(conn, addrs: Iterable[tuple[str, int]]) -> set[tuple[str, int]]:
    """Of these (host, port), the ones the ledger already holds — a node met
    before, recaptured without a probe."""
    pairs = sorted({(canon_host(h), int(p)) for h, p in addrs})
    if not pairs:
        return set()
    with conn.cursor() as cur:
        rows = execute_values(
            cur,
            """SELECT s.host, s.port FROM p2p_nodes_seen s
               JOIN (VALUES %s) AS q(host, port)
                 ON q.host = s.host AND q.port = s.port""",
            pairs, fetch=True,
        )
    return {(r[0], int(r[1])) for r in rows}


def known_pubkeys(conn, pubkeys: Iterable[str]) -> set[str]:
    keys = sorted({k.lower() for k in pubkeys if k})
    if not keys:
        return set()
    with conn.cursor() as cur:
        cur.execute("SELECT pubkey FROM p2p_nodes_seen WHERE pubkey = ANY(%s)", (keys,))
        return {r[0] for r in cur.fetchall()}


def ledger_size(conn) -> int:
    """Nodes met inside the window. Older sightings go first: a node that
    left the network must stop counting as one that could be recaptured."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM p2p_nodes_seen "
            "WHERE last_seen_at < now() - make_interval(days => %s)",
            (SIGHTING_WINDOW_DAYS,))
        cur.execute("SELECT count(*) FROM p2p_nodes_seen")
        return int(cur.fetchone()[0])


def estimate(marked: int, sample: int, recaptured: int, exhaustive: bool) -> int:
    """Chapman's capture-recapture estimate of the reachable population:
    `marked` nodes met before this run, `sample` live nodes in this run's
    DHT reply, `recaptured` of them already known. `exhaustive` (a reply
    under the cap) means the sample is the population and nothing is
    estimated."""
    if exhaustive or marked == 0:
        return sample
    return (marked + 1) * (sample + 1) // (recaptured + 1) - 1


def rare_mode(estimate: int, previous: bool) -> bool:
    if estimate >= RARE_ON:
        return True
    if estimate <= RARE_OFF:
        return False
    return previous
