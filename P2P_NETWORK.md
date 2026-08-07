# P2P Network — Sautium

Design notes for the P2P layer. Describes **why** things are the way they are —
implementation details live in the code (`desktop/p2p/`, `backend/dht_service.py`).

---

## Vision

Turn Sautium from a local player into a **serverless P2P network** where people
with large offline FLAC libraries can share metadata, audio embeddings and
features, find kindred listeners and talk to each other — with no central
server (public bootstrap resources only).

**Shared**: metadata, CLAP embeddings, audio features, bios/tags, chat (P4),
lyrics (P4+), audio files (P5, legal content only).

**Never shared**: local paths, player state, private notes, listening history
(unless the user opts in).

---

## Architecture

```
Sautium Node
├── Local layer: PostgreSQL+pgvector, FastAPI, CLAP/text embeddings, HQPlayer, Web UI
├── P2P layer:    Account identity (Argon2id+Ed25519) → aiohttp sync server
│                 libtorrent DHT (per-artist + per-user announces)
│                 E2E chat (NaCl Box) → NAT traversal (UPnP)
│                 Sync client (HTTP pull, layered LAN→DHT)
└── UI layer:     Connect/Disconnect, peers list, Friends/Chat, cross-library search
```

Node A (Kyiv) ↔ Node B (Berlin) ↔ Node C (Tokyo) — direct UDP/TCP connections
through NAT (UPnP + STUN-style hole punching), bootstrapped off the public
BitTorrent DHT.

---

## Technology Choices

| Component | Library | Why |
|-----------|---------|-----|
| DHT + file transfer | **libtorrent** (C++ with Python bindings) | Access to the public BT DHT, file exchange built in, pip-installable |
| NAT traversal | **miniupnpc** (UPnP) + STUN | UPnP for the router, STUN to learn the external IP |
| Transport | **HTTP + JSON + gzip** | The same protocol sync already speaks; no custom binary format |
| Identity | **cryptography** (Ed25519) + **argon2-cffi** | Standard, compact 32-byte keys; Argon2id for deterministic identity |
| Chat encryption | **PyNaCl** (NaCl Box) | Curve25519 + XSalsa20-Poly1305, simple API |
| TLS | Self-signed ECDSA P-256 | Node ID in the CN, HTTPS for every P2P connection |
| Async networking | **asyncio** + **aiohttp** | Already used across the project |

### Why libtorrent and NOT `kademlia` (pure Python) — a **critical decision**

> The `kademlia` library (bmuller) is **incompatible** with the BitTorrent DHT:
> - MsgPack serialization (BT DHT uses Bencode)
> - Different RPC operations (STORE/FIND_VALUE vs get_peers/announce_peer)
> - Cannot connect to `router.bittorrent.com`
> - Effectively creates a **separate private network** that has to be grown
>   from zero

libtorrent gives access to millions of existing nodes plus built-in file
exchange for the future Phase P5, and is pip-installable on Windows/Linux/Mac.

### "Serverless"

Free public resources (no server to rent):
- **DHT bootstrap**: `router.bittorrent.com:6881`, `dht.transmissionbt.com:6881`
- **STUN**: `stun.l.google.com:19302`, `stun.cloudflare.com:3478`
- **Relay fallback**: Oracle Cloud Free Tier, or relaying through other peers

---

## Content-Addressable IDs (Deterministic UUID v5)

For P2P exchange the same data must carry the same ID on every node. Namespace
`adc1ec0b-2c81-5e26-9938-a369c6f7a5e1` (in `backend/uuid_utils.py`).

| Entity | Formula |
|--------|---------|
| Artist | `uuid5(NS, "artist:{normalize(name)}")` |
| Album | `uuid5(NS, "album:{normalize(artist)}:{normalize(title)}")` |
| Track | `uuid5(NS, "song:{normalize(artist)}:{normalize(title)}")` |
| Genre | `uuid5(NS, "genre:{normalize(name)}")` |
| Tag | `uuid5(NS, "tag:{normalize(name)}")` |
| EmbeddingModel | `uuid5(NS, "embedding_model:{normalize(name)}")` |

