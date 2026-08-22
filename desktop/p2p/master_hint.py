"""Master address hint — the discovery tier of last resort.

The master's identity is pinned in every install (master_node.py), but its
ADDRESS lived only in the DHT — and mobile carriers throttle UDP hard
enough that a newborn's DHT bootstrap can find zero nodes (seen live
2026-08-20: first run 53 nodes, second run 0/20 on the same hotspot) while
HTTPS to the Worker sails through. The Worker learns the master's address
for free: the master holds the mailbox wake socket, the edge sees its
egress IP, the declared peer port rides inside the master's signature
(worker/verify.js, replay-guarded). `GET /master-hint` republishes it.

This is DISCOVERY, not trust: the hint only supplies an address candidate;
whoever dials it still runs the full peer handshake against the pinned
master pubkey, certificates and signatures — a lying Worker could at worst
point us at a node that fails those checks. Consumers add the hint ONLY
when every organic tier (LAN, cache, DHT) came up empty:

- `_find_friend_peers` (chat handshake, message push, history pull, the
  relay-#0 wake subscription, the reachability probe);
- `_find_dump_peers` (MB slice sources + remote MB search);
- the sync walk's node-discovery step.

One in-process cache; a fetch failure is cached briefly so an offline
Worker never turns into a hammering loop.
"""

import json
import logging
import threading
import time
import urllib.request
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

HINT_TTL_S = 600.0
NEGATIVE_TTL_S = 60.0
FETCH_TIMEOUT_S = 6.0

_lock = threading.Lock()
_cached: Optional[Tuple[str, int]] = None
_fetched_at = 0.0
_failed_at = 0.0


def _worker_url() -> str:
    from desktop.p2p.email_verify import VERIFY_WORKER_URL
    return VERIFY_WORKER_URL


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Sautium/1.0",
                                               "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch(force: bool = False, _get=None) -> Optional[Tuple[str, int]]:
    """(host, port) of the master's last known peer address, or None.
    Blocking (one HTTPS GET at most every HINT_TTL_S) — async callers run
    it in a thread. On a transport error the previous value keeps serving
    (stale beats nothing for a last-resort tier); an authoritative empty
    answer clears it. `_get` is injectable for tests."""
    from desktop.p2p.master_node import master_configured
    if not master_configured():
        return None
    global _cached, _fetched_at, _failed_at
    now = time.monotonic()
    with _lock:
        if not force:
            fresh = _fetched_at and now - _fetched_at < HINT_TTL_S
            backing_off = _failed_at and now - _failed_at < NEGATIVE_TTL_S
            if fresh or backing_off:
                return _cached
        try:
            hint = (_get or globals()["_get"])(f"{_worker_url()}/master-hint")
        except Exception as e:
            logger.debug("master hint fetch failed: %s", e)
            _failed_at = now
            return _cached
        # Two family slots since IPv6-Ф0: `host` is the v4 address, `host6`
        # the v6 one. Prefer v4 (universally dialable); a v6-only hint is
        # still a valid candidate — fmt_addr brackets it for the dial.
        host, port = hint.get("host") or hint.get("host6"), hint.get("port")
        if isinstance(host, str) and host and isinstance(port, int) and 0 < port < 65536:
            _cached = (host, port)
        else:
            _cached = None                      # the Worker answered: no hint yet
        _fetched_at, _failed_at = now, 0.0
        return _cached
