"""The CLIENT half of chat, one implementation for both runtimes.

The launcher's P2PManager composes it, and the Docker backend builds it
beside the sync walk (backend/main.py `_build_chat_client`), as the walk
became one on 2026-09-08. What it does: the friends' addresses (LAN by
identity, the known-good cache, the DHT, the master's Worker hint), the
contacts (the shipped Support contact, the handshakes that resolve a
`pending:` contact, the nudge, the rotation notices), the outbox (direct
push, relay forward with the recipient's receipt, the master's Worker
mailbox), the history pull, the wake streams it holds as a client (relay #0,
the master, and the peer relays an unreachable node recruits), and the
reachability verdict (the master's probe, the runtime's local tells, the
inbound proof).

The SERVER half stays on each peer surface (desktop/p2p/sync_server.py,
backend/routers/peer_chat.py); what both halves spell alike is in chat_wire.
Each runtime injects what is its own: the store (a ChatService), the
identity, the DHT, the LAN tier, the port the probe dials back, its local
reachability tells and the verdict's consumers, and the callbacks for what
arrives. The master runs no client half (p2p_chat.start_client says why).

The store blocks: every call goes through one thread of this client's own,
so the loop that runs it — the backend's serves the Web UI — never waits on
a chat query, the store's one connection is never used from two threads,
and the calls keep their order.
"""

import asyncio
import hashlib
import json
import logging
import random
import secrets
import time
import uuid as uuid_mod
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Awaitable, Callable, NamedTuple, Optional
from urllib.parse import quote

import psycopg2

from desktop.p2p import chat_wire, diag_protocol, master_node, wire
from desktop.p2p.addrs import canon_host, fmt_addr, is_internet_vantage
from desktop.p2p.chat_wire import (FORWARD_ACK_TIMEOUT, RELAY_HEADER, VOUCHER_TTL,
                                   delivery_payload, voucher_payload)
from desktop.p2p.peer_auth import pinned_pubkey, pinned_ssl_context

logger = logging.getLogger(__name__)

# What a friend, relay or probed node may answer a chat or relay request
# with (wire.read_json): a history export is the largest, the rest are a few
# fields. Each request's session already bounds its time.
PEER_ANSWER_CAP = 8 * wire.MiB
PEER_HEALTH_CAP = 256 * wire.KiB
# A handshake's, a voucher's, a probe's or a forward's answer: a few fields
# (a grant at most) — over channels anyone claiming an invite may answer.
PEER_SMALL_CAP = 64 * wire.KiB
# A relay holds our forward open until the recipient signs a receipt; give
# it that window plus the round trip before we give up on the request.
FORWARD_TIMEOUT = FORWARD_ACK_TIMEOUT + 10
# A contact made by accepting a mailed invite nudges its inviter at a refusal
# of its handshake in at most one resolver pass in this long while the
# answers are busy ones.
NUDGE_RETRY_S = 600
# A peer relay is a stranger that announced the capability: what its stream
# may send before this node lets it go (and asks it again in an hour), in
# lines (an event is two: its line and the blank one that ends it). A real
# relay sends a keepalive per 15 s and a frame per message forwarded to us —
# a few a minute, a backlog drained after a friend's absence. Refusals bound
# the abuse a relay can do with real frames (it cannot forge a stored one).
RELAY_FRAMES_PER_WINDOW = 600
RELAY_REFUSALS_PER_WINDOW = 5          # deliveries that did not open, or came again
RELAY_WINDOW_S = 60
# A relay's wake stream is dialled again after a failure on a backoff that
# doubles up to RECONNECT_CAP_S. The master's, while a message waits kept for
# Support (L4): nothing reaches a node behind NAT to say the master is back
# but its own retry — the master's drain pings a stream that is not there yet
# at its start — and the person reads "Kept for Support" until it connects.
RECONNECT_CAP_S = 300.0
KEPT_RECONNECT_CAP_S = 60.0


def replied_friends(notes) -> set:
    """The friends a reply was just stored for: the msg:{friend_id}:out
    payloads among `notes` on sautium_chat (trg_p2p_messages_notify) — read
    alike by both runtimes' listeners."""
    out = set()
    for note in notes:
        parts = (note.payload or "").split(":")
        if (note.channel == "sautium_chat" and len(parts) == 3 and parts[0] == "msg"
                and parts[2] == "out" and parts[1].isdigit()):
            out.add(int(parts[1]))
    return out


def upnp_heuristic(upnp, dht):
    """Local CGNAT tells, no network calls: a private/RFC 6598 router WAN
    address, or a router-WAN vs DHT-observed mismatch (another NAT above the
    router). (status, detail) or None — the launcher's, and a host-networked
    Docker node's, which has the host's router."""
    import ipaddress
    cgnat_net = ipaddress.ip_network("100.64.0.0/10")
    wan = upnp.external_ip if upnp else None
    if wan:
        try:
            addr = ipaddress.ip_address(wan)
            if addr.is_private or addr in cgnat_net:
                return ("cgnat", f"router WAN {wan} is private/carrier-grade")
        except ValueError:
            wan = None
    observed = getattr(dht, "observed_external_ip", None) if dht else None
    if wan and observed and wan != observed:
        return ("cgnat", f"router WAN {wan} != observed {observed}")
    return None


def public_addresses(upnp, dht) -> frozenset:
    """The internet addresses this node knows as its own router's: the
    router's WAN address (UPnP) and the one DHT peers see. A peer at one of
    them stands behind the same router as this node."""
    known = (upnp.external_ip if upnp else None,
             getattr(dht, "observed_external_ip", None) if dht else None)
    return frozenset(canon_host(a) for a in known if a and is_internet_vantage(a))


# The verdict's detail (shown on the Sync & P2P screen's reachability row)
# when the only node that could look stands behind this node's own router
SAME_ROUTER_DETAIL = ("Cannot be checked from here: the only node that could look is behind "
                      "this node's own router.")


# What a cert-gated issuer answers a handshake whose identity lacks its
# certificate or its proof (identity_registry.IdentityGate.admit). An issuer
# before 2026-11-18 answered an absent certificate "invalid".
_LACKING_REFUSALS = {
    "certificate": ("identity certificate required", "identity certificate invalid"),
    "proof": ("identity proof required",),
}
# An issuer too small to check a proof-of-work identity
# (identity_registry.UNVERIFIABLE): the same answer until OUR identity is
# another — a verified email, which every node knows by its signature.
_UNVERIFIABLE_REFUSAL = "identity proof cannot be checked on this node"


@dataclass(frozen=True)
class ClientIdentity:
    """This node's account as the client speaks it. `cert_bundle()` is
    {"cert", "proof"} (the proof None until mined) or None without a
    certificate — loaded lazily, the certificate and the proof can land
    after the client exists. `identity_dir` holds the rotation archive (None:
    node_identity's own folder, the launcher's)."""
    public_key_hex: str
    username: str
    invite_code: str
    sign: Callable[[bytes], bytes]
    cert_bundle: Callable[[], Optional[dict]]
    identity_dir: Optional[Path] = None


class _ProbeResult(NamedTuple):
    """One reachability probe cycle. `verdict` None = no internet vantage
    answered; `retry_soon` = the reason is transient (master mid-resolution,
    cooldown) rather than settled."""
    verdict: Optional[bool]
    detail: str
    retry_soon: bool = False


def _iso_utc(ts) -> str:
    """Message timestamp as a tz-aware ISO string (the wire format both
    /api/chat/message and the relay envelope use)."""
    out = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
    if "+" not in out and not out.endswith("Z"):
        out += "+00:00"
    return out


def accepted_pubkey(result: dict, expected_invite: str, expect_master: bool = False,
                    fingerprint: Optional[str] = None) -> Optional[str]:
    """The responder's public key — or None when it does not belong to
    the contact we set out to reach.

    An invite code is SHA-256(pubkey)[:6]: it cannot produce a key, but
    it can prove one. Only the RECEIVER of a handshake used to check
    that binding, so an initiator accepted whatever key answered — and
    DHT announces are unauthenticated, so anyone willing to announce a
    contact's infohash could hand us their own key and read everything
    addressed to that contact from then on. The mutual-add rule does
    not help: our own key and invite ride in the handshake we just
    sent them.

    Everything downstream — E2E encryption, delivery receipts, the
    relay's whole "cannot lie about delivery" property — is anchored
    to this one comparison.
    """
    from desktop.node_identity import verify_invite_code
    pubkey = result.get("public_key_hex", "")
    if not pubkey:
        return None
    if expect_master:
        if pubkey.lower() != master_node.MASTER_PUBKEY_HEX.lower():
            # The shipped contact is pinned by its full key: 48 bits of
            # fingerprint are grindable, 256 are not.
            logger.warning("Handshake answered for the master with a "
                           "foreign key %s… — ignoring", pubkey[:16])
            return None
        return pubkey
    if not verify_invite_code(expected_invite, pubkey) or not chat_wire.binds(fingerprint, pubkey):
        logger.warning(
            "Handshake for %s answered with key %s… that does not match "
            "the invite code or the key the contact was made with — "
            "ignoring (impersonation attempt or a peer that rotated keys "
            "without notifying us)",
            expected_invite, pubkey[:16])
        return None
    return pubkey


def _verify(pubkey_hex: str, message: bytes, signature: bytes) -> bool:
    from desktop.node_identity import verify_signature
    return verify_signature(message, signature, pubkey_hex)


def nudge_invite(friend: dict, pinned: Optional[str]) -> Optional[str]:
    """The invite a refused handshake's nudge names: a contact made by
    accepting a mailed invite nudges its inviter, armed by the acceptance
    itself (accepted_invite_id) until an answer spends it
    (ChatClient._nudge_refused), and only over a channel the inviter's own
    key holds — the nudge names the invite. None when there is nothing to
    nudge with, or nobody to nudge here."""
    accepted, bound = friend.get("accepted_invite_id"), friend.get("key_fingerprint")
    if accepted and bound and not friend.get("join_token_id") and pinned \
            and chat_wire.binds(bound, pinned):
        return accepted
    return None


def verify_delivery_receipt(ack: dict, expected_pubkey: str,
                            message_uuid: str, ct_hash: str) -> bool:
    """The receipt must be signed by the RECIPIENT over exactly the
    message we sent — that is what makes a relay unable to fake
    delivery."""
    from desktop.node_identity import verify_signature
    if (ack.get("public_key_hex", "").lower() != expected_pubkey.lower()
            or ack.get("message_uuid") != message_uuid
            or ack.get("ciphertext_sha256") != ct_hash):
        return False
    try:
        return verify_signature(
            delivery_payload(message_uuid, ct_hash).encode("utf-8"),
            bytes.fromhex(ack.get("signature", "")), expected_pubkey)
    except (ValueError, TypeError):
        return False


