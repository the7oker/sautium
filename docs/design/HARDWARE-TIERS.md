# Hardware Tiers & Graceful Degradation

**Status:** analysis complete (2026-07-10); **profile mechanism SHIPPED same day**
(P0 + P1 + parts of P2 — see §6 for per-item status). Sections 1–3 describe the
PRE-fix state as found by the audit; where a finding is now fixed, §6 says so.
Verified live on the reference rig: profile auto-resolves to `full`
(cuda 17.2 GB / 16.6 GB RAM / 32 cores), BGE-M3 loads bf16 (−1.1 GB weights,
whole-GPU −0.7 GB observed), PaSST-net bf16 scores match the fp32 baseline to
±0.001 on a reference track, `SAUTIUM_PROFILE=lite` resolves prewarm=none /
analysis-off / trickle / io=4, Discovery semantic search returns sane results
end-to-end, `/api/settings/hardware` serves the UI block.
**Method:** full code audit (4 parallel deep-dives: ML paths, background processes,
DB footprint, launcher/install) + live measurements on the reference rig
(i9-14900HX, RTX 4090 Laptop 16GB, 32GB RAM → WSL2 envelope 15.5GB; library
~41k files / 37k analyzed tracks).

The project was built and optimized on high-end hardware only. This doc maps
where the resources actually go, which degradation mechanisms already exist,
what is broken for weak hardware today, and proposes a hardware-profile layer
plus user-facing minimum/recommended configurations.

---

## 1. Measured resource map (reference rig, 2026-07-10)

### Disk
| Item | Size | Notes |
|---|---|---|
| Docker image `djai-backend` | **24 GB** | CUDA + torch stack; distribution barrier by itself |
| HF model cache (`data/cache`) | **11.3 GB** | NLLB 4.7 + BGE-M3 4.3 + CLAP 1.2 + AST 0.33 + PaSST(torch hub) 0.33 + 2×MiniLM 0.55 |
| Database total | **32 GB** | for ~37k analyzed tracks |
| — of which `mb_*` dump | ~19 GB | **optional layer** (API fallback / P2P slices) |
| — `embedding_segments` | 3.36 GB | 451,693 rows; HNSW index alone 1.18 GB |
| — phantom surface | ~2.5 GB | `tracks` = **3.0M rows** (879 MB) + `album_tracks` 1.13 GB + `albums` 274k rows — phantom minting, not owned files |
| — text stack | ~1.9 GB | text/lyrics/bio embeddings + HNSW |
| Fresh-node download (native launcher) | ~10–11 GB | models 8–9 GB from HF + portable PG ~200 MB + media tools ~100 MB + venv |

### RAM / VRAM (runtime)
| Item | Measured/estimated | Notes |
|---|---|---|
| Backend idle RSS | 709 MB | models live on GPU; on CPU-only they'd sit in RAM instead |
| Postgres RSS | 1.32 GB | stock config (`shared_buffers=128MB` — untuned everywhere) |
| VRAM resident after prewarm | ~~4.3 GB~~ → **2.68 GB measured** (2026-07-11, self-reported "backend VRAM" log line) | CLAP bf16 0.31 + BGE-M3 **bf16 1.14** + NLLB bf16 1.2; allocated == reserved (zero parked cache). Pre-fix state for history: BGE-M3 ran fp32 (2.27 GB) |
| VRAM during analysis | +0.5–2 GB + batch audio | AST+PaSST lazy-load; adaptive budget `device.py:123-150` |
| Whole stack envelope | fits in **15.5 GB** WSL2 VM | i.e. real floor is ~16 GB host for the full profile, not 32 |
| Model cold-load from NTFS | BGE-M3 **2m15s–5m10s** (logged) | startup latency driver; worse on HDD |

