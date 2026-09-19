# Backup, restore and portable data

> **Status: Phases 1–2 (Products A, B) BUILT 2026-09-13/14; Phase 3 DESIGN.** Origin:
> Valerii's idea 2026-09-13 — make the database backup a Sautium feature,
> split it by data class (MusicBrainz / enrichment / life data), make the
> enrichment part mergeable into another user's database, and protect the
> personal part with a key derived from what the user already has. Where
> the build departed from the sketch below, § "Phase 1 as built" says how
> and why; the sketch is kept as the record of the decision.
> **Relates to:** `P2P-SYNC-INTEGRITY.md` (signed records, the import gate —
> the merge machinery this reuses), `P2P_NETWORK.md` § carry (the selection
> gates), `backend/seed_export.py` / `backend/seed_import.py` (the existing
> file-shaped export), `desktop/node_identity.py` (Argon2id derivation the
> key scheme mirrors), `desktop/db_init.py` (migrations runner the restore
> hands over to).

## Problem

A node is a PostgreSQL database plus a handful of files. Moving to another
machine, surviving a disk failure, or handing curated enrichment to another
collector all need a portable form of it, and today the only tool is a
hand-run `pg_dump`. Three different needs hide behind "backup":

1. **Restore my own node** — byte-faithful, complete, fast. A clone.
2. **Share enrichment with another node** — selective, verifiable, merged
   under the network's provenance rules. Not a clone.
3. **Merge my own life data** across two of my nodes or after a rebuild.

Conflating them produces a format that is bad at all three. They are
three products sharing one file toolkit.

## Non-goals

- Sautium is not a cloud backup client. It writes files; where they go
  (external disk, restic/Kopia, object storage) is the user's choice.
- No backup of the MusicBrainz layer (`mb_*`, 22 GB): it is re-loadable
  from the MusicBrainz dump and is the same on every node.
- No backup of re-creatable state: model cache, transcode cache, TLS
  certificates, the DHT routing table, the gate pool, contact events.
- No "backup into the P2P network" (friends holding encrypted blobs) — a
  different product with a different abuse surface.

## Data classes

Measured on the master, 2026-09-13: database 33 GB, of which `mb_*` 22 GB;
own data ≈ 11 GB live, ≈ 3 GB as a compressed dump.