Embeddings are identified by `(track_uuid, model_uuid)` — both deterministic.
The embedding PK itself can stay SERIAL (it is not shared; what travels is the
vector, bound to `track_uuid`).

---

## DHT Discovery Strategy

### Principle: announce **per artist**, not per node

The launcher does not announce itself as a single node. It announces **every
artist** it holds enrichment for (an embedding or audio_features for at least
one track):

```python
artist_infohash = SHA1("Sautium-artist:" + artist_uuid)
session.dht_announce(artist_infohash, port=sync_port, flags=0)
```

**Advantages:**
- Precise lookup: "who has Pink Floyd?" → a direct DHT lookup
- No broadcast/flood: only the artists actually wanted are searched for
- Natural scaling: more participants → more artists
- ~2550 announces every 15 min ≈ 3/sec — nothing for libtorrent

> Superseded in part: the tail is now capped and ranked (see the announce-storm
> lesson in Open Questions #4) and reflects "I hold analysis", not file
> ownership.

### Layered sync flow (P3)

```
Sync trigger
     │
     ▼
LAN Discovery (UDP broadcast on 19002 + localhost Docker probe)
     │
     ├── Peers found → direct HTTP (fast, reliable)
     └── None → DHT lookup for one artist → find a seed
                        │
                        ▼
                  Inventory call to the seed about ALL unenriched artists
                        │
                        ▼
                  Batch pull (gzip JSON) → import
```

**Smart seed reuse**: 1 DHT lookup + 1 inventory call instead of N lookups for
N artists.

### Account Identity + Chat Discovery

Phase P4 added **per-user announces** on top of the per-artist ones:
```python
user_infohash = SHA1("Sautium-user:" + invite_code)
session.dht_announce(user_infohash, port=sync_port, flags=0)
```

A friend holding the `invite_code` does a DHT lookup → gets IP:port →
establishes an HTTPS connection for handshake/chat. Offline queue: undelivered
messages are kept and retried every minute through a fresh DHT lookup.

---

## Account System (Phase P4)

Deterministic identity: the same username+password on any device yields the
same identity (same keys, same invite code).

```
username + password → Argon2id KDF (256MB, 4 iter) → 32-byte seed → Ed25519 keypair
```

**Invite Code**: `username#XXXX-XXXX-XXXX` where XXXX is `SHA-256(public_key)[:6]`
in hex. Human-readable, and the hash part defends against forgery (a different
"bob42" gets a different hash).

**Key rotation** (password change):
1. New keypair (from the new password)
2. A `{new_public_key}` message is signed with the **old** private key
3. Broadcast to friends over `/api/chat/key-rotation`
4. The friend verifies the signature with the old key → updates the record

### Email verification (optional)

Cloudflare Worker (`sautium-verify.sautium.workers.dev`) + Resend:
- Signed requests (Ed25519) — a modified client cannot forge one
- KV store maps `invite_code → verified_email`
- Invite emails show a ✅ Verified / ⚠️ Unverified badge
- Auto-reciprocate when both accounts are verified through the Worker KV

### Mutual invite exchange (anti-impersonation)

A leaked invite code does not create a friendship on its own — **both** sides
must add each other's invite code. The handshake only completes on
seen_by_both. Without it, one leaked code would produce a fake friendship.

### Invite tokens (auto-confirm) — SHIPPED 2026-07-31

The share string gains a third segment: `username#XXXX-XXXX-XXXX#<token-uuid>`.
Any node mints tokens (`invite_tokens`) with its own parameters: rights
(`p2p_right`: `can_message`, `can_search`), use limit, expiry, revocation,
welcome message, `require_birth_cert`. Presenting a live token in the handshake
**bypasses mutual-add**: the token AUTHORIZES the friendship, while
`verify_invite_code` (code↔key) still IDENTIFIES the guest — so a stolen share
string impersonates nobody. Because this path bypasses consent, the guest must
sign `token_handshake:{ts}:{token}:{issuer_invite}` (±60 s window).

Every accept mints a **grant** signed by the issuer's key:
`sautium-grant:v1:{token}:{rights}:{guest_pubkey}:{issued_at}:{expires}`.
The guest stores it (`friend_grants`) and presents it when the issuer has moved
to a new device: the friends table there is empty, but the deterministic
identity still verifies its own old signature — the grant stands in for the
lost DB row. Rights are snapshotted at accept time (`friend_rights`): editing
or revoking a token only affects later admissions.

### Master node + relay protocol — SHIPPED 2026-07-31

The master node (the maintainer's Docker instance) is pinned by constants in
`master_node.py` (mirrored `desktop/p2p/` ↔ `backend/`): invite code, FULL
pubkey (the 48-bit fingerprint in the code is guessable on its own) and the
UUID of a public support token with `require_birth_cert=TRUE` — an attacker
must pass the Worker's birth-certificate rate limit to mass-produce
identities. `_ensure_master_contact` seeds it as a pending friend when P2P
starts; the existing resolver (LAN → cache → DHT `lookup_user`) performs the
token handshake, stores the grant and pulls the welcome message through the
ordinary history sync. Deleting the contact sets `p2p.master_removed` — the
auto-add never resurrects it (re-adding the code by hand clears the flag).

`/api/relay/*` is a deliberately **proxy-agnostic** contract (both surfaces):
- `GET /api/relay/wake-stream?pubkey&ts&sig` — the "you have mail" SSE channel.
  A CGNAT node holds ONE outbound connection (outbound works from behind any
  NAT) and pulls history on every ping: the maintainer's reply lands in ~0.2 s
  instead of "at the next restart". The subscription registry doubles as live
  presence.
- `POST /api/relay/probe-connect` — the relay knocks BACK on the request's
  source address (never on an IP from the body — that would be a
  reflector/port scanner) and checks `node_id` in `/health`. This is how
  torrent trackers derive the connectable flag.

An unreachable node **suppresses its own DHT announces**
(`set_announces_enabled`): a dead address in the DHT pollutes everyone's
lookups. Lookups and the LAN beacon keep working.

---

## Data Format for P2P Exchange

### Catalog entry (per track)
```json
{
  "track_uuid": "550e8400-...",
  "title": "Comfortably Numb",
  "artist_uuid": "6ba7b810-...",
  "artist_name": "Pink Floyd",
  "album_uuid": "7ca7b810-...",
  "album_title": "The Wall",
  "year": 1979,
  "genres": [{"uuid": "...", "name": "Progressive Rock"}],
  "duration_seconds": 382,
  "available_formats": [
    {"format": "FLAC", "sample_rate": 96000, "bit_depth": 24, "lossless": true}
  ]
}
```

### Embedding exchange (on demand)
```json
{
  "track_uuid": "...",
  "model_uuid": "...",
  "model_name": "laion/clap-htsat-unfused",
  "vector": [0.123, -0.456, ...]
}
```

### Bulk protocol
```
Phase 1: Catalog sync
  A → B: artist UUIDs set (compact)
  B → A: overlap report + unique artists (gzip JSON)

Phase 2 & 3: Embeddings + features (lazy, on demand, gzip)
```

**Compression**: 30k tracks of metadata ≈ 15MB JSON → ~3MB gzip. Embeddings
(512 floats × 30k) ≈ 60MB → ~25MB gzip.

---

## Security Considerations

### Phase 1–3 (MVP + sync)
- Ed25519 identity (portable, deterministic)
- Connect/Disconnect kill switch (the user stays in full control)
- Metadata only (never file paths)
- Rate limiting on inbound peer requests
- Self-signed ECDSA P-256 TLS for all P2P traffic

### Phase P4 (Chat)
- E2E encryption with NaCl Box — passwords/keys never cross the network
- Mutual invite exchange — a leaked invite code grants no friendship
- Email verification is optional — the Worker acts as a CA, not as a relay
- Friend blocklist — blocked friends cannot send messages

### Future
- Selective sharing (choose which artists/albums are visible)
- Bandwidth limiting
- IP reputation (auto-ban flood/spam)

---

## Design Decisions (lessons learned)

| Decision | Rationale |
|----------|-----------|
| **libtorrent over pure-python kademlia** | kademlia is incompatible with the BT DHT; it would mean growing a private network from zero |
| **HTTP+JSON+gzip over custom binary** | Same protocol as sync; debuggable with curl |
| **Per-artist DHT announces** | Precise lookup without broadcast, natural scaling |
| **Deterministic identity (Argon2id)** | Same username+password = the same node on any device |
| **Mutual invite exchange** | A leaked invite code grants no friendship — both sides must confirm |
| **Email as convenience, not trust root** | The Worker delivers and flags a verified badge, but the mutual exchange stays P2P |
| **Smart seed reuse** | 1 DHT lookup + 1 inventory call instead of N lookups for N artists |
| **Random P2P port 20000–29999** | Avoids collisions between instances on one machine; persisted in config |
| **Event-driven chat delivery (SSE + direct HTTP)** | Polling cost ~8s of latency; SSE + direct push is instant |
| **Persistent DB connections in long-lived services** | ChatService with a per-call connection cost 2s per message |
| **alert_mask += dht_operation_notification** | Without it `dht_get_peers_alert` is silently never emitted (libtorrent gotcha) |
| **libtorrent 2.1+ `peers()` compat** | Returns `(ip, port)` tuples instead of objects — handle both |
| **Idempotent enrichment** | Every enrichment task must be safe to re-run — a correctness property, not an optimization |

---

## Open Questions

1. **Embedding quantization**: is quantizing the 512 floats for transfer
   (float16, int8) worth it? Bandwidth saved vs precision lost.
2. **Conflict resolution**: when two peers hold different Last.fm tags for the
   same artist — who is "right"?
3. **PyInstaller + libtorrent**: does bundling the C++ extension (.pyd) into
   the .exe work well? Needs testing.
4. ~~**DHT announce rate limits**~~ — ANSWERED (announce storm, 2026-07):
   25 announces/s produced timeout bursts near the END of the paced window and
   for ~a minute past it. The current regime is 5 announces + a 1 s pause; a
   ~300-key tail takes ~60 s out of the 15-minute cycle. Announce-on-behalf
   (phase D) adds at most the client cap — tens of keys — which fits that
   budget.
5. **Slice replication and freshness**: a replica holds the blob of the dump
   version it was signed under. Once a dump node updates, two generations of
   one name's blob coexist in the network. Today whoever answers first wins
   (replicas are asked first), and it only self-corrects when the name is
   re-opened. Whether selection should consider `dump_version` is open.

---

## Future Phases

- **P3b: Cross-library search** — "who on the network has something like this
  track?" through embedding similarity. Distributed query fanned out to the
  peers found, 5s timeout.
- **P4b: Music recommendations** — broadcast "I recommend this album" to
  friends, shared playlists (track metadata lists, not files).
- **P5: File sharing** — libtorrent BitTorrent for legal content (indie
  artists, Creative Commons, self-released). Opt-in, with a licence tag system
  (CC-BY, CC-SA, Public Domain, Self-Released).

Deferred out of the shipped phases (deliberate pauses, not roadmap):
uptime-ratio and passive uplink measurement as relay-selection criteria (no
data source); the taste profile for finding nodes with similar taste (carry
turned out not to need it — the phantom catalogue materializes it); the
phase-A wake channel that grows linearly with the network ("alive only while a
support thread is"); the mbid bridge for the "recording matches, uuid does not"
class (price: a v3 seal format and re-signing the corpus).

---
## Shipped network phases (2026-08)

Chronology and reasoning below. Every phase extends THE SAME `/api/relay/*`
and sync contracts — no new transport was introduced.

### Relay forwarding — SHIPPED 2026-08-02

A pair where BOTH sides sit behind CGNAT had no channel at all: the message
existed only at the sender, there is no reachable stand-in (in torrents that
case is masked by replication — here there is nothing to mask it with), so the
retry looped forever and neither side could see it.

Closed by **forwarding**, not storage. The relay is a pure forwarder:

```
A --POST /api/relay/forward--> R
                               R --SSE {type:"deliver", envelope}--> B
                               R <--POST /api/relay/ack------------- B
A <--{delivered, ack}--------- R      A verifies B's signature
```

Both actions are outbound, so they work from behind any NAT; the relay is
needed only by the side that RECEIVES — the sender needs nobody. The envelope
is byte-for-byte the body of `/api/chat/message`, so at the recipient it is a
literal replay through `handle_incoming` (same decryption, same `message_uuid`
dedup).

**The receipt is end-to-end.** The ack is the recipient's signature over
`sautium-delivery:v1:{message_uuid}:{sha256(ciphertext)}`. The relay can
neither lie about delivery (it has no private key) nor slip in its own
envelope (a forgery will not decrypt, so no signature appears). `delivered`
flips only on a verified signature.

**The relay stores nothing** — not a row, not a table. Its only state: an
in-memory future while it waits for the receipt (`FORWARD_ACK_TIMEOUT = 10 s`)
and a queue of envelopes per subscription (`FORWARD_QUEUE_MAX = 100`). No TTL,
no prune, no disk quotas, no reconciliation. Recipient not connected → 409
immediately rather than a timeout: the sender should know at once that waiting
is pointless.

Mirrors: `backend/routers/peer_chat.py` and `desktop/p2p/sync_server.py` — any
reachable launcher becomes a relay with no protocol change.

**Deposit/collect (a mailbox) — CANCELLED.** The reasons, recorded 2026-08-02:
(1) the master is the maintainer's laptop, not infrastructure, and a network
whose guaranteed delivery rests on one machine is client-server with a P2P
façade; (2) to DELIVER a message you do not need to store it — the mailbox
solved two different problems (transport and persistence) and dragged in all
its machinery for the second; (3) in a multi-relay world it creates rendezvous
("where is my mail"), the "relay B offline while B is online" case, and false
expectations of delivery.

If store-and-forward is ever needed, build it **not** as the recipient's
mailbox but as the sender's **outbound proxy** ("my relay keeps pushing my
outbox for me"): a node always knows its own relay, whereas someone else's
would have to be found — so rendezvous disappears. Even that is unnecessary
today: the sender's queue IS an offline buffer (`delivered=FALSE` + endless
retry), and `import_history` fills in what was missed on both sides at first
contact. The only real hole is "A and B are never online at the same time".

### Carry — push-seeding audio analysis — SHIPPED 2026-08-02, v4 2026-08-07

Sync is **pull-only**. So a node that accepts no inbound connections takes from
the network and gives it nothing: there is nobody to connect to it. Its own
analysis of the rare tail — precisely what nobody else holds — dies with it.

What is fixed is the direction, not the trust. A signed record is
self-authenticating, so anyone may serve it; the carrier still runs it through
the **ordinary import gate** (`sync_client.import_pushed`), where unsigned
material is dropped — there is no "friends only" condition and none is needed.

**Only audio analysis travels.** v1 carried the artist layer and was replaced
within a day: every bio/tag/similar is a Last.fm fetch by name, reproducible on
any node for two API calls, and importing similars minted stub artists straight
into the carrier's phantom-canon feed (unsolicited canonization plus slice
fetches). Audio analysis is the opposite: GPU work with no external source.
`track_stats` and `genre_descriptions` are Last.fm too → they do not travel.

**What is offered — the full canonized snapshot.** `get_pushable_tracks`:
sealed segments **AND** a first-hand source (`analysis_sources NOT imported`)
**AND** a canon primary artist (non-`phantom` MB anchor) **AND** a sealed
recording binding (`track_mbids`) **AND** a sealed tracklist row under an
RG-anchored album. Measured: 28023 of 38202 first-hand sealed tracks (73%) —
the rest are compilations and residue whose canon has not matured; they ride a
later push. v2 carried only track + analysis and the carried rows were
orphans: without an album the radio pool rejected them on its
`(media_files OR album_tracks)` condition, Now Playing had no album, no cover
and no `length_ms` for stream matching, and a future owner's canonization got
neither structure nor durations.

Seals that support that gate: `album` (`rg_mbid:title:year:confidence`;
**cover_url deliberately outside the seal** — a CAA URL is a deterministic
derivative of rg_mbid, so the importer may fill a blank one with
`coverartarchive.org/release-group/{rg}/front-500`, exactly what the phantom
minter writes), `album_track` (album:disc:position:**length_ms**:recording),
`track_mbid` (recording:confidence).

The owned layer lives in `album_variants`, so tracklist rows for owned tracks
did not exist — `sign_audio` **materializes** them from file tags before every
signing pass (`_materialize_owned_tracklists`: disc, position, duration,
recording — this node's first-hand observation, incremental, an MB-minted row
keeps its slot). Only rows of owned analysis-source tracks are signed; the ~3M
MB-minted phantom tracklist rows stay unsigned (attesting them would mean
signing a copy of MusicBrainz).

A trap caught by the re-serve check: `created_at` IS the seal's fetched_at
slot, so importers PRESERVE the author's `created_at`; a local `DEFAULT now()`
would break verification for every subsequent recipient.

**v4 (2026-08-07): the offer speaks recording MBIDs, the transfer speaks track
UUIDs, and the carrier answers ONLY with uuids it already has.** The carrier's
phantom catalogue (~20× owned, grown from its own discovery graph) IS the taste
profile — no separate mechanism needed: a recording it has no row for is a
recording it never cared about, and nothing is minted to hold it. The double
key buys both guarantees at once: an MBID exists only for canonized material
(canonicity), and a uuid match means the author's seal (which binds track_uuid)
survives re-serve (integrity). Both mismatch classes die quietly on the right
side: "same name, different recording" is cut by the MBID join;
"same recording, different name" never leaves the pusher, which holds no such
uuid.

The v3 structural transport (albums/tracks/album_tracks/artist_mbids
categories, the identity-recompute functions, their importers) was DELETED —
git history holds it; the carrier's skeleton already has structure, covers and
canonized artists from the MB mint, and they are more canonical than the
pusher's. The album/album_track SEALS remain — the pusher's full-snapshot gate
reads them; the artist_mbids seal layer (a v2 legacy: columns, trigger, kind,
2596 signatures) went with its transport — the canon gate reads confidence, not
a signature. Analysis lands in a ready socket — the same "best case" measured
earlier (15/15 radio-eligible with zero canon work). The pusher serves the
wanted uuids through the ordinary pull handlers, which naturally return only
what it holds. An empty carrier (a fresh node before discovery) takes nothing —
honestly so; its profile will grow. A lite node without phantom_minting is not
a carrier. The master with its dump (2.9M phantoms) is a de-facto broad
carrier, the network's safety anchor.

**The "don't send what they already have" filter** is the offer/answer round:
16 bytes per recording to ask against ~46 KB to send blind. **Budget** is
`sync.carry_limit` (2000 tracks ≈ 92 MB by default), counted honestly — only
tracks with imported analysis and no file. It is now the second belt; the first
is the phantom intersection itself. **Provenance is never erased**:
`_drop_protected` keeps segments off tracks that already have a grid or a
first-hand source (verified by pushing a node's own rows back to it — 0
imported). **The announce tail means "I hold analysis"** (`ANNOUNCE_TAIL_SQL`):
the announce mirrors what a node can actually serve rather than file ownership
— it covers carried rows (the author suppresses its own announces, so the
carrier is its only DHT address), stream-derived analysis, and first-hand
analysis orphaned by a prune; a file with no analysis advertises nothing a peer
could take. **Carried rows cannot be deleted by accident**: both phantom-track
deleters (`_reconcile_phantoms`, `prune_missing_files`) unconditionally spare a
track that carries embeddings.

A side effect accepted deliberately: the carrier's background Last.fm
enrichment will fetch bios/tags for carried artists on its own (it gates on
`track_artists`, not `media_files`; similars are owned-gated, so there is no
blowup). This is "a relay accumulates data it can use itself": a carried artist
becomes visible in the carrier's search, and carried phantom tracks with
segments are material for its discovery.

Mirrors: `desktop/p2p/sync_server.py` and `backend/routers/sync.py`. Docker
mounts `./desktop:/app/desktop:ro` and uses **the same**
`sync_client.import_pushed` rather than a copy of its own — a second copy of a
signature-verification gate is the copy that eventually drifts open.

### D: Peer-relays — SHIPPED 2026-08-06

The master stopped being special: any reachable node is a relay now, and
delivery no longer rests on one laptop. The forwarding protocol did not change
— the roles around it did.

**Trust is voucher-only** (no new tables, no new rights). A CGNAT client
subscribing to a stranger's wake stream presents a **voucher** — its own
signature over `sautium-relay-voucher:v1:{client}:{relay}:{until}`. One
signature does two jobs: it authorizes the subscription (in place of the
friendship that does not exist with a stranger) and it is the proof the relay
hands to senders at `GET /api/relay/voucher` — a black-hole impostor announcing
someone else's invite has nothing to answer with. The relay_pubkey inside the
payload means a voucher issued to one relay cannot be presented by another;
`verify_invite_code` binds the invite to the client's key; `until` (24 h,
re-issued on every reconnect) bounds the authority's lifetime. The voucher
registry is in-memory: a relay restart simply has clients re-issue.