### Key model facts
- 5 singletons (`model_cache.py`), **never unloaded**; `InstrumentEnsembleTagger.unload()` exists with zero callers (`ensemble_instruments.py:186`).
- 4 models pre-warmed unconditionally at every boot (`main.py:278-284`), no toggle.
- dtype tiers (`device.py:41-52`): CUDA Ampere+ → bf16, Turing → fp16, MPS → bf16, CPU → fp32.
- **dtype variance is an accepted property, not corruption.** Vectors were
  never bit-deterministic across the network — the tier policy itself gives
  different nodes different dtypes, and GPU inference isn't bit-stable even
  at a fixed dtype. Measured on the reference node (2026-07-10) after the
  BGE-M3 bf16 switch: cos(fp32, bf16) for identical texts mean ≈ 1.000 /
  min 0.998, and top-20 ranking overlap of a bf16 query against the stored
  fp32 corpus = 19–20/20 — i.e. the drift sits 2–3 orders of magnitude
  below inter-candidate ranking gaps. Consequences: (a) NO re-embedding
  after dtype changes — a mixed-precision corpus is fine, and the stored
  fp32 vectors are strictly higher-precision than a bf16 re-run would be;
  (b) any P2P verify-by-recompute (P2P-SYNC-INTEGRITY: "recompute is the
  detector") must compare with a tolerance, never byte-equality.
- **fp32 holes on the bf16 tiers:** BGE-M3 (`text_embedder.py:56-58`) and PaSST
  (`ensemble_instruments.py:178-179`) call `.half()` only on the fp16 tier, so on
  Ampere/MPS they run fp32 → ~+1.5 GB VRAM vs what the tiering intends.
  Ironic consequence: a 6 GB Turing card holds the resident set (~2.5 GB, all fp16)
  while a 4 GB Ampere card OOMs on prewarm (~4.3 GB).

---

## 2. Cost drivers by subsystem

### 2.1 One-time/incremental: audio analysis (the big one)
Per track: ffmpeg full decode to 48kHz mono f32 (**11.5 MB/min RAM**), CLAP over
K segment windows (balanced K = 12/16/24 by duration), librosa amplitude pass
(whole track, CPU), CLAP zero-shot + AST+PaSST windowed instruments (10s window
/ 30s stride, batch 12). Already well-guarded on VRAM: adaptive duration budget
(`device.py:123-150`, env `SAUTIUM_ACCEL_MEMORY_GB`, `SAUTIUM_AUDIO_BUDGET_MIN`),
OOM catch-and-skip, NaN guard, per-batch `empty_cache`.

**Not guarded on CPU:** hardcoded 16-worker I/O pool (`embeddings.py:422`,
comment says "for i9-14900HX"), CLAP batch 16 hardcoded (`audio_analysis.py:460`),
and **no `torch.set_num_threads` anywhere** — torch grabs all cores. On a 4-core
laptop this oversubscribes badly.

Throughput reality (order-of-magnitude, needs a bench command to confirm):
full pipeline on a 30k library ≈ hours–a day on a 4090-class GPU; ~2–4× that on
a 3060-class; ~5–10× on Apple Silicon; **30–100× on CPU (weeks — impractical
beyond ~2–3k tracks)**. Feasibility of local analysis is a function of
`library_size × GPU class`.

### 2.2 Steady-state background load
| Loop | Interval | Toggle today |
|---|---|---|
| HQPlayer status poller (`routers/player.py:335`) | **1 s** always | **none** — runs even with no HQPlayer configured |
| DHT alert pump ×2 (backend `dht_service.py:314`, launcher `p2p/dht_service.py:420`) | **0.5 s** | via `p2p_enabled` only |
| DHT re-announce (∝ enriched artists) | 15 min | announce_limit setting |
| Background enrichment (network-only) | 30 min, 1s cancel ticks | `enrichment.background_enabled` (default **on**) |
| Launcher stats poll / health watchdog / friend resolve | 60 s / 10 s / 15 s | none |
| Model prewarm at boot | every boot | **none** |
| 2 idle psycopg2 LISTEN connections (launcher chat+sync) | permanent | — |

Frontend is clean — SSE-driven, zero `setInterval` polling.

### 2.3 Database
- Stock PG defaults everywhere; the only tuning in the repo is `shm_size: 1gb`
  in the main compose. **`docker-compose.wsl.yml` / `docker-compose.mac.yml`
  lack `shm_size` and use the abandoned `ankane/pgvector:latest` image** — the
  18 GB MB dump load can OOM there (parallel index builds hit /dev/shm).
- All 6 HNSW indexes are `m=16, ef_construction=64` (memory-friendly). Filtered
  KNN overscans with `ef_search` 500–1000 + `iterative_scan=relaxed_order` via
  `db_pool.db_query_with_ef_search`.
- `embedding_segments` ≈ **52 KB/track incl. HNSW** — the dominant local cost.
  Currently not synced (`sync_client.py:451`) — **confirmed architecture gap
  (Valerii, 2026-07-10), fix planned**: segment sync + verify is part of the P2P
  sync refactor tracked in P2P-SYNC-INTEGRITY (segments are the signed entities,
  so syncing them also transports the signatures).
- Optional layers degrade cleanly when empty: `mb_*` (API fallback +
  `mb_slice_client`), phantoms, text-embedding stack.

### 2.4 P2P as the weak-node escape hatch (verified)
A weak node **can** import from peers: `embeddings` (track-level mean vector),
`audio_features`, `analysis_sources` (marked `imported=true`), bios/tags/similar/
stats. Local first-hand/signed analysis always outranks peer copies (Tier-0-lite
guards, `sync_client.py:405-461`). It currently **cannot** import
`embedding_segments` or seals — a **known architecture gap being fixed** (segment
sync + verify, see P2P-SYNC-INTEGRITY). State of the world for an import-only node:
- HNSW similarity, radio, home shelves, Discovery mean-vector channels: **work today**.
- Segment-MAX Discovery channel, future "deepen analysis": unavailable **until
  segment sync ships**, then available on imported data too.
- Sizing note for the fix: importing segments costs ~52 KB/track (≈1.5–2 GB per
  30k imported tracks) plus an HNSW build on the receiving node — with stock
  `maintenance_work_mem=64MB` that build is slow on weak CPUs. Segment import
  should probably be a per-profile default (lite may prefer mean-only to save
  disk), decided within the sync refactor. The Tier-0-lite guard that uses
  "has segments" as the marker for first-hand analysis (`sync_client.py:456-460`)
  also needs a new discriminator once imported rows can carry segments.

### 2.5 Search-time compute
Per text query: encode on whichever of CLAP-text/BGE/NLLB is **already warm** —
cold models are skipped gracefully (block shows `loading`) and `kick_load`ed in
background (`discovery_engine.py:806-816`). Similar-tracks/radio = pure pgvector,
zero inference. This means search degrades *already*; the missing piece is only
a policy for what to warm per profile. On CPU, single-query encodes are 1–3 s —
acceptable.

### 2.6 Output expansion (built-in player, DLNA) — resource & architecture view

Planned outputs: a built-in no-upsampling player and DLNA, alongside HQPlayer.
Resource-wise all of them are ~free (plain FLAC decode is a few % of one core;
DLNA is SSDP multicast + HTTP serving + SOAP; browser playback costs the backend
nothing) — **outputs are not a hardware-tier concern and profiles must not gate
them**. They matter for tiers in two other ways:

1. **They complete the lite tier as a product.** Today playback requires
   HQPlayer — a heavy, paid, enthusiast component. Without it Sautium is a
   library/discovery app with no sound. The built-in player is effectively a
   *prerequisite* for the lite tier, not an optimization.
2. **They multiply stream-enrichment contribution** (§2.7): today only HQPlayer
   owners can stream phantoms at all, because HQP is the only consumer of the
   streaming proxy. The enrichment hook fires on the proxy's *fetch* loop
   (`proxy.py:261` → `on_track_ready`), not on the output — so built-in player,
   DLNA renderers and browser playback all trigger the same enrichment with
   zero extra wiring. Mass-audience outputs = mass-audience network contribution.

Architecture sketch — an output-backend abstraction:
- `PlayerBackend` interface: transport commands, `capabilities()`, and a status
  event stream. The consolidated play tracker (source-agnostic on `track_id`)
  subscribes to the *active* backend's status stream — one tracking point, as
  today, just moved above the abstraction.
- **Status acquisition is owned by the active backend**; no active output = no
  status loop at all. Per backend: HQPlayer = 1 s poll (boundary adapter — the
  control protocol cannot push; documented exception to the no-polling rule);
  built-in player = fully event-driven (we own the engine); DLNA = GENA event
  subscription for state changes + `GetPositionInfo` poll only *while playing*
  (RelTime is not evented — another boundary-forced poll).
- Real refactor costs to plan for: (a) **queue ownership** — HQP owns its
  playlist today; local/DLNA outputs need a Sautium-canonical queue with an HQP
  adapter mirroring into HQPlayer; (b) **browser playback auth** — `<audio>`
  elements can't set HMAC headers (the `auth.js` fetch monkey-patch doesn't
  cover them), so media URLs need signed-URL query params or a scoped whitelist —
  touch the Security Posture rules when this lands; (c) DLNA renderer quirks
  (FLAC support, DLNA.ORG content-features headers) need a capability probe.