| Class | Tables / files | Backup | Share | Merge own |
|---|---|---|---|---|
| **MusicBrainz layer** | `mb_*` | no (re-load) | no | no |
| **Catalog** (identity graph, owned + phantom) | `artists`, `albums`, `tracks`, `album_tracks`, `track_artists`, `album_artists`, `artist_mbids`, `genres`, `tags`, `embedding_models` | yes | yes — the structural rows the records hang off, in FK order (`seed_export.structural_sections`; `tags` and `embedding_models` are minted by the import gate, not carried) | — |
| **Catalog, node-local** | `media_files` (this node's files and their cue bounds), `album_variants`, `artist_name_aliases`, `artist_members`, `seed_picks` | yes | no — nothing here means anything on another node | — |
| **Analysis** (sealed, travels) | `embedding_segments`, `embeddings`, `analysis_sources`, `signing_batches`, `audio_features`, `track_mbids` | yes | **yes — sealed records only**, own and received alike, each under its author's seal (decided 2026-09-14; the sketch said first-hand only). Analysis travels as segments with their provenance and batch map; the track-level mean is derived by the importer | — |
| **Last.fm layer** (node-local) | `artist_bios`, `artist_tags`, `similar_artists`, `track_stats`, `genre_descriptions` | yes | no — Last.fm's API terms do not allow redistribution: out of the protocol, the share file (format v2) and the seed bundle since 2026-09-19; every node fetches its own by name | — |
| **Album-grain enrichment** | `album_genres`, `album_descriptions` — outside the sync contour (albums never sync by UUID), but structural rows in a file, where albums do travel under their seal | yes | yes | — |
| **Text vectors** (derived) | `text_embeddings`, `lyrics_embeddings`, `artist_bio_embeddings`, `genre_desc_embeddings` | yes | no — a BGE-M3 vector is a deterministic function of text the file already carries and of the local metadata composed around it; every node encodes its own in background enrichment, on every profile (`lite` on the CPU, smaller slices) | — |
| **Local ledgers** | `external_metadata` (which source was asked for what and whether it answered — `not_found` included, so a step never re-asks), `covers` (fetched art) | yes | no — a record of this node's own fetches | — |
| **Lyrics** | `track_lyrics` | yes | no — the one category that is verbatim copyrighted text; out of the sync protocol since 2026-07-11, every node fetches its own from the public sources | — |
| **Life data** (personal) | `listening_history`, `demo_plays`, `listening_sessions`, `session_tracks`, `local_play_stats`, `friends`, `friend_rights`, `friend_grants`, `friend_grant_rights`, `invite_tokens`, `invite_token_rights`, `sent_invites`, `p2p_messages`, `chat_sessions`, `chat_messages`, `user_profile`, `user_gear`, `gear_pair_notes`, `pending_key_rotations`, `p2p_identities`, `p2p_node_bans`, `support_*`, `diag_*` | yes, encrypted | no | **yes, keyed dedup** (§ Phase 3) |
| **Node settings** | `user_settings` | yes (in the dump) | no | allowlist only |
| **Runtime, re-creatable** | `p2p_gate_pool`, `p2p_contact_events`, `p2p_action_costs`, `p2p_dht_state`, `p2p_nodes_seen`, `external_api_cooldown`, `_gap`, `_schema_migrations` (travels with the dump, see restore) | in the dump, harmless | no | no |
| **Files** | identity dir: `info.json`, `birth_certificate.json`, `identity_proof.json`, `previous/` (rotation archive), `.api_secret` | yes, encrypted | no | — |
| **Files, elsewhere** | `.env`, MCP config, launcher `config.json`, a provider plugin's `secrets.json` | **not here** — the maintainer's private repo / the user's own secret store | no | no |

The private Ed25519 seed is never written to a backup: it derives from
username + password (`node_identity.derive_seed`), which the restore asks
for anyway. `.api_secret` (random, the device-token key) is included so
paired browsers keep working after a restore; a restore may also mint a new
one, which only logs every browser out.

---

## Product A — node backup and restore (Phase 1)

### The file

`sautium-backup-<node-short>-<YYYY-MM-DD>.sbk` — one encrypted container:

```
header (plaintext, authenticated as AAD)
  magic        "SAUTIUM-BACKUP\0" + format version u16 (1)
  kdf          {"alg":"argon2id","t":4,"m":262144,"p":2,"salt":"sautium-backup:v1:<username>"}
  wrapped_key  SecretBox(nonce_random, KEK, data_key)      # 32-byte data key
  chunk_size   u32 (16 MiB)
  node         pubkey hex, username (for the "wrong account" message)
payload (encrypted stream)
  chunk i      SecretBox(nonce = prefix16 || counter64(i), data_key, plaintext_i)
plaintext inside the stream = a tar:
  manifest.json   format_version, created_at, app commit, applied migrations
                  (from _schema_migrations), node pubkey, table row counts,
                  sha256 of every member
  db.dump         pg_dump -Fc --exclude-table-data='mb_*' music_ai
  identity/       the identity dir minus nothing (see above)
```

- **KEK = Argon2id(password, salt = `"sautium-backup:v1:" + username`)** with
  the identity parameters (`ARGON2_TIME_COST=4`, `MEMORY_COST=256 MiB`,
  `PARALLELISM=2`). The salt prefix differs from the identity seed's
  (`"<username>:sautium"`), so a backup key can never be turned into the
  node key and vice versa. Same password, two unrelated keys.
- **Envelope, not direct encryption.** The payload is encrypted under a
  random per-file `data_key`; the KEK only wraps that key. A password
  change re-wraps 72 bytes instead of re-encrypting 3 GB, and a second
  recipient (a printed recovery key, later) is one more wrapped copy in
  the header. The key is deterministic; the ciphertext never is.
- **Chunked SecretBox** (PyNaCl, already in the stack: `nacl.secret.SecretBox`,
  XSalsa20-Poly1305) so a 3 GB dump streams through a fixed 16 MiB buffer;
  the nonce is a random 16-byte prefix plus the chunk counter — unique by
  construction under a per-file key. Per-chunk Poly1305 plus the manifest's
  sha256 list give integrity; the header is authenticated as associated
  data of chunk 0 (a tampered KDF block fails before any password is
  tried on it).
- `--exclude-table-data='mb_*'`, not `-T 'mb_*'`: the schema of the MB
  tables rides along empty, so a restored database is complete and the
  MB loader fills them later; `-T` would leave the tables missing after the
  migrations runner skips the already-recorded `001`.
- The failure domain is deliberately the account password: losing it loses
  the identity anyway, so the backup adds no new thing to remember.

### Creating a backup (backend job)

_Superseded 2026-09-14: there is no Web UI surface — the backup is a
launcher function and a CLI, see § "Phase 1 as built". The sketch stays as
the record of the job's policy, which is unchanged._

`POST /api/settings/backup` `{password}` → the job:

1. Verify the password derives this node's pubkey (`derive_account_identity`
   vs `get_node_id`); a wrong password is refused before any work. The
   KDF runs under the same semaphore as device-auth login (256 MiB per
   attempt, one at a time).
2. Check free space in the target directory ≥ 2 × the last dump size
   (or 3 GB when unknown).
3. Stream `pg_dump` stdout → tar writer → chunk encryptor → file; never
   a plaintext temp file. `pg_dump` comes from `PG_BIN` (launcher passes
   `pgsql/bin`; the Docker image gains `postgresql-client-18`).