**Announce-on-behalf.** The BT DHT does not verify infohash ownership — that is
the feature: the relay announces `Sautium-user:{invite}` for every registered
client (`_client_invites` in both DHT services, on the same paced cycle) and
withdraws the announce the moment the SSE closes — presence in the DHT becomes
real rather than a 15–30-minute ghost. The sender finds the client through the
UNCHANGED `lookup_user`: `_lookup` has always merged every announcer of one
key, so K relays come back as N candidates for free.

**The sender** widens exactly the seam the design reserved:
`_resolve_relay_for` now returns ALL of a friend's relays — lookup_user
candidates whose `/health` node_id is not the friend go through full voucher
verification (invite↔key binding, the friend's signature, `until`,
relay_pubkey == node_id) before a single byte is forwarded; the master closes
the list as relay #0. A 409 from one relay is a reason to move to the next, not
to give up (the friend holds live streams to K relays in parallel).

**The client** holds K=2 peer relays on top of the master — hot standby: one
relay's death leaves senders a live candidate instead of a decaying announce.
`_master_wake_loop` shrank to a descriptor over the shared
`_relay_subscription_loop` (master = friend path + history catch-up; peer =
voucher in the query, no history, unknown frame = no-op). Previously used
relays (`p2p.relay_pubkeys`) are tried first, so announces stay stable across
restarts. Once the node becomes reachable it closes the subscriptions: being
found directly beats being found through anyone.