### 2.7 Stream enrichment is network contribution — do not shed it first

When a node streams a phantom track, the post-buffer enrichment (CLAP + librosa
+ AST/PaSST on the streamed bytes, tier-3 signed, `origin='deezer'/'youtube'`)
is how the network gains analysis for tracks **nobody owns**. Unlike library
analysis — which a weak node can import from any peer who owns the same files —
streamed-phantom analysis is produced exactly when someone cares enough to play
the track, and it feeds every peer's phantom discovery. Disabling it on weak
nodes (the original lite proposal) would cut the mass audience out of the
contribution loop precisely as outputs expand (§2.6).

The reconciliation is that stream enrichment has a **natural real-time budget**:
it only has to beat 1× listening pace (one track per track-length), not bulk
throughput. Estimated CPU cost of the full per-track pipeline (CLAP K-windows +
zero-shot + AST/PaSST windows + librosa) on a mid 4-core is ~0.5–3 min per
~4-min track — around real time; `sautium bench` should confirm. Hence
**trickle mode** for CPU-only nodes instead of "off":
- keep the existing single-worker `PreviewEnricher`, add a bounded queue with
  drop-on-backlog (idempotency guarantees a dropped track gets another chance
  on its next stream);
- lazy-load the audio models on first stream, **idle-unload after** (finally a
  real caller for `unload()`); on an 8 GB node the ~2–2.5 GB fp32 model set is
  the peak consumer, so it must be transient;
