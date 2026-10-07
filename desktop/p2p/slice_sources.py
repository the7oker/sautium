"""The nodes a slice cycle asks — found once, kept while they answer.

Both slice families look for their sources the same way (MB:
desktop/p2p/mb_slice_cycle.py, LB: desktop/p2p/lb_slice_cycle.py), so they
share this finder. Candidates come in tiers: manual peers, LAN peers, and the
DHT capability key — every holder the traversal hears, not the first reply,
which is one node's view of the key and can hold only stale ports. When those
tiers yield no usable source (none at all, or only unreachable addresses, or
this node's own twin under the same account), the Worker directory's
volunteers are asked, the master hint last (Ф16c). A tier that yielded
candidates used to skip that fallback, so one dead DHT entry left a node
"with no source" beside a reachable master (2026-09-24).

A candidate is probed once — the walk's connect is the /health — and the
verified set is kept between runs: a slice request is itself proof of life,
so the next run reuses the set until a source fails or SOURCES_TTL passes.
Re-probing every candidate on every run spent one /health (two, with the
connect) per candidate per trigger, and during a history import the triggers
came every few seconds: the budget a node shares with everything behind its
router went to probes, and a 429 on a probe read as "no source".

A search that found nothing is kept too, for NO_SOURCE_TTL: the wakes came
every few seconds during a history import, and each one searched again — a DHT
traversal, and a 5 s /health per dead address (2026-10-07). Keying the
re-search to the sync walk instead did not hold, because the import's canon
starts a walk after every pass. A LAN peer that turns up, or the DHT attached
after the cycle exists, ends an empty result at once: those tiers cost nothing
to list. A discovered address that did not answer is skipped through the
record the walk keeps (sync_walk.DeadAddresses), which the walk's connect
feeds with how every probe went — a node that refused is busy, not dead;
manual and LAN peers are asked every time, as the walk asks them.
"""

import asyncio
import logging
import time
from typing import Awaitable, Callable, List, Optional, Tuple

from desktop.api_client import BackendAPIClient
from desktop.p2p import master_hint, node_hints
from desktop.p2p.addrs import fmt_addr
from desktop.p2p.sync_walk import DeadAddresses

logger = logging.getLogger(__name__)

SOURCES_TTL = 15 * 60
# Shorter for an empty search: "nothing found" is often a node that refused or
# timed out once, and an opened artist page waits on the next search.
NO_SOURCE_TTL = 5 * 60
PROBE_CONCURRENCY = 4

# (client, node id, the /health it answered)
Source = Tuple[BackendAPIClient, str, dict]
# (address, discovered) — a discovered one (DHT, directory, master hint) is
# subject to the dead-address record, a manual or LAN peer never
Candidate = Tuple[str, bool]