**The role turns itself on**: reachable + `p2p.relay_enabled` (default on — a
relay layer nobody opts into never cold-starts) → announce
`Sautium-cap:relay`; losing reachability withdraws the announce and closes
foreign subscriptions (clients re-register elsewhere — graceful degradation
instead of global reputation). There is no hardware gate: relaying is bytes,
not ML.

**The master carries under the same cap** (fixed 2026-08-06 after the
load-distribution objection): an unreachable node sends its voucher on the
master path too, so the master registers it, announces its invite and COUNTS IT
AGAINST ITS OWN CAP like any relay — surplus clientele spreads across peer
relays instead of pooling on one laptop. A 429 from a full master does not kill
the wake channel: the client falls back to the bare friend subscription for an
hour (support chat stays alive; peer relays do the relaying). A friend's
subscription WITHOUT a voucher is not a "friendly relay" but the phase-A wake
channel (pings to "pull the history" from a passive Docker master): it takes no
cap slot and produces no announce. Recruit candidates are shuffled — otherwise
every client piles onto the first relay in the DHT list and the cap distributes
them the expensive way (through 429s). A known phase-A scaling debt, left alone
deliberately: the permanent wake SSE from every node to the master for support
chat — one day it becomes "alive only while a thread is".

**Adaptive cap**: a base of 20 foreign clients; a full relay withdraws its cap
announce (newcomers stop knocking in vain) and listens for others — if no other
relay is visible in the DHT it raises the cap by 20 (up to 100) instead of
letting the network starve; with room again in a relay-rich network the cap
decays back to base. The signal is binary and the reaction one-sided on the
re-announce cadence — no oscillation. Existing clients are never shed by a cap
change.