- keep the full pipeline per track (no partial features rows — they'd complicate
  `analysis_version` semantics and signing);
- user-facing off-switch stays (`streaming_preview_analyze`), plus an optional
  "pause on battery" nicety for laptops.

---

## 3. Broken paths that block weak hardware today (fix regardless of profiles)

1. **CPU install path is dead from version drift:** `requirements-torch-cpu.txt`
   pins torch 2.1.2 / transformers 4.37.2 while the backend needs torch 2.12 /
   transformers 4.57.6 / sentence-transformers 5.5.1 / hear21passt≥0.0.26.
2. **Media tools bootstrap has no retry:** ffmpeg/fpcalc/flac install only inside
   the first-run wizard (`wizard.py:1358-1365`); normal startup only PATH-adds
   them (`service_manager.py:309-312`). Failure → silent "0/N enriched". Linux
   never auto-installs them at all (`db_init.py:468`).
3. **WSL/Mac compose regressions:** no `shm_size`, abandoned pgvector image (§2.3).
4. **4 GB Ampere GPUs OOM on prewarm** due to the BGE-M3 fp32 hole (§1).
5. UPnP maps once with a 1 h lease and no renewal loop (`upnp_service.py:19`,
   `p2p_manager.py:259`) — reachability lapses; found during audit, not a
   resource item.
6. Dead config: `audio_analysis_batch_size=8` (`config.py:53`) unreferenced;
   real value 16 hardcoded.

---

## 4. Proposal: hardware profile layer

Selection is **automatic** (Valerii, 2026-07-10: no manual picker — detection
must be good enough on its own; `SAUTIUM_PROFILE` env stays as the diagnostics
override). Auto-detection via `mem_get_info`/psutil at backend startup:
- NVIDIA ≥8 GB or Apple Silicon ≥24 GB unified → **full**
- NVIDIA/Apple Silicon with less, or ≥16 GB RAM CPU-only → **standard**
- else → **lite**

A small resolver (`hardware_profile.py`) expands the profile into effective
flags consumed at the existing seams (most gates already exist — this is mostly
wiring, not new machinery):