class SourceFinder:
    def __init__(
        self,
        label: str,
        *,
        capability: str,
        directory: Tuple[str, ...],
        usable: Callable[[dict], bool],
        connect: Callable[[str], Awaitable[Optional[BackendAPIClient]]],
        dead: DeadAddresses,
        dht,
        lan,
        manual_peers: List[str],
        load_bans: Callable[[], Tuple[set, set]],
        addr_uuid: Callable[[str], str],
    ):
        """
        label: the log prefix ("MB slice").
        capability: the DHT key dump holders announce ("mbdump").
        directory: the Worker directory capabilities to ask, replicas first
            ("mbslices", "mbdump").
        usable: /health → whether the node serves this family at all.
        connect: the walk's connect_peer — the /health probe, the TLS-pinned
            key, the own-address guard.
        dead: the walk's record of dead discovered addresses — skipped here;
            `connect` feeds it with how each one answered.
        """
        self.label = label
        self.capability = capability
        self.directory = directory
        self._usable = usable
        self._connect = connect
        self._dead = dead
        self.dht = dht
        self.lan = lan
        self.manual_peers = list(manual_peers)
        self._load_bans = load_bans
        self._addr_uuid = addr_uuid
        self._kept: List[Source] = []
        self._kept_at = 0.0
        self._kept_inputs: Optional[tuple] = None

    def drop(self, node_id: str) -> None:
        """A source that failed a request leaves the kept set; once none is
        left, the next find searches again."""
        self._kept = [s for s in self._kept if s[1] != node_id]
        if not self._kept:
            self._kept_at = 0.0

    def forget(self) -> None:
        self._kept, self._kept_at = [], 0.0

    async def find(self) -> Tuple[List[Source], bool]:
        """(sources, probed): the last search's result while it holds, else a
        new one — `probed` tells the caller it is new. A found set holds
        until a source fails or SOURCES_TTL passes; an empty one holds
        NO_SOURCE_TTL, and only while the LAN table and the DHT offer what
        they offered then."""
        if self._kept_at:
            age = time.monotonic() - self._kept_at
            if self._kept and age < SOURCES_TTL:
                return list(self._kept), False
            if not self._kept and age < NO_SOURCE_TTL and self._inputs() == self._kept_inputs:
                return [], False
        inputs = self._inputs()
        loop = asyncio.get_event_loop()
        banned_keys, banned_addrs = await loop.run_in_executor(None, self._load_bans)
        seen: set = set()
        found = await self._probe(await self._primary(), seen, banned_keys, banned_addrs)
        if not found:
            found = await self._probe(await self._fallback(), seen, banned_keys, banned_addrs)
        self._kept, self._kept_at, self._kept_inputs = found, time.monotonic(), inputs
        return list(found), True

    def _inputs(self) -> tuple:
        """What the tiers that cost nothing to list offer right now: the LAN
        table, and whether there is a DHT to ask."""
        lan = frozenset(self.lan.peers) if self.lan is not None else frozenset()
        return lan, self.dht is not None

    async def _primary(self) -> List[Candidate]:
        candidates = [(addr, False) for addr in self.manual_peers]
        if self.lan is not None:
            for ip, port in self.lan.peers:
                info = self.lan.get_peer_info(ip, port) or {}
                candidates.append(
                    (f"{info.get('scheme', 'https')}://{fmt_addr(ip, port)}", False))
        if self.dht is not None:
            try:
                for ip, port in await self.dht.lookup_capability(self.capability, want_all=True):
                    candidates.append((fmt_addr(ip, port), True))
            except Exception as e:
                logger.warning(f"{self.label}: DHT capability lookup failed: {e}")
        return candidates

    async def _fallback(self) -> List[Candidate]:
        """The Worker directory's volunteers FIRST, the master hint LAST —
        that ordering is the de-specialization (Ф16c)."""
        loop = asyncio.get_event_loop()
        candidates: List[Candidate] = []
        for cap in self.directory:
            for host, hport, _pk in await loop.run_in_executor(None, node_hints.fetch, cap):
                candidates.append((fmt_addr(host, hport), True))
        hint = await loop.run_in_executor(None, master_hint.fetch)
        if hint:
            candidates.append((fmt_addr(*hint), True))
        return candidates

    async def _probe(self, candidates: List[Candidate], seen: set,
                     banned_keys: set, banned_addrs: set) -> List[Source]:
        """The usable sources among `candidates`, in their order, one per node
        (a LAN and a public address of one node are one source). A discovered
        address the record holds as dead is not dialled."""
        addrs: List[str] = []
        for addr, discovered in candidates:
            if addr in seen:
                continue
            seen.add(addr)
            if self._addr_uuid(addr) in banned_addrs:
                logger.info(f"{self.label}: skipping banned address {addr}")
                continue
            if discovered and self._dead.backing_off(addr):
                continue
            addrs.append(addr)
        sem = asyncio.Semaphore(PROBE_CONCURRENCY)

        async def probe(addr: str) -> Optional[BackendAPIClient]:
            async with sem:
                return await self._connect(addr)

        found: List[Source] = []
        for api in await asyncio.gather(*(probe(a) for a in addrs)):
            if api is None or not api.last_health:
                continue
            health = api.last_health
            node_id = health.get("node_id", "")
            if node_id and node_id in banned_keys:
                logger.info(f"{self.label}: skipping banned node {node_id[:16]}… at {api.base_url}")
                continue
            if any(node_id == n for _, n, _ in found) or not self._usable(health):
                continue
            found.append((api, node_id, health))
        return found