Live test on 8801: no voucher → 403; voucher registration → 200 plus the
invite announced in the DHT; GET /voucher verifies against the client's key; a
stranger's forward → deliver frame → ack → delivered:true with a verified
receipt; a forward to an UNregistered stranger → 403. (The test caught a gate
left over from phase A: ack demanded friendship — delivered:true was
unreachable for exactly the clients the phase exists for.)

Deferred out of D: uptime-ratio and passive uplink measurement as
relay-selection criteria (no data source; a refinement of selection, not of the
mechanism).

### E: MB slices — replication instead of a proxy — SHIPPED 2026-08-07

**The old E (a relay proxy) was cancelled.** It treated a disease that does not
exist: slices are pulled by the requester over an OUTBOUND request, and
outbound works from behind any NAT — a proxy would only help a dump node behind
CGNAT (a rarity, not worth a second echelon) and would add a hop to the most
expensive path in the system. The real problem is different: **dump nodes are
few, and they pay the entry bill of every new node**. Measured on a live dump
node: rare artists ~30 ms / 14 KB, mid-tier ~80 ms / 34 KB, and prolific ones
(Bach, Dylan, Miles) **~8 s and ~9 MB each**. The tail is free, the stars are
brutal, and every new node asks for the stars.

**Signatures became per-artist** — the change that unlocked everything else.
v1 signed the batch response, welding the signature to one transport exchange:
a replica could not re-serve a single name without replaying the whole original
batch byte-for-byte. The signing grain now matches the data grain
(`pending_slice_names`, `mb_slice_fetches` and the closed-world rule are all
per name already), so **the original dump node's signature travels with each
name through any number of hops**, verified independently by each. `name_key`
and `dump_version` live inside the signed bytes: one name's blob cannot be
passed off as another's, nor an old dump version as a fresh one. Rows inside a
blob are sorted by their JSON form — Postgres row order must not leak into a
signature.