| Feature | full | standard | lite |
|---|---|---|---|
| Prewarm at boot | all 4 | CLAP + BGE; NLLB lazy on first Cyrillic query (kick_load path exists) | none — everything lazy |
| Local audio analysis (CLAP+librosa) | on | on, smaller budgets | **off — P2P import only** |
| Instruments (AST+PaSST) | on | on + call `unload()` after each run | off |
| `embedding_segments` (local computation) | balanced K | balanced K | none — no local analysis; imported segments arrive once segment sync ships (import default per profile, see §2.4) |
| Stream enrichment (`streaming_preview_analyze`) | on (GPU, as today) | on (GPU) / trickle (CPU-only) | **trickle** (§2.7) — off only by user switch or below floor |
| Phantom minting (similars / missing albums) | on | capped | off |
| Background enrichment | on | on | manual button |
| Player status loop | owned by the active output backend (§2.6) — no configured output, no loop | | |
| torch threads / I/O pool | default / 16 | cores−2 / min(8,cores) | max(2,cores/2) / min(4,cores) |
| P2P sync + chat + DHT | **on in all profiles** — sync is the lite node's lifeline | | |

The MB dump is **not** in the matrix: it is already an independent opt-in today
(manual load; `musicbrainz.auto_update` default off) and slices arrive via P2P
on every profile. Profiles must not gate what is already optional — the tier
descriptions only note its +19 GB disk / shm requirement, which makes it
realistic on full-tier machines.

Search UX per profile: full/standard = as today; lite = SQL/filter blocks
instant, semantic blocks show `loading` on first use while the encoder cold-loads
(CPU, one-off), segment-MAX block hidden until segment sync lands.

## 5. User-facing configurations

**Minimum ("Lite" — listener node):** x86-64 w/ AVX2, 4 cores / any Apple
Silicon; **8 GB RAM**; **25 GB free SSD** (HDD unsupported — model cold-loads
take minutes even on NTFS SSD); no GPU; broadband (first run downloads ~6–8 GB).
Works: library, player/HQPlayer, metadata enrichment, P2P sync/chat, text +
semantic search (first semantic query warms the encoder for 1–3 min), similarity
/radio over imported embeddings; stream enrichment in CPU trickle mode (§2.7 —
the node still contributes phantom analysis to the network). Not available:
bulk local library analysis (P2P import instead; an explicit opt-in with a
bench-estimated ETA can stay for small libraries); segment search — until
segment sync ships. MB dump technically possible but infeasible at this disk
size (+19 GB) — slices via P2P cover canon needs.

**Recommended ("Standard"):** 6+ cores; **16 GB RAM**; 40 GB SSD; NVIDIA ≥6 GB
(Turing OK — fp16 tier) or Apple Silicon 16 GB. Everything works; analyzing a
large library is an overnight-scale job; instruments optional.

**Full ("Curator"):** 8+ cores; **32 GB RAM** (Mac: 24 GB+ unified); NVIDIA
≥8 GB Ampere+ / M-Pro-class; NVMe with 100 GB+ free (image/venv + models 11 GB
+ DB ~13 GB per 40k tracks + optional 19 GB MB dump). Analysis of a 30k library
in hours–a day; serves MB slices and analysis to peers.

Unsupported: <8 GB RAM, HDD, 32-bit.

Post-bf16 note (2026-07-11): 4 GB NVIDIA cards are now a fully functional
**lite** (the whole resident model set is 2.68 GB, so warm semantic search
and trickle stream-enrichment fit) — they are no longer "unsupported", but
they do NOT get bulk analysis: the audio-batch memory model
(`device.py` `fixed=4.0`, marked provisional) is uncalibrated on small
cards, so the standard-tier VRAM floor deliberately stays at ≥5.5 GB until
`sautium bench` data on such hardware says otherwise.

## 6. Prioritized backlog

**P0 — unblock (SHIPPED 2026-07-10):**
1. ✅ Torch install pinned to the requirements.txt trio; dead
   `requirements-torch-{cpu,gpu}.txt` deleted; index cu124→cu126 (cu124
   stopped at torch 2.6 — fresh Windows installs were silently ending up on
   the CPU wheel); `service_manager` self-heals a CPU build on a CUDA
   machine (`torch.version.cuda` build check); installer command pinned.
