# P2P Sync Integrity — Data-Poisoning Defense

> **Status: phase 1 SHIPPED (2026-07-05/06), rest is design.** Shipped:
> per-segment + audio_features author signatures, Merkle batches, Worker
> `/timestamp` notary; **provenance refactor 2026-07-06** — content-address
> captured AT ANALYSIS TIME into `analysis_sources` (one row per
> track × physical material: chromaprint + duration_seconds +
> provider_id, NULL = own file — an origin enum until 2026-09-18; a
> BLAKE2b PCM hash stood beside the fingerprint until 2026-09-18, when
> the fingerprint became the only address and the audio payload went to
> **v3**; whole owned library backfilled), records
> link via `analysis_source_id`, segments re-keyed onto `embeddings(id)`
> so segments/mean/provenance can never diverge; seal-guard DB triggers
> (payload change without new signature strips the seal — no writer can
> silently break a sealed record); record payload **v2** adds
> duration_seconds to the material declaration (cheap no-decode import
> gate; tamper-evident); tier-3 stream signing live for every first-hand
> stream (any provider tier and YouTube since 2026-09-02 — lossless-only
> before); **Tier-0 lite on import** — signed/first-hand rows never
> overwritten by sync, `analysis_sources.imported` excludes synced-in
> provenance from signing. Still design: signed records on the wire +
> import verify, segments sync, full Tier 0 (per-node origin, purge),
> karma/verification fabric. **Origin:** poisoning concern raised by
> Valerii 2026-06-02 (verifiable/subjective split, tiers,
> master-as-cache); expanded 2026-07-03 (signature cost, atomicity,
> redistribution, Sybil mass-purge, legitimate-material ambiguity →
> content-addressing; identity certificates for cold-start weight —
> donations / birth date / proof-of-work; earned karma — dilution
> economics, timestamp priority, trap jobs; revocation protocol; karma
> curve, priority-conflict resolution, notary scaling, big-picture
> lifecycles). Updated 2026-07-05: golden age, weight-degraded
> acceptance replaces hard quarantine, mutable-source caveat, phase-1
> audio-only signing scope + possession-privacy stop on mass backfill.
> **Updated 2026-08-14:** identity-scarcity tier redesigned — Worker =
> pure notary (PoW cert merges with birth cert), one peer-verified
> ~2 GB hashcash, T_min ripening, ~2 GB instance-size rule; new
> admission-gate layer (K/R sampling, gold/silver by-product pool,
> quorum promotion, audit-on-mismatch); limits go identity-bound,
> per-IP demoted to WAF backstops — §§ "Proof-of-work certificates",
> "Admission gate".
> **Updated 2026-08-16:** defense *strategy* on top of the mechanism —
> price by similarity (conjunction of unforgeable axes) not ban by
> attribute; congestion pricing (base × load × sim, PI controller,
> dormant in calm, free O(1) quote, merged gold-prefiltered round);
> local standing replaces the friend bit; email-HMAC axis doubling as
> a succession/ban-transfer engine; Worker-mailbox de-specializes the
> master; measure-before-arm phasing — § "Defense strategy".
> **Relates to:** `P2P_NETWORK.md` (transport & identity layer this builds
> on), `PHANTOM-DISCOVERY.md` (phantom rows sync as hints — same
> hint+local-verify rule), `desktop/sync_client.py` (the importer this
> hardens).

## Problem

Sautium's P2P network distributes audio analysis (CLAP embeddings, audio
features, BPM) and metadata (canonical names, MBIDs, tags, stats) between
collectors. A hostile actor — e.g. a competitor out to discredit the
project — can inject plausible-looking fake analysis. Once poison spreads,
detection alone doesn't help: there is no way to tell poisoned rows from
honest ones, so the network's answers degrade and trust is gone.

**Current state (2026-07):** Ed25519 signatures authenticate the *sender*
of a sync request, but nothing vouches for the *content*. Worse, the
importer takes the `source` label from the payload and applies
`ON CONFLICT DO UPDATE` — a malicious peer can spoof `source='lastfm'`
and overwrite authoritative first-party rows. The only bound today is the
friends-only (mutual-invite) topology.

## Threat model

Adversary capabilities we defend against:

- Generates keypairs for free, in bulk (**Sybil**).
- Produces fake analysis and **honestly signs it** with its own keys.
- Redistributes (re-serves) other nodes' records, mixing in fakes signed
  by throwaway keys, claiming "I just relayed it" (**deniability**).
- Sends fake accusations trying to get honest keys banned (**censorship
  via the defense mechanism itself**).

Out of scope: compromise of the user's own machine or a malicious build
of the client. Transport tampering is already covered by TLS on every
P2P connection.

## Why the naive scheme fails (design history)

The intuitive defense — sign every record, spot-check by re-hashing and
verifying the signature, broadcast-ban keys that fail — does **not** stop
the adversary above:

1. **A signature proves authorship, not truth.** The attacker signs his
   fakes honestly; every hash/signature spot-check passes perfectly.
   Signature verification only catches third-party tampering in transit —
   which TLS already prevents.
2. **Key bans don't bite.** Keygen is free; ban one key and a thousand
   pre-generated ones remain. Mass-deleting data "signed by different
   keys" is a losing whack-a-mole race.
3. **Proof-free broadcast bans are themselves a weapon.** If the network
   believes unsubstantiated "key X is poisoned" announcements, the
   cheapest attack is not poisoning — it is announcing *your* key.

The conclusions below keep the signatures (they are cheap and necessary)
but reassign their job: **attribution and evidence, not detection**.

## Core principles

1. **Signing ≠ integrity.** Signatures attribute records to keys and make
   accusations provable. They never certify content truth.
2. **Recomputation is the detector.** Audio analysis is a deterministic
   function of the audio. Whoever owns the same material can recompute
   and compare. Verification is semantic, not cryptographic.
3. **Content-addressing.** A record binds to the *specific audio
   material* it was computed from (acoustic fingerprint + duration), not
   just the logical track. "Legitimately different material" then stops
   being a false-positive source.
4. **Default-deny, not blacklist.** Influence follows weight (friends,
   golden-era birth, certificates, endorsements, age) — a Sybil key's
   records may be visible, but they lose every conflict, mint no karma
   and carry an unverified label, so a thousand free keys still mean
   zero influence. Enforcement is weight degradation, not visibility
   blocking: hard quarantine was rejected (2026-07-05) as friction
   without proportional defense in a friends-first topology.
5. **Accusations carry proof; bans are local decisions.** A flag report
   embeds both signatures and both content-addresses so any owner of the
   material can independently re-verify it. No node auto-deletes on an
   unverified broadcast.
6. **Relays are accountable for their feed.** Redistribution is allowed
   (availability requires it), but the author signature is inviolable and
   the relay's own reputation backs what it re-serves.

## The big picture: a record's life, an attack's life

**A record's life.** A node computes analysis for its own files (GPU,
local). Once a day it Merkle-hashes everything new, sends the 32-byte
root to the Worker, and gets back one signed `{root, date}` stamp — one
HTTP request whether the day produced ten records or ten thousand, and
the Worker never sees the data itself. Records then travel through
ordinary sync sessions, each carrying its author signature, its content
address (chromaprint + duration), and its Merkle path to the stamped
root — every record independently verifiable with no callbacks. An
importing node checks signatures locally, quarantines by default, and
spot-checks a sample via the recompute ladder on material it owns;
endorsements it decides to publish ride its own next daily batch. There
is **no per-record ceremony anywhere**: ~2 notary requests per node per
day, everything else is sync traffic that happens anyway.

**Trust is a local derivative, not a transferred balance.** Nobody
"sends karma". Each node computes every author's weight for itself from
the signed facts sync already delivered: authored records with stamps,
endorsements, flag reports, trap-job grants (the only karma artifact
that exists as its own signed object — gossiped like everything else).
Like authorship in git: derived from the log, never transmitted. Trust
in a record grows monotonically as independent owners encounter and
confirm it; there is no "now it is safe" event — and no race to verify,
because unverified data is inert (default-deny), not dangerous.

**An attack's life.** Fake data from a weightless key reaches no one —
quarantine has no audience. Weight must be bought (certificates) or
earned (karma): real money, real GPU-work, or months of maturation, all
bound to one key. Published poison then meets accumulating spot-checks
(survival `(1−f)^(k·M)`), and dilution to slow them down costs ~99
honest records per fake at 1% poison. The first proven lie — the
author's own signature is the evidence — burns the key, its karma, its
certificates, and its entire purchased history; one `DELETE` per node
erases its contribution. A new key starts at zero, at full price. The
invariant that ties every section together: **the cost of a trusted
identity exceeds the payoff of the fakes it can push before it burns.**

## Data classes and their verifiers

| Class | Examples | Verifier | Defense |
|---|---|---|---|
| Verifiable by recompute | CLAP embeddings, audio features, BPM, duration | Any node owning the same material | Recompute ladder (below) |
| Verifiable by authority | MBIDs, canonical names, aliases, album existence | Local MB dump / owned-album overlap | Hint + local re-verify; never peer-driven merges |
| Subjective | similar-artist opinions, tags, curatorial notes | none (no ground truth) | Trust-based: friends-only, reputation, local flags — the Last.fm-fetched ones never travel since 2026-09-19 (below) |
| Self-reported | play stats | none | Trust-based; low blast radius, provenance-labeled — Last.fm track stats never travel since 2026-09-19 |

**Mutable-source caveat (2026-07-05).** Authority- and source-fetched
data (bios, tags, external descriptions) legitimately changes upstream
over time — a bio edited on Last.fm is a new version, not a lie. A
re-fetch mismatch is therefore NOT evidence of forgery, and **flag
reports apply only to the recompute class**, where the function is
deterministic and the input is content-addressed. This also fixes the
signing rollout order: phase 1 signs audio-derived records only; what
signatures mean for source-fetched classes is an open question.

**Last.fm data is node-local (2026-09-19).** The source-fetched classes
that came from Last.fm — bios, tags, similars, track stats, genre
descriptions — are out of the protocol altogether: Last.fm's API terms do
not allow redistributing its answers. Their tables carry no seal columns
(migration 019), the enrichment grammar keeps only the carry canon layer
(`album`, `album_track`, `track_mbid`), and every node fetches its own by
name. For Last.fm the open question above is closed: nothing of it is
signed, because nothing of it travels.

The **public/friends flag** is data-class-aware: public mode accepts only
verifiable (and verified) classes; subjective data stays friends-only.
The flag changes *exposure*, never disables defenses. Serving (privacy
exposure) and pulling (poisoning exposure) may later split into two flags.

## Signed record format

```
{
  track_uuid,        -- logical identity (UUID v5)
  chromaprint,       -- acoustic fingerprint of the analyzed material
  duration_seconds,  -- whole seconds of it (the cheap import gate)
  model_uuid,        -- EmbeddingModel UUID v5 (existing entity)
  payload,           -- the analysis values themselves
  version,           -- monotonic per (author, track_uuid, model); replay guard
  author_pubkey,
  author_sig         -- Ed25519 over all fields above
}
+ optional endorsements: [ { endorser_pubkey, endorser_sig } ]
```