**Blobs are stored verbatim (gzip)** — which removes the most brittle part of
the job: re-serving hands back THE SAME bytes, so no re-serialization contract
with the database exists at all. The single `mb_slice_blobs` table plays three
roles: a dump node's hot cache (Bach is computed once per dump version —
**26.6 s → 103 ms, 257×**), a replica's re-serve inventory, and the wire
payload itself. Replicas do not keep blobs over 2 MB: the expensive names are
already served from dump-node caches for pennies, while the body of the
distribution (tens of KB) spreads — and the most popular names replicate
fastest.

**Who answers.** `/health` reports `mb_slices` (inventory size);
`_find_dump_peers` puts **replicas BEFORE dump nodes**, and misses (`missing`
in the response) carry over to the next candidate. A dump node no longer 404s:
partially useful is useful. Closed-world semantics are preserved exactly: a
name closes only on the author's **signed** zero-match, never on transport
silence.

**A "Music catalogue" wizard step** with the box pre-ticked when the disk has
3× the dump size (~21 GB): the case stated plainly (discographies beyond the
shelf → streaming and recommendations; exact identity → search, radio,
duplicates; canonization → what peers can exchange analysis about at all), the
cost shown explicitly, and **Settings → Delete catalogue** as the reverse
action — that is what makes a bold default fair. The download starts once after
the first launch (the flag clears immediately, so a restart never repeats a
multi-GB transfer).

E2E: dump → client (2 matched, 12277 rows, zero-match closed) → a dump-less
replica re-serves a verified blob → a second hop verifies it **against the dump
node's key**; a flipped byte and a blob served under another name are both
rejected.