2. ✅ `ensure_media_tools()` now runs on every backend start (idempotent-fast
   when present) — the wizard is no longer the only install path.
3. ✅ `docker-compose.wsl.yml`/`.mac.yml`: `shm_size: 1gb` +
   `pgvector/pgvector:pg15`.

**P1 — big wins (SHIPPED 2026-07-10):**
4. ✅ BGE-M3 + PaSST-net → bf16 on half tiers (`text_embedder.py`,
   `ensemble_instruments.py`; PaSST mel stays fp32 — no stft half kernels,
   and scores verified ±0.001 vs baseline). Also fixed en route: `psutil`
   added to backend requirements (image lacked it; resolver has a
   no-psutil fallback until the rebuild).
5. ✅ Hardware profile layer: `backend/hardware_profile.py` — fully automatic
   (auto-detect + `SAUTIUM_PROFILE` env override for diagnostics; the manual
   user_settings picker was built and then REMOVED same day per Valerii's
   call), `/api/settings/hardware` GET (read-only), Library-screen info block.
6. ✅ Prewarm list is profile-driven (full = all 4; standard drops NLLB —
   lazy on first Cyrillic query; lite warms nothing).
7. ✅ `torch.set_num_threads` on CPU device + I/O pool (was hardcoded 16)
   and analysis prefetch sized from profile × cores.
8. ✅ `release_instrument_tagger()` after bulk runs on standard/lite.
9. ✅ `python cli.py bench` — tracks/hour + library ETA + slower-than-realtime
   warning. UI surfacing of the number still TODO.
10. ✅ Trickle mode in `PreviewEnricher`: bounded backlog (1 running +
    1 queued, drop = retry on next stream), 10-min idle unload of AST+PaSST
    (CLAP/BGE stay — shared with search).
11. Segment sync (P2P) — owned by the P2P sync refactor (P2P-SYNC-INTEGRITY
    TODO), not by this doc; unblocks the full search surface for import-only
    nodes (§2.4 sizing note applies).

**P2 — polish:**
12. ✅ Per-output-backend abstraction (§2.6) SHIPPED 2026-07-10:
    `backend/playback/` — `PlayerBackend` (transport + capabilities() +
    status emit), `PlaybackManager` (activation lifecycle, SSE payload +
    `output` field, tracker feed above the abstraction), Sautium-canonical
    `CanonicalQueue` with a one-way HQP mirror (adopt-on-attach restores
    the queue after a backend restart; a 30-tick drift canary logs external
    HQPlayer edits, never reconciles). HQPlayer = 1 s poll (documented
    boundary exception), gated on a configured endpoint as before. The
    built-in player / DLNA / browser backends plug into this next.
13. ✅ Per-tier Postgres tuning (SHIPPED 2026-07-10): compose files run
    `postgres -c` with env-overridable defaults targeting the 16GB+ Docker
    host (`PG_SHARED_BUFFERS:-1GB`, `PG_EFFECTIVE_CACHE_SIZE:-6GB`,
    `PG_MAINTENANCE_WORK_MEM:-512MB`, `PG_WORK_MEM:-16MB`; weak hosts
    override via .env); embedded PG gets RAM-tiered values written by
    `db_init._pg_memory_tier()` (≥24GB / ≥15GB / stock below).
14. ✅ Phantom prune for lite (SHIPPED 2026-07-10): startup background job
    reuses `_reconcile_phantoms(artist, [], spare_analyzed=True)` per
    artist — spares owned rows, streaming mints, and analysis-carrying
    phantoms (the node's own streamed-enrichment contribution). Guarded by
    a **3-consecutive-lite-boots streak** (`hardware.lite_streak`) so a GPU
    node that transiently loses CUDA (driver-update WSL failure mode) never
    auto-nukes 3M re-derivable-but-hours-to-re-mint rows.
15. ✅ UPnP lease renewal (SHIPPED 2026-07-10): `renew_ports()` re-adds each
    mapping at 75% of the 1h lease from a p2p_manager task; falls back to
    full re-discover on failure and pushes a changed external port into the
    DHT announce state.
16. ✅ Dead `audio_analysis_batch_size` removed.