- **The fingerprint is the whole content address (2026-09-18).** Until
  then a `pcm_hash` — BLAKE2b over the decoded PCM samples — stood beside
  it as the exact-bytes key: invariant to FLAC compression level and tags,
  deterministic for a lossless decode on one ffmpeg build, and nothing
  else — a different build or any lossy decode gave the same material a
  new address, and a seal over it was a possession proof of exact bytes.
  Dropped for one anchor that converges.
- **`chromaprint`** distinguishes "same recording, different rip"
  (fingerprint matches) from "different edition/remaster" (fingerprint
  close but different) from "entirely different audio" (fingerprint far).
  Computed by `fpcalc` (AcoustID toolchain) at analysis time; material
  fpcalc cannot fingerprint (a few seconds of audio) is not analysed at
  all — the floor is one grid window.
- **`model_uuid`** pins the model version so recompute-verification
  compares like with like.
- **Per-record signatures, not batches.** Ed25519 signs at ~15–20k/s and
  verifies at ~7–10k/s per core (batch-verify ~2× faster): the whole
  ~35k-track library signs in seconds, once. Records stay atomic — any
  subset can be served with no batch context. Merkle trees (one signature
  per batch + a log₂(N)-hash inclusion proof per record, still atomic and
  self-verifying) are a *deferred optimization* for if record counts
  reach millions (e.g. per-window full-track analysis).

### Signing rollout & privacy (phase 1: audio-derived only)

Phase 1 signs **audio-derived records only** (embeddings, audio
features) — the deterministic, content-addressed class where a mismatch
is evidence. Source-fetched classes are excluded by the mutable-source
caveat above.

**Non-repudiation cuts both ways (Valerii, 2026-07-05).** An author
signature over a content-addressed record is a permanent, provable
statement of *possession* of that exact material. For bootlegs and other
grey-area recordings the owner may not want an eternal signed proof of
ownership traveling the network — so there is **no mass backfill
signing**, and signing one's own grey material through anonymous keys was
**rejected**: it is structurally the Sybil pattern the project defends
against (fatal optics for the founder specifically), the anonymity is
illusory (keys co-located on one node are trivially correlated), and it
merely relocates the possession proof behind a thin mask. Unsigned data
needs no such trick — it flows today, and if correct it earns weight when
a *verified owner* recomputes and endorses it.

**Three-tier signing policy (2026-07-05):**

1. **Owned-official** (provable purchases — the Bandcamp list; later an
   official-MB-release gate) → signed by the node's real identity;
   local batch re-enrich is allowed and re-signs.
2. **Grey / vinyl-rip local** → left **unsigned**; flows transport-signed
   and provenance-labeled, low weight, mints no karma. Batch re-enrich
   **skips it** — specifically, never overwrites a signed row nor
   re-derives locally what should come from a clean stream.
3. **Streamed clean source** → the key move: enrichment computed from a
   streamed source (any provider tier or YouTube — lossless-only until
   2026-09-02) is signed against **the
   stream's** fingerprint, not the local file's. So a grey album gets
   *signed* enrichment via its clean digital master while the local file
   stays an unsigned vinyl rip — **no possession of the bootleg is ever
   claimed**, and a verifier who owns the official version recomputes and
   confirms. Enrichment provenance (the fingerprint) is deliberately
   decoupled from the playback file: the signature attests to what was
   analyzed, not to what is played. This needs a provenance bit on the
   record (`origin ∈ {local, stream}`) so re-enrich knows what not to
   clobber.

**The signed and synced unit is the segment, not the mean (2026-07-05,
Valerii).** CLAP analysis is windowed — a track is a canonical 10s grid
(window `i` = `[i·10s, i·10s+10s)` from the track start), and
`embedding_segments` stores a position-indexed **subset** (K=12/16/24 by
duration); the track-level embedding is the *mean* of those segments. The
mean is **not** a verifiable unit: two honest nodes that sampled different
K produce different means from the same audio, so a signed mean cannot be
reproduced by a peer. Each **segment** is: `CLAP(PCM[i·10s:(i+1)·10s],
model)` — fully deterministic given the whole-track PCM, the index, and
the model. So segments are the verifiable primitive → **sign per segment,
sync segments, and let each node compute its own mean locally** from the
union of segments it holds (own + pulled). P2P thereby densifies the grid
— the "deepen analysis" path.

**Content-address stays whole-track.** The material declaration is the
track-level chromaprint (fpcalc) plus the duration in whole seconds,
computed once at analysis time. Until 2026-09-18 a `pcm_hash` — BLAKE2b
of the **natively-decoded** PCM, hashed *before* the `-ac1 -ar48000`
analysis conversion (2026-07-05, Valerii: lossless decoding is
deterministic across ffmpeg builds, the resample to 48k is the one step
that can differ) — stood beside it as the exact-bytes key. It went
(Valerii, 2026-09-18): one anchor instead of two, and one that converges.
A hash of exact bytes changed with the decoder build and with every lossy
decode, so two nodes analysing the same stream addressed two materials,
and a seal over it was a possession proof of those bytes; the fingerprint
is a public recording identity that any decode of the material
reproduces. The resample non-determinism stays confined to the segment
*recompute*, where step-2 tolerance already lives. The 48k-mono analysis
frame is a derivation defined by `grid_version`. It stays whole-track
*even though we sign segments*, because `segment_index` is only definable
in the whole-track frame. Per-slice hashing was rejected — a 10s
fingerprint maps to no external recording identity and is too noisy for
the step-2 tolerance check, and it multiplies cost 12–24× for no gain. No
Merkle root either: nodes hold different subsets, so a per-track root is
not shared — each segment is signed **independently** and is
self-contained (atomic, syncable alone).

**Chromaprint is bound into the signature from the start — never added
later (2026-07-05, Valerii).** Deferring it would be a trap: adding it
later changes the payload → a new signature → a new, *later* Worker
timestamp → the original authorship priority is forfeit. So the
fingerprint must be present the first time a record is signed — and since
2026-09-18 a record without one cannot exist: no fingerprint, no
provenance row, no analysis saved. (It is safe for grey material — an
AcoustID fingerprint is a public fuzzy recording ID, not a possession
proof of specific bytes; streamed signing uses the stream's fingerprint.)

The signed segment record (audio payload v3, 2026-09-18):

```
sautium-record:v3:segment:{author_pubkey}:{track_uuid}:{chromaprint}:{duration_seconds}
  :{model_uuid}:{grid_version}:{segment_index}:{vector_hash}
```

`audio_features` (per-track deterministic scalars) is a parallel signed
record under the same content-address:

```
sautium-record:v3:features:{author_pubkey}:{track_uuid}:{chromaprint}:{duration_seconds}
  :{analysis_version}:{features_hash}
```

Audio records and enrichment records are versioned separately
(`AUDIO_RECORD_VERSION`, `ENRICHMENT_RECORD_VERSION` — the latter still
v2, its grammar carries no material hash; since 2026-09-19 it names only
the carry canon layer, the Last.fm kinds having left it), so a change to one never
invalidates the other's seals. No record carries its version: a bump is a
corpus re-sign, and an older format has no verifier.

**`author_pubkey` is bound INTO the payload (2026-07-05, Valerii)**, not
left as an external column, so a signature is an intrinsic statement by a
named identity — the birth certificate then vouches for that pubkey's age
and weight (the certificate rides once per author, not per record). This
does not *prevent* re-signing deterministic public content — anyone can
recompute and sign it — but it makes evidence in a flag report name its
author unambiguously and closes the class of bug where the pubkey is
treated as swappable metadata. Authorship *theft* is caught elsewhere, by
timestamp priority: a re-signer is always later, hence an endorser, not
the author.

Only hashes and IDs enter the signed string — never raw floats — so the
payload is byte-stable across signer and verifier; float determinism lives
only in computing `vector_hash`/`features_hash`, done over fixed-layout
bytes. The mean `embeddings.vector` is left unsigned — a local aggregate,
recomputed as segments accrue.

**Every signed record carries a Worker timestamp (authorship priority).**
The author signature alone proves *who*, not *when* — and for deterministic
data "when" is what defeats a plagiarist (identical content signed later =
endorser, not author; see *The karma curve* and *Notary scaling*). So
phase-1 signing is a **two-signature** flow: the node author-signs its
records, batches them into a Merkle tree, submits the **root** to the
Worker once, and gets back `sautium-timestamp:v1:{root}:{date}` signed by
the master authority. Each record then stores its author signature **plus**
a Merkle inclusion proof to that batch root and the Worker's signed
`{root, date}`. This is the per-batch notary Merkle (one Worker call per
batch, transparency-logged) — distinct from and unrelated to the rejected
per-track *content* root: nodes still hold different segment subsets, but
each node timestamps its *own* batch of signed records, and that is exactly
what a priority claim needs.

Since 2026-09-06 the two signatures are two **stages** with one owner.
`sign_audio.sign()` author-signs on every producer's wake — scan, analysis
run, stream enricher, background pass, all routed through
`backend/notary.py` — so a record is final the moment its analysis
commits, and no producer has to remember to seal (the stream enricher never
did: 30 streamed tracks sat unsigned until an unrelated scan).
`sign_audio.stamp()` batches everything signed-but-unstamped into one root
on the notary's own cadence (≥ 15 min apart: one stamp per listening burst
against the Worker's per-IP budget, which a CGNAT cohort shares), and a
Worker failure or 429 now defers the stamp instead of discarding the
batch. A record still travels only once stamped — the pull and carry gates
read `batch_root` — so the stamp remains the admission signal that binds a
free key to an address.

**P2P replace (signed supersedes unsigned) — deferred design.** A signed
record (typically stream-derived, official material) should be able to
replace an unsigned same-track record locally and across sync. Open
questions: the audio_features/embeddings tables are keyed by `track_id`
(one row/track), so replacement is an in-place upgrade keyed on
weight (signed > unsigned) — but the two may carry *different*
fingerprints (a stream of one master vs a rip of another), so "same
track, better provenance" must be an explicit
upgrade rule, not a content-address match. Precedence, whether the
superseded row is retained, and sync-time conflict resolution are TBD.

## Verification ladder (recompute path)

When a node holding the audio checks a foreign record:

1. **My fingerprint == record's fingerprint** (the same recording; a
   bit-identical decode reproduces the result exactly, any other honest
   decode of the same material lands within the step-2 tolerance) →
   recompute with `model_uuid` → a mismatch beyond that tolerance is a
   **proven lie**. Strongest evidence; generates a flag report.
2. **Fingerprints similar, not equal** (a different rip or master of the
   same recording) → tolerance compare: cosine ≥ ~0.99 for embeddings
   (empirically stable across lossless→lossy transcodes), exact or
   ±tolerance for discrete features. Mismatch → weak, cross-rip-labeled
   report.
3. **Fingerprint differs** → different edition/remaster → **not a
   conflict**. Parallel records for different material coexist; no
   report. An honest author analyzing a remaster can never be framed as
   a poisoner by owners of the original.

**Sampling policy:** verification costs GPU-seconds per track, so it is
selective — a random ~1% of imported foreign records, plus **mandatory**
verification on any record that conflicts with a locally computed value,
plus quarantine-exit checks (below).

**Network immune system:** the more owners a track has, the more
independent verifiers exist — poisoning popular material is caught fast.
A fake bound to a fingerprint that nobody owns is **self-limiting**: it
applies to material that doesn't exist in the network, so it influences
no one's search or recommendations.

## Trust & acceptance model (anti-Sybil)