class ChatClient:
    """The client half of chat for one node (module docstring)."""

    PEER_RELAY_K = 2          # peer relays held on top of the master
    RELAY_SCAN_INTERVAL = 10 * 60

    def __init__(self, store, *,
                 identity: Callable[[], Optional[ClientIdentity]],
                 announce_port: Callable[[], int],
                 dht=None, lan=None,
                 heuristic: Optional[Callable[[], Optional[tuple]]] = None,
                 own_addresses: Optional[Callable[[], frozenset]] = None,
                 on_reachability: Optional[Callable[[str, str], Awaitable[None]]] = None,
                 on_incoming: Optional[Callable[[dict], None]] = None,
                 on_warrant: Optional[Callable[[dict, tuple], None]] = None,
                 on_master_reachable: Optional[Callable[[tuple], None]] = None):
        """
        Args:
            store: a ChatService on this node's database and key.
            identity: () -> ClientIdentity | None (no account yet).
            announce_port: () -> the port the master's probe dials back —
                the one peers dial (a UPnP external port when mapped).
            dht: a DHTService (desktop/p2p/dht_service.py) or None; may be
                set later (`client.dht = ...`), the DHT starts after.
            lan: a LANDiscovery or None (a container on a bridge).
            heuristic: () -> (status, detail) | None, the local tells.
            own_addresses: () -> this node's own public addresses
                (public_addresses), where the runtime has a router to ask.
            on_reachability: the runtime's consumers of a verdict.
            on_incoming: a message stored from a relay, or a history pull
                that imported (`history_sync`).
            on_warrant: a support warrant from the master's stream, with
                the master's address (desktop/p2p/diag_worker.py); None
                drops it.
            on_master_reachable: the master's stream connected, at (ip, port).
        """
        self.store = store
        self._identity = identity
        self._announce_port = announce_port
        self.dht = dht
        self.lan = lan
        self._heuristic = heuristic or (lambda: None)
        self._own_addresses = own_addresses or frozenset
        self._on_reachability = on_reachability
        self._on_incoming = on_incoming
        self._on_warrant = on_warrant
        self._on_master_reachable = on_master_reachable
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="chat-store")
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.running = False
        self._tasks: list = []
        self._notify: Optional[asyncio.Event] = None
        self._resolve_nudge: Optional[asyncio.Event] = None
        # Token handshakes parked on what our own identity lacks (its
        # certificate, its proof), and the identity they were parked under
        self._identity_wait_invites: set = set()
        self._identity_wait_for: Optional[tuple] = None
        self._reachability_said: Optional[tuple] = None    # the verdict last logged at info
        self._dht_refresh_inflight: set = set()
        # The resolver pass a contact last nudged in (its monotonic start),
        # by friend id: a contact nudges at most one pass in NUDGE_RETRY_S
        # while busy answers keep its nudge armed.
        self._nudged_at: dict[int, float] = {}
        # Peer address cache: friend_id -> peers_list. Persists until a
        # connection failure triggers a refresh.
        self._friend_peer_cache: dict[int, list[tuple]] = {}
        # Friends whose history has been pulled this session (skip re-pull)
        self._history_synced_friends: set[int] = set()
        # (friend id, the answering node's address) -> the history cursor the
        # last pull from it ended on: a pull goes on from it (a wake pulls
        # once per reply), so a thread is read whole once a session and then
        # only what is new — per node: two of one account answer from their
        # own rows.
        self._history_after: dict[tuple, str] = {}
        # Friends a stored message is being pushed to at once (_deliver_now),
        # and those a further message arrived for meanwhile.
        self._pushing: set[int] = set()
        self._push_again: set[int] = set()
        self._push_tasks: set = set()
        # Live peer-relay subscriptions: relay_pubkey -> asyncio.Task
        self._peer_relay_subs: dict[str, asyncio.Task] = {}
        self._relay_refused_until: dict[str, float] = {}     # peer relay key -> when to ask again
        self._peer_relay_addrs: dict[str, tuple] = {}
        # This node among the nodes of its account: every relay keeps one
        # wake stream per instance, so a second machine signed into the same
        # account holds its own stream instead of taking this one's
        # (desktop/p2p/wake_registry.py).
        self._wake_instance = secrets.token_hex(8)
        # unknown | reachable | cgnat | unreachable — see _reachability_loop
        self.reachability = "unknown"
        # Kept messages the master refused on a direct push, said once each.
        self._refusals_said: set = set()
        # The master's addresses that failed its stream lately: address ->
        # until when a stream tries the others first.
        self._master_failed: dict[tuple, float] = {}
        # A peer relay's frames and refusals, kept across its reconnects.
        self._relay_windows: dict[str, tuple] = {}
        self._recheck: Optional[asyncio.Event] = None
        # The master's first handshake with this node's key (_handshake_
        # accepted): what the master's stream waits for. And a message just
        # kept for Support: the stream's next dial comes sooner (L4).
        self._master_accepted = asyncio.Event()
        self._master_redial = asyncio.Event()
        # The delivery path's state per friend, as last said at info (_say_path)
        self._path_said: dict[int, str] = {}

    # ------------------------------------------------------------------
    # Lifecycle and the runtime's events
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """On the loop that runs the client: the identity checks a start
        makes, then every loop of the client half."""
        self._loop = asyncio.get_running_loop()
        self._notify = asyncio.Event()
        self._resolve_nudge = asyncio.Event()
        self._recheck = asyncio.Event()
        self.running = True
        try:
            await self._reintroduce_after_identity_change()
        except Exception as e:
            logger.error(f"Identity re-introduction check failed: {e}")
        try:
            await self._ensure_master_contact()
        except Exception as e:
            # A shipped-contact hiccup must never take chat down; the
            # master contact is a convenience.
            logger.error(f"Master contact seeding failed: {e}")
        self._tasks = [asyncio.create_task(c) for c in (
            self._pass_loop(), self._resolve_loop(), self._master_wake_loop(),
            self._peer_relay_manager(), self._reachability_loop())]

    async def stop(self) -> None:
        self.running = False
        if self._notify is not None:
            self._notify.set()
        tasks = [*self._tasks, *self._push_tasks, *self._peer_relay_subs.values()]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.debug(f"chat client task error during stop: {e}")
        self._tasks, self._peer_relay_subs = [], {}
        self._pool.shutdown(wait=False)

    def _soon(self, fn, *args) -> None:
        """Run fn(*args) on the client's loop — from any thread."""
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(fn, *args)

    def chat_notes(self, notes) -> None:
        """NOTIFY sautium_chat as the runtime's listener heard it (any
        thread): the pass wakes, and a friend a message was just stored for
        is pushed to at once."""
        self._soon(self._on_notes, list(notes))

    def _on_notes(self, notes: list) -> None:
        if not self.running:
            return
        # A message stored, or a friend seen (a presence bump: they reached
        # us, so what we hold for them may go now) — not a state change
        # (state:{fid}), which the pass itself causes.
        if any(note.channel == "sautium_chat" and not (note.payload or "").startswith("state:")
               for note in notes):
            self._notify.set()
        replies = replied_friends(notes)
        if replies:
            self._deliver_now(replies)

    def resolve_soon(self) -> None:
        """The resolver, now (any thread): a LAN peer appeared, an acceptance
        was picked up, our identity proof was mined."""
        if self._resolve_nudge is not None:
            self._soon(self._resolve_nudge.set)

    def recheck_reachability(self) -> None:
        """Probe again now (any thread): what the probe would see changed —
        the router mapped the peer port."""
        if self._recheck is not None:
            self._soon(self._recheck.set)

    async def _db(self, fn, *args):
        """fn(*args) on the store's own thread."""
        return await asyncio.get_running_loop().run_in_executor(self._pool, partial(fn, *args))

    # ------------------------------------------------------------------
    # Settings (the store's connection for reads, a short-lived one for
    # writes — executor-safe)
    # ------------------------------------------------------------------

    def _read_setting_blocking(self, key: str):
        with self.store.db_conn().cursor() as cur:
            cur.execute("SELECT value FROM user_settings WHERE key = %s", (key,))
            row = cur.fetchone()
            return row[0] if row else None

    async def setting(self, key: str):
        """One user_settings value (JSONB)."""
        return await self._db(self._read_setting_blocking, key)

    def _write_settings_blocking(self, values: dict) -> None:
        conn = psycopg2.connect(self.store.db_dsn)
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                for key, value in values.items():
                    cur.execute("""
                        INSERT INTO user_settings (key, value, updated_at)
                        VALUES (%s, %s::jsonb, NOW())
                        ON CONFLICT (key) DO UPDATE
                            SET value = EXCLUDED.value, updated_at = NOW()
                    """, (key, json.dumps(value)))
        finally:
            conn.close()

    async def _write_settings(self, values: dict) -> None:
        await asyncio.get_running_loop().run_in_executor(None, self._write_settings_blocking, values)

    async def master_contact(self) -> Optional[dict]:
        """The resolved master contact, or None."""
        return await self._db(self.store.get_friend_by_public_key, master_node.MASTER_PUBKEY_HEX)

    # ------------------------------------------------------------------
    # Addresses
    # ------------------------------------------------------------------

    async def _note_peer_alive(self, friend_id: Optional[int],
                               pubkey: Optional[str], ip: str, port: int) -> None:
        """One event, two facts: this friend answered us AT this address,
        just now.

        The address goes to the head of the candidate list — a DHT hit only
        proves someone announced an address, never that it answers, so the
        proven one must outrank it.

        `last_seen` moves too, because an answered request is presence.
        Without this a passive peer — the master never pushes, by design —
        goes "offline" five minutes after the handshake while its wake
        stream is still connected and its history pulls still succeed.
        """
        if friend_id:
            peer = (ip, port)
            cached = [p for p in self._friend_peer_cache.get(friend_id, [])
                      if p != peer]
            self._friend_peer_cache[friend_id] = [peer] + cached
        if pubkey and not pubkey.startswith("pending:"):
            await self._db(self.store.update_friend_last_seen, pubkey)

    async def find_friend_peers(self, friend: dict, refresh: bool = False,
                                untargeted: bool = True) -> list[tuple]:
        """Candidate addresses for a friend — the SINGLE source every path
        uses (resolve, push, history pull, wake subscribe). Order: LAN by
        identity, then last-known-good cache, then DHT, then every other
        LAN peer as a final tier.

        That last tier matters on a peer's own LAN: the address a node
        announces in the DHT is its router's public IP, which answers only
        with NAT hairpinning and usually doesn't — while the same node sits
        one hop away on the LAN (or on 127.0.0.1 as a localhost-probed
        Docker node).

        Args:
            friend: friend dict with id, invite_code, public_key_hex.
            refresh: if True, skip cache and do fresh DHT lookup.
                     If fresh lookup finds nothing, the old cached
                     address is kept (peer may come back at same addr).
        """
        fid = friend.get("id")
        invite_code = friend.get("invite_code", "")
        pubkey = friend.get("public_key_hex", "")

        peers: list[tuple] = []

        def add(candidates):
            for p in candidates:
                if p and p not in peers:
                    peers.append(p)

        # 1. LAN by identity — instant and always right when it hits.
        if self.lan:
            lan = self.lan.find_peer_by_invite_code(invite_code)
            if not lan and pubkey and not pubkey.startswith("pending:"):
                lan = self.lan.find_peer_by_node_id(pubkey)
            add([lan])

        # 2. Last known good (_note_peer_alive puts the winner first).
        if fid and not refresh:
            add(self._friend_peer_cache.get(fid, []))

        # 3. DHT — an announcement, not a promise of reachability. Awaited
        # only when it is the ONLY hope (no LAN hit, no known-good cache)
        # or the caller asked for fresh data: a get_peers that finds
        # nothing burns its full 30 s timeout and empty results are never
        # cached, so awaiting it with a working candidate already in hand
        # put half a minute in front of EVERY chat message whenever the
        # DHT was unhealthy (measured 2026-08-19: 30–33 s per message on a
        # LAN pair; the master-wake resolve burned the same 30 s per
        # reconnect attempt). With candidates present the lookup still
        # runs — in the background, refreshing the cache for later calls.
        # The untargeted final tier (every live LAN peer, step 4 below)
        # counts as "something to try now": the push path survives wrong
        # nodes (403 → next candidate), so a cold cache on a LAN pair must
        # not wait out a dead DHT either.
        lan_others = list(self.lan.peers) if self.lan else []

        # Last resort, MASTER only: the Worker-served address hint. Mobile
        # carriers throttle UDP hard enough that a newborn's DHT bootstrap
        # can find zero nodes while HTTPS works — without this tier such a
        # node has NO route to the support/dump/relay node at all. Purely a
        # discovery candidate: the handshake still checks the pinned pubkey.
        # The master is known by its invite code as well as its key: a
        # newborn's contact is still `pending:`, and its first handshake is
        # the very call this tier is for. Nor do untargeted LAN peers stand
        # in for it — they are other nodes, not a route to this one.
        # (2026-10-05: a node born while the master was down never resolved
        # it; on that network the DHT answered no user key at all.)
        if not peers:
            if (pubkey == master_node.MASTER_PUBKEY_HEX
                    or invite_code == master_node.MASTER_INVITE_CODE):
                from desktop.p2p import master_hint
                hint = await asyncio.get_running_loop().run_in_executor(None, master_hint.fetch)
                if hint:
                    add([hint])

        if self.dht and self.dht.is_available:
            if refresh or not (peers or lan_others):
                add(await self.dht.lookup_user(invite_code))
            elif fid and fid not in self._dht_refresh_inflight:
                self._dht_refresh_inflight.add(fid)
                asyncio.ensure_future(self._dht_refresh_peers(fid, invite_code))

        if fid and peers:
            self._friend_peer_cache[fid] = list(peers)
        elif fid:
            add(self._friend_peer_cache.get(fid, []))

        # 4. Every other live LAN peer. A handful of addresses, and the
        # reason a same-LAN or localhost-probed node stays reachable when
        # its DHT address is an unhairpinnable public IP. Not for a caller
        # that holds one address at a time (the master's stream).
        if untargeted:
            add(lan_others)

        return peers

    async def _dht_refresh_peers(self, fid: int, invite_code: str) -> None:
        """Background half of find_friend_peers step 3: merge whatever the
        DHT knows into the friend's candidate cache without holding up the
        caller. Append-only — the known-good head keeps its slot."""
        try:
            found = await self.dht.lookup_user(invite_code)
            if found:
                cached = self._friend_peer_cache.get(fid, [])
                self._friend_peer_cache[fid] = cached + [
                    p for p in found if p not in cached]
        except Exception as e:
            logger.debug(f"background DHT refresh for {invite_code} failed: {e}")
        finally:
            self._dht_refresh_inflight.discard(fid)

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------

    async def _sync_chat_history(self, friend: dict, peers: list[tuple]) -> dict:
        """Request chat history from a friend's peer and import it, a page
        at a time from where the last pull ended (_history_after), each
        after the `next` cursor of the one before while the peer says there
        is `more` — at most chat_wire.HISTORY_PAGES_PER_PULL pages a pull.

        Returns: import stats {imported, skipped, delivered} or {}.
        """
        if not peers:
            return {}
        friend_pubkey = friend.get("public_key_hex", "")
        if not friend_pubkey or friend_pubkey.startswith("pending:"):
            return {}
        ident = self._identity()
        if not ident:
            return {}

        for ip, port in peers:
            url = f"https://{fmt_addr(ip, port)}/api/chat/history"
            stats = {"imported": 0, "skipped": 0, "delivered": 0}
            pages = 0
            cursor = (friend["id"], fmt_addr(ip, port))
            while pages < chat_wire.HISTORY_PAGES_PER_PULL:
                data = await self._history_page(url, friend_pubkey, ident,
                                                self._history_after.get(cursor))
                if data is None:
                    break
                if pages == 0:
                    await self._note_peer_alive(friend.get("id"), friend_pubkey, ip, port)
                pages += 1
                for k, v in (await self._import_history_page(friend, friend_pubkey, data)).items():
                    stats[k] += v
                after = data.get("next")
                if isinstance(after, str) and after:
                    self._history_after[cursor] = after
                if data.get("more") is not True:
                    break
            if not pages:
                continue
            logger.info(
                f"History sync with {friend.get('username', '?')}: "
                f"{stats} in {pages} page(s)"
            )
            if stats["imported"] > 0 and self._on_incoming:
                self._incoming({"friend_id": friend["id"], "content": "",
                                "timestamp": None, "history_sync": True})
            return stats

        return {}

    def _incoming(self, result: dict) -> None:
        try:
            self._on_incoming(result)
        except Exception as e:
            logger.warning(f"incoming-message callback failed: {e}")

    async def _history_page(self, url: str, friend_pubkey: str, ident: ClientIdentity,
                            after: Optional[str]) -> Optional[dict]:
        """One page of a friend's history export, or None when the peer did
        not answer with one. Each request is signed for that friend alone
        and bound to its time (±60 s server-side), so a captured signature
        replays neither elsewhere nor later."""
        import aiohttp
        nonce = str(uuid_mod.uuid4())
        ts = int(time.time())
        payload = {
            "public_key_hex": ident.public_key_hex,
            "nonce": nonce,
            "ts": ts,
            "signature": ident.sign(chat_wire.history_payload(ts, nonce, friend_pubkey)).hex(),
        }
        if after:
            payload["after"] = after
        try:
            async with aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=pinned_ssl_context(friend_pubkey))
            ) as session:
                async with session.post(
                    url, json=payload, timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status != 200:
                        return None
                    return await wire.read_json(resp, PEER_ANSWER_CAP)
        except Exception as e:
            logger.debug(f"History request to {url} failed: {e}")
            return None

    async def _import_history_page(self, friend: dict, friend_pubkey: str, data: dict) -> dict:
        """One page opened and imported on the store's thread: up to
        HISTORY_EXPORT_MAX boxes to open is no work for the loop."""
        return await self._db(self._import_page_blocking, friend, friend_pubkey, data)

    def _import_page_blocking(self, friend: dict, friend_pubkey: str, data: dict) -> dict:
        """Open each message's envelope of one page and import them: a
        message's uuid and time are the box's, never the listing's, and its
        size is a message's, as on the direct path."""
        from desktop.p2p.chat_service import MAX_ENCRYPTED_CHARS, MAX_PLAINTEXT_CHARS
        raw_messages = data.get("messages", [])
        if not isinstance(raw_messages, list) or not raw_messages:
            return {}
        decrypted = []
        for msg in raw_messages[:chat_wire.HISTORY_EXPORT_MAX]:
            try:
                if len(msg["encrypted"]) > MAX_ENCRYPTED_CHARS:
                    raise ValueError("ciphertext over a message's size")
                env = self.store.decrypt_envelope(msg["encrypted"], friend_pubkey)
            except Exception as e:
                logger.debug(f"Failed to open history msg: {e}")
                continue
            if (msg.get("message_uuid") != env["uuid"]
                    or msg.get("direction") not in ("in", "out")
                    or len(env["text"]) > MAX_PLAINTEXT_CHARS):
                logger.debug("History msg disagrees with its envelope or its size — skipped")
                continue
            decrypted.append({
                "message_uuid": env["uuid"],
                "direction": msg["direction"],
                "content": env["text"],
                "timestamp": env["ts"],
                "sig": env["sig"],
            })
        if not decrypted:
            return {}
        return self.store.import_history(friend["id"], decrypted)

    # ------------------------------------------------------------------
    # Contacts: the resolver, the nudge, the master contact, rotations
    # ------------------------------------------------------------------

    async def _resolve_loop(self):
        """Resolve pending friends by handshake. The 15 s cadence is the
        resolver's existing fallback (moved as it was); `resolve_soon` cuts
        it short on an event — a LAN peer appeared, an acceptance was picked
        up, our identity proof finished mining (the parked cert-gated
        handshake retries the moment it can succeed)."""
        await asyncio.sleep(5)  # initial delay for LAN beacons to arrive
        while self.running:
            try:
                await self._do_resolve_pending()
            except Exception as e:
                logger.debug(f"Resolve pending friends error: {e}")
            try:
                await asyncio.wait_for(self._resolve_nudge.wait(), 15)
            except asyncio.TimeoutError:
                pass
            self._resolve_nudge.clear()

    async def _do_resolve_pending(self):
        """Check LAN/DHT peers for pending friends and do handshake."""
        await self._deliver_rotation_notices()
        pass_at = time.monotonic()
        friends = await self._db(self.store.get_friends)
        if not self._master_accepted.is_set() and any(
                f["public_key_hex"] == master_node.MASTER_PUBKEY_HEX for f in friends):
            # Resolved by no handshake of this client's (a life-data merge of
            # the owner's backup): its stream starts all the same
            self._master_accepted.set()

        # Collect invite codes that are already resolved (have real public key)
        resolved_invites = {
            f["invite_code"] for f in friends
            if not f["public_key_hex"].startswith("pending:")
        }

        pending = []
        for f in friends:
            if not f["public_key_hex"].startswith("pending:"):
                continue
            if f["invite_code"] in resolved_invites:
                # Stale duplicate — real entry already exists, remove this one
                await self._db(self.store.remove_friend, f["id"])
                logger.info(f"Removed stale pending entry for {f['invite_code']}")
                continue
            pending.append(f)

        if not pending:
            return

        ident = self._identity()
        if not ident:
            return

        import aiohttp
        handshake_data = {
            "public_key_hex": ident.public_key_hex,
            "username": ident.username,
            "invite_code": ident.invite_code,
        }

        bundle = ident.cert_bundle()
        own_cert = bundle["cert"] if bundle else None
        own_proof = bundle.get("proof") if bundle else None
        # While our own identity lacks its certificate (not issued yet) or
        # its pow proof (still being mined), a cert-gated issuer can only
        # refuse — friends parked on that refusal are skipped instead of
        # re-asked every tick, until the identity read here changes. The
        # worker's `ready` calls resolve_soon, so the retry happens the
        # moment it can succeed.
        if own_cert is None:
            lacking = "certificate"
        elif own_cert.get("method") == "pow" and own_proof is None:
            lacking = "proof"
        else:
            lacking = None
        held = (own_cert and own_cert.get("sig"), own_proof and own_proof.get("nonce"))
        if held != self._identity_wait_for:
            self._identity_wait_invites.clear()
            self._identity_wait_for = held

        for friend in pending:
            invite = friend.get("invite_code", "")
            if not invite:
                continue

            # Token invites auto-accept: attach the token + a fresh signed
            # timestamp (the issuer skips its mutual-add check on this
            # path, so it demands proof of key possession). Birth cert
            # rides along whenever we have one — the master token requires
            # it as its anti-sybil gate.
            per_friend = dict(handshake_data)
            join_token = friend.get("join_token_id")
            if join_token and invite in self._identity_wait_invites:
                logger.debug(f"Handshake for {invite} waits for our identity "
                             f"{lacking or 'to be one its node can check'}")
                continue
            if join_token:
                ts = int(time.time())
                per_friend["token_id"] = join_token
                per_friend["ts"] = ts
                per_friend["signature"] = ident.sign(
                    f"token_handshake:{ts}:{join_token}:{invite}".encode("utf-8")).hex()
                if own_cert:
                    per_friend["birth_cert"] = own_cert
                    if own_proof:
                        per_friend["identity_proof"] = own_proof
            else:
                # The classic path, signed for this one responder
                # (desktop/p2p/chat_wire.handshake_payload).
                per_friend = chat_wire.sign_handshake(ident.sign, per_friend, invite)

            peers = await self.find_friend_peers(friend)
            if not peers:
                continue

            nudged = None
            for ip, port in peers:
                url = f"https://{fmt_addr(ip, port)}/api/chat/handshake"
                try:
                    channel_ctx = pinned_ssl_context()
                    async with aiohttp.ClientSession(
                        connector=aiohttp.TCPConnector(ssl=channel_ctx)
                    ) as session:
                        async with session.post(
                            url, json=per_friend,
                            timeout=aiohttp.ClientTimeout(total=10),
                        ) as resp:
                            if resp.status == 200:
                                result = await wire.read_json(resp, PEER_SMALL_CAP)
                                if result.get("accepted"):
                                    peer_pubkey = accepted_pubkey(
                                        result, invite,
                                        expect_master=friend.get("source") == "master",
                                        fingerprint=friend.get("key_fingerprint"))
                                    if (not peer_pubkey
                                            or peer_pubkey.lower() != pinned_pubkey(channel_ctx)):
                                        continue
                                    await self._handshake_accepted(
                                        peer_pubkey, invite, result, ident, ip, port)
                                    break

                            # Handshake rejected — try nudge (auto-reciprocate
                            # via Worker). Peer checks their pending accepts
                            # and adds us if found. Identity-gate answers
                            # (proof pending / verifier busy) are retried by
                            # this loop as they are — a nudge cannot help.
                            if resp.status in (429, 503):
                                logger.debug(
                                    f"Handshake for {invite} deferred by "
                                    f"{ip}:{port}: HTTP {resp.status}")
                                continue
                            if resp.status == 403:
                                err = ""
                                try:
                                    err = (await wire.read_json(resp, PEER_SMALL_CAP)).get("error", "")
                                except Exception:
                                    pass
                                if err.startswith("identity"):
                                    if lacking and err in _LACKING_REFUSALS[lacking]:
                                        # A known-failing precondition on OUR
                                        # side: park this friend until our
                                        # identity changes instead of asking
                                        # again for the same answer.
                                        self._identity_wait_invites.add(invite)
                                        logger.info(
                                            f"Handshake for {invite} parked until our node has its "
                                            "identity (a mined proof, or a verified email)"
                                            if lacking == "proof" else
                                            f"Handshake for {invite} parked until our identity "
                                            "certificate is issued")
                                        break      # same answer from every address
                                    if err == _UNVERIFIABLE_REFUSAL:
                                        # Its node cannot check a mined proof,
                                        # now or later: asked again only when
                                        # our identity is another
                                        self._identity_wait_invites.add(invite)
                                        logger.info(
                                            f"Handshake for {invite} parked: its node is too small "
                                            "to check a proof-of-work identity — a verified email "
                                            "is known to every node")
                                        break
                                    logger.info(
                                        f"Handshake for {invite} refused by "
                                        f"{ip}:{port}: {err}")
                                    continue
                                outcome = await self._nudge_refused(
                                    friend, ip, port, invite,
                                    chat_wire.sign_handshake(ident.sign, handshake_data, invite),
                                    session, pinned_pubkey(channel_ctx), pass_at)
                                nudged = outcome or nudged
                                if outcome == "taken":
                                    break
                except Exception as e:
                    logger.debug(f"Handshake to {ip}:{port} for {invite} failed: {e}")
            if nudged == "answered":
                # Every address of the inviter's key had its nudge this pass,
                # and one answered without taking it (a node of its account
                # without the route): spent.
                await self._db(self.store.nudged, friend["id"])

    async def _handshake_accepted(self, peer_pubkey: str, invite: str, result: dict,
                                  ident: ClientIdentity, ip: str, port: int) -> None:
        """A pending contact resolved: its row bound to the key that answered,
        the issuer's grant kept, presence, and the first history pull."""
        await self._db(self._bind_resolved, peer_pubkey, invite, result.get("username", ""),
                       result, ident.public_key_hex)
        logger.info(f"Pending friend resolved via handshake: {invite} ({ip}:{port})")
        if peer_pubkey.lower() == master_node.MASTER_PUBKEY_HEX.lower():
            self._master_accepted.set()           # the row is resolved: its stream may start
        resolved_friend = await self._db(self.store.get_friend_by_public_key, peer_pubkey)
        await self._note_peer_alive((resolved_friend or {}).get("id"), peer_pubkey, ip, port)
        if peer_pubkey.lower() == master_node.MASTER_PUBKEY_HEX.lower():
            await self._keep_mail_voucher(result.get("mail_voucher"), ident.public_key_hex)
        if resolved_friend:
            await self._sync_chat_history(resolved_friend, [(ip, port)])
            if peer_pubkey.lower() == master_node.MASTER_PUBKEY_HEX.lower():
                # What was written to Support before it answered goes now,
                # where it just answered — the handshake is its event (B-116)
                await self._push_master_outbox(resolved_friend, ip, port)

    async def _keep_mail_voucher(self, voucher, own_pubkey: str) -> None:
        """The master's voucher for its mailbox, from its handshake's answer
        or its stream's header, kept when it is the master's for our key now
        (SUPPORT-FIRST §2.4); anything else is no voucher."""
        if voucher and chat_wire.mail_voucher_holds(voucher, own_pubkey,
                                                    master_node.MASTER_PUBKEY_HEX, _verify):
            await self._db(self.store.keep_mail_voucher, voucher)

    def _bind_resolved(self, peer_pubkey: str, invite: str, username: str,
                       result: dict, own_pubkey: str) -> None:
        self.store.add_friend(public_key_hex=peer_pubkey, invite_code=invite, username=username)
        self._store_incoming_grant(result, own_pubkey)
        if peer_pubkey.lower() == master_node.MASTER_PUBKEY_HEX.lower():
            # What a node with no identity of its own still reaches is part
            # of the identity state its pages hold (routers.p2p.support_known)
            with self.store.db_conn().cursor() as cur:
                cur.execute("NOTIFY sautium_identity")
        # Mirror the recipient-side presence bump: a 200 "accepted" is proof
        # the remote node is alive. Without this update one side of a
        # freshly-paired duo always shows the other as offline because only
        # the accept-handler bumps last_seen.
        self.store.update_friend_last_seen(peer_pubkey)

    async def _nudge_refused(self, friend: dict, ip: str, port: int, invite: str, signed: dict,
                             session, pinned: Optional[str], pass_at: float) -> Optional[str]:
        """After the inviter refused our handshake at one address: the
        contact's nudge (nudge_invite), to every address its key holds in
        one resolver pass, and in at most one pass in NUDGE_RETRY_S. →
        "taken" (a 200: the inviter took it, it is spent, our handshake goes
        again at once), "answered" (an answer that spends it at the pass's
        end — a Docker node of the inviter's account has no nudge route,
        and picks up at its own Friends screen), or None (not nudged, or a
        busy answer — none, 403, 429, 5xx — which keeps it armed)."""
        nudge = nudge_invite(friend, pinned)
        last = self._nudged_at.get(friend["id"])
        if not nudge or (last is not None and last != pass_at and pass_at - last < NUDGE_RETRY_S):
            return None
        self._nudged_at[friend["id"]] = pass_at
        status = await self._try_nudge(ip, port, invite, {**signed, "invite_id": nudge}, session)
        if status == 200:
            await self._db(self.store.nudged, friend["id"])
            self.resolve_soon()
            return "taken"
        if status is None or status in (403, 429) or status >= 500:
            return None
        return "answered"

    async def _try_nudge(self, ip: str, port: int, invite: str, handshake_data: dict,
                         session) -> Optional[int]:
        """POST the nudge → the inviter's status (it answers every nudge it
        takes alike — nothing of the peer's comes back in it), None when it
        did not answer."""
        import aiohttp
        url = f"https://{fmt_addr(ip, port)}/api/chat/invite-accepted"
        try:
            async with session.post(
                url, json=handshake_data,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                return resp.status
        except Exception as e:
            logger.debug(f"Nudge to {ip}:{port} for {invite} failed: {e}")
        return None

    def _store_incoming_grant(self, result: dict, own_pubkey: str) -> None:
        """Persist the issuer-signed grant from an accepted handshake — the
        contact-recovery document (re-presented when the issuer's device,
        and friends table, changes). On the store's thread."""
        grant = result.get("grant")
        if not grant:
            return
        from desktop.p2p import invite_tokens
        issuer_pubkey = result.get("public_key_hex", "")
        if not invite_tokens.verify_grant(grant, issuer_pubkey):
            logger.warning("Grant from %s failed verification — discarded",
                           result.get("username", "?"))
            return
        if grant.get("guest_pubkey", "").lower() != own_pubkey.lower():
            return
        friend = self.store.get_friend_by_public_key(issuer_pubkey)
        if friend:
            invite_tokens.store_grant(self.store.db_conn(), friend["id"], grant)
            logger.info("Friendship grant stored for %s", result.get("username", "?"))

    async def _reintroduce_after_identity_change(self) -> None:
        """Every friendship this node holds is built on its own key: the peer
        bound our invite code to the key it accepted, and re-checks it on
        every friend-gated request. Which of our keys each friend knows is
        recorded on the row (friends.bound_identity); the node-wide
        p2p.bound_identity is the identity the LAST run held.

        A ROTATION (Profile → nickname / password) left a notice signed by
        the old key in the identity archive: _deliver_rotation_notices hands
        it to each friend as they are reached, and the friendship migrates on
        their side without a new handshake. A REPLACEMENT — a new account
        through the wizard, or a rotation this node no longer holds the
        notice for — has nothing to prove continuity with: every row goes
        back to `pending:` and the resolver introduces us again with the
        invite token. Never the other way round — a re-introduction by token
        after a rotation makes the peer a SECOND friendship (the invite code
        carries a digest of the key), which is what the notice exists to
        avoid.
        """
        from desktop.node_identity import rotation_record
        ident = self._identity()
        if not ident:
            return
        ours = ident.public_key_hex
        bound = await self.setting("p2p.bound_identity")
        if bound == ours:
            return
        if bound is not None:
            if rotation_record(bound, ident.identity_dir) is not None:
                logger.info(
                    "Identity rotated (%s… → %s…): friends get the notice as "
                    "they are reached", bound[:16], ours[:16])
            else:
                reset = await self._db(self.store.unbind_friendships)
                logger.warning(
                    "Identity changed (%s… → %s…): %d friendship(s) queued for "
                    "re-introduction", bound[:16], ours[:16], reset)
        await self._write_settings({"p2p.bound_identity": ours})

    async def _deliver_rotation_notices(self) -> None:
        """Offer each friend not known to bind our current key the notice
        that retires the key they DO know, over the direct path; runs on
        the friend-resolver cadence, so an offline friend gets it when they
        are back rather than a second friendship. A row whose bound key is
        unknown (NULL, from before the column) is offered the chain oldest
        first — a notice for a key the friend never knew is a 404, which
        is an answer: past the last one, they know only the current key.
        A chain is walked one link per pass."""
        from desktop.node_identity import previous_identities, rotation_record
        ident = self._identity()
        if not ident:
            return
        retired = previous_identities(ident.identity_dir)
        if not retired:
            return
        ours = self.store.public_key_hex
        for friend in await self._db(self.store.friends_needing_notice):
            bound = friend["bound_identity"]
            if bound is not None:
                record = rotation_record(bound, ident.identity_dir)
                if record is None:
                    # A key of ours this node holds no notice for — the
                    # token road is all that is left for this friend.
                    await self._db(self.store.unbind_friend, friend["id"])
                    continue
                candidates = [record]
            else:
                candidates = [r for r in (rotation_record(e["public_key_hex"], ident.identity_dir)
                                          for e in retired) if r is not None]
            peers = await self.find_friend_peers(friend)
            if not peers:
                continue
            for record in candidates:
                status = await self._post_rotation_notice(friend, peers, record)
                if status == 200:
                    await self._db(self.store.set_bound_identity,
                                   friend["id"], record["new_public_key"])
                    logger.info("Rotation notice accepted by %s (%s… → %s…)",
                                friend.get("username") or friend["public_key_hex"][:16],
                                record["old_public_key"][:16],
                                record["new_public_key"][:16])
                    break
                if status != 404:
                    break                       # unreachable now — next pass
            else:
                # Every notice offered was for a key the friend does not
                # hold. From a walk of the whole chain that means the
                # friendship was made under the current key; from a single
                # recorded link it means the record is stale (the notice
                # landed and the row never heard) — walk the chain next pass.
                await self._db(self.store.set_bound_identity,
                               friend["id"], ours if bound is None else None)

    async def _post_rotation_notice(self, friend: dict, peers: list,
                                    record: dict) -> Optional[int]:
        """The HTTP status the first answering address gave, or None."""
        import aiohttp
        pubkey = friend["public_key_hex"]
        payload = {
            "rotation_message": record["message"],
            "old_signature": record["old_signature"],
            "new_signature": record["new_signature"],
        }
        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=pinned_ssl_context(pubkey)),
            timeout=aiohttp.ClientTimeout(total=10),
        ) as session:
            for ip, port in peers:
                url = f"https://{fmt_addr(ip, port)}/api/chat/key-rotation"
                try:
                    async with session.post(url, json=payload) as resp:
                        if resp.status == 200:
                            await self._note_peer_alive(friend["id"], pubkey, ip, port)
                        elif resp.status != 404:
                            logger.debug("Rotation notice refused (%d) by %s:%s",
                                         resp.status, ip, port)
                        return resp.status
                except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                    logger.debug("Rotation notice to %s:%s failed: %s", ip, port, e)
        return None

    async def _ensure_master_contact(self) -> None:
        """Seed the shipped master contact as a pending friend — the
        resolver does the rest (LAN/DHT lookup, token handshake, welcome
        pull). Removal is respected forever via the p2p.master_removed flag;
        a manual re-add clears it (backend)."""
        if not master_node.master_configured():
            return
        ident = self._identity()
        if not ident or master_node.is_master():
            return  # the master must not add itself
        if await self.setting("p2p.master_removed"):
            return
        if await self._db(self._seed_master_contact):
            logger.info("Master contact seeded (%s) — resolver will connect",
                        master_node.MASTER_INVITE_CODE)
            self.resolve_soon()

    def _seed_master_contact(self) -> bool:
        with self.store.db_conn().cursor() as cur:
            cur.execute(
                "SELECT 1 FROM friends WHERE public_key_hex IN (%s, %s)"
                " OR invite_code = %s",
                (master_node.MASTER_PUBKEY_HEX, f"pending:{master_node.MASTER_INVITE_CODE}",
                 master_node.MASTER_INVITE_CODE))
            if cur.fetchone():
                return False
            cur.execute("""
                INSERT INTO friends (username, public_key_hex, invite_code,
                                     display_name, source, join_token_id, key_fingerprint)
                VALUES (%s, %s, %s, %s, 'master', %s, %s)
                ON CONFLICT (public_key_hex) DO NOTHING
            """, (master_node.MASTER_USERNAME, f"pending:{master_node.MASTER_INVITE_CODE}",
                  master_node.MASTER_INVITE_CODE, master_node.MASTER_USERNAME,
                  master_node.MASTER_TOKEN_ID,
                  bytes.fromhex(chat_wire.key_fingerprint(master_node.MASTER_PUBKEY_HEX))))
            return cur.rowcount == 1

    # ------------------------------------------------------------------
    # Relays: relay #0 (the master), the peer relays, the frames
    # ------------------------------------------------------------------

    async def _master_wake_loop(self) -> None:
        """The master-relay subscription (relay #0): a friend-path wake
        stream with connect-time history catch-up. Peer relays run the same
        machinery with a voucher instead of the friendship. The waits below
        are this loop's existing ones (a contact not resolved yet)."""
        if not master_node.master_configured():
            return
        while self.running:
            try:
                ident = self._identity()
                if (not ident or master_node.is_master()
                        or await self.setting("p2p.master_removed")):
                    await asyncio.sleep(60)
                    continue
                if await self.master_contact():
                    break
            except Exception as e:
                logger.warning(f"master wake loop: {e}")
                await asyncio.sleep(30)
                continue
            # Not resolved yet: the master's first handshake is the event
            await self._master_accepted.wait()
            self._master_accepted.clear()
        if not self.running:
            return

        async def resolve():
            # The best candidate that has not failed the stream lately: an
            # address that answers under another key (a LAN host beaconing
            # the master's public invite) fails the pin and must not hold the
            # stream for good; with every one failed, the least lately.
            # Untargeted LAN peers are no candidates (other nodes, never
            # this one: find_friend_peers' master tier says why).
            master_row = await self.master_contact()
            if not master_row or await self.setting("p2p.master_removed"):
                return None
            peers = await self.find_friend_peers(master_row, untargeted=False)
            if not peers:
                return None
            now = time.time()
            fresh = [p for p in peers if self._master_failed.get(p, 0) <= now]
            return master_row, (fresh[0] if fresh else
                                min(peers, key=lambda p: self._master_failed.get(p, 0)))

        await self._relay_subscription_loop(master_node.MASTER_PUBKEY_HEX, resolve, is_master=True)

    async def _peer_relay_manager(self) -> None:
        """Keep K peer-relay subscriptions alive while this node cannot
        accept inbound connections (Phase D). Hot standby, not failover:
        every held relay announces our invite in parallel, so the death of
        one leaves senders a live candidate instead of a decaying ghost.
        When the node becomes reachable the subscriptions close — being
        findable directly is strictly better than through anyone."""
        await asyncio.sleep(90)   # let DHT bootstrap + reachability settle
        while self.running:
            try:
                for pk, task in list(self._peer_relay_subs.items()):
                    if task.done():
                        del self._peer_relay_subs[pk]
                ident = self._identity()
                need = (self.reachability in ("cgnat", "unreachable")
                        and ident and self.dht)
                if not need:
                    for task in self._peer_relay_subs.values():
                        task.cancel()
                    self._peer_relay_subs.clear()
                elif len(self._peer_relay_subs) < self.PEER_RELAY_K:
                    await self._recruit_peer_relays(ident)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug(f"peer relay manager: {e}")
            await asyncio.sleep(self.RELAY_SCAN_INTERVAL)

    async def _recruit_peer_relays(self, ident: ClientIdentity) -> None:
        """Find relay-capable nodes and subscribe until K are held.
        Previously used relays (p2p.relay_pubkeys) are tried first so our
        DHT announces stay on stable addresses across restarts."""
        import aiohttp
        known = await self.setting("p2p.relay_pubkeys") or []
        candidates = await self.dht.lookup_capability("relay")
        if not candidates:
            # Dead DHT: relay volunteers from the Worker directory (Ф16c) —
            # the subscription handshake (voucher, signatures) judges them
            # exactly like DHT-found relays.
            from desktop.p2p import node_hints
            candidates = [(h, p) for h, p, _ in
                          await asyncio.get_running_loop().run_in_executor(
                              None, node_hints.fetch, "relay")]
        if not candidates:
            return
        # Every client sees the SAME candidate order from the DHT — without
        # a shuffle they all pile onto the first relay in the list and the
        # cap does the load spreading the hard way (429s). Known relays
        # still win afterwards via the sort key.
        candidates = list(candidates)
        random.shuffle(candidates)
        own = ident.public_key_hex
        scored: list[tuple[int, str, int, str]] = []
        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(),
            timeout=aiohttp.ClientTimeout(total=5),
        ) as session:
            for ip, port in candidates[:10]:
                if len(scored) >= self.PEER_RELAY_K * 3:
                    break
                channel_ctx = pinned_ssl_context()
                try:
                    async with session.get(
                            f"https://{fmt_addr(ip, port)}/health",
                            ssl=channel_ctx) as r:
                        node_id = (await wire.read_json(r, PEER_HEALTH_CAP)).get("node_id", "")
                except Exception:
                    continue
                if not node_id or node_id.lower() != pinned_pubkey(channel_ctx):
                    continue    # /health lies about the channel owner
                if (node_id == own
                        or node_id == master_node.MASTER_PUBKEY_HEX
                        or node_id in self._peer_relay_subs
                        or self._relay_refused_until.get(node_id, 0) > time.time()):
                    continue
                scored.append((0 if node_id in known else 1, ip, port, node_id))
        # Dependency diversity (Ф14): the least similar candidates first —
        # a relay in the /24 of one we already hold is an eclipse waiting to
        # happen — then previously used relays, then the shuffled order.
        from desktop.p2p import similarity
        held = [{"ip": a[0], "pubkey": pk} for pk, a in self._peer_relay_addrs.items()
                if pk in self._peer_relay_subs]
        index = similarity.current()
        ordered = await asyncio.get_running_loop().run_in_executor(
            None, partial(similarity.relay_order,
                          [{"ip": ip, "port": port, "pubkey": node_id, "known": k == 0}
                           for k, ip, port, node_id in scored],
                          held, index.relay_pair if index else (lambda a, b: 0.0)))
        for cand in ordered:
            if len(self._peer_relay_subs) >= self.PEER_RELAY_K:
                break
            ip, port, node_id = cand["ip"], cand["port"], cand["pubkey"]
            addr = (ip, port)

            async def resolve(a=addr):
                return (None, a)

            task = asyncio.create_task(self._relay_subscription_loop(
                node_id, resolve, is_master=False))
            self._peer_relay_subs[node_id] = task
            self._peer_relay_addrs[node_id] = addr
            logger.info("Recruited peer relay %s… at %s:%s", node_id[:8], ip, port)
        if self._peer_relay_subs:
            await self._write_settings({"p2p.relay_pubkeys": list(self._peer_relay_subs.keys())})

    async def _relay_subscription_loop(self, relay_pubkey: str, resolve,
                                       is_master: bool) -> None:
        """Hold ONE outbound SSE connection to a relay's wake stream.
        Outbound works from behind any NAT — this is how an unreachable
        node learns 'you have mail' the moment someone writes, instead of
        at its next restart.

        `resolve` is an async callable returning (friend_row_or_None,
        (ip, port)) — the master path resolves through the friends table,
        peer paths through relay discovery. For a peer relay the friend row
        is None: the subscription carries a VOUCHER (query params), there
        is no history to sync with the relay itself, and unknown frames
        degrade to no-ops instead of a history pull."""
        import aiohttp
        backoff = 5.0
        relay_denied_until = 0.0
        while self.running:
            addr, connected = None, False
            try:
                ident = self._identity()
                if not ident:
                    await asyncio.sleep(60)
                    continue
                resolved = await resolve()
                if not resolved:
                    raise ConnectionError("no relay address")
                relay_row, (ip, port) = resolved
                addr = (ip, port)
                own = ident.public_key_hex
                ts = int(time.time())
                sig = ident.sign(
                    f"wake_subscribe:{ts}:{relay_pubkey}:{own}".encode("utf-8")).hex()
                url = (f"https://{fmt_addr(ip, port)}/api/relay/wake-stream"
                       f"?pubkey={own}&ts={ts}&sig={sig}"
                       f"&instance={self._wake_instance}")
                # A voucher rides along whenever this node needs relaying —
                # peer relays always (that is their whole point), the master
                # too while we are unreachable: it then announces us and
                # COUNTS US AGAINST ITS CAP like any other relay, which is
                # what makes the load spread instead of pooling on one
                # laptop. A friend's bare subscription (reachable node, the
                # phase-A wake channel) carries none and costs no cap slot.
                # A 429 (relay full) backs off to the bare channel for an
                # hour rather than losing the wake stream — the peer relays
                # this node also holds are the ones meant to carry it then.
                wants_relay = (not is_master
                               or self.reachability in ("cgnat", "unreachable"))
                headers = {}
                if wants_relay and time.time() >= relay_denied_until:
                    until = int(time.time()) + VOUCHER_TTL
                    v_sig = ident.sign(voucher_payload(own, relay_pubkey, until).encode("utf-8")).hex()
                    url += (f"&invite={quote(ident.invite_code)}"
                            f"&voucher_until={until}&voucher_sig={v_sig}")
                if wants_relay or is_master:
                    # A relay that is not our friend relays for a verified
                    # identity only, and the master vouchers for its mailbox
                    # an identity its gate verified (a key new to it since a
                    # rotation is checked once): our certificate and proof
                    # ride along.
                    bundle = ident.cert_bundle()
                    if bundle:
                        from desktop.p2p.peer_auth import HDR_CERT, encode_cert_bundle
                        headers[HDR_CERT] = encode_cert_bundle(bundle["cert"], bundle.get("proof"))
                # sock_read=45 → two missed 15s keepalives = dead stream
                async with aiohttp.ClientSession(
                    connector=aiohttp.TCPConnector(ssl=pinned_ssl_context(relay_pubkey)),
                    timeout=aiohttp.ClientTimeout(total=None, connect=10, sock_read=45),
                ) as session:
                    async with session.get(url, headers=headers) as resp:
                        if resp.status == 429 and is_master and "voucher_sig" in url:
                            relay_denied_until = time.time() + 3600
                            raise ConnectionError(
                                "master relay full — falling back to the bare wake channel")
                        if not is_master and (resp.status in (403, 429)
                                              or resp.headers.get(RELAY_HEADER) == "off"):
                            # A peer relay that is full or will not relay
                            # for us — a friend's that is not relaying
                            # answers with its chat stream, said so: let it
                            # go — the manager recruits another — instead of
                            # holding a slot on it.
                            self._relay_refused_until[relay_pubkey] = time.time() + 3600
                            logger.info("Peer relay %s… refused us (HTTP %s) — released",
                                        relay_pubkey[:8], resp.status)
                            return
                        if resp.status != 200:
                            raise ConnectionError(f"wake subscribe: HTTP {resp.status}")
                        connected = True
                        if is_master:
                            await self._keep_mail_voucher(
                                chat_wire.decode_mail_voucher(
                                    resp.headers.get(chat_wire.MAIL_VOUCHER_HEADER) or ""), own)
                        logger.info("Relay wake stream connected (%s:%s)%s", ip, port,
                                    "" if is_master else " [peer relay]")
                        if relay_row:
                            await self._note_peer_alive(relay_row.get("id"),
                                                        relay_row.get("public_key_hex"), ip, port)
                        backoff = 5.0
                        if is_master and relay_row:
                            await self._master_stream_connected(relay_row, ip, port)
                        last_bump = time.time()
                        # A peer relay's budget outlives its connections: a
                        # relay that reconnects keeps what it spent.
                        frames, refusals = self._relay_windows.setdefault(
                            relay_pubkey, (deque(), deque()))
                        async for raw in resp.content:
                            if not self.running:
                                break
                            # Lines already buffered come back without a
                            # suspension: the loop serves others between them.
                            await asyncio.sleep(0)
                            line = raw.decode("utf-8", "replace").strip()
                            outcome = None
                            if line.startswith("data:"):
                                outcome = await self._handle_relay_frame(
                                    line[5:], relay_row, relay_pubkey, ip, port)
                            # Every line counts, keepalives and blank ones too.
                            if not is_master and self._over_budget(frames, refusals, outcome):
                                self._relay_refused_until[relay_pubkey] = time.time() + 3600
                                self._relay_windows.pop(relay_pubkey, None)
                                logger.warning("Peer relay %s… sent more than a relay does — "
                                               "released", relay_pubkey[:8])
                                return
                            elif relay_row and time.time() - last_bump >= 120:
                                # Keepalives prove the relay is alive.
                                # Throttled to the cadence the relay uses
                                # for us — an UPDATE per 15s keepalive
                                # would NOTIFY the UI into a refetch loop.
                                last_bump = time.time()
                                await self._db(self.store.update_friend_last_seen,
                                               relay_row["public_key_hex"])
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("Relay wake stream (%s…): %s", relay_pubkey[:8], e)
                if is_master and addr is not None and not connected:
                    # Only a connect that failed (a pin, a refusal, no answer)
                    # sends this address behind the others; a stream that ran
                    # and dropped does not.
                    self._master_failed[addr] = time.time() + 600
            if not self.running:
                break
            try:
                cap = await self._reconnect_cap(is_master)
            except psycopg2.Error as e:
                # The database is away (a restart): the stream retries at
                # the ordinary cap rather than end with it
                logger.warning("Relay wake stream (%s…): the kept mail unread: %s", relay_pubkey[:8], e)
                cap = RECONNECT_CAP_S
            backoff = min(backoff, cap)
            if is_master:
                await self._rest_master(backoff + random.uniform(0, backoff / 4))
            else:
                await asyncio.sleep(backoff + random.uniform(0, backoff / 4))
            backoff = min(backoff * 2, cap)

    async def _reconnect_cap(self, is_master: bool) -> float:
        """The longest wait before a relay's stream is dialled again: shorter
        for the master's while a message waits kept for Support (L4)."""
        if is_master and any(m.get("parked_at") and m["public_key_hex"] == master_node.MASTER_PUBKEY_HEX
                             for m in await self._db(self.store.get_pending_messages)):
            return KEPT_RECONNECT_CAP_S
        return RECONNECT_CAP_S

    async def _rest_master(self, seconds: float) -> None:
        """The master stream's wait between dials — shortened to the kept
        cap the moment a message is kept for Support, not at the next dial."""
        deadline = time.monotonic() + seconds
        try:
            await asyncio.wait_for(self._master_redial.wait(), seconds)
        except asyncio.TimeoutError:
            return
        self._master_redial.clear()
        await asyncio.sleep(max(0.0, min(deadline - time.monotonic(), KEPT_RECONNECT_CAP_S)))

    async def _master_stream_connected(self, master: dict, ip: str, port: int) -> None:
        """The master is reachable again, at (ip, port) — the event for what
        is still on its way to it. The connect-time catch-up pull covers the
        wakes missed offline and marks delivered what its drain stored (the
        import marks our rows the master holds); then what is left of its
        outbox, the messages kept in its mailbox included, is pushed to it
        directly — a row the drain missed is delivered now, the master
        dedups by uuid. The runtime hears of it (the launcher's support
        diagnostics resume)."""
        self._master_failed.pop((ip, port), None)
        await self._sync_chat_history(master, [(ip, port)])
        await self._push_master_outbox(master, ip, port)
        if self._on_master_reachable:
            self._on_master_reachable((ip, port))

    @staticmethod
    def _over_budget(frames: deque, refusals: deque, outcome: Optional[str]) -> bool:
        """One more frame on a peer relay's stream — past what a relay
        sends (RELAY_FRAMES_PER_WINDOW), or past RELAY_REFUSALS_PER_WINDOW
        deliveries that did not open or came again, in RELAY_WINDOW_S."""
        now = time.monotonic()
        for window in (frames, refusals):
            while window and now - window[0] > RELAY_WINDOW_S:
                window.popleft()
        frames.append(now)
        if outcome in ("rejected", "duplicate"):
            refusals.append(now)
        return len(frames) > RELAY_FRAMES_PER_WINDOW or len(refusals) > RELAY_REFUSALS_PER_WINDOW

    async def _push_master_outbox(self, master: dict, ip: str, port: int) -> None:
        """What is still on its way to the master, the messages kept in its
        mailbox included, pushed directly to it at (ip, port) — it is up.
        Beside an immediate push to it a message may go twice (and, both
        failing, be deposited twice): the Worker and the master dedup by
        uuid."""
        leftover = await self._pending_for(master["id"], with_kept=True)
        if leftover:
            await self._push_pending_messages(master["id"], master["public_key_hex"],
                                              leftover, [(ip, port)])

    async def _handle_relay_frame(self, data: str, relay: Optional[dict],
                                  relay_pubkey: str, ip: str, port: int) -> Optional[str]:
        """One SSE frame from a relay.

        `deliver` carries a forwarded message and is answered with a signed
        receipt. Anything else — including a frame kind this build has never
        heard of — falls back to the history pull when the relay is a FRIEND
        (the master): the two are separately deployed binaries, so an
        unknown frame must degrade, not break. A peer relay is not a friend
        and holds no history — there the degradation is a no-op. → the
        delivery's outcome (stored | duplicate | rejected), None for any
        other frame."""
        try:
            frame = json.loads(data)
        except ValueError:
            frame = {}
        if not isinstance(frame, dict):
            frame = {}
        if frame.get("type") == diag_protocol.FRAME_TYPE:
            # A support warrant rides ONLY the master's own stream; a peer
            # relay carrying one is ignored. Handed over, not handled
            # inline — collecting a bundle must not stall the stream that
            # delivers chat (desktop/p2p/diag_worker.py, both runtimes since
            # 2026-11-21). A client built without a handler drops it.
            if relay_pubkey == master_node.MASTER_PUBKEY_HEX:
                if self._on_warrant:
                    self._on_warrant(frame.get("warrant") or {}, (ip, port))
                else:
                    logger.info("Support warrant on the master's stream dropped: "
                                "this node answers no warrant")
            return
        if frame.get("type") != "deliver":
            if relay:
                await self._sync_chat_history(relay, [(ip, port)])
                if relay_pubkey == master_node.MASTER_PUBKEY_HEX:
                    # The drain's ping (B-92): the pull shows what it stored —
                    # unless our cursor had passed its time; then the kept
                    # message goes to the master directly, deduped by uuid.
                    await self._push_master_outbox(relay, ip, port)
            return None

        envelope = frame.get("envelope")
        if not isinstance(envelope, dict):
            return "rejected"
        result = await self._db(
            partial(self.store.handle_incoming,
                    envelope.get("from_public_key", ""),
                    envelope.get("encrypted", ""),
                    envelope.get("timestamp", ""),
                    message_uuid=envelope.get("message_uuid") or None))
        if result is None:
            # Unknown sender, blocked, oversized or undecryptable — no
            # receipt, so the sender keeps it queued and nobody is misled.
            logger.warning("Relayed message from %s… rejected",
                           str(envelope.get("from_public_key", ""))[:8])
            return "rejected"

        # A duplicate is receipted too — the first receipt may be what was
        # lost; a relay replaying one is bounded by its budget (_over_budget).
        await self._send_delivery_receipt(envelope, relay_pubkey, ip, port)
        if result.get("duplicate"):
            return "duplicate"   # already had it; the receipt above is what was missing
        if self._on_incoming:
            self._incoming(result)
        return "stored"

    async def _send_delivery_receipt(self, envelope: dict, relay_pubkey: str,
                                     ip: str, port: int) -> None:
        """Sign the receipt the sender verifies. Signing only AFTER
        handle_incoming succeeded is what makes a fabricated envelope
        unprovable: it never decrypts, so it never gets signed."""
        import aiohttp
        ident = self._identity()
        if not ident:
            return
        message_uuid = envelope.get("message_uuid", "")
        ct_hash = hashlib.sha256(envelope.get("encrypted", "").encode("utf-8")).hexdigest()
        payload = {
            "public_key_hex": ident.public_key_hex,
            "message_uuid": message_uuid,
            "ciphertext_sha256": ct_hash,
            "signature": ident.sign(delivery_payload(message_uuid, ct_hash).encode("utf-8")).hex(),
        }
        try:
            async with aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=pinned_ssl_context(relay_pubkey)),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as session:
                async with session.post(f"https://{fmt_addr(ip, port)}/api/relay/ack",
                                        json=payload) as resp:
                    if resp.status != 200:
                        logger.debug("Receipt refused by relay: %s", resp.status)
        except Exception as e:
            # The message IS stored; only the sender's confirmation is lost,
            # and their retry will produce a fresh forward we dedup on.
            logger.debug(f"Delivery receipt to {ip}:{port} failed: {e}")

    # ------------------------------------------------------------------
    # Reachability
    # ------------------------------------------------------------------

    def note_inbound_reachable(self) -> None:
        """Passive proof from the peer surface (on the client's loop): a
        non-LAN peer just reached us — definitive 'reachable', overrides
        every heuristic."""
        if self.reachability == "reachable" or not self.running:
            return
        self.reachability = "reachable"
        asyncio.ensure_future(self._apply_reachability("reachable", "inbound request observed"))

    async def _apply_reachability(self, status: str, detail: str) -> None:
        self.reachability = status
        if self._on_reachability:
            await self._on_reachability(status, detail)
        await self._write_settings({
            "p2p.reachability": status,
            "p2p.reachability_detail": detail,
            "p2p.reachability_checked_at": datetime.now(timezone.utc).isoformat(),
        })
        # A verdict is news once: the same one again (a node that waits for
        # its identity re-checks every minute) is per-pass noise
        said = (status, detail)
        logger.log(logging.DEBUG if said == self._reachability_said else logging.INFO,
                   f"Reachability: {status} ({detail})")
        self._reachability_said = said

    async def _probe_reachability_via_master(self) -> _ProbeResult:
        """Definitive test — ask the master to connect back to our peer
        port (BT-tracker style). Only the master's globally routable
        addresses are asked: a call-back from a LAN or loopback vantage
        tests the wrong network — a Docker master on this very host dialled
        its own container loopback and answered `false` (2026-09-08) — so
        the master refuses such a verdict (`reachable: null`) and we never
        spend its per-key cooldown on one. The Worker's address hint stands
        in when the organic tiers know the master by LAN alone. A vantage at
        this node's own public address is behind the same router and is no
        vantage either: its call-back would land on the router's forward of
        that port, its own or anyone's, and `false` from it wrongly silenced
        a node whose mapping worked (PI-NODE-2B §8.3) — it is not asked, and
        one that finds as much itself says so (`same_router`)."""
        if not master_node.master_configured():
            return _ProbeResult(None, "no master configured")
        ident = self._identity()
        if not ident:
            return _ProbeResult(None, "no account yet", retry_soon=True)
        master = await self.master_contact()
        if not master:
            return _ProbeResult(None, "master contact not resolved yet", retry_soon=True)
        peers = await self.find_friend_peers(master)
        own = self._own_addresses()
        internet = [p for p in peers if is_internet_vantage(p[0])]
        if not internet:
            from desktop.p2p import master_hint
            hint = await asyncio.get_running_loop().run_in_executor(None, master_hint.fetch)
            if hint and is_internet_vantage(hint[0]):
                internet = [hint]
        vantages = [p for p in internet if canon_host(p[0]) not in own]
        if not vantages:
            if internet:
                return _ProbeResult(None, SAME_ROUTER_DETAIL)
            if not peers:
                return _ProbeResult(None, "master not resolved yet", retry_soon=True)
            known = ", ".join(fmt_addr(ip, p) for ip, p in peers)
            return _ProbeResult(None, f"no internet vantage: master known only at {known}",
                                retry_soon=True)
        port = self._announce_port()
        ts = int(time.time())
        sig = ident.sign(
            f"probe_request:{ts}:{master_node.MASTER_PUBKEY_HEX}:{port}".encode("utf-8")).hex()
        payload = {"public_key_hex": ident.public_key_hex, "port": port, "ts": ts, "signature": sig}
        import aiohttp
        same_router = False
        for ip, mport in vantages:
            try:
                async with aiohttp.ClientSession(
                    connector=aiohttp.TCPConnector(
                        ssl=pinned_ssl_context(master_node.MASTER_PUBKEY_HEX)),
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as session:
                    async with session.post(
                            f"https://{fmt_addr(ip, mport)}/api/relay/probe-connect",
                            json=payload) as resp:
                        if resp.status == 429:
                            return _ProbeResult(None, "master probe on cooldown", retry_soon=True)
                        if resp.status != 200:
                            continue
                        data = await wire.read_json(resp, PEER_SMALL_CAP)
                        await self._note_peer_alive(master.get("id"),
                                                    master_node.MASTER_PUBKEY_HEX, ip, mport)
                        seen = fmt_addr(data.get("observed_ip") or "unknown", port)
                        verdict = data.get("reachable")
                        if verdict is None:
                            # The master still saw a LAN/loopback source —
                            # a hairpin that keeps the private address — or
                            # found itself behind our router.
                            same_router |= data.get("same_router") is True
                            logger.debug(f"Probe via {fmt_addr(ip, mport)}: "
                                         f"no vantage (seen as {seen})")
                            continue
                        if verdict:
                            return _ProbeResult(True, f"master probe connected (seen as {seen})")
                        return _ProbeResult(
                            False, "master probe failed: "
                            f"{data.get('error') or 'no answer'} (seen as {seen})")
            except Exception as e:
                logger.debug(f"Probe via {fmt_addr(ip, mport)} failed: {e}")
        return _ProbeResult(None, SAME_ROUTER_DETAIL if same_router
                            else "master probe: no vantage answered")

    async def _reachability_loop(self) -> None:
        """Adaptive-cadence self-check (existing cadence, moved): retry every
        RETRY_UNKNOWN while the probe has nothing to ask yet — at startup the
        master contact is usually mid-resolution — then settle into the
        infrastructure cadence (6 h, like REANNOUNCE); a probe that WAS asked
        and returned no verdict is settled too. Passive inbound proof
        overrides in between via note_inbound_reachable."""
        RETRY_UNKNOWN = 60
        await self._rest(20)  # let UPnP/DHT settle after startup
        while self.running:
            retry_soon = True
            try:
                probe = await self._probe_reachability_via_master()
                heuristic = self._heuristic()
                status, detail = "unknown", probe.detail
                if probe.verdict is True:
                    status = "reachable"
                elif probe.verdict is False:
                    status, detail = heuristic or ("unreachable", detail)
                elif heuristic:
                    status, detail = heuristic
                elif self.reachability == "reachable":
                    status, detail = ("reachable", "inbound request observed")
                retry_soon = status == "unknown" and probe.retry_soon
                await self._apply_reachability(status, detail)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug(f"Reachability check error: {e}")
            await self._rest(RETRY_UNKNOWN if retry_soon else 6 * 3600)

    def _say_path(self, friend_id: int, state: str) -> None:
        """The delivery path's state toward a friend, at info once per change
        (L3, as the verdict's line is): which step a message waits on reads
        from the log, without a line per attempt."""
        if self._path_said.get(friend_id) != state:
            self._path_said[friend_id] = state
            logger.info("Delivery to friend %s: %s", friend_id, state)

    async def _rest(self, seconds: float) -> None:
        """The verdict's cadence — cut short by recheck_reachability."""
        try:
            await asyncio.wait_for(self._recheck.wait(), seconds)
        except asyncio.TimeoutError:
            pass
        self._recheck.clear()

    # ------------------------------------------------------------------
    # Outbox
    # ------------------------------------------------------------------

    async def _push_pending_messages(self, friend_id: int, pubkey: str,
                                     messages: list[dict], peers: list[tuple]):
        """Push undelivered messages to a friend's peer, then hand whatever
        the direct path could not deliver to a relay.

        `peers` may legitimately be empty: an unreachable friend suppresses
        its own DHT announces, so "no known address" is the normal state for
        exactly the pair the relay exists for.
        """
        undelivered: list[dict] = []
        import aiohttp
        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=pinned_ssl_context(pubkey)),
            timeout=aiohttp.ClientTimeout(total=10),
        ) as session:
            for idx, msg in enumerate(messages):
                msg_uuid = msg["message_uuid"]
                sent_at = _iso_utc(msg["timestamp"])
                try:
                    encrypted = self.store.encrypt_envelope(msg["content"], msg_uuid, sent_at, pubkey)
                except Exception as e:
                    logger.error(f"Encrypt failed for friend {friend_id}: {e}")
                    break

                payload = {
                    "from_public_key": self.store.public_key_hex,
                    "encrypted": encrypted,
                    "timestamp": sent_at,
                    "message_uuid": msg_uuid,
                }

                delivered = False
                for ip, port in peers:
                    url = f"https://{fmt_addr(ip, port)}/api/chat/message"
                    try:
                        async with session.post(url, json=payload) as resp:
                            if resp.status == 200:
                                await self._db(self.store.mark_delivered, msg["id"])
                                await self._note_peer_alive(friend_id, pubkey, ip, port)
                                self._say_path(friend_id, "delivered directly")
                                delivered = True
                                break
                            if resp.status == 403:
                                # This address is not our friend (a
                                # stranger on the LAN, or they haven't
                                # added us yet) — try the next candidate.
                                # Aborting here used to let any bystander
                                # answering first block delivery entirely.
                                logger.debug(f"Push rejected (403) by {ip}:{port}")
                                self._kept_refused(pubkey, msg)
                    except Exception:
                        pass
                if not delivered:
                    # Every address refused, timed out, or there were none
                    # at all. Later messages fare no better over the direct
                    # path — hand what's left to a relay.
                    logger.debug(f"No route to friend {friend_id} ({len(peers)} candidates tried)")
                    undelivered = messages[idx:]
                    break

        if undelivered:
            await self._relay_forward(friend_id, pubkey, undelivered)

    def _kept_refused(self, pubkey: str, msg: dict) -> None:
        """A message kept in the master's mailbox that the master refused on
        a direct push (it blocked or removed this node after the handshake):
        it stays "kept" (B92 Q7) — a support signal, said once."""
        uid = msg.get("message_uuid")
        if (msg.get("parked_at") and pubkey == master_node.MASTER_PUBKEY_HEX
                and uid not in self._refusals_said):
            self._refusals_said.add(uid)
            logger.warning("Message %s kept for Support was refused by the master (403)", uid)

    async def _mailbox_deposit(self, friend_id: int, messages: list[dict]) -> str:
        """The master's off-master fallback (Ф16): what the direct path could
        not deliver is parked, still E2E-encrypted, in the Worker mailbox the
        master drains when it is back. The Worker's acceptance is "kept",
        never "delivered" (B-92): the row is marked parked, and becomes
        delivered only on a proof — a direct push answered, the history pull
        that follows the master's ping on its drain or our next connect to
        it. A message already kept is not deposited again; a refusal (the
        mailbox full, a cap) leaves the rest waiting here. → the state said.

        The one place a drop is decided (SUPPORT-FIRST §1.3): only while the
        master has accepted the key this node signs with now. A `pending:`
        Support contact never gets here (the pass and the push skip it), and
        a node that rotated while the master was away waits for its notice —
        the drain keeps a message only from a key the master knows (§1.4)."""
        import aiohttp
        from desktop.p2p import mailbox_client
        ident = self._identity()
        if not ident:
            return "waiting for this node's identity"
        master_pubkey = master_node.MASTER_PUBKEY_HEX
        master = await self.master_contact()
        if master is None or not chat_wire.accepts_current_key(master.get("bound_identity"),
                                                              ident.public_key_hex):
            return "waiting for Support to accept this node's current key"
        outcome = "kept in the master's mailbox"
        # The voucher kept for our key, while the master it names is the one
        # pinned now (a release that changed the pin leaves an older one:
        # without it, a drop takes the small share rather than a refusal)
        voucher = await self._db(self.store.mail_voucher, ident.public_key_hex)
        if voucher and not chat_wire.mail_voucher_holds(voucher, ident.public_key_hex,
                                                        master_pubkey, _verify):
            voucher = None
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15),
                                         headers={"Accept-Encoding": "gzip"}) as session:
            for msg in messages:
                if not msg.get("message_uuid") or msg.get("parked_at"):
                    continue                       # pre-uuid rows stay on the direct path
                sent_at = _iso_utc(msg["timestamp"])
                try:
                    encrypted = self.store.encrypt_envelope(
                        msg["content"], msg["message_uuid"], sent_at, master_pubkey)
                except Exception as e:
                    logger.error(f"Encrypt for mailbox failed: {e}")
                    return "the message could not be sealed for the mailbox"
                result = await mailbox_client.deposit(
                    session, master_pubkey=master_pubkey,
                    sender_pubkey=self.store.public_key_hex, sign=ident.sign,
                    encrypted=encrypted, timestamp_iso=sent_at,
                    message_uuid=msg["message_uuid"], voucher=voucher,
                    worker_url=mailbox_client.WORKER_URL)
                if not result.kept:                # the outbox keeps the rest
                    if result.refusal == "invalid voucher":
                        # No voucher the master signed for this key is invalid:
                        # one this node kept is wrong, said loudly
                        logger.warning("The master's mailbox refused this node's voucher (%s…)",
                                       ident.public_key_hex[:16])
                    return (f"the master's mailbox refused it: {result.refusal}" if result.refusal
                            else "the master's mailbox is unreachable")
                await self._db(self.store.mark_parked, msg["id"])
                self._master_redial.set()
                logger.info("Message %s kept in the master mailbox", msg["message_uuid"][:8])
        return outcome

    async def _relay_forward(self, friend_id: int, friend_pubkey: str,
                             messages: list[dict]) -> None:
        """Deliver via a relay: the recipient holds an outbound stream to it,
        so a message reaches a node that accepts no inbound connections at
        all. The relay stores nothing and cannot fake success — delivery is
        the recipient's Ed25519 receipt, which we verify here before
        touching `delivered`."""
        if friend_pubkey == master_node.MASTER_PUBKEY_HEX:
            # The relay is the recipient, so no relay helps — the Worker
            # mailbox is the master's fallback (Ф16).
            self._say_path(friend_id, f"no direct route; {await self._mailbox_deposit(friend_id, messages)}")
            return

        relays = await self._resolve_relay_for(friend_pubkey)
        if not relays:
            self._say_path(friend_id, "no direct route; no relay available")
            return
        ident = self._identity()
        if not ident:
            return

        import aiohttp
        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(),
            timeout=aiohttp.ClientTimeout(total=FORWARD_TIMEOUT),
        ) as session:
            for relay_pubkey, relay_peers, relay_row in relays:
                if await self._forward_via_relay(session, relay_pubkey, relay_peers, relay_row,
                                                 friend_id, friend_pubkey, messages, ident):
                    self._say_path(friend_id, "no direct route; delivered through a relay")
                    return
                # 409 / no receipt from THIS relay — the friend may hold a
                # live stream to the next one (K relays announce it in
                # parallel), so a dead candidate is a reason to move on,
                # not to give up the batch.
        self._say_path(friend_id, f"no direct route; none of {len(relays)} relay(s) delivered it")

    async def _forward_via_relay(self, session, relay_pubkey: str, relay_peers: list,
                                 relay_row, friend_id: int, friend_pubkey: str,
                                 messages: list, ident: ClientIdentity) -> bool:
        """Push the batch through one relay. True when at least one message
        got a verified receipt (the batch is 'placed' — remaining failures
        retry next cycle through the same resolution)."""
        delivered_any = False
        relay_ctx = pinned_ssl_context(relay_pubkey)
        for msg in messages:
            msg_uuid = msg["message_uuid"]
            try:
                encrypted = self.store.encrypt_envelope(
                    msg["content"], msg_uuid, _iso_utc(msg["timestamp"]), friend_pubkey)
            except Exception as e:
                logger.error(f"Encrypt failed for friend {friend_id}: {e}")
                return delivered_any
            ct_hash = hashlib.sha256(encrypted.encode("utf-8")).hexdigest()
            ts = int(time.time())
            payload = {
                "public_key_hex": ident.public_key_hex,
                "to_public_key": friend_pubkey,
                "ts": ts,
                "signature": ident.sign(
                    f"relay_forward:{ts}:{relay_pubkey}:{msg_uuid}"
                    f":{ct_hash}".encode("utf-8")).hex(),
                "envelope": {
                    "from_public_key": ident.public_key_hex,
                    "encrypted": encrypted,
                    "timestamp": _iso_utc(msg["timestamp"]),
                    "message_uuid": msg_uuid,
                },
            }
            for ip, port in relay_peers:
                url = f"https://{fmt_addr(ip, port)}/api/relay/forward"
                try:
                    async with session.post(url, json=payload, ssl=relay_ctx) as resp:
                        if resp.status == 409:
                            # Recipient holds no stream to THIS relay — the
                            # rest of the batch fares no better here.
                            logger.debug("Relay %s:%s: recipient offline", ip, port)
                            return delivered_any
                        if resp.status != 200:
                            continue
                        data = await wire.read_json(resp, PEER_SMALL_CAP)
                        if relay_row:
                            await self._note_peer_alive(relay_row.get("id"), relay_pubkey, ip, port)
                    if not data.get("delivered"):
                        return delivered_any  # no receipt; retry next cycle
                    if verify_delivery_receipt(data.get("ack") or {}, friend_pubkey,
                                               msg_uuid, ct_hash):
                        await self._db(self.store.mark_delivered_by_uuid, msg_uuid)
                        delivered_any = True
                        logger.info("Relayed to %s… via %s:%s", friend_pubkey[:8], ip, port)
                    else:
                        logger.warning("Relay %s:%s returned an unverifiable "
                                       "delivery receipt — ignoring", ip, port)
                    break
                except Exception as e:
                    logger.debug(f"Relay forward via {ip}:{port}: {e}")
        return delivered_any

    async def _resolve_relay_for(self, friend_pubkey: str) -> list:
        """Every relay that can reach this friend, best first. Returns
        [(relay_pubkey, [(ip, port), ...], friend_row_or_None), ...].

        Phase D: the friend's own peer relays come from the SAME
        lookup_user candidates the direct path already fetched — a relay
        announces its client's invite, so a candidate whose /health
        identity is NOT the friend is a potential relay, proven by the
        VOUCHER: the friend's signature over {friend, relay, until}, which
        an impostor announcing someone else's invite cannot produce. The
        master closes the list as relay #0 — every node is its friend."""
        candidates: list = []
        friend = await self._db(self.store.get_friend_by_public_key, friend_pubkey)
        invite = (friend or {}).get("invite_code") or ""
        if friend and invite and self.dht:
            try:
                hits = await self.dht.lookup_user(invite)
            except Exception:
                hits = []
            import aiohttp
            async with aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(),
                timeout=aiohttp.ClientTimeout(total=5),
            ) as session:
                for ip, port in hits[:6]:
                    relay_pk = await self._voucher_checks_out(session, ip, port, invite, friend_pubkey)
                    if relay_pk:
                        candidates.append((relay_pk, [(ip, port)], None))

        if master_node.master_configured() and friend_pubkey != master_node.MASTER_PUBKEY_HEX:
            master = await self.master_contact()
            if master:
                peers = await self.find_friend_peers(master)
                if peers:
                    candidates.append((master_node.MASTER_PUBKEY_HEX, peers, master))
        return candidates

    async def _voucher_checks_out(self, session, ip: str, port: int,
                                  invite: str, friend_pubkey: str):
        """Full voucher verification against one relay candidate. Returns
        the relay's pubkey, or None when anything fails — a black-hole
        impostor dies here."""
        from desktop.node_identity import verify_signature
        channel_ctx = pinned_ssl_context()
        try:
            async with session.get(f"https://{fmt_addr(ip, port)}/health",
                                   ssl=channel_ctx) as r:
                node_id = (await wire.read_json(r, PEER_HEALTH_CAP)).get("node_id", "")
            if (not node_id
                    or node_id.lower() != pinned_pubkey(channel_ctx)
                    or node_id.lower() == friend_pubkey.lower()):
                return None      # impostor — or the friend itself, which
                                 # is direct path territory
            async with session.get(
                    f"https://{fmt_addr(ip, port)}/api/relay/voucher"
                    f"?invite={quote(invite)}", ssl=channel_ctx) as r:
                if r.status != 200:
                    return None
                v = await wire.read_json(r, PEER_SMALL_CAP)
            if (v.get("client_pubkey", "").lower() != friend_pubkey.lower()
                    or v.get("relay_pubkey", "").lower() != node_id.lower()
                    or int(v.get("until") or 0) <= int(time.time())):
                return None
            if not verify_signature(
                    voucher_payload(friend_pubkey, node_id, int(v["until"])).encode("utf-8"),
                    bytes.fromhex(v.get("signature") or ""), friend_pubkey):
                return None
            return node_id
        except Exception:
            return None

    def _deliver_now(self, friend_ids: set) -> None:
        """Push what is pending for these friends at once, on the stored
        message's own notice, beside the pass — which works through every
        pending friend in turn, an offline one's timeouts included. A friend
        a push is already under way for is pushed to again when it ends."""
        for fid in friend_ids:
            if fid in self._pushing:
                self._push_again.add(fid)
            else:
                self._pushing.add(fid)
                task = asyncio.ensure_future(self._push_to(fid))
                self._push_tasks.add(task)
                task.add_done_callback(self._push_tasks.discard)

    async def _pending_for(self, fid: int, with_kept: bool = False) -> list[dict]:
        """A friend's undelivered messages; those kept in the master's
        mailbox only `with_kept` — they wait for the master's stream, not for
        the pass or a push (_master_stream_connected)."""
        return [m for m in await self._db(self.store.get_pending_messages)
                if m["friend_id"] == fid and (with_kept or not m.get("parked_at"))]

    async def _push_to(self, fid: int) -> None:
        try:
            while self.running:
                self._push_again.discard(fid)
                pending = await self._pending_for(fid)
                if pending and pending[0]["public_key_hex"].startswith("pending:"):
                    self._say_path(fid, "waiting for the contact's first handshake")
                elif pending:
                    friend_info = {"id": fid, "invite_code": pending[0]["invite_code"],
                                   "public_key_hex": pending[0]["public_key_hex"],
                                   "username": ""}
                    peers = await self.find_friend_peers(friend_info)
                    await self._push_pending_messages(
                        fid, friend_info["public_key_hex"], pending, peers)
                if fid not in self._push_again:
                    break
        except Exception as e:
            # The pass delivers what this push did not.
            logger.warning(f"Immediate push to friend {fid} failed: {e}")
        finally:
            self._pushing.discard(fid)

    async def _pass_loop(self):
        """The pass over every friend with pending messages: push, then the
        first history pull of the session.

        It wakes on NOTIFY sautium_chat (chat_notes). The
        60 s timeout is EXISTING code, moved as it was (B91-DESIGN §2.6):
        what it retries without an event is a friend coming back online that
        this node cannot observe — they cannot reach us, so only our retry
        through their relays delivers. A friend with nothing pending costs
        nothing, and a message kept in the master's mailbox is not retried
        here: the master's stream connecting is its event. Replacing it by a per-friend backoff is a behaviour change
        of its own (BACKLOG B-98's follow-up).

        Hybrid (pull + push):
          1. Push undelivered messages (normal offline delivery).
          2. Pull history once per session (identity restoration, catching
             up); a friend that refuses with 403 has not added us — they
             pull when ready.
        """
        while self.running:
            try:
                await asyncio.wait_for(self._notify.wait(), timeout=60)
                self._notify.clear()
            except asyncio.TimeoutError:
                pass
            if not self.running:
                break
            try:
                await self._pass_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"chat pass failed: {e}")

    async def _pass_once(self) -> None:
        pending = await self._db(self.store.get_pending_messages)
        # Unique friends with undelivered messages — not one a push is
        # already under way for (_deliver_now): two at once deliver one
        # message twice.
        seen_friends: set[int] = set()
        for msg in pending:
            fid = msg["friend_id"]
            if fid in seen_friends or fid in self._pushing or msg.get("parked_at"):
                continue
            seen_friends.add(fid)
            pubkey = msg["public_key_hex"]
            if pubkey.startswith("pending:"):
                self._say_path(fid, "waiting for the contact's first handshake")
                continue
            friend_info = {"id": fid, "invite_code": msg["invite_code"],
                           "public_key_hex": pubkey, "username": ""}
            # No `continue` on an empty peer list: an unreachable friend
            # suppresses its own DHT announces, so "no address" is exactly
            # the case the relay fallback exists for — _push_pending_messages
            # degrades correctly, its direct loop just does not run.
            peers = await self.find_friend_peers(friend_info)

            still_pending = await self._pending_for(fid)
            if still_pending:
                await self._push_pending_messages(fid, pubkey, still_pending, peers)

            if peers and fid not in self._history_synced_friends:
                self._history_synced_friends.add(fid)
                await self._sync_chat_history(friend_info, peers)
                # Still undelivered — refresh the peer cache so the next
                # pass does a fresh LAN/DHT lookup.
                if await self._pending_for(fid):
                    await self.find_friend_peers(friend_info, refresh=True)
