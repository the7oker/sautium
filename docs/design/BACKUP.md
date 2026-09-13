# Backup, restore and portable data

> **Status: DESIGN (2026-09-13), nothing built.** Origin: Valerii's idea
> 2026-09-13 — make the database backup a Sautium feature, split it by data
> class (MusicBrainz / enrichment / life data), make the enrichment part
> mergeable into another user's database, and protect the personal part
> with a key derived from what the user already has.
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
| **Catalog** (identity graph, owned + phantom) | `artists`, `albums`, `tracks`, `media_files`, `track_artists`, `album_artists`, `album_tracks`, `album_variants`, `artist_mbids`, `track_mbids`, `artist_name_aliases`, `artist_members`, `genres`, `tags`, `embedding_models`, `seed_picks` | yes | structural rows ride with the enrichment they carry (as the seed bundle does) | — |
| **Enrichment** (first-hand, sealed) | `analysis_sources`, `embeddings`, `embedding_segments`, `audio_features`, `signing_batches`, `artist_bios`, `artist_tags`, `similar_artists`, `track_stats`, `track_lyrics`, `text_embeddings`, `artist_bio_embeddings`, `lyrics_embeddings`, `genre_descriptions`, `genre_desc_embeddings`, `external_metadata`, `covers` | yes | **yes — signed records only** (`_SIGNABLE_SRC`: first-hand, never re-exported P2P imports) | — |
| **Local-only enrichment** | `album_descriptions`, `album_genres` (never sync by design) | yes | no | — |
| **Life data** (personal) | `listening_history`, `listening_sessions`, `session_tracks`, `local_play_stats`, `friends`, `friend_rights`, `friend_grants`, `friend_grant_rights`, `invite_tokens`, `invite_token_rights`, `sent_invites`, `p2p_messages`, `chat_sessions`, `chat_messages`, `user_profile`, `user_gear`, `gear_pair_notes`, `pending_key_rotations`, `p2p_identities`, `p2p_node_bans`, `support_*`, `diag_*` | yes, encrypted | no | **yes, keyed dedup** (§ Phase 3) |
| **Node settings** | `user_settings` | yes (in the dump) | no | allowlist only |
| **Runtime, re-creatable** | `p2p_gate_pool`, `p2p_contact_events`, `p2p_action_costs`, `p2p_dht_state`, `p2p_nodes_seen`, `external_api_cooldown`, `_gap`, `_schema_migrations` (travels with the dump, see restore) | in the dump, harmless | no | no |
| **Files** | identity dir: `info.json`, `birth_certificate.json`, `identity_proof.json`, `previous/` (rotation archive), `.api_secret` | yes, encrypted | no | — |
| **Files, elsewhere** | `.env`, MCP config, launcher `config.json`, the Deezer plugin's `secrets.json` | **not here** — the maintainer's private repo / the user's own secret store | no | no |

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

- Launcher: Settings › Maintenance › "Restore from backup…" and the wizard's
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
  "Import from file…"; both report through the same SSE events.

Side benefits: offline transfer between two of one's own nodes, and
distribution of curated bundles (the seed bundle becomes one instance).

---

## Product C — merge my own life data (Phase 3)

For the "two nodes, one person" case (laptop + desktop, or an old backup
after a rebuild): union, keyed so that a second import changes nothing.

| Table | Key | Rule |
|---|---|---|
| `listening_history` | `(track_id, started_at)` | insert missing rows only |
| `listening_sessions`, `session_tracks`, `local_play_stats` | — | **recompute** from history after the merge, never merge counters |
| `friends`, `friend_rights`, `friend_grants` | friend pubkey | insert missing; rights = union |
| `p2p_messages` | `message_uuid` | insert missing (already the dedup key) |
| `chat_sessions`, `chat_messages` | session uuid, message uuid | insert missing |
| `user_gear`, `gear_pair_notes`, `user_profile` | natural keys | insert missing |
| `p2p_identities`, `p2p_node_bans` | pubkey | insert missing; a ban on either side is a ban |
| `user_settings` | key | **allowlist only** (`discovery.*`, `sync.*`, `p2p.gate_mode`); never machine-specific keys (`hqplayer.host`, ports, paths) |
| identity `previous/` | pubkey | union of rotation records |

Input = a Product-A file of one's own (same account: the password unwraps
it, the manifest pubkey matches or is a `previous/` key). The merge reads
the life-data tables out of the dump via `pg_restore` into a scratch
schema, then runs the keyed inserts. Enrichment inside such a file is not
merged this way — it goes through Product B's gate like anyone else's.

---

## Code layout

- `backend/backup.py` — format v1: KDF, envelope, chunked cipher, tar
  writer/reader, `pg_dump` / `pg_restore` drivers, `--selftest`, the
  `python -m backup` CLI (`create`, `restore`, `inspect`).
- `backend/routers/settings.py` — `GET/POST /api/settings/backup`,
  `POST /api/settings/backup/export`, `POST /api/settings/backup/import`;
  events on the library SSE channel.
- `desktop/restore.py` — launcher restore flow (stop services, restore,
  migrations, identity, start) used by Settings › Maintenance and the
  wizard.
- `backend/seed_export.py` / `seed_import.py` — grow into the share
  export/import (pick list → scope selector); the seed bundle stays a
  caller.
- `backend/static/app-shell.js` — the Backup card under Settings › Library,
  next to "Remove phantom layer"; dialogs via `notifyDialog` /
  `confirmDestructive`, never `alert`/`confirm`.
- Docker image: `postgresql-client-18` (pg_dump/pg_restore in the backend
  container); launcher: `pgsql/bin` on `PG_BIN`.

## Phases and acceptance

1. **Backup + restore.** Done when a backup of the master restores into a
   fresh `music_ai_test` with identical row counts for every non-`mb_*`
   table, the node starts on it, the identity matches, newer migrations
   apply, and the format tests fail closed on tamper / wrong password.
2. **Share export + import.** Done when an export from the Docker node
   imports on the launcher stand through the gate with the expected
   counts, a re-import changes nothing, and a tampered record is refused.
3. **Own life-data merge.** Done when two clones with divergent listening
   histories merge to the union in either order, and `local_play_stats`
   recomputes to the same numbers as a single history would give.

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
  only); merge is Product C.