**Weight-degraded acceptance (hard quarantine rejected 2026-07-05).**
Imported records are visible and usable immediately; what varies with
the author's weight is *influence*:

- Conflicts resolve by precedence: first-party > golden-era / endorsed >
  aged > fresh unverified keys. Low-weight data never overrides
  verified data — it fills gaps.
- The UI labels data from unverified sources and offers a **"verify this
  source" button** — the *human* trigger of the recompute ladder: one
  tap batch-recomputes everything from that source over the local
  library overlap → clean, or evidence + flag + purge.
- The background ladder stays on regardless (random ~1% sampling +
  mandatory recompute on conflict with a locally computed value). It is
  invisible to UX and catches the subtle poisoning no human would notice
  enough to press a button about.
- Containment is unchanged: peer data touches only reversible surfaces
  (Tier 2), and one `DELETE` per key rolls anything back.

Weight sources, strongest first:

- **Mutual-invite friends** — expensive, socially anchored keys.
- **Golden-era birth** — born_at before the network's first proven
  forgery (own section below); a mitigating factor, never an indulgence.
- **Email-verified keys** — the existing Worker CA badge; weak but real
  cost.
- **Endorsed keys** — records countersigned by nodes I already trust.
- **Identity certificates** — donation receipts, birth certificates,
  proof-of-work certificates issued by the Worker CA (own section below);
  the cold-start path for nodes without social ties.
- **Earned karma** — matured authorship, verification work, solved trap
  batches (own section below); compounds over time where certificates
  only bootstrap.
- **Key age / clean history** — earned over time, anchored by birth
  certificates.