4. Progress = bytes encrypted, pushed as `backup.progress` events on the
   existing library SSE channel (`/api/settings/library/stream`), then
   `backup.done {path, size, sha256}` or `backup.failed {error}`. No
   polling anywhere: the writer loop is the event source.
5. Output: `<data_dir>/backup/` (launcher) or `./data/backup/` (Docker bind
   mount). The UI lists what is there (`GET /api/settings/backup`), shows
   the path, offers "reveal in folder" on the launcher. Retention is the
   user's (their storage, their restic).

The password lives in process memory for the KDF only; nothing is stored.
Playback is not blocked: `pg_dump` takes a consistent snapshot; the job
runs at below-normal priority and yields to the load meter like the miner.

### Restoring

Restore is a **launcher / CLI** operation with the backend stopped, because
it replaces the database the backend is serving:

- Launcher: Settings & Tools › Backup & Restore › "Restore from backup…" and the wizard's
  first-run screen ("Restore from a backup" beside "Create account").
- Docker: `python -m backup restore <file>` inside the backend container
  with the app stopped (`docker compose stop backend`), or on the host
  against the published Postgres port.

Steps: ask password → derive KEK, unwrap, read manifest → refuse when the
manifest's newest migration is newer than the code's (`update first`) →
compare manifest pubkey with the identity the password derives (mismatch =
another account; the user may still restore *data only*) → create an empty
database (`music_ai`, or a fresh name then swap) → `pg_restore -Fc
--no-owner --exit-on-error` → `db_init.apply_migrations` for deltas newer
than the dump → write the identity dir (the restore asks whether this
machine becomes the node: **identity is moved, never duplicated** — two
live nodes with one key confuse the DHT, relays and the support desk) →
start the backend → `_schema_migrations` and `identity_rule_v*` markers
are inside the dump, so the data-migration ledger is consistent.

### Verification (integration, real database)

`python -m backup --selftest`: dump this node → restore into `music_ai_test`
→ compare row counts of every non-`mb_*` table and the sha256 of the
manifest → drop the test database. Pure-logic tests
(`tests/test_backup_format.py`): KDF vector, chunk round-trip, truncated
stream, flipped header byte, wrong password — each must fail closed.

---

## Product B — share export / import (Phase 2)

"A dump another user can merge" already exists as a wire format: the sync
payload. The seed bundle (`backend/seed_export.py`, `backend/seed_import.py`)
is its file form — structural rows plus sealed analysis for a pick list,
imported through `desktop.sync_client.import_pushed`, the same
seal-verifying gate carry uses. Phase 2 generalises the pick list; it does
not invent a format.

- **Selection** = the carry gates: owned files or engaged artists
  (completed listens), or an explicit list of artists / albums from the UI.
  Never the whole phantom layer (3M tracks).
- **Content** = what `build_bundle` produces: structural rows the records
  need, then first-hand sealed records per sync category. Imported P2P
  rows are excluded (`_SIGNABLE_SRC`), local-only tables are excluded by
  design.
- **File** = `sautium-export-<date>.json.gz` (gzip JSON, the seed format)
  plus a detached signature by the node key over its sha256, so a recipient
  knows who packed it — the records inside carry their own seals anyway.
  Not encrypted: it is the data the node serves openly.
- **Import** = per category through `import_pushed`, then the post-import
  classifiers (`_update_artist_gender`, `_update_artist_is_vocalist`) on
  the touched rows — identical to a carry push. First-hand rows on the
  receiving node are never overwritten; the source label is
  `p2p:<sender pubkey>`. Size guard: refuse a file above the carry budget
  unless the user confirms.
- UI: Settings › Library › "Export for sharing…" (pick scope) and
  "Import enrichment…"; both report through the same SSE events.

Side benefits: offline transfer between two of one's own nodes, and
distribution of curated bundles (the seed bundle becomes one instance).

---

### Phase 2 as built (2026-09-14)

`backend/share.py`, the `export` / `import` subcommands of `python -m
backup`, the "Share enrichment with friends" section of the launcher's Backup & Restore tab
(desktop/backup_task.CliRun.export / plan_import / apply_import — the
launcher runs the CLI, as for backups). Departures from the sketch above:

- **JSON lines, not one JSON document.** An "everything I own" export of
  the master is 36.6k analysed tracks × ~46 KB ≈ 1.7 GB of segment
  bundles; a single document would have to be held whole on both ends.
  The file is gzip'd lines — header, structural sections in FK order
  (`batches` first, ≤500 rows per line), one line per pull-handler
  envelope (≤500 entities), a summary, then a trailer `{"end", "sha256",
  "signature"}`: the exporter's node key over the running digest of every
  byte above. **The signature is inside the file**, not detached: one
  thing to hand over, the same guarantee (who packed it, nothing cut or
  edited). Both ends stream; memory is one chunk.
