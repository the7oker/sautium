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
"""

import asyncio
import logging
import time
from typing import Awaitable, Callable, List, Optional, Tuple

from desktop.api_client import BackendAPIClient
from desktop.p2p import master_hint, node_hints
from desktop.p2p.addrs import fmt_addr

logger = logging.getLogger(__name__)

SOURCES_TTL = 15 * 60
PROBE_CONCURRENCY = 4

# (client, node id, the /health it answered)
Source = Tuple[BackendAPIClient, str, dict]


class SourceFinder:
    def __init__(
        self,
        label: str,
        *,
        capability: str,
        directory: Tuple[str, ...],
        usable: Callable[[dict], bool],
        connect: Callable[[str], Awaitable[Optional[BackendAPIClient]]],
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
        """
        self.label = label
        self.capability = capability
        self.directory = directory
        self._usable = usable
        self._connect = connect
        self.dht = dht
        self.lan = lan
        self.manual_peers = list(manual_peers)
        self._load_bans = load_bans
        self._addr_uuid = addr_uuid
        self._kept: List[Source] = []
        self._kept_at = 0.0

    def drop(self, node_id: str) -> None:
        """A source that failed a request leaves the kept set; the next find
        probes again."""
        self._kept = [s for s in self._kept if s[1] != node_id]

    def forget(self) -> None:
        self._kept, self._kept_at = [], 0.0

    async def find(self) -> Tuple[List[Source], bool]:
        """(sources, probed): the kept set while it is fresh and not empty,
        else a new one — `probed` tells the caller it is new."""
        if self._kept and time.monotonic() - self._kept_at < SOURCES_TTL:
            return list(self._kept), False
        loop = asyncio.get_event_loop()
        banned_keys, banned_addrs = await loop.run_in_executor(None, self._load_bans)
        seen: set = set()
        found = await self._probe(await self._primary(), seen, banned_keys, banned_addrs)
        if not found:
            found = await self._probe(await self._fallback(), seen, banned_keys, banned_addrs)
        self._kept, self._kept_at = found, time.monotonic()
        return list(found), True

    async def _primary(self) -> List[str]:
        candidates = list(self.manual_peers)
        if self.lan is not None:
            for ip, port in self.lan.peers:
                info = self.lan.get_peer_info(ip, port) or {}
                candidates.append(f"{info.get('scheme', 'https')}://{fmt_addr(ip, port)}")
        if self.dht is not None:
            try:
                for ip, port in await self.dht.lookup_capability(self.capability, want_all=True):
                    candidates.append(fmt_addr(ip, port))
            except Exception as e:
                logger.debug(f"{self.label}: DHT capability lookup failed: {e}")
        return candidates

    async def _fallback(self) -> List[str]:
        """The Worker directory's volunteers FIRST, the master hint LAST —
        that ordering is the de-specialization (Ф16c)."""
        loop = asyncio.get_event_loop()
        candidates: List[str] = []
        for cap in self.directory:
            for host, hport, _pk in await loop.run_in_executor(None, node_hints.fetch, cap):
                candidates.append(fmt_addr(host, hport))
        hint = await loop.run_in_executor(None, master_hint.fetch)
        if hint:
            candidates.append(fmt_addr(*hint))
        return candidates

    async def _probe(self, candidates: List[str], seen: set,
                     banned_keys: set, banned_addrs: set) -> List[Source]:
        """The usable sources among `candidates`, in their order, one per node
        (a LAN and a public address of one node are one source)."""
        addrs = []
        for addr in candidates:
            if addr in seen:
                continue
            seen.add(addr)
            if self._addr_uuid(addr) in banned_addrs:
                logger.info(f"{self.label}: skipping banned address {addr}")
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