Sybil economics collapse: a thousand free keys × zero weight = zero
influence — their records lose every conflict, mint no karma, wear the
unverified label, and a per-key purge erases any of them in one
statement. The blacklist still exists but only for the rare *expensive*
key gone rogue (a friend's compromised node), where it is cheap and
effective.

**MVP weighting is binary** (friend / email-verified / nobody). Floating
reputation scores are deferred until real abuse data justifies them.

**Accepted trade-off — cold start:** a new user without friends imports
little until they connect or verify locally. That is a product decision
(the network optimizes for trustworthy data over instant bulk), not a
technical gap — and identity certificates (below) are the deliberate
mitigation: a way to earn initial weight without social ties. The
*network's* cold start is a different problem with its own answer — the
golden age, next.

## The golden age (network-level cold start)

Proposed by Valerii 2026-07-05. Before the network is worth attacking,
nobody attacks it: early adopters are legitimate with overwhelming
probability, and defense friction (weight grinding, verification
ceremony) would punish exactly the people building the network. So the
period from launch until the **first proven forgery** is the golden age:
trust machinery is dormant, everything flows freely.

- **Golden birth is a mitigating factor, never an indulgence.** A key
  with `born_at` inside the golden era (provable — birth certificates
  are already live, and the Worker never signs a past date, so nobody
  can retro-enroll) gets a standing weight bonus. The core invariant
  still holds: weight, never immunity — a golden key caught forging
  burns exactly like any other, ladder checks apply to everyone.
- **The era ends locally, not by decree.** Each node tracks the flag
  reports it has *validated itself* (self-verifying evidence — the same
  reports that drive revocation). Accumulating validated incidents
  erode the golden bonus gradually — a fading factor, not a switch — so
  there is no global "end date" anyone must agree on and no announcement
  that could itself be attacked. Optionally, once enough weighted
  incidents accumulate, a community vote can anchor a canonical era-end
  date on the Worker for latecomers who joined after the incidents —
  mechanics deliberately left open (erosion curve, vote protocol,
  thresholds).
- **Sleeper risk, accepted knowingly:** farming golden keys requires
  knowing the project before it matters — when it is also not worth
  farming (the same asymmetry the whole idea rests on). Registration
  spikes during the quiet era are visible in the issuance transparency
  counters, and a dormant golden key that wakes up to forge still burns
  on its first proven fake.

## Identity certificates (anti-Sybil weight sources)

The Worker (today an email CA) generalizes into a lightweight certificate
authority issuing **costly identity certificates** — independent weight
markers a fresh node can earn without knowing anyone. The master public
key ships pinned in the distributive (already required for the
master-cache role). Proposed by Valerii 2026-07-03.

### Donation receipts (strongest)

After a donation, the Worker issues
`sign_master(node_pubkey, amount_tier, date)`. Sybil cost scales
**linearly in real money** — 1000 nodes × $5 = $5000 — and a key burned
on a proven fake burns the money with it. No other marker has this
economics. (Same mechanism as the deferred "PayPal CA" idea from contact
discovery, repositioned.)

- The receipt **must embed the node pubkey** — a bare receipt is
  transferable, a resale market collapses the attack cost to one
  donation.
- Privacy: the payment-identity ↔ node-key link exists only at the
  Worker; the certificate itself carries the tier, not the person.
  Blind-signature issuance (Worker signs a blinded pubkey it never sees)
  is the later privacy upgrade if that link becomes unacceptable.

### Birth certificates (key age)

The Worker signs `{node_pubkey, current_date}` — and **never a past
date** — so a key cannot be backdated. Deployment is nearly free: add
the date to the existing email-verification certificate.

- **Not** the inverted form ("Worker publishes a signed daily beacon,
  nodes keep it"): cached public beacons can be counter-signed
  retroactively by any fresh key. A bare signed date proves *freshness*
  (anti-backdating of messages), never *age*. The pubkey must be inside
  the Worker's signature.
- **Aging attack:** keys farmed today mature in a year at zero cost.
  Age is therefore a weak multiplier over other markers — ideally
  age × clean contribution history over that period — never standalone
  weight.
- **Shipped as certificate v2 (2026-08-17)** — merged with the
  proof-of-work certificate below; the v1 `{pubkey, born_at}` shape is
  gone (pre-release, no shims): `born_at` lives on as `issued_at`, the
  Worker upgrades v1 KV records in place on first touch (keeping the v1
  fields for rollback) and every client re-fetches automatically because
  a v1 file no longer verifies.

### Proof-of-work certificates (redesigned 2026-08-14)

**The Worker is a pure notary — it verifies no work.** The PoW
certificate merges with the birth certificate into one type:
`{pubkey, method: pow|email, issued_at, difficulty, params_version}`
signed by `TRUSTED_AUTHORITIES`. `method: email` (verified users)
carries no work requirement; `method: pow` is the anonymous path. The
2026-07 sketch (Worker-verified 64 MB hashcash, ~1 min of work) is
superseded: Worker-side verification capped instance memory at the
128 MB Workers-isolate ceiling, and small instances are exactly what
GPU farms eat (size rule below). Moving verification to peers removed
the ceiling and collapsed the planned tiers into one task.

- **The work:** one hashcash — find a nonce with
  `SHA256(Argon2id(cert_payload ‖ nonce)) < target` at **~2 GB per
  instance**; minutes of background mining in the wizard. The
  challenge is the signed cert payload itself: work cannot start
  before issuance, so certs cannot be pre-mined ahead of the key.
- **Peers verify, once, lazily.** Seeing a `method: pow` identity costs
  an upsert into the local registry (`p2p_identities`: certificate
  fields, `first_seen_at` = the witnessed-age anchor, contact counters);
  the Ed25519 cert check (µs) happens on sight, but the **one** Argon2id
  call (seconds — measured below) runs the first time the identity claims
  something identity-gated — accepting a certificate-gated invite token
  today, the identity lane / relay vouchers / preferred-source picks
  later. It runs under a process-wide semaphore of one with an
  available-memory guard (the 2 GiB working set beside a resident ML
  stack is the limit, not CPU) and the verdict is cached for good. A
  stranger who never asks for anything is never verified, and a flood of
  forged proofs is never evaluated unless its author invests in coming
  back (revised 2026-08-17 from "verify at first contact": most first
  contacts are one-off pulls, so eager verification bought nothing and
  handed a CPU-burning vector to free keys). A failed check blacklists
  the pubkey (`p2p_node_bans`, local): a cert holder presenting fake work
  is hostile by definition. An allocation failure on the verifier
  (`argon2.exceptions.HashingError`) is a transient *verifier* fault,
  never evidence against the prover — "busy, retry", not blacklist.
  Ripening `now ≥ issued_at + T_min` (clock-enforced wall-clock floor —
  the honest replacement for VDF-style sequentiality) is a computed
  property the identity lane will require; token acceptance does not
  require it (a newborn befriending the master minutes after birth is
  the product, and ripening buys nothing on a social bit).
- **Instance size is the anti-GPU knob.** Memory-hard work totals in
  GB·seconds and is shape-independent (call time is welded to memory:
  ~0.5–2 GB/s per core, so 64 MB ≈ 0.1–0.3 s, 2 GB ≈ 5–15 s — there
  is no "2 GB spike for 100 ms"), but per-instance footprint decides
  who runs at full hardware utilization: at 64 MB a desktop CPU is
  core-limited (8–16 threads) while a 24 GB card runs ~375 instances
  (20–50× edge); at ≥ RAM/core of commodity hardware (**~2 GB**) both
  sides are capacity-limited (~12 vs ~4–6 instances, ~1.5–3×). Small
  instances are reserved for the admission gate, whose job is
  throttling, not scarcity.
  **Measured 2026-08-16** (`desktop/p2p/identity_pow.py --bench`,
  i9-14900HX, argon2-cffi 25.1, WSL and the Docker image agree):
  2 GiB p=1 ≈ **1.3–1.5 s** (1.4–1.6 GiB/s single lane), 2 GiB p=4 ≈
  0.5 s, 64 MiB ≈ **35–40 ms** (the gate's unit `w`), peak RSS 2,067 MiB,
  and argon2-cffi releases the GIL (2 threads → 1.8× throughput). The
  challenge is the certificate's authority *signature* (commits to every
  signed field, deterministic, absent before issuance). Verification is
  therefore ~1.5 s per stranger identity on desktop-class hardware and
  a semaphore of 1 already clears ~2,000 first contacts an hour; the
  2 GiB working set — not CPU — is what caps concurrency on a machine
  where the ML stack is resident. `difficulty` is carried as the
  expected attempt count E (integer, target = 2^256 // E) — a continuous
  price scale, so the authority can move it in small steps and pin the
  golden-age value low (seconds of background work) while escalation
  raises it only for new births. Wait at 1.4 s/call (geometric): E=128 →
  mean 3 min / p90 7 / p99 14; E=256 → mean 6 / p90 14 / p99 28; a lite
  laptop at ~2× the call time doubles these.
- **Memory-hard (Argon2id, already in the stack), not hash-hard:**
  plain SHA puzzles hand GPU farms a ~1000× edge, and the target
  audience owns RTX 4090s — so attackers do too.
- Considered & rejected: **weakened crypto problems** (small-RSA /
  small-group ECDLP — O(1) to verify, but compute-bound: the
  100–1000× gap between an optimized GPU solver and bundled client
  code breaks calibration and discounts exactly the attacker's farm);
  **VDFs** (prove elapsed *time*, not spent *resources* — parallel
  instances mint identities in bulk, and with an interactive notary
  the signed `issued_at` + T_min measures wall-clock directly, no
  exotic crypto needed); **sampled 64 MB checkpoint chains** (only
  ever existed to let a weak verifier check minutes of work — atomic
  peer verification obsoleted them).
- **PoW is a linear barrier, not a wall.** A mined identity costs
  under a cent of cloud spot compute. What holds is the stack around
  it: newborn allowance ≈ 0, growing with *witnessed* age (first-seen
  — never `issued_at` alone, or hoarded certs would ripen in a
  drawer); karma confiscation burning the mined cost on abuse; and
  node-global windows as the absolute resource ceiling. Issuance
  keeps only WAF-grade per-IP backstops: identity birth is a
  once-per-lifetime event, so no honest CGNAT crowd approaches them.
- **Certificate v2 wire format (shipped 2026-08-17):**
  `{v: 2, pubkey, issued_at, method: pow|email, difficulty,
  params_version, email_token, email_class, issuer, sig}` over the
  fixed nine-field payload
  `sautium-birth:v2:{pubkey}:{issued_at}:{method}:{difficulty}:{params_version}:{email_token|""}:{email_class|""}`
  (three mirrors: `worker/verify.js`, `desktop/p2p/birth_cert.py`,
  `backend/birth_authority.py`). Decisions taken: the cert is **free**
  (no issuance deposit — the Worker stays a notary on the free plan;
  peer verification + T_min + witnessed age carry the cost, the per-IP
  backstop on `/birth-certificate` stays); `difficulty` is the
  **expected attempt count E** (continuous scale; golden-age policy
  E=32 ≈ 45 s of background mining, raised for new births only);
  `method: email` carries **no work bond** and adds
  `email_token = HMAC(EMAIL_PEPPER, "email:" + normalize(email))`
  (lowercase/NFC, `+tag` stripped for every domain, dots stripped and
  googlemail→gmail for Gmail) plus `email_class ∈ major|other|disposable`
  from Worker-side domain lists — the node never sees the domain, so the
  class rides the cert. Email verification upgrades the SAME record
  (issued_at unchanged) and `/register-email` returns the new cert.
  `GET /issuance-stats` publishes per-day birth/upgrade counters and the
  current policy (CT-lite, best-effort KV counters).
  **v3 (2026-08-18, Valerii's proposal):** hashing the whole address
  destroys the domain as a cluster axis, so the certificate gains
  `email_domain_token = HMAC(EMAIL_PEPPER, "email-domain:" + domain)` as a
  tenth payload field — shared by every gmail identity (no information),
  unique to a rare domain ("fifty identities on one odd domain" is a
  conjunction the mailbox token cannot express). No local-part token on
  purpose: near-zero signal (common names collide, an attacker varies it
  for free) and it would link a person across providers by name — beyond
  the accepted mailbox-level trade. Records migrate lazily (v1/v2 → v3,
  older signature fields kept for rollback; an email record gains the
  domain token on its next `/register-email` or `/check-email`). Cost of a
  version bump, stated once: every record is re-signed, so a pow identity's
  proof (mined over the signature) goes stale and the node re-mines it —
  fine pre-release, a grace policy before any public release.
  **v4 (2026-08-18, Ф13):** an eleventh payload field, `predecessor` — the
  pubkey that held this mailbox before the current registration. The
  Worker keeps `mailbox:{email_token}` → current holder; a fresh
  `/register-email` moves the mailbox to the registering key and names
  the previous holder in the new certificate (`/check-email` and lazy
  migration only backfill the index, they never take a mailbox over —
  the newest REGISTRATION owns it, not the newest touch). Email records
  only; empty otherwise; a key re-registering its own mailbox changes
  nothing; a re-take by an earlier holder names the intermediate key
  (a cycle is fine — the registry keeps "who currently holds"). Nodes
  need no Worker round-trip: the link is signed into the identity
  document, and the `email_token` links every key that ever verified
  the mailbox even across a hop this node never met.
- **Node-side proof (shipped 2026-08-17)** — `desktop/p2p/identity_proof.py`,
  shared by the launcher (thread started with P2P) and the Docker backend
  (lifespan task): the proof `{v, pubkey, cert_sig, nonce, difficulty,
  params_version, mined_at}` lives in `identity_proof.json` beside
  `birth_certificate.json` (launcher identity dir; Docker
  `./data/node_identity` bind mount) and travels in the export/import
  bundle. Policy: `method: email` → nothing to mine; a stored proof that
  binds to the certificate is re-verified once in the background (a
  corrupt proof must never be presented — a failed proof blacklists its
  presenter); otherwise mine at below-normal thread priority behind a
  per-attempt gate (≥ 2.5 GiB available memory, mains power), with
  `HashingError` treated as the same pause. Progress is published to
  `user_settings['p2p.identity']` + `NOTIFY sautium_identity` (Settings →
  P2P card "Identity": odds so far, not a percentage of work). A node
  whose email was verified before v2 self-heals at startup via
  `/check-email` (method:email certificate, no work).
- **Adaptive birth pricing — shadow (shipped 2026-08-17, Valerii's
  proposal):** the Worker is the only party that sees the whole birth
  process, and a botnet is a mass event. Every first issuance is recorded
  in a SQLite Durable Object (`BirthLedger`: time, ASN, country, peppered
  /24 token and — since 2026-08-18 — the peppered exact address, method)
  and scored against the recent births that share an axis with it
  (`n_addr24`, `n_sub24`, `n_asn1`, `n_asn24`, `n_glob1`, `n_glob24`; a
  /24 in a cloud provider spans many tenants, one VPS minting twenty
  identities is the sharper conjunction); a would-be multiplier
  `m_shadow` is stored per birth and aggregated by `GET /issuance-stats`
  (`ledger.*`); the thresholds and the arming policy are operational
  notes. Rules carried into the design: price per CLUSTER, never a
  global switch (an honest launch wave from one ISP must not pay for a
  cloud flood; a griefer with free keys must not raise the price for
  everyone); a hard cap bounds both the false-positive cost (a newborn
  needs no proof for hours anyway) and a formula bug; nodes read an
  elevated `difficulty` as a hint axis whose weight decays with witnessed
  age — a surge price paid is not evidence against the payer; issuance
  stays idempotent (a re-request returns the original difficulty); the
  ledger is advisory (issuance never fails because it did).
- **Identity registry + first enforcement (shipped 2026-08-17)** —
  `desktop/p2p/identity_registry.py`, shared by both peer surfaces:
  `p2p_identities` (registry rules for a known pubkey: `issued_at` must
  match — anything else is an anomaly and the update is refused; `method`
  only moves pow → email — a stale pow certificate for an email identity
  is ignored, not suspicious; `email_token` may change; `first_seen_at`
  and a `verified` status survive every update) and `IdentityGate.admit`
  (shape + authority signature → ban list → registry → one evaluation
  under semaphore/memory guard, `busy` = 503 + Retry-After, a per-address
  failure backstop = 429, forged proof = 403 + `failed` + ban). The invite
  token gate `require_birth_cert` now runs BEFORE the use is burned and
  demands the certificate AND, for `method: pow`, the mined proof; the
  guest's 15 s resolver loop absorbs "proof pending / busy". State caps:
  50 000 identity rows (oldest unverified evicted), 10 000 `pow_failed`
  bans. Measured live: 1.5 s per admission on the master, 80 attempts /
  111 s to mine one E=32 proof (the p90 tail is real).

### Shared mechanics

- **Certificate = initial weight, never immunity.** Certified nodes
  still pass the recompute ladder; certificates only speed quarantine
  exit and raise serve rank. A proven fake burns the certified key and
  everything spent on it. Design invariant: **cost of an identity >
  payoff of one proven fake**.
- **Every certificate embeds the node pubkey.** Unbound receipts are
  transferable and worthless as identity.
- **Transparency counters.** The Worker publishes issuance counts
  (CT-lite — the same signed append-only head idea), so a compromised
  master key cannot *silently* mint certificates: mass minting shows up
  as a public discrepancy. Master-key compromise then degrades to
  "attacker gets initial weight faster", not a network-wide trust
  bypass — the ladder still catches the poison itself.
- **Weight portfolio** (extends the binary MVP scale when needed):
  friend > donation-cert > (email + birth + PoW) > email-only > nobody.
- **Optional accelerator, never a paywall.** A free node still joins,
  sits in quarantine, and earns weight through verified contributions —
  formalized as karma in the next section. Mandatory payment would kill
  adoption — mass adoption is the product goal.

## Admission gate — per-event pricing (designed 2026-08-14)

Identity certificates make *pseudonyms* scarce; this layer prices
*events* — CGNAT-clean, no IP math anywhere. It fronts unauthenticated
surfaces: first contact (ahead of the 5–15 s PoW verification above)
and an optional anonymous search lane (queries paid in client compute —
a partial walk-back of the identity-bound-search privacy trade).

### Mode A — sampling gate

Per connection the server derives K fresh nano-tasks
`task_i = Argon2id(conn_nonce ‖ client_pubkey ‖ i)` (64 MB /
30–100 ms each, K = 10–20) — client-bound and single-use by
construction, zero server state. The client answers **all K**; the
server samples **R = 2** of them *after* submission (interactive, so
no Fiat–Shamir grinding) and recomputes only those.

- Honest pass: client K·w, server R·w — a K:R asymmetry.
- Cheating (solve fraction f): pass probability f^R, expected cost
  W/f^(R−1) — strictly a loss at R ≥ 2.
- Garbage residual: R·w per attempt, capped by a verification
  semaphore — and every audit mints pool ammo (below), so garbage
  floods arm the defense they attack.
- *Primitives shipped 2026-08-18 (Ф9): `desktop/p2p/admission.py` —
  fresh inputs derived from the node's gate secret (`HMAC(gate_secret,
  "gate-fresh:v1" ‖ nonce ‖ client_pubkey ‖ i)`), the deterministic
  position shuffle, one answer function for every task
  (`Argon2id(input, salt "sautium-gate:v1", 64 MiB, t=1, p=1)`), the
  signed quote core and the `X-Sautium-Gate` payment encoding, sampled
  verification with server randomness drawn after submission — all
  reproducing the wire-format vectors. Measured on the i9-14900HX:
  **w = 34 ms** per task single-threaded, a 20-task packet in 227 ms on
  4 threads (3.0× — argon2-cffi releases the GIL) / 163 ms on 8, peak
  RSS 520 MiB at 8 threads; honest packet n=20 costs the client ≈0.7 s
  serial (0.2 s pooled) and the server R=2 ≈ 67 ms; a cheater solving
  half passes 25 % of the time and pays 2× the honest price per
  success — the asymmetry as designed.*

### Mode B — two-class pool, the by-product economy

Every first-party computation — R-samples, conflict audits, even
checks of garbage — yields a true `(task, answer)` pair for free: the
server computed the truth in order to compare. Those pairs are the
pool.

- **gold** (first-party computed) — ground truth. Mode B hands one
  gold task to a stranger: client pays w, server verifies by O(1)
  memcmp and rejects mismatches with O(1) confidence (our own
  computation cannot be wrong). Entries minted while failing garbage
  are the cleanest — their author never solved them, so zero
  self-redemption risk.
- **silver** (`not_verified` — the K−R unchecked claims from batches
  that passed R) — **never ground truth**. Mixed into future Mode-A
  packets as free cross-validation probes: a match is a quorum vote
  (M votes from distinct passed connections → promoted to serve
  Mode B), a mismatch triggers a w-audit that mints the truth and
  exposes the liar, an R-sample landing on silver gilds it for free.
- **Audit-on-mismatch** for silver and promoted entries — never a
  rejection. Combined with gold's certainty this gives the gate a
  hard property: **false rejection of honest strangers does not exist
  anywhere**.

Poison economics: planting is cheap (~0.25 entries per w spent at
R=2, f=0.5) but inert — silver rejects nobody and admits nobody.
Promotion costs M·K·w per entry (each quorum vote rides a connection
that passed its own R check), any honest touch destroys the entry —
and converts it to gold. A fully promoted poison entry buys ≈ one
w-audit of damage. Conservation law: **cached truth must be
first-party** (or quorum-corroborated with the audit safety valve);
unverified claims never become rejection grounds.

Pool policy: single-use (retire on redemption); an abandoned lease
*retires* the entry (entries are free by-products — returning them
would enable fishing for one's own); passed-source entries are never
reissued to their author's pubkey and expire, so author
self-redemption amortizes back to ≈ the honest per-event price — no
profit. The family is BOINC/SETI result validation — replication +
quorum + spot-check — applied to an admission gate.

The precomputed-answer variant (verifier pre-solves cores offline,
memcmp online) runs at 1:1 per-pass parity with the attacker and is
kept only for CPU-less verifiers (the Worker, with the master node
filling KV pairs) — never for peers.

*Shipped 2026-08-18 (Ф11): `desktop/p2p/gate_pool.py` (`p2p_gate_pool`:
task, answer, class gold|silver, origin sample|audit|garbage|claim,
source, votes, use_count, `recipient_keys`, lease, expiry 7 d, cap
10k) + the Mode B loop in `gate_service.py`. A priced packet =
n_fresh + 1 gold + 2 silver, shuffled; verification order: our
signature/binding/nonce → **gold memcmp prefilter** (garbage dies at
O(1), the entry is retired) → R fresh recomputed (every truth minted as
gold: origin `sample` when the packet passed, `garbage` when not) →
**silver probes** (match = vote, quorum M=3 promotes — origin stays
`claim` so a later mismatch still audits; mismatch = w-audit that gilds
the entry and names the liar: the source via `on_evidence` →
`p2p_node_bans(reason='gate_lie')`, or the current client whose packet
then fails) → up to 6 unsampled fresh claims minted as silver. Reissue
is an EXCLUSION on hard axes, not a ranking: `recipient_keys` (pubkey,
`subnet:`, `domain:`) of everyone who held an entry, one indexed
array-overlap query and a random pick among the eligible — gold is
single-use so its check is against one author, silver carries ≤ M keys.
Abandoned leases retire the entry; a presented lease is a use whatever
follows. Verified live against Postgres (`--selftest`: two honest
packets → gold/silver minted and reused, garbage → burned, a planted
lying claim → audited, gilded, source named; replay → refused) and
in-process on the aiohttp surface (three clients: 6, 9, 9 tasks).*

### Identity-bound limits — the resulting layer map

Search and sync requests carry ts-bound Ed25519 signatures (±60 s —
the existing relay-endpoint pattern). Privacy trade accepted
2026-08-14: the pubkey is a join key by construction, but taste is
already broadcast (per-artist DHT announces from the node's IP, open
sync inventory); in reserve: a purpose-derived search subkey
(Worker-certified, socially unlinked), blind tokens (§ above). Each
layer owns exactly one property:

1. **identity scarcity** — cert + ~2 GB hashcash, peer-verified once;
2. **event pricing** — this admission gate;
3. **allocation** — per-identity buckets, witnessed-age weight, karma;
4. **resource ceiling** — node-global windows;
5. **per-IP** — WAF-grade emergency backstops only, at
   once-per-lifetime frequencies (birth, first contact) — CGNAT-safe
   by frequency alone.

#### Wire format v1 (designed 2026-08-17 — the protocol checkpoint; certificate fixture v3 since 2026-08-18)

Fixed here so that identity-bound requests (Ф6) and the gate slots (Ф8+)
land without a second protocol change. Scope: the two peer surfaces
(launcher sync server, Docker `p2p_app` on 8801). Chat and relay
endpoints keep their existing per-endpoint signatures. Reference values
for every rule below live in `tests/p2p/vectors/peer_wire_v1.json`
(fixed test seeds); an implementation that cannot reproduce them is
wrong.

**1. Request authentication (the identity-bound lane).**

- Headers `X-Sautium-Peer-Pubkey` (hex Ed25519), `X-Sautium-Peer-Ts`
  (unix seconds), `X-Sautium-Peer-Sig` (hex). Named apart from the Web-UI
  HMAC (`X-Sautium-Ts/Sig`) on purpose: the peer surface never sees the
  local secret, and a peer client sends only the `Peer-*` set.
- Canonical message, UTF-8, LF-joined, signed by the requester's key:
  `sautium-request:v1 ⏎ {server_pubkey} ⏎ {METHOD} ⏎ {request-target} ⏎ {ts} ⏎ {sha256_hex(body)}`.
  `server_pubkey` is the `node_id` the client read from the peer's
  `/health` — a signature is good for exactly one recipient, so a
  captured request replayed to another node proves nothing there.
  `request-target` is the origin-form as sent, undecoded (aiohttp
  `request.raw_path`; ASGI `scope["raw_path"]` + `?` + query string) —
  never a re-serialised URL. Empty body → sha256 of the empty string.
- Freshness: `|now − ts| ≤ 60 s` (the relay `TS_WINDOW`). No nonce cache:
  the identity-bound endpoints are reads or idempotent imports, and the
  one side effect that must be single-use (a gate payment) carries its
  own nonce.
- Unsigned request = **anonymous lane** (today's per-IP + node-global
  windows, the future compute-priced market). Signed = **stranger lane**
  until the identity is `verified` and ripe, then **identity lane**
  (standing joins the condition in Ф15). In the golden age all lanes
  share the same limits; the lane is only decided and logged (Ф7). A
  banned pubkey gets `403 identity banned` on signed requests (it can
  still walk the anonymous lane — that lane is priced, not trusted).
- Response headers on signed requests: `X-Sautium-Peer-Identity:
  unknown|unverified|verified|failed|banned` (`unknown` = no registry row
  → introduce yourself) and `X-Sautium-Peer-Lane: anonymous|stranger|identity`.

**2. Introduction (first contact).** Header `X-Sautium-Peer-Cert` =
base64url of the JSON `{"cert": <certificate v2>, "proof": <proof|null>}`
(sorted keys, no spaces, unpadded). Sent when the client's in-memory
per-peer flag says "not introduced" and again whenever a response says
`unknown`. The server verifies the certificate (µs) and the pubkey match,
then `identity_registry.observe()` — the proof is stored, never evaluated
here (lazy verification, Ф4). An invalid certificate is `400 identity
certificate invalid` — a bug or version skew, not a ban.

**3. Whitelist.** Never signed, never gated: `GET /health`,
`GET /api/gate/quote`, `GET /api/relay/voucher`. Identity-bound:
`/api/sync/*`, `/api/mb/*`.

**4. Gate slots (dormant until priced — Ф8/Ф10, pool in Ф9/Ф11).**

- `GET /api/gate/quote?pubkey={client}&action={family}` → `{"quote":
  core, "tasks": [hex32…], "sig": hex}`. `core = {v:1, server, client,
  action, nonce (16 B hex), issued, deadline, price_version,
  params_version, n, tasks_digest = sha256(task inputs concatenated)}`,
  signed by the server key over `sautium-gate-quote:v1:` + canonical JSON
  (sorted keys, compact). `action` (2026-08-18) is the endpoint family
  the payment is good for — `base(action)` prices the request the client
  is about to make and the server refuses a payment whose quote names
  another action.
  Dormant: `n = 0`, no tasks, the client attaches nothing. Deadline ∝ n
  (placeholder `issued + 30 s + 0.5 s × n`).
- Tasks are opaque 32-byte inputs. Fresh ones are derived, not stored:
  `HMAC-SHA256(gate_secret, "gate-fresh:v1" ‖ nonce ‖ client_pubkey ‖ i₂)`
  (`gate_secret` = a per-node random key, not the Ed25519 key); pool ones
  (gold/silver) are leased to the nonce; positions are shuffled by
  `HMAC(gate_secret, nonce)` — the client cannot tell which are checked.
- Answer function, same for every task and every connection (so pool
  answers stay valid across connections):
  `Argon2id(secret = input, salt = "sautium-gate:v1", 64 MiB, t = 1, p = 1, 32 B)`.
- Payment rides the priced request: `X-Sautium-Gate` = base64url of
  `{"quote": core, "sig": …, "answers": [hex32 …]}` (~1 KB at n = 3,
  ~2 KB at n = 20). Server: verify its own signature, deadline, `client`
  == request signer, nonce single-use (seen-set until deadline), then
  gold memcmp → sample R fresh → silver compare.
- Codes: `402 gate_required` (armed and unpaid; body carries
  `quote_url`), `403 gate_failed`, `403 gate_replay`, `410 gate_expired`,
  `503 gate_busy` + `Retry-After`.

**Shipped 2026-08-17 (Ф6):** items 1–3 are live on both surfaces —
`desktop/p2p/peer_auth.py` (shared sign/verify, vector-tested),
`BackendAPIClient(peer=PeerIdentity)` for every outbound peer client
(launcher and Docker), the aiohttp middleware in `sync_server.py` and the
ASGI `PeerAuthMiddleware` in `p2p_app.py` (body buffered and replayed,
`raw_path` + query as the request-target).

**Shipped 2026-08-18 (Ф10):** item 4 is live and DORMANT —
`desktop/p2p/gate_service.py` on both surfaces: `GET /api/gate/quote`
(signed, client-bound, n = 0 while the price function returns 0), the
gate secret derived from the node's Ed25519 seed (no new file), and
`X-Sautium-Gate` verification in both middlewares: our signature, our
pubkey, the quoted client == request signer, alive (±60 s skew, ≤
deadline), single-use (a per-nonce seen-set that lives until the
deadline and is released again on a transient failure so a busy verifier
never burns a quote), tasks_digest recomputed from the secret, then R
answers recomputed under a semaphore of 2 (R × 64 MiB) with server
randomness drawn after submission. Verdicts: `ok` (response header
`X-Sautium-Gate-Result: ok`), `403 gate_invalid | gate_replay |
gate_failed`, `410 gate_expired`, `503 gate_busy` + Retry-After; a
failed or short packet burns the nonce (deterministic evidence). The
client (`BackendAPIClient.gate_pay/gate_prepay`) verifies the quote is
the peer's and its own before working, solves in a small thread pool
and, on `402 gate_required`, pays once and retries. Verified live
against the Docker surface at n = 0 (ok / replay / tampered / unsigned)
and in-process against the aiohttp surface with an armed test instance
(n = 4: ok / replay / garbage → gate_failed). Pool tasks (Ф11) and the
price function (Ф12) plug into `_pool_tasks()` and `price`.

**Closed 2026-08-24 — peer TLS is pinned to the node key.** The gap
was: peer TLS self-signed and unverified (`CERT_NONE` everywhere), so
nothing authenticated the *server* — an impersonator could serve
health/quotes under its own key and waste a client's work (seals
protected the data regardless). Now the server cert carries a private
X.509 extension (`peer_auth.TLS_BINDING_OID` — int32-safe components
only: Go's x509, i.e. the master's Caddy front, refuses to load a cert
whose extension OID has a component over int32, which killed the
prettier 2.25.{uuid} arc): the node pubkey + its Ed25519 signature over the
cert's own SPKI (`sautium-tls-bind:v1:` + sha256). The handshake proves
possession of the TLS key, the extension proves the node key vouches
for it. Client side, `peer_auth.pinned_ssl_context(expected_pubkey)`
verifies per-handshake (SSLSocket for urllib, SSLObject for
aiohttp/asyncio) and REQUIRES a valid binding: known-key callers
(friends, relays, the master, probe-connect callbacks) pin hard;
unknown-key callers (DHT-discovered sync/slice peers) lock onto the
first verified key per context, and `BackendAPIClient` takes the
server pubkey FROM THE CHANNEL — a `/health` body that disagrees is
refused, so quotes are only ever paid to the key that owns the
connection. Certs regenerate themselves when the binding is missing or
belongs to a previous identity (`node_identity.ensure_tls_cert`,
`tls_gen.ensure_cert(binding=...)`); the master's Caddy front serves
the same bound file. The schemeless plain-HTTP peer fallback in
`_try_connect_peer` is gone — a downgrade path would have nullified
the pin.

## Defense strategy — congestion pricing + similarity (designed 2026-08-16)

The mechanism (cert + gate + pool) is built; this is the *strategy*
that aims it. Core reframe by Valerii: **stop banning by attribute
(identity, IP) — those are defeated by rotation — and price by
similarity to already-suspect traffic.** Established families:
congestion pricing, client-puzzle auctions (Portcullis, SIGCOMM'07),
payment-fraud link analysis.

### Threat inventory

The attacks worth pricing against, on a single-user home appliance:

- **(a) Denial of legit service** — starve chat / relay / P2P for real
  users.
- **(b) Resource exhaustion as product-killer** — "Sautium makes my PC
  lag, I'll disable P2P." The most dangerous: it kills the product,
  not the node. Ceiling on Sautium's own footprint (25% CPU/RAM, lower
  on lite) is a *product* requirement, not just defense.
- **(c) Eclipse / isolation** — surround a node with attacker peers
  (DHT + relay) and filter its view of reality.
- **(d) Amplification** — make our node attack a third party (the
  DNS-amplification shape: small request → large action aimed at a
  spoofed victim). Audit any "we contact X because Y asked" endpoint.
  Current state: `probe-connect` is safe (connects only to the
  *observed* TCP source, unspoofable, + per-pubkey cooldown); DHT
  announce-on-behalf is voucher-gated; Worker email is per-recipient
  capped.
- **(e) State-growth** — bloat queues / tables / the task pool. Every
  new store needs a cap (the pool included).
- **(f) Pricer griefing** — hold a node near its ceiling so legit
  users see a high price. Countered by the integral term below.

**Master node is the one asset worth guarding.** At birth every
identity auto-friends master as a feedback channel — tempting to
block. Strategic answer is **de-specialization**: master is already
just relay #0 under the shared cap (phase D). What stays unique — the
auto-friend feedback channel and support tokens — gets an off-master
fallback: an E2E-encrypted **Worker mailbox** (KV, TTL + caps +
client-signed) that master drains when online. Goal: blocking master
achieves nothing — and it also covers master going offline on its own.

*Shipped 2026-08-18 (Ф16): `POST /mailbox` on the Worker parks the
ordinary chat wire payload — NaCl Box ciphertext to the master's chat
key, the Worker never sees plaintext — signed by the sender
(`mailbox:v1:{to}:{message_uuid}:{timestamp}:{sha256(encrypted)}`),
gated by a birth certificate issued there (no free keys) and by per-IP
(60/h), per-sender (30/day) and global (5 000/day) caps, in a
SQLite-backed Durable Object (`MasterMailbox`: TTL 30 d, 20 k rows,
oldest evicted; one instance, keyed by the pinned master pubkey — the
only mailbox that exists). The master drains on an EVENT, never a timer:
it holds an outbound WebSocket to `GET /mailbox/wake` (hibernating on
the DO side; keepalives auto-answered) and receives "mail" on every
store; drain = master-signed `GET /mailbox` pages of 200 → import
through the same `handle_incoming` as the direct path (friend right,
size cap, `message_uuid` dedup) → `DELETE /mailbox?upto=id` ack; a
handler failure leaves the page unacked. Sender side (launcher
`_mailbox_deposit`): only when the direct path found no route — the
place `_relay_forward` used to give up for the master; acceptance is
delivery (handed to the mailbox, like mail to an MTA). Shared code
`desktop/p2p/mailbox_client.py`; the drain runs in the Docker backend
when its identity IS the master. Verified live: deposit from a
throwaway certified key → the master's socket woke and drained within
seconds, the non-friend message was dropped by the friend-right check
and acked. Not done: a launcher-side drain (no launcher is the master)
and mailboxes for anyone else (a general dead-drop is a different
product with a different abuse surface).*

### Similarity: a price coefficient, not a verdict

A single axis fails both ways — CGNAT neighbours share an IP (false
positive), cheap proxies unshare it (false negative). A **conjunction**
of axes inverts both: honest CGNAT neighbours collide only on IP
(births scattered across years, tastes and schedules divergent), while
an attacker fleet born from one process collides on many axes at once.
Similarity = product of individually-unlikely coincidences.

Axes, classified by how an attacker beats them:

- **Unforgeable by construction** (real weight): the Worker-signed
  `issued_at`; the email token (below). These are hard links, stronger
  than any statistic — equality means same origin.
- **Expensive to vary** (moderate weight): IP / subnet.
- **Expensive only to simulate** (≈ zero identity weight — *economic
  ballast*): behaviour / taste. A bot fills these with noise for free
  as identity signal — but in a priced world **the noise is not free**:
  every camouflage request is paid in puzzles, so taste's value is not
  detection, it is *tax*.

Two hard rules:

- **Weights live on conjunctions, not single axes** — "same birth day"
  ≈ 0 alone (a growth wave, e.g. a viral review, births hundreds
  honestly in one day); "same day + same IP + same request targets" is
  signal. Growth waves recur, so this is permanent hygiene even though
  the first one passes before enforcement is armed.
- **Deterministic evidence → ban; statistical evidence → price.** A
  failed 2 GB check or a broken protocol cannot be a false positive →
  blacklist. A cluster *cannot*: two honest people in one flat share
  IP, near births, similar taste. A cluster is only a **collective
  price multiplier**, anchored on a deterministically-caught member.
  This dissolves the classic fingerprint-ban fear: the cost of a false
  positive drops from "excluded" to "entered a bit more expensively."

**Second use of the same metric — dependency diversity.** When picking
K relays, pick the *most dissimilar* (distinct births, subnets,
profiles) — a direct eclipse (c) defense. One metric, two consumers
(and a third: pool reuse below).

*Shipped 2026-08-18 (Ф14, SHADOW): `desktop/p2p/similarity.py`.
Pair score = sum of coinciding axes, ZERO unless ≥ 2 axes hit (the
mailbox token is the one hard link that counts alone): mailbox 4.0;
birth within 10 min 2.0 / same UTC day 0.5; "wave" 1.0 when both were
born above the base difficulty within 10 min (the Ф2b hint as an AXIS
— they paid more, they are not punished for it); exact address 1.5 or
subnet /24 1.0 (registry `first/last_addr` + new `first/last_subnet`);
mailbox domain 1.5 for class other|disposable (gmail carries no
information). Behaviour/taste are not computed — economic ballast.
Cluster of X = candidates over the threshold 2.0 from ONE indexed
query (token, domain, address, subnet, birth ± 1 day, LIMIT 2000);
sim_mult(X) = 1 + 0.5 × Σ min(6, score) over the members that are
BANNED or `failed`, capped 4× — a cluster nobody was caught in pays
nothing. `SimilarityIndex` caches per pubkey (TTL 10 min, cap 10k) and
refreshes on a background thread, so the priced path never waits for
the database (first ask = 1×, then the value). Wired as `sim_mult` into
both pricers (shadow: it only moves the logged would-be price), and as
`relay_order` into the launcher's peer-relay recruitment: least similar
first — a shared /24 with a held relay counts like a hard link — then
previously used relays, then the shuffled DHT order. Weight calibration is an operational note. Self-test on the live database: a 6-key fleet born
in one minute from one /24 on one odd domain clusters (birth+subnet+
domain) but prices 1× until one member is caught, then 3.25×; a CGNAT
yard (one /24, births months apart, gmail) never scores, even with a
caught neighbour.*

*Mailbox domains as a node-side policy table (Valerii, 2026-08-18: "the
domain hash can be a reliability index too") — `desktop/p2p/email_domains.py`.
Nodes only ever see the domain TOKEN, so the maintainer precomputes the
tokens of the domains worth an opinion (under EMAIL_PEPPER, `--regen`,
maintainer-only; `--check` is a release step) and ships them inline in
code: `domain: (tier, reliability, token)`. Two readings per token:
`tier` (protected | open | disposable) says how POPULOUS the provider is
and decides whether sharing the domain is informative for similarity
(populous — no; disposable or absent from the table — yes; the Worker's
issuance-time class is only the fallback when the table has no opinion,
so a list drift never fakes a "rare domain"); `reliability` ∈ [0, 1] is
the RELATIVE COST OF ONE MAILBOX (1.0 phone/ML-gated, ~0.3 captcha-only,
0.0 disposable) — a price prior for the standing seed of a newborn (Ф15)
and for whatever later asks "how expensive was this identity to mint",
never trust (PVA markets exist; still orders of magnitude above a proof
of work). Why in code and not in the certificate: policy per DOMAIN,
changed by a release, applied retroactively to every certificate already
issued, and the Worker stays a notary (its coarse `email_class` remains
the issuance-time catch for fresh disposable births, since the Worker
sees the domain). Cost stated once: the shipped tokens de-anonymise
exactly the listed domains — the populous ones; rare domains stay behind
the pepper. Ф2b will read the same fact directly on the Worker (protected
email births weigh less in a wave).*

### Pricing formula v1

```
price(action, client) = base(action) × load_mult(headroom) × sim_mult(cluster)
```

- **base(action)** — from self-profiling (ms CPU + bytes per
  chat/relay/slice/sync), **calibrated per node** — a lite node is
  legitimately dearer, its ceiling below 25%. Merges with the planned
  hardware-tier work.
- **load_mult** — progressive near the ceiling; HQP playing = low
  headroom = expensive. Merges with the backlogged playback-aware
  throttling — one mechanism serves both.
- **sim_mult** — the statistical layer above.

Engineering shape:

- **Dormant below a load threshold** — entry is free and mechanism-less
  when there's headroom, because *our* check also costs R·w. The
  formula already yields ≈ 0 in calm; dormancy also skips the
  machinery. Only the once-per-identity 2 GB verification never sleeps
  (amortized by cache). Side effect: the pool grows only under load —
  exactly when there's traffic to grow it from.
- **PI controller against griefing (f)** —
  `price = f(current level) × g(duration held)`: the proportional term
  hits spikes, the **integral term hits sieges** (sustained
  non-falling consumption escalates), with decay after relief. The
  integral term acts on the **stranger market only** — a legit long
  load (a new friend's initial sync) rides its identity budget, not the
  market. Attrition favours us: the attacker pays superlinear ×
  escalation × an army of *dissimilar* bots (else similarity raises his
  price too); we pay R·w per entry, which he funded.
- **Free quote → one working round.** The quote is a plain O(1) GET
  (spam on it is WAF-grade HTTP flood), so the priced round-1 vanishes.
  The single packet is opaque to the client:
  `N fresh tasks (payment; sampled R only) + ≥1 gold (O(1) prefilter —
  garbage dies on memcmp, not on R·w) + 1–2 silver (quorum probes)`.
  The gold prefilter raises a garbage attempt's cost from ≈ 0 to w
  (must honestly solve gold to even reach sampling) while dropping our
  filter cost from R·w to O(1). Gold is author-recognizable → issued
  via the pool's dissimilarity gate.
- **Quotes are signed, short-TTL, client-bound** (else a cheap calm
  quote is presented in a storm); the TTL deadline scales with N.
  Suspects are never *refused*, only priced (refusal = price ∞ is
  deterministic-evidence only) — "pay first, then learn you're
  throttled" becomes "pay the going rate."
- *Shipped 2026-08-18 (Ф12): `desktop/p2p/pricing.py` — `base(action)`
  = the endpoint's cost EMA (CPU ms + bytes_out / 20 000) in units of
  **w** (one 64 MiB task, calibrated per node at start: 34.6 ms on the
  master), floor 1 task; `load_mult` = 1 while headroom ≥ 0.5, linear to
  8× at headroom 0; the integral term = pressure above the dormant
  threshold integrated over time with a 600 s half-life, +1× per 300 s
  of full pressure (steady state ≈ 3.9×; hard cap 8×) — on the stranger
  market only, the identity lane (verified ∧ ripe) never pays; dormant →
  0; `sim_mult` = 1 until Ф14; MAX 30 tasks per packet. Modes in
  `user_settings['p2p.gate_mode']` (default **shadow**): off — nothing;
  shadow — the would-be price is computed on every identity-bound
  request and logged (`p2p_contact_events.gate_price/gate_status`), the
  wire says 0; enforce — quotes carry the price and an unpaid
  market-lane request answers `402 gate_required` with `price` and a
  `quote_url` (the client pays once and retries). Quotes are bound to an
  ACTION (endpoint family in the signed core, added to wire format v1
  2026-08-18) so a cheap quote cannot be spent on an expensive action.
  The pricer's live multipliers show as "Market" in the P2P card.
  Verified in-process on the aiohttp surface (enforce at headroom 0.2:
  402 price 14 → quote n=13 for the action → paid → 200; a quote for
  another action → 403; shadow → served with the would-be price logged;
  off → n=0) and live on Docker in shadow (dormant → 0).*
- *Shipped 2026-08-18 (Ф12b) — pressure must not be free.* Three gaps
  made the escalation runnable at zero cost to the attacker: an empty
  pool (gold minted from a client's own garbage is never reissued to
  its author, so a fresh flood met no prefilter and cost us R·w per
  packet), no cost for a stream of *failed* payments, and a verifier
  that kept spending at the ceiling. Now: **(1) idle gold seeding** —
  the node mints authorless gold (`origin='seed'`, no recipient keys,
  eligible for everyone) one 64 MiB task per load-meter sample while
  dormant, up to 24 on offer, count re-read every 30 s; a stranger's
  very first garbage packet dies on memcmp (measured live: 22 seeds
  in the first 41 s after a restart). Not the pre-mined pool the design
  rejects (that argument is about scarcity between peers at parity —
  this bootstraps a filter, bounded and idle-only). **(2) Failed-payment
  backstop** — 5 failed payments per hour per exclusion key (pubkey /
  subnet / email domain) → `429 gate_rate_limited`, decided BEFORE the
  nonce and before any work; an honest client fails once by accident,
  not five times. **(3) The ceiling wins** — at headroom <
  `VERIFY_MIN_HEADROOM` (0.1) a priced packet is answered `503 gate_busy`
  with the nonce released (the same quote pays after relief; a free
  n=0 packet never reaches the verifier), the 2 GiB identity proof
  evaluation yields the same way, and a lite profile verifies one packet
  at a time. Also fixed on the way: `gate_pool.lease` picks through a
  MATERIALIZED CTE — as an `id IN (SELECT … ORDER BY random() … FOR
  UPDATE SKIP LOCKED)` semi-join the planner may re-run the volatile
  subquery per candidate row and the LIMIT stops bounding the packet
  (observed once: 2 gold + 3 silver leased to one quote).*

### Local standing replaces the friend bit

The master auto-friends every identity at birth and invite tokens
auto-add, so **"friend" is a social UI bit, not a trust signal.** Reserved (off-market) lanes key
on **local standing** = witnessed age *at this node* + karma + clean
history. A newborn auto-friend has standing 0 → rides the market like a
stranger; a multi-year contributor → reserved lane. The support channel
gets its own narrow budgeted lane (+ the mailbox fallback). "Everyone's
a friend" dissolves because friendship now guarantees nothing.

### Email as a hard axis + a succession engine

`email_token = HMAC(worker_pepper, normalize(email))` — the `IP_PEPPER`
pattern from `verify.js`. Nodes test token *equality* (same token =
same person — a hard link) without recovering the email (a bare
`H(email)` is dictionary-weak at low entropy; HMAC under secret is
not). Normalization before HMAC is mandatory (gmail dots, +tags;
disposable domains → reduced weight). The token rides the `method:
email` cert. Double duty: a similarity axis **and** a **succession
engine** — carrying standing across a password change (new pubkey), and
symmetrically carrying **bans** across it, closing re-birth evasion for
verified users. Trap: the pepper can't rotate without migrating every
link (the `IP_HASH_VERSION=2` lesson).

*Shipped 2026-08-18 (Ф13): certificate v4 names the `predecessor` (see
"Certificate v2 wire format" above) and `identity_registry.observe`
carries succession on both surfaces: for the notary-named predecessor and
every row sharing the `email_token` — excluding the key itself — the new
identity takes `first_seen_at = LEAST(...)` (witnessed age carries: only
what THIS node saw, a predecessor unknown here carries nothing), the
contact count of the freshest link, and a ban if any of them is banned or
`failed` (`p2p_node_bans` reason `succession` — the same request that
introduces the heir is answered 403). Old rows get `succeeded_by` = the
current holder; the operation is idempotent (a row already pointing at
this holder contributes nothing twice) and cycle-safe (a re-take flips
the pointers). Standing (Ф15) reads `first_seen_at`, so it inherits by
construction. Verified against the live database (`identity_registry
--selftest`): a banned email identity's heir is banned on sight with the
40-day witnessed age carried; a clean 70-day veteran's heir keeps
`verified`, contacts 1+5, age carried, even with an unknown intermediate
hop; a second observe carries nothing twice. `contact_log --report`
prints the succession counters.*

### No enrichment whitelist — standing is verified contribution

An enrichment-source whitelist would make enrichment poisoning an
attack vector. Instead, standing comes from **verified** enrichment,
not *received* enrichment: default-deny (unverified grants nothing),
the recompute ladder / spot-checks confirm it, a proven fake burns the
key with everything accrued. "Who I have enrichment from" folds into
the same standing value as "verified contributions" — the conservation
law again: trust must be first-party-verified or quorum-corroborated.

### Phased rollout

The mechanism lands in phases — price the gate, then the similarity
multiplier, then pool reuse — each useful alone and blocking none after
it. The current phase, its measurements and the arming policy are
maintainer-private operational notes.

### The one-line goal

**Make indistinguishability from an honest user no cheaper than being
honest.** Randomized births cost patience; unshared IPs cost money;
simulated taste costs paid puzzle-per-request; live schedules cost real
processes. Every camouflage axis carries its own price, and none is
free.

## Earned reputation (useful work, karma)

Identity certificates are *bought* weight; karma is *earned* weight —
and the work that earns it is the same verification work the immune
system needs anyway. Proposed by Valerii 2026-07-03.

### Dilution economics (why partial poisoning doesn't pay)

An attacker publishing only fakes is caught by the first spot-check. To
hide, he must dilute with honest data: at fake fraction `f`, one
importer running `k` random checks misses him with probability
`(1−f)^k` — but checks accumulate across importers, so survival is
`(1−f)^(k·M)`, decaying toward zero as the material's owner count `M`
grows. Meanwhile:

- **Masking is linear-cost:** pushing `N` fakes at fraction `f` requires
  `~N·(1−f)/f` honest computations — real GPU-hours (at 1% poison,
  99 honest records per fake).
- **Confiscation:** the first proven fake (the author's own signature is
  the evidence) burns the key, all accumulated karma, and all the
  masking work with it.

Net: damage is bounded by `f`, cost grows as `1/f`, punishment takes
everything. A diluting attacker spends most of his budget doing the
network's work.

### The plagiarism problem

Deterministic computation makes content-authorship unprovable in
principle: the correct result for `(chromaprint, model_uuid)` is identical
for everyone, so a copied record is indistinguishable from honest work.
Re-signing someone else's valid records would give an attacker free
"honest mass" to dilute with. Two mechanisms close this:

- **Timestamp priority.** The Worker countersigns
  `{merkle_root, date}` per published batch (RFC 3161-style, one request
  per batch). A plagiarist is provably later than the original author.
- **Dedup: second author = endorser.** The network treats the first
  published `(chromaprint, model_uuid)` record as authorship; identical
  later records are automatically counted as endorsements. Copying
  structurally cannot mint authorship karma.

### Priority conflicts: how "earlier" is discovered and resolved

The dedup key `(chromaprint, model_uuid)` is deterministic, so publication
naturally begins with an inventory check against peers / the master
cache: if a stamped record already exists, the new computation is
published as an endorsement in the first place. If two nodes published
independently without seeing each other (network partition), the
conflict resolves **on encounter**: compare the two notary stamps, the
earlier one keeps authorship, the later record is **reclassified** as
an endorsement — no penalty, nothing deleted, the payloads are
identical anyway. The comparison is deterministic, so every node in the
network converges to the same answer with no consensus round.

### The karma curve (enrichment is not mining)

Two collectors who own the same track do identical GPU-work and both
deserve karma — but they deliver **different value**: the first brings
data that didn't exist; the second brings an independent confirmation —
the scarcest resource the immune system has. So the reward is not
winner-takes-all, and no work is ever orphaned (unlike mining, where a
losing block is waste — here it converts into verification):

```
author > first verifier > second > … > n-th ≈ ε
```

The decay has two honest reasons. First, the marginal trust added by
the n-th confirmation falls fast. Second, a **late endorsement is
indistinguishable from free copying** — countersigning a record already
confirmed five times carries ~zero risk and proves no computation, so
paying full price for it would make copy-farming the optimal strategy.
Timestamp priority itself exists *against plagiarism* (claiming someone
else's work as your own), not to reward racing: the honest runner-up
loses only the authorship label, not the reward — his identical work
lands at the top of the verification scale.

Practical softeners: different masters → different fingerprints → **both
are full authors** (cross-verified via the fingerprint bridge; two rips
of one master share the address and race like bit-identical material); every
collector's rare tail guarantees uncontested authorship somewhere; a
new `model_uuid` resets the race library-wide; trap batches pay
regardless of priority. The honest limit, stated plainly: for
bit-identical material under the same model, the second computation
earns less for equal work — the unavoidable price of determinism
(independent computation is unprovable), compensated by making that
same work the best-paid rung of verification.

### Notary scaling (why stamps stay cheap at 100k+ users)

The Worker signs **roots, not records**: a node submits 32 bytes once a
day, so notary cost is a function of *batches*, not of library size —
ten records or ten thousand, one Ed25519 signature (~50µs CPU), and the
Worker never sees the data. At Roon scale (100k nodes × ~2 requests/day
≈ 1–2 req/s) this sits in the $5–10/month Cloudflare tier; MB-scale
track counts are irrelevant because stamps cover *computation batches
per node*, not tracks. The genuinely expensive part would be the
transparency log (KV writes cost more than signatures): solved by the
Worker keeping its own Merkle log of issued stamps, publishing only the
head (CT-style) and archiving raw entries to R2 — cents per month.
Funding closes structurally: donation certificates pay for the notary
that certifies them; one free stamp per node per day (a rate limit that
doubles as anti-spam), more for weighted keys. Nor is the notary a
single point of failure: a **quorum of reputable nodes** (2–3
independent stamps ≈ one Worker stamp) or external free services
(OpenTimestamps' Bitcoin anchoring, public RFC 3161 TSAs) are drop-in
fallbacks, and a node can publish unstamped — the records work,
authorship karma just waits for a stamp (graceful degradation).

**Identity-priced notary (Ф20, 2026-09-06, shadow).** The "one free stamp
per node per day, more for weighted keys" above is now an algorithm on the
Worker, keyed by the identity the plan's invariants allow it to trust — not
the PoW (a 128 MB isolate cannot verify 2 GiB Argon2id, and the Worker is
the notary, not the verifier), but what the Worker vouches for itself: the
mailbox class it verified, the birth it witnessed, the stamps it issued.
`/timestamp` carries `pubkey, ts, signature` over
`sautium-timestamp-request:v1:{ts}:{roots}`, and the birth ledger prices
each request as a token bucket (`STAMP_BURST` deep, refilled at
`STAMP_BASE_PER_DAY × weight(method, email_class) × age_ramp(witnessed)
÷ m(conjunction)` per day): a verified mailbox doubles the budget, a day-old
key gets a quarter of it (enough for its first batch — mint-and-flood pays
in idle weeks, no PoW check needed), and `m` conjoins the birth's own
multiplier with the stamps table — identities stamping from the same
address that were also born in the same window (the address alone is a
CGNAT cohort; address and birth window together is a farm). Only NEW roots
are charged, so a re-stamp is free. Shadow until `STAMP_BUDGET_ARMED`: the
verdict is recorded (`/issuance-stats` → `ledger.notary`) and returned as
`not_before`, which the client's notary obeys as its pace; per-address
brakes (Rate Limiting bindings, exact IPv4 / IPv6 /64, no KV write) are the
only 429 — WAF-grade, sized for a cohort. Unsigned requests (older clients)
still stamp and are counted, so the arming decision can read the share.
`ip_hash` hashes the /64 for IPv6 (v3); `signing_batches.timestamp_version`
travels with every batch so a payload bump invalidates nothing issued.

### Trap-job protocol (fast lane for newcomers)

A reputable node `R` keeps a **holdback set** — records it has computed
but not yet published. A newcomer `N` requests work; `R` issues a batch
of analysis tasks over the `R∩N` library overlap (files are never
shared, so tasks can only cover material `N` already owns; the overlap
is already exposed by inventory sync), seeding it with holdback items
`N` cannot tell apart from the rest.

- `N` cannot copy the holdback answers — they exist nowhere public.
- `R` verifies for free — it already holds the answers. This is the only
  way around the core asymmetry problem of useful work for ML:
  *checking inference normally costs a full re-inference*; here the
  checker precomputed it (the reCAPTCHA pattern, peer-to-peer).
- Matching results earn `N` karma signed by `R`; `R` then publishes the
  held-back records and rotates fresh computations in.

### Collusion and endorsement-copying

- **Karma weight = f(issuer weight).** A Sybil pair ("A issues tasks, B
  solves, A signs karma") mints nothing: zero-weight issuers grant
  zero-weight karma, and a cluster's total stays zero. Issuers stake
  their own standing — exactly like relay accountability: a node whose
  karma-grantees keep getting caught devalues all its signatures.
- **Blind endorsement is Russian roulette.** Copying a "+1" onto an
  already-endorsed record costs no work — but an endorsement is a
  signature under the content: if the record turns out poisoned (its
  early endorsers were accomplices), the copier burned himself with
  them. Early endorsements on not-yet-verified records outweigh late
  ones, and occasional trap batches calibrate whether a node actually
  computes at all.

### Three earning paths

1. **Author** — publish analysis of your own material; karma matures as
   other owners' spot-checks confirm it over time. Slow, organic,
   protected by timestamp priority.
2. **Verifier** — recompute foreign records against your own material,
   publish endorsements/flags; the network pays karma for exactly the
   work its immune system runs on.
3. **Trap-solver** — the fast lane above; available immediately given
   library overlap with a reputable node.

Karma stacks with identity certificates: certificates buy initial
weight, karma compounds it. Bootstrap: the first reputable task-issuer
is naturally the master node (already the edge-verified cache).

## Flag reports (accusation protocol)

A verifier whose ladder check fails at step 1 (or 2, weakly) publishes:

```
{
  accused_record,          -- full signed record (author's sig = the evidence)
  reporter_chromaprint,    -- what material the reporter checked against
  reporter_value,          -- the recomputed result
  reporter_pubkey, reporter_sig
}
```

The author's own signature under the fake is **non-repudiable evidence**;
he cannot recall it. Report consumers scale their response to what they
can verify themselves:

- Same fingerprint owned → full independent recompute; the report is
  self-verifying.
- Same recording, other rip → tolerant recompute.
- Material not owned → the report is testimony, weighted by the
  reporter's trust standing.

Two response layers (unchanged from the June design):

- **LOCAL** — immediate hide/override of the flagged data for the user
  who flagged it. Their node, their call. Strong, always available.
- **NETWORK** — reports are **advisory**, feeding a review queue weighted
  by friend/trusted status. Never auto-delete on crowd flags: flag
  aggregation is itself Sybil-prone (flag-bombing good data, unflagging
  poison). Ban decisions are per-node and evidence-based.

UX must distinguish **"wrong/fake"** (moderation signal, propagates
negatively, requires evidence) from **"not for me"** (personalization
signal, tunes local recommendations, never propagates).

## Revocation (how a ban works)

There is deliberately **no global ban primitive**. In a serverless
network, any authority able to delete a participant would itself be the
cheapest attack — censoring a competitor is easier than poisoning data.
A "ban" is an **emergent convergence of local decisions around
self-verifying evidence**: nobody orders it, everybody arrives at it.

**The catching node.** After a ladder step-1 mismatch (same fingerprint,
same model, different result), node `V`:

1. Adds `X`'s pubkey to its local `banned_keys(pubkey, evidence, ts)`.
2. Purges `X`'s entire contribution — `DELETE ... WHERE source =
   'p2p:X'`, the one-shot rollback Tier 0's source labeling exists for.
3. Publishes the flag report (previous section).

**Propagation without commands.** Every receiver of the report
classifies itself:

- **Owns the same material** → recomputes, sees the lie first-hand,
  bans independently. For popular material this is an avalanche of
  independent convergence with no mutual trust required.
- **Owns another rip** → tolerant check, same conclusion at lower
  confidence.
- **Owns nothing** → the report is testimony. One report = nothing;
  N independent reports from weighted keys ⇒ **auto-quarantine** of
  everything from `X` (data hidden, connections suspect). Full ban only
  after own verification or an explicit user decision — never auto-ban
  on foreign words alone (flag-bombing defense).

**Why an innocent key cannot be banned.** An accusation requires a
record **signed by X** that fails recompute. Signatures are unforgeable,
so evidence against an honest author cannot be fabricated. The only lie
available to a slanderer is misreporting his own recompute of a valid
record — but material owners re-verify, find the record valid, and now
the *slanderer* has published a signed false accusation: he burns
instead. Accusing is exactly as staked as publishing.

**What burns for X:**

- His data — purged everywhere the evidence reached.
- His karma — the key *is* the karma.
- Karma he **issued** (as a trap-job issuer) — cascade devaluation: his
  signature weight drops to zero, grantees lose that component.
- His identity certificates — the Worker revokes them for that pubkey
  (a donation burns in the most literal sense).
- Relays that habitually carried him — a reputation hit
  (accountability), not a ban.

A new key restarts him at zero weight, in quarantine, at full price for
new weight. The ban need not be eternal or perfect — losing the entire
stake, every time, is the deterrent.

**Master as amplifier, not judge.** The master/Worker may run the
advisory review queue and publish a **signed revocation notice, which
must embed the same flag-report evidence**. Nodes treat it as a strong
signal (auto-quarantine), never as a verdict. Consequence: even a
compromised master key is not a censorship weapon — a notice without
valid evidence is ignored under the same rules nodes already live by.

Mechanics summary: local `banned_keys` table · sender check at sync
handshake · author-signature check at import (relayed records from
banned keys are dropped) · flag reports gossiped as ordinary sync
objects.

## Redistribution & relay accountability

Forbidding redistribution ("only serve what you signed") was considered
and **rejected**: rare data would die with its author's uptime, and the
master-cache pattern would be impossible. Instead:

- **The author signature is inviolable and travels with the record.**
  Content is path-invariant: whatever chain A→B→C it took, the signature
  either verifies or it doesn't. The distribution path therefore needs
  **no** cryptographic protection.
- **The immediate sender is always known** (P2P sessions are
  Ed25519-signed) **and accountable for its feed**, BGP-style: a relay
  that repeatedly delivers records from keys later proven poisoned
  degrades its own standing, even though it "just relayed". This removes
  the deniability the redistribution attack relies on.
- **Endorsements** let verification travel: a node that owned the
  material and ran the ladder may countersign the record. A record from
  an unknown author endorsed by a trusted node ranks far above a bare
  one.
- **Master node = edge-verified cache, never a trusted authority.** The
  shipped master invite does the MB legwork once and serves *hints*;
  every client still re-verifies against its own owned material, which
  the master cannot control. Even a fully compromised master cannot
  poison a specific client. (Rejected forms: blind-trust central
  authority — key leak poisons the whole network; reactive
  "patch+migration deletes the poison" — slow whack-a-mole.)

## Rejected: blockchain

A blockchain solves *global consensus on event ordering among mutually
distrusting parties* and charges for it with a consensus mechanism and
full replication. This design needs none of that:

- Attribution → signatures (have them).
- Revocation → per-key purge (Tier 0).
- Sybil mass-poison → default-deny weights (nothing accepted, nothing to
  purge).
- Chain-of-custody integrity → unnecessary: author signatures make
  content path-invariant (see redistribution).

The one blockchain-adjacent idea worth keeping in the back pocket is a
**signed append-only head** (certificate-transparency-lite): an author
periodically publishes `sign(merkle_root, seq)` over his own record log,
making version-replay and split-view (serving different people different
data) detectable via gossip — with zero consensus machinery. For the
MVP, the monotonic `version` field inside each signed record is enough
replay protection.

## Rollout and open questions

Build order, the current tier and the open design questions are tracked
in the maintainer-private operational notes.