- **Two passes on import.** A streamed import can take nothing back, so
  pass one reads the whole file — hash, trailer, header (format, version,
  identity rule), summary — and only a file that passes is applied in pass
  two. The launcher shows pass one's summary and the carry budget before
  asking; `import --dry-run` is the same step on the CLI.
- **One builder for the seed and the export.** `seed_export.structural_
  sections` / `envelope_chunks` are generators now; `build_bundle` (the
  seed) collects them into its dict, `share.export_file` streams them.
  Verified byte-identical against the previous code on the master.
  Structural means the album grain too: `album_genres` and
  `album_descriptions` ride in a file although they stay out of the sync
  contour — on the wire albums never travel by UUID, in a file they do,
  under their seal.
- **Full export, not first-hand only.** The sketch restricted the file to
  this node's own observations; Valerii's call (2026-09-14): "what I know"
  is more than "what I analysed" — the file carries every sealed record
  the node holds, own and received, each under its author's seal, exactly
  what the node serves on the network. The pull handlers are untouched.
  "Every sealed record" is the seed bundle's three analysis categories —
  `segments` / `audio_features` / `track_mbids` per track
  (`seed_export.ANALYSIS_CATEGORIES`). Until 2026-09-19 the file also
  carried `artist_bios` / `artist_tags` / `similar_artists` per artist;
  that layer is node-local now (Last.fm's terms), and the format moved to
  v2 so a file that carried it is refused rather than half-read.
- **Two import modes.** Default: add what the file names — new artists,
  albums and tracks land as phantoms so their records attach (like the
  seed and a carry push). `--existing-only` (launcher: "Enrich only what I
  already have"): no artist, album or track row is created; link rows and
  records land only where every entity they reference already exists, the rest is
  dropped (`share.keep_existing`). The plan reports how much of the file
  is already here (`existing` / `named` per table), and with the streaming
  library switched off only this mode is offered.
- **Scope.** `--scope analysed` (every album with at least one track
  carrying sealed audio analysis here — own, seeded or synced: a
  streaming-only node owns nothing and may have listened to nothing, yet
  holds the seed's picks and what the network gave it, and that IS its
  enrichment — Valerii, 2026-09-14), `--scope engaged` (albums owned or
  with a completed listen — the carry gate; the CLI default), `--scope
  owned`, `--artist NAME` (name, uuid or a Latin alias; repeatable),
  `--album UUID`. Never the minted catalog itself. The launcher's dialog
  runs `export --plan` first and shows each scope's album count; an empty
  scope cannot be picked.
- **Its own folder.** Exports land in `EXPORT_DIR` — `./data/export` on
  Docker (bind mount), `<data_dir>/export` under the launcher — not beside
  the backups: a backup is for this node's return, an export is a file
  handed to someone else, and the weekly task prunes the backup folder by
  age. Each row in the launcher has a "Folder" button.
- **Import = the gate.** Structural sections through
  `seed_import.insert_structural` (ON CONFLICT DO NOTHING — a node keeps
  its own rows), envelopes through `SyncClient._import_items` (seal
  verification, first-hand precedence — a receiving node's own records
  are never overwritten). Provenance is what the seals say: `imported`
  rows under the author's pubkey. Above the carry budget
  (`sync.carry_limit`) only with `--yes` / the launcher's confirm.

Acceptance run 2026-09-14: three artists exported from the Docker master
(55 albums, 447 tracks, 373 analysed, 12.5 MB) and imported on the
launcher stand through the gate — every album, track, artist, feature,
segment bundle and track_mbid present afterwards; a copy with one bio
record altered and the file re-signed with the exporter's own key passed
the outer check and lost exactly that record at the gate (16 of 17 bios);
the clean file then landed the 17th; a second import of the clean file
changed no row count.

## Product C — merge my own life data (Phase 3)

For the "two nodes, one person" case (laptop + desktop, or an old backup
after a rebuild): union, keyed so that a second import changes nothing.

| Table | Key | Rule |
|---|---|---|
| `listening_history` | `(track_id, started_at)` | insert missing rows only; the other machine's `media_file_id` becomes this machine's file for the track, or NULL |
| `local_play_stats` | — | **recompute** from history for every track the merge touched, never merge counters |
| `demo_plays` | `track_id` | the one full demo stream of a track is spent on whichever machine heard it first: insert missing, and an earlier `played_at` replaces a later one |
| `listening_sessions`, `session_tracks` | session uuid | insert missing **closed** sessions whose tracks are all known here (an open one is the other machine's live queue; a partial card would never complete) |
| `friends`, `friend_rights`, `friend_grants` | friend pubkey | insert missing; rights = union; blocked on either side = blocked; a friend who rotated is one row under the newer key; a `pending:<invite>` row binds when the other side has the key |
| `p2p_messages` | `message_uuid` | insert missing (already the dedup key) |
| `chat_sessions`, `chat_messages` | session `created_at`, message `(created_at, role)` | insert missing — the tables have no uuid; the agent session ids (`claude_session_id`, `codex_thread_id`) stay behind, they name a session on the other machine |
| `invite_tokens`, `sent_invites` | token uuid, `(email, sent_at)` | insert missing; revoked on either side = revoked (tokens are parents of `friends.source_token_id`) |
| `gear_brands`, `gear_models`, `user_gear`, `gear_pair_notes` | deterministic ids | insert missing (the catalogue rows the chain needs come along; a model already here keeps its own research) |
| `user_profile` | the one row | fields empty here fill in |
| `p2p_identities`, `p2p_node_bans` | pubkey, `(pubkey, addr)` | insert missing; a ban on either side is a ban |
| `user_settings` | key | **allowlist only** (`life_merge.SETTINGS_ALLOWLIST`: phantom layer, sync switches and budgets, gate mode, enrichment switches, scrobbling, album sort, language, diagnostics); a key set here wins; never machine state or machine-specific keys (`hqplayer.host`, ports, paths, `sync.last_at`, secrets) |
| identity `previous/` | pubkey | union of rotation records (the notice, certificate and proof of each retired identity the backup lists and this node does not) |

Input = a Product-A file of one's own (same account: the password unwraps
it, the manifest pubkey matches or is a `previous/` key). Enrichment inside
such a file is not merged this way — it goes through Product B's gate like
anyone else's.

### Phase 3 as built (2026-09-14)

`backend/life_merge.py`, `python -m backup merge <file.sbk> [--dry-run]`,
and "Merge from backup…" in the launcher's Backup & Restore tab (the same
CLI, `desktop/backup_task.CliRun.merge`). Departures from the sketch, each
for a reason found while building:

- **Sessions are not derivable from history.** `listening_history` has no
  session column; a session is a queue snapshot with an origin, a card and
  positions. So sessions merge by their uuid like everything else, and only
  `local_play_stats` is recomputed. Recomputing is also the *right* thing:
  measured on the master, the incrementally kept counters had drifted from
  the history they summarise (567 of 2,205 tracks carried a skip's seconds
  as listening time — the first row for a track being a skip landed in the
  INSERT branch of the upsert). The tracker now runs the same statement
  (`backend/play_stats.PLAY_STATS_SQL`) after every listen, and a one-time
  startup step (`db_migrate`, marker `play_stats_derived_v1`) re-derives
  the rows every node already has, so the stats are a function of the
  history everywhere, and two histories merged in either order give one
  answer.
- **A scratch schema inside the live database, no scratch database.**
  PostgreSQL cannot query across databases, and a plaintext dump on disk
  is what the format forbids. The dump member streams once through
  `pg_restore --data-only -t <life tables> -f -` — pg_restore selects the
  twenty tables out of a non-seekable stream in one pass (3.3 GB in ~20 s
  on the master) — and the `COPY … FROM stdin` blocks of the script it
  emits are parsed and COPY'd into `_merge.<table>` (`LIKE public.<table>
  INCLUDING DEFAULTS`, dump-only columns added as text). The keyed inserts
  run in the same transaction; `--dry-run` is that transaction rolled back,
  reporting the same counts. A crash leaves nothing behind: the schema was
  never committed.
- **Unknown tracks wait.** A listen or a session naming a track this node
  has never seen (a phantom listened to on the other machine) is neither
  inserted under a foreign FK nor invented structurally — the merge reports
  it as waiting, and the next merge of the same file lands it once the
  track has arrived (sync, or a share import). Keyed idempotence makes
  "merge again later" free.
- **Concurrency.** The backend keeps serving: the merge only adds rows and
  recomputes stats from history, so a listen recorded during the merge is
  not lost and not double counted — both writers derive, neither
  increments.

Acceptance run 2026-09-14 (`tests/test_life_merge.py`, two databases built
from the migrations on the container's cluster): clones with divergent
histories, sessions, friends, messages, chats, gear, settings and identity
rows merged to the union in either order; `local_play_stats` identical on
both for every shared track and equal to what one history gives; the open
session and the session naming an unknown track stayed out (reported as
waiting); the machine-specific settings stayed out; a second merge of the
same file added nothing; another identity's file was refused before a row
was read. A dry run of the master's own latest backup into the master
loaded 3,894 listens, 657 sessions, 5,729 session tracks and the rest in
~20 s and found nothing new.

## Phase 1 as built (2026-09-13)

The format and the drivers live in **`desktop/node_backup.py`** — shared by
the launcher (its own process restores) and the backend (which imports it
the way `db_migrate` imports `db_init`), not in `backend/backup.py` as
sketched; that module is the backend binding (job, SSE, CLI). Departures
from the sketch above, each for a reason found while building:

- **AEAD with associated data, not SecretBox.** The header must be
  authenticated as AAD, and `nacl.secret.SecretBox` has no AAD slot. The
  chunks use libsodium's XChaCha20-Poly1305 (`nacl.bindings.
  crypto_aead_xchacha20poly1305_ietf_*`, 24-byte nonce = 16-byte random
  prefix ‖ u64 counter) with the sha256 of the header bytes as AAD on
  **every** chunk, so an edited header fails the first chunk. The wrapped
  data key is the same AEAD under the KEK with the identity fields
  (username, pubkey) as AAD. KDF parameters are pinned: a header asking for
  more memory is refused before any derivation runs (a crafted file cannot
  turn "try the password" into a 16 GiB allocation).
- **Framed members, not a tar.** A tar header carries the member size up
  front; pg_dump's size is unknown until it exits, and spooling it would be
  the plaintext temp file the design forbids. Each chunk's plaintext is one
  record — `MEMBER_START {name}`, `DATA`, `MEMBER_END {size, sha256}`,
  `END` — so the stream is written and read strictly sequentially, member
  digests are verified as they pass, and a file cut short fails as
  "truncated" because `END` is authenticated plaintext the reader insists
  on. Member order: `manifest.json`, `db.dump`, `identity/…`.
- **One snapshot for counts and dump.** The manifest's per-table row counts
  are taken in a `REPEATABLE READ` transaction that exports its snapshot
  (`pg_export_snapshot`), and `pg_dump --snapshot=<id>` dumps that same
  snapshot — so a restore is checked against the counts exactly, on a node
  that keeps writing (listens, sync imports) throughout.
- **`mb_*` data is excluded whole** (`--exclude-table-data=mb_*`), the
  slice cache included: MusicBrainz facts are network-replicated, the
  loader / slice fetch refill them. The manifest records which tables were
  excluded, the server version, the extensions and the database's locale
  settings, and the restore recreates the database with them (falling back
  to the cluster default when the OS lacks the locale — a Docker
  `en_US.utf8` dump on Windows; indexes are rebuilt by the restore anyway).
- **Identity: the seed is never in the file, at any depth.**
  `node_ed25519.key` / `.pub` and the TLS pair are excluded from the live
  dir *and* from `previous/` archives; the live key is re-derived at
  restore from username + password and written only when it reproduces the
  recorded public key. A rotation archive therefore loses its private keys
  in a restore (messages to a retired key can no longer be opened) — the
  accepted v1 limit of "the seed never leaves". `.api_secret` rides along
  so paired browsers keep working.
- **Restore is staged and reversible.** The dump lands in
  `<db>__restore`; only a complete, migrated restore is swapped in. A
  database holding own data (owned files, listens, friends, chat, gear) is
  replaced only on an explicit confirm / `--replace` and is kept as
  `<db>__previous` (one copy; the next restore drops the older one); a
  fresh database (schema, seed, settings only) is dropped. Other sessions
  on the target are terminated at the swap; the backend must be stopped
  first (the CLI refuses when it sees them, `--yes` overrides). The
  restore runs as the app role with `--no-owner --no-privileges
  --no-comments --exit-on-error`, extensions pre-created by the admin role
  (the launcher's `sautium` is not a superuser; `COMMENT ON EXTENSION`
  would otherwise abort it). Newer migrations apply via `db_init.
  apply_migrations` before the swap; a dump from **newer** code (an
  unknown `NNN_*.sql`, or a higher `identity_rule_v*`) is refused with
  "update first".
- **Identity move.** `write_identity` moves a *different* live identity to
  `replaced-<time>/` (never deletes), overwrites the same key's files in
  place. A backup from an env-credential Docker node carries no
  `node_info.json`; the launcher then runs `create_account` with the pair
  that opened the file — the same key.
- **Docker → launcher (and back) works on the same account.** The KEK is
  username + password, so the file opens on any machine that knows the
  pair; the launcher's wizard ("Restore from a backup…") builds the fresh
  cluster, restores the dump under its own database name and owner, writes
  the identity documents and derives the key — the new node IS the old one
  (stop the old node first: two live nodes on one key confuse the DHT, the
  relays and the support desk). What travels: the whole catalog, the
  sealed enrichment, listens, friends, chat, settings. What does not: the
  `mb_*` layer (re-load the dump or let slices fill it), machine-specific
  settings (`hqplayer.host` is `host.docker.internal` on Docker — re-pick
  the output), and file paths are the native ones the Docker node stored
  (`E:/Music/...`), so the same machine finds its files at once and another
  machine needs a rescan. The database is created with the dump's locale
  when the OS has it, else with the replaced database's (the launcher's
  ICU `und`), else the cluster default.
- **Playback wins, across processes.** pg_dump runs at below-normal
  priority and the writer loop pauses while the node plays. Since
  2026-09-14 the job never runs inside the backend, so the signal crosses
  through PostgreSQL: the backend holds a session advisory lock
  (`backup.PLAYBACK_LOCK_KEY`, `PlaybackSignal`, reconciled on every load
  meter sample) while its meter reports playback, and the job's dedicated
  session waits on it between reads (`PlaybackHold`) — `pg_advisory_lock`
  blocks in the server until the holder releases, an event rather than a
  poll, and a backend that dies takes the lock with it, so no stale flag
  can ever hang a backup. A cancel aborts a blocked wait by cancelling the
  statement. pg_dump keeps its snapshot across the pause.
- **Launcher and CLI only — no Web UI (2026-09-14).** A browser cannot
  receive a 3 GB file and the password that keys it belongs where the file
  lands, so the Settings › Library card and `/api/settings/backup*` were
  removed. The launcher's Settings & Tools › Backup & Restore › "Create backup…" runs
  the very CLI a Docker node's weekly task runs (`desktop/backup_task.py`
  spawns `python -m backup create --password-env SAUTIUM_BACKUP_PASSWORD
  --progress-json --cancel-on-stdin` on the backend interpreter with
  `service_manager.backend_env()`), shows its JSON events in the action's
  own row — the button that started the job turns into its red Cancel, a
  progress line and a thin bar appear beneath the section and the result
  line stays (the launcher's scan button is the pattern; the window stays
  open, the launcher's progress line mirrors it) and cancels through the
  child's stdin — a quit mid-backup cancels too, so no half file is left.
  Backup, export and import are independent jobs and may overlap (pg_dump
  reads a snapshot, the export reads, the import writes through the gate);
  only a restore needs the field clear, and its button waits for them. The child never spawns git: `updater.current_commit`
  reads `.git/HEAD` by hand, after a launcher-spawned CLI hung forever in
  `git rev-parse` on Windows (git's own child held the pipes past
  `run()`'s timeout). `python -m backup` itself runs on either
  interpreter: under the launcher it loads `<data_dir>/backend.env` into
  its environment before `config` builds Settings (`_bootstrap_launcher_env`),
  so the DSN, identity dir, `BACKUP_DIR=<data_dir>/backup` and
  `PG_BIN=pgsql/bin` are the backend's own; in launcher mode `pg_target()`
  adds the cluster's `postgres` role as the admin a restore needs. The
  embedded Windows interpreter (`python312/`) ignores the current directory
  and PYTHONPATH (`python312._pth`), so the launcher provisions
  `Lib/site-packages/sautium-project.pth` with `backend/` and the project
  root (`python_env.ensure_project_pth`, every start) — which is also what
  makes the backend's own `desktop.*` imports explicit there instead of a
  side effect of `routers/sync.py`.
- **The weekly task's contract** (sautium-private/scripts/backup.sh):
  `docker exec sautium-backend python -m backup create --password-env
  P2P_PASSWORD`, the password from the container's environment, the file
  under `/app/data/backup` (bind mount `./data/backup`) with the `.sbk`
  suffix, a non-zero exit on any failure, and a terminal counter whose
  `counting…` / `dumping…` / `identity…` lines the script's log filter
  strips. Changing any of it means changing that script.
- **Docker needs the PG 18 client.** jammy's `postgresql-client` is 14 and
  refuses an 18 server; both Dockerfiles install `postgresql-client-18`
  from PGDG and pin `PG_BIN=/usr/lib/postgresql/18/bin`. The launcher
  passes its `pgsql/bin` (or Homebrew's) as `PG_BIN` in `backend.env`, and
  `BACKUP_DIR=<data_dir>/backup`; Docker mounts `./data/backup`.
- **Anonymous identities cannot back up**: the key is the account password
  and a minted one was never seen. The Settings card says so and points at
  Profile.

Entry points: launcher Settings & Tools › Backup & Restore › "Create backup…" and
"Restore from backup…", the wizard's identity step ("Restore from a
backup…", the restore runs after `full_init`); `python -m backup
create|inspect|restore|selftest` in the container (`docker compose run
--rm --no-deps backend python -m backup restore /app/data/backup/<file>
[--db music_ai_test] [--replace] [--identity]`) or on the launcher's
interpreter (`python312\python.exe -m backup …` from `backend/`).
Tests: `tests/test_node_backup.py` (17 cases: KDF domain vector, round trip,
manifest-first, wrong password, pinned KDF parameters, edited header,
flipped byte, truncation at four points, reorder / duplicate / drop,
trailing bytes, compatibility refusals, identity file selection, identity
write with archive) and `tests/test_backup_cli.py` (the JSON event lines,
the terminal counter's words, stdin cancel, the env-file bootstrap, the
launcher helpers); the database half is `python -m backup selftest`.

## Code layout

- `desktop/node_backup.py` — format v1: KDF, envelope, chunked AEAD,
  member framing (writer / reader), `pg_dump` / `pg_restore` drivers,
  `restore_database`, `write_identity`, `selftest`.
- `backend/backup.py` — the node binding and the ONE "make a backup": the
  `python -m backup` CLI (`create`, `inspect`, `restore`, `selftest`,
  `export`, `import`, `merge`) on
  either interpreter (backend.env bootstrap under the launcher), password
  verification through `device_auth.verify_password`, the playback signal
  (`PlaybackSignal` held by the backend, `PlaybackHold` waited on by the
  job), `--progress-json` / `--cancel-on-stdin` for the launcher.
- `desktop/backup_task.py` — the launcher's "Create backup…", "Merge from
  backup…", "Export enrichment…", "Import enrichment…": runs that CLI on
  the backend interpreter with `service_manager.backend_env()`, streams its
  events into the action's row, cancels via stdin; `latest_backup()` for
  the status line.
- `desktop/restore.py` — launcher restore flow (`restore_launcher_node`:
  database, migrations, identity) and the `RestoreDialog` used by
  Settings & Tools › Backup & Restore and the wizard; `desktop/launcher.py` stops and
  restarts the services around it.
- `desktop/settings.py` — the Settings & Tools dialog: General (ports) and
  Backup & Restore (node backup status line, "Create backup…" / "Cancel
  backup", "Restore from backup…"; "Merge from backup…"; the share export /
  import; the identity certificate transfer), `PasswordDialog`,
  `ExportDialog`.
- `backend/main.py` — wires `PlaybackSignal` to the load meter's samples.
  No router, no Web UI.
- `backend/share.py` — Product B: the JSON-lines file (writer, reader,
  signed trailer), scope selection, `export_file`, `plan_import`,
  `apply_import` (default / `existing_only`); `python -m backup
  export|import [--existing-only]` in backend/backup.py.
- `backend/seed_export.py` / `seed_import.py` — the shared section and
  envelope generators and the structural importer; the seed bundle is one
  caller of them.
- `backend/life_merge.py` — Product C: the same-account rule, the
  pg_restore script → scratch schema loader, the keyed union
  (`merge_life`), the rotation-archive union, `merge_backup`; `python -m
  backup merge [--dry-run]` in backend/backup.py.
- `backend/play_stats.py` — `local_play_stats` as a function of
  `listening_history`: the one statement the tracker and the merge share.
- Docker image: `postgresql-client-18` from PGDG, `PG_BIN` pinned; launcher:
  `pgsql/bin` on `PG_BIN`, `BACKUP_DIR=<data_dir>/backup` in `backend.env`.

## Phases and acceptance

1. **Backup + restore.** Done when a backup of the master restores into a
   fresh `music_ai_test` with identical row counts for every non-`mb_*`
   table, the node starts on it, the identity matches, newer migrations
   apply, and the format tests fail closed on tamper / wrong password.
   **Built and verified 2026-09-13** — `python -m backup selftest` on the
   master: dump 3.30 GB (11 GB live, 78 own tables) in ~5 min, restore into
   `music_ai_test` in 418 s (the HNSW index build is most of it), 78/78 row
   counts identical, `db_migrate.apply_pending()` on the restored database a
   no-op, 313 indexes like the live one. Re-run after the 2026-09-14
   refactor (launcher + CLI only): dump 3,300 MB in 293 s, restore 413 s,
   78/78 again; the same CLI on the launcher's python312 with
   `pgsql\bin\pg_dump.exe` wrote the 3.3 GB file against the Docker
   database in ~7 min, and the weekly `backup.sh` ran end to end. Tk flows
   (launcher Settings & Tools › Backup & Restore, wizard) are written, not yet run on
   the stand.
2. **Share export + import.** Done when an export from the Docker node
   imports on the launcher stand through the gate with the expected
   counts, a re-import changes nothing, and a tampered record is refused.
   **Built and verified 2026-09-14** — see § "Phase 2 as built".
3. **Own life-data merge.** Done when two clones with divergent listening
   histories merge to the union in either order, and `local_play_stats`
   recomputes to the same numbers as a single history would give.
   **Built and verified 2026-09-14** — see § "Phase 3 as built".

## Open questions

- Scheduling: manual button first. A "backup is older than N days" reminder
  in the Settings card is cheap; automatic backups to a folder are a
  later switch.
- Recovery key: a second wrapped copy of the data key under a printed
  random key, for users who would rather not tie backups to the account
  password. Format v1 leaves room (a list of wrapped keys); UI later.
- `covers` (471 MB of art in the database) stays in the dump for now;
  moving art to files is a separate question.
- Restore into a node that already has data: v1 refuses (fresh database
  only); merge is Product C (built). A merge from a *different* account's
  backup (the same person under two names without a rotation record) is
  refused today; a "listens and preferences only" mode for that case is
  a possible follow-up.
