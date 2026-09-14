"""Merge my own life data out of a node backup (docs/design/BACKUP.md,
Product C) — the "two nodes, one person" case: a laptop and a desktop, or
an old backup after a rebuild. What the owner DID — listens, listening
sessions, friends and their messages, AI chats, gear, a handful of
preferences — is unioned into this database by natural key, so a second run
changes nothing. Nothing here is replaced or removed. Enrichment inside the
file is not touched: that travels as a share export through the gate
(Product B), like anyone else's.

The file is a Product-A `.sbk` of the same account: the password unwraps it,
and the manifest's key is this node's or one it retired. Its `pg_dump`
member streams through `pg_restore --data-only -t <life tables> -f -` — one
pass, nothing decrypted on disk — and the SQL text that comes out is COPY'd
into a scratch schema (`_merge`) inside this very database, in the same
transaction the keyed inserts then run in: a failure anywhere rolls the
scratch and the merge back together, and PostgreSQL itself needs no cleanup
after a crash. Rows that name a track this node does not know (a phantom
listened to elsewhere) wait — a later merge picks them up once the track has
arrived through the sync or a share import — and everything else lands.

`local_play_stats` is never merged: it is recomputed from the merged history
(play_stats.PLAY_STATS_SQL, the tracker's own definition), so two clones'
histories merged in either order give the same numbers as one history would.
"""

import json
import logging
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Tuple

from psycopg2 import sql

from desktop import node_backup as nb
from play_stats import refresh_play_stats

logger = logging.getLogger(__name__)

SCRATCH = "_merge"

# Every table the merge reads out of the dump; a backup written before one of
# them existed simply yields it empty.
LIFE_TABLES = (
    "listening_history", "listening_sessions", "session_tracks",
    "invite_tokens", "sent_invites",
    "friends", "friend_rights", "friend_grants", "friend_grant_rights", "p2p_messages",
    "chat_sessions", "chat_messages",
    "gear_brands", "gear_models", "user_gear", "gear_pair_notes", "user_profile",
    "p2p_identities", "p2p_node_bans", "user_settings",
)

# Preferences of the person, never of the machine: no hosts, ports, paths,
# secrets, tokens, nor anything a node computes about itself (`sync.last_at`,
# `p2p.load`, the reachability verdict). A key already set here wins.
SETTINGS_ALLOWLIST = frozenset({
    "discovery.phantom_layer",
    "sync.p2p_enabled", "sync.auto_interval_min", "sync.announce_limit", "sync.carry_limit",
    "p2p.gate_mode",
    "enrichment.background_enabled", "enrichment.reanalyze_imported",
    "lastfm.scrobbling_enabled", "albums.sort", "ui.language",
    "support.diagnostics_enabled", "ai.canonization_enabled",
})

ProgressFn = Callable[..., None]

_COPY_LINE = re.compile(r'^COPY (?:"?[\w]+"?\.)?"?(\w+)"? \((.*)\) FROM stdin;$')


# ---------------------------------------------------------------------------
# Same account
# ---------------------------------------------------------------------------

def check_same_account(manifest: dict, *, pubkey: str, previous: List[str]) -> None:
    """The merge takes this account's own backups only: the manifest's key is
    the node's current one or one it retired (a renamed account is the same
    person; a friend's backup whose password one happens to know is not)."""
    node = manifest.get("node") or {}
    key = str(node.get("pubkey") or "").lower()
    if key and (key == pubkey.lower() or key in {p.lower() for p in previous}):
        return
    raise nb.Refused(f"the backup belongs to another identity ({node.get('username') or '?'}, "
                     f"{key[:12]}…) — the merge takes this account's own backups only")


# ---------------------------------------------------------------------------
# pg_restore's SQL text → the scratch schema
# ---------------------------------------------------------------------------

class _CopyData:
    """File-like over one COPY block of pg_restore's output: `read()` hands
    the rows to copy_expert as they arrive and stops at the terminator."""

    def __init__(self, stream):
        self._stream = stream
        self.rows = 0
        self.done = False

    def read(self, size: int = -1) -> bytes:
        if self.done:
            return b""
        out, n = [], 0
        while size < 0 or n < size:
            line = self._stream.readline()
            if not line:
                raise nb.BackupError("pg_restore's output ended inside a COPY block")
            if line.endswith(b"\r\n"):
                line = line[:-2] + b"\n"
            if line == b"\\.\n":
                self.done = True
                break
            out.append(line)
            n += len(line)
            self.rows += 1
        return b"".join(out)

    def drain(self) -> None:
        while not self.done:
            self.read(1 << 16)


def copy_blocks(stream) -> Iterator[Tuple[str, List[str], _CopyData]]:
    """(table, columns, rows) for every `COPY … FROM stdin;` block in a
    pg_restore script; everything else in it (the `\\restrict` guard, SETs,
    sequence resets) is passed over. Each block must be consumed or drained
    before the next is asked for — the stream is read once."""
    while True:
        line = stream.readline()
        if not line:
            return
        m = _COPY_LINE.match(line.rstrip(b"\r\n").decode("utf-8", "replace"))
        if not m:
            continue
        columns = [c.strip().strip('"') for c in m.group(2).split(",")]
        data = _CopyData(stream)
        yield m.group(1), columns, data
        data.drain()


def _live_columns(cur, table: str) -> List[str]:
    cur.execute("""SELECT column_name FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = %s
                    ORDER BY ordinal_position""", (table,))
    return [r[0] for r in cur.fetchall()]


def _create_scratch_table(cur, table: str, columns: List[str]) -> None:
    """The live table's shape (types, NOT NULLs, defaults — no constraints, no
    triggers, no indexes) plus, as text, any column the dump has and the live
    schema no longer does."""
    cur.execute(sql.SQL("CREATE TABLE IF NOT EXISTS {}.{} (LIKE {} INCLUDING DEFAULTS)").format(
        sql.Identifier(SCRATCH), sql.Identifier(table), sql.Identifier("public", table)))
    live = set(_live_columns(cur, table))
    for c in columns:
        if c not in live:
            cur.execute(sql.SQL("ALTER TABLE {}.{} ADD COLUMN IF NOT EXISTS {} text").format(
                sql.Identifier(SCRATCH), sql.Identifier(table), sql.Identifier(c)))


def load_scratch(reader: nb.BackupReader, target: nb.PgTarget, conn, *, tables: List[str],
                 progress: ProgressFn, cancel: threading.Event,
                 file_size: Optional[int] = None) -> Tuple[Dict[str, int], Dict[str, bytes]]:
    """Stream the dump member through pg_restore and COPY the selected tables
    into the scratch schema on `conn` (its open transaction). A feeder thread
    writes the decrypted dump into pg_restore while this thread reads the
    script out of it — the two pipes must move together or neither does.
    Returns per-table row counts and the identity members that follow the
    dump in the file (already verified by the reader)."""
    stderr = tempfile.TemporaryFile()
    cmd = [target.tool("pg_restore"), "--data-only", "--no-owner", "-f", "-"]
    for t in tables:
        cmd += ["-t", t]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
                            env=target.tool_env(), **nb._spawn_kwargs(False))
    identity: Dict[str, bytes] = {}
    feeder_error: List[BaseException] = []

    def feed() -> None:
        try:
            dump_seen = False
            for member in reader.remaining_members():
                if member.name == nb.DB_MEMBER:
                    dump_seen = True
                    last_report = time.monotonic()
                    try:
                        for block in member.chunks():
                            if cancel.is_set():
                                raise nb.Cancelled("merge cancelled")
                            proc.stdin.write(block)
                            now = time.monotonic()
                            if now - last_report >= 0.5:
                                last_report = now
                                progress("reading", bytes=reader.bytes_read, total=file_size)
                    except BrokenPipeError:
                        pass                      # pg_restore is gone; its exit code speaks
                    finally:
                        try:
                            proc.stdin.close()
                        except OSError:
                            pass
                elif member.name.startswith(nb.IDENTITY_PREFIX):
                    rel = member.name[len(nb.IDENTITY_PREFIX):]
                    if ".." in rel.split("/") or rel.startswith("/"):
                        raise nb.Tampered(f"identity member with an unsafe path: {rel}")
                    identity[rel] = member.read()
                else:
                    raise nb.Tampered(f"unexpected member {member.name}")
            if not dump_seen:
                raise nb.Tampered("the backup has no database dump")
        except BaseException as e:               # handed to the main thread below
            feeder_error.append(e)
            try:
                proc.stdin.close()
            except OSError:
                pass

    feeder = threading.Thread(target=feed, name="merge-feed", daemon=True)
    feeder.start()
    counts: Dict[str, int] = {}
    try:
        with conn.cursor() as cur:
            cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(SCRATCH)))
            for table, columns, data in copy_blocks(proc.stdout):
                if cancel.is_set():
                    raise nb.Cancelled("merge cancelled")
                if table not in tables:
                    continue                      # drained by copy_blocks
                _create_scratch_table(cur, table, columns)
                stmt = sql.SQL("COPY {}.{} ({}) FROM STDIN").format(
                    sql.Identifier(SCRATCH), sql.Identifier(table),
                    sql.SQL(", ").join(sql.Identifier(c) for c in columns))
                cur.copy_expert(stmt.as_string(conn), data)
                counts[table] = counts.get(table, 0) + data.rows
                progress("loading", table=table, rows=counts[table])
            for table in tables:
                if table not in counts:
                    _create_scratch_table(cur, table, [])
                    counts[table] = 0
    except BaseException:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        feeder.join()
        stderr.close()
        raise
    rc = proc.wait()
    feeder.join()
    try:
        if feeder_error:
            raise feeder_error[0]
        if rc != 0:
            raise nb.BackupError(f"pg_restore failed (exit {rc}): {nb._stderr_tail(stderr)}")
    finally:
        stderr.close()
    return counts, identity


# ---------------------------------------------------------------------------
# The keyed union
# ---------------------------------------------------------------------------

def _cols(cur, table: str) -> Tuple[sql.SQL, sql.SQL]:
    """(`a, b, c`, `s.a, s.b, s.c`) over the live table's columns — a whole-row
    insert that follows the schema instead of restating it."""
    names = _live_columns(cur, table)
    return (sql.SQL(", ").join(sql.Identifier(c) for c in names),
            sql.SQL(", ").join(sql.Identifier("s", c) for c in names))


def _whole_rows(cur, table: str, *, where: sql.SQL = sql.SQL("TRUE")) -> int:
    into, from_ = _cols(cur, table)
    cur.execute(sql.SQL("INSERT INTO {t} ({into}) SELECT {from_} FROM {s}.{t} s WHERE {where} "
                        "ON CONFLICT DO NOTHING").format(
        t=sql.Identifier(table), into=into, from_=from_, s=sql.Identifier(SCRATCH), where=where))
    return cur.rowcount


_MEDIA_FILE_FOR = ("(SELECT mf.id FROM media_files mf WHERE mf.track_id = {track} "
                   "ORDER BY mf.is_analysis_source DESC, mf.id LIMIT 1)")


def merge_life(conn, *, progress: Optional[ProgressFn] = None) -> dict:
    """Union the scratch schema into the live tables on `conn`'s open
    transaction. Returns {"merged": {what: rows added}, "waiting": {…}} —
    `waiting` counts what named a track this node does not know yet."""
    progress = progress or (lambda *_a, **_k: None)
    merged: Dict[str, int] = {}
    waiting: Dict[str, int] = {}
    with conn.cursor() as cur:
        def run(stmt: str, params=None) -> int:
            cur.execute(stmt.replace("_S_", SCRATCH), params)
            return cur.rowcount

        # --- invite tokens: parents of friends.source_token_id; revoked on
        # either side is revoked.
        progress("merging", what="friends")
        merged["tokens"] = _whole_rows(cur, "invite_tokens")
        run("""UPDATE invite_tokens t SET revoked_at = s.revoked_at FROM _S_.invite_tokens s
                WHERE s.id = t.id AND s.revoked_at IS NOT NULL AND t.revoked_at IS NULL""")
        merged["invites"] = run("""
            INSERT INTO sent_invites (to_email, sent_at)
            SELECT s.to_email, s.sent_at FROM _S_.sent_invites s
             WHERE NOT EXISTS (SELECT 1 FROM sent_invites x WHERE x.to_email = s.to_email
                                  AND x.sent_at IS NOT DISTINCT FROM s.sent_at)""")

        # --- friends by public key. A friend who rotated: the newer key wins
        # on whichever side has it (chat_service applies a rotation the same
        # way), and a row still waiting for its key ('pending:<invite>') is
        # bound when the other side already has the key.
        run("""UPDATE friends f
                  SET previous_public_key_hex = f.public_key_hex,
                      public_key_hex = s.public_key_hex, invite_code = s.invite_code
                 FROM _S_.friends s
                WHERE s.previous_public_key_hex = f.public_key_hex
                  AND s.public_key_hex NOT LIKE 'pending:%'
                  AND NOT EXISTS (SELECT 1 FROM friends x WHERE x.public_key_hex = s.public_key_hex)""")
        run("""UPDATE friends f
                  SET public_key_hex = s.public_key_hex,
                      username = COALESCE(NULLIF(s.username, ''), f.username),
                      display_name = COALESCE(f.display_name, s.display_name),
                      bound_identity = COALESCE(f.bound_identity, s.bound_identity)
                 FROM _S_.friends s
                WHERE f.public_key_hex = 'pending:' || f.invite_code
                  AND s.invite_code = f.invite_code AND s.public_key_hex NOT LIKE 'pending:%'
                  AND NOT EXISTS (SELECT 1 FROM friends x WHERE x.public_key_hex = s.public_key_hex)""")
        merged["friends"] = run("""
            INSERT INTO friends (username, public_key_hex, invite_code, display_name, added_at,
                                 last_seen, is_blocked, previous_public_key_hex, city, bio,
                                 avatar_cover_id, source, source_token_id, join_token_id,
                                 favorite, last_activity_at, bound_identity)
            SELECT s.username, s.public_key_hex, s.invite_code, s.display_name, s.added_at,
                   s.last_seen, s.is_blocked, s.previous_public_key_hex, s.city, s.bio,
                   (SELECT c.id FROM covers c WHERE c.id = s.avatar_cover_id), s.source,
                   (SELECT t.id FROM invite_tokens t WHERE t.id = s.source_token_id),
                   s.join_token_id, s.favorite, s.last_activity_at, s.bound_identity
              FROM _S_.friends s
             WHERE NOT EXISTS (SELECT 1 FROM friends f
                                WHERE f.previous_public_key_hex = s.public_key_hex
                                   OR (s.invite_code <> '' AND f.invite_code = s.invite_code))
            ON CONFLICT DO NOTHING""")
        run("""UPDATE friends f SET is_blocked = TRUE FROM _S_.friends s
                WHERE s.public_key_hex = f.public_key_hex AND s.is_blocked AND NOT f.is_blocked""")
        merged["rights"] = run("""
            INSERT INTO friend_rights (friend_id, p2p_right)
            SELECT f.id, r.p2p_right
              FROM _S_.friend_rights r
              JOIN _S_.friends sf ON sf.id = r.friend_id
              JOIN friends f ON f.public_key_hex = sf.public_key_hex
            ON CONFLICT DO NOTHING""")
        run("""INSERT INTO friend_grants (friend_id, token_id, issuer_pubkey_hex, issued_at,
                                          expires_at, signature)
               SELECT f.id, g.token_id, g.issuer_pubkey_hex, g.issued_at, g.expires_at, g.signature
                 FROM _S_.friend_grants g
                 JOIN _S_.friends sf ON sf.id = g.friend_id
                 JOIN friends f ON f.public_key_hex = sf.public_key_hex
               ON CONFLICT DO NOTHING RETURNING friend_id""")
        granted = [r[0] for r in cur.fetchall()]
        merged["grants"] = len(granted)
        if granted:
            run("""INSERT INTO friend_grant_rights (friend_id, p2p_right)
                   SELECT f.id, gr.p2p_right
                     FROM _S_.friend_grant_rights gr
                     JOIN _S_.friends sf ON sf.id = gr.friend_id
                     JOIN friends f ON f.public_key_hex = sf.public_key_hex
                    WHERE f.id = ANY(%(ids)s)
                   ON CONFLICT DO NOTHING""", {"ids": granted})
        merged["messages"] = run("""
            INSERT INTO p2p_messages (friend_id, direction, content, "timestamp", delivered,
                                      read, message_uuid)
            SELECT f.id, m.direction, m.content, m."timestamp", m.delivered, m.read, m.message_uuid
              FROM _S_.p2p_messages m
              JOIN _S_.friends sf ON sf.id = m.friend_id
              JOIN friends f ON f.public_key_hex = sf.public_key_hex
             WHERE m.message_uuid IS NOT NULL
            ON CONFLICT DO NOTHING""")

        # --- listens by (track, started_at); the file this machine has for the
        # track, if any, stands in for the other machine's file id.
        progress("merging", what="listens")
        run(f"""INSERT INTO listening_history (media_file_id, track_id, started_at, ended_at,
                                               duration_listened, percent_listened, completed,
                                               skipped, created_at)
                SELECT DISTINCT ON (s.track_id, s.started_at)
                       {_MEDIA_FILE_FOR.format(track='s.track_id')},
                       s.track_id, s.started_at, s.ended_at, s.duration_listened,
                       s.percent_listened, s.completed, s.skipped, s.created_at
                  FROM _S_.listening_history s
                 WHERE EXISTS (SELECT 1 FROM tracks t WHERE t.id = s.track_id)
                   AND NOT EXISTS (SELECT 1 FROM listening_history h
                                    WHERE h.track_id = s.track_id AND h.started_at = s.started_at)
                 ORDER BY s.track_id, s.started_at, s.id
                RETURNING track_id""")
        added = cur.fetchall()
        merged["listens"] = len(added)
        merged["stats"] = refresh_play_stats(cur, sorted({str(r[0]) for r in added}))
        cur.execute(f"""SELECT count(*), count(DISTINCT track_id) FROM {SCRATCH}.listening_history s
                         WHERE NOT EXISTS (SELECT 1 FROM tracks t WHERE t.id = s.track_id)""")
        waiting["listens"], waiting["tracks"] = (int(v) for v in cur.fetchone())

        # --- listening sessions by id: closed ones whose tracks are all known
        # here (an open one is the other machine's live queue; a partial card
        # would never be completed, so it waits whole).
        run(f"""INSERT INTO listening_sessions (id, origin, title, subtitle, cover_id, origin_album_id,
                                                seed_media_file_id, track_count, started_at, ended_at,
                                                created_at, seed_track_id, cover_url)
                SELECT s.id, s.origin, s.title, s.subtitle,
                       (SELECT c.id FROM covers c WHERE c.id = s.cover_id),
                       (SELECT a.id FROM albums a WHERE a.id = s.origin_album_id),
                       {_MEDIA_FILE_FOR.format(track='s.seed_track_id')},
                       s.track_count, s.started_at, s.ended_at, s.created_at,
                       (SELECT t.id FROM tracks t WHERE t.id = s.seed_track_id), s.cover_url
                  FROM _S_.listening_sessions s
                 WHERE s.ended_at IS NOT NULL
                   AND NOT EXISTS (SELECT 1 FROM _S_.session_tracks st
                                    WHERE st.session_id = s.id
                                      AND NOT EXISTS (SELECT 1 FROM tracks t WHERE t.id = st.track_id))
                ON CONFLICT DO NOTHING RETURNING id""")
        sessions = [r[0] for r in cur.fetchall()]
        merged["sessions"] = len(sessions)
        if sessions:
            run(f"""INSERT INTO session_tracks (session_id, position, media_file_id, track_id, album_id)
                    SELECT st.session_id, st.position, {_MEDIA_FILE_FOR.format(track='st.track_id')},
                           st.track_id, (SELECT a.id FROM albums a WHERE a.id = st.album_id)
                      FROM _S_.session_tracks st
                     WHERE st.session_id = ANY(CAST(%(ids)s AS uuid[]))
                    ON CONFLICT DO NOTHING""", {"ids": [str(s) for s in sessions]})
        run("""SELECT count(*) FROM _S_.listening_sessions s
                WHERE s.ended_at IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM listening_sessions l WHERE l.id = s.id)
                  AND EXISTS (SELECT 1 FROM _S_.session_tracks st
                               WHERE st.session_id = s.id
                                 AND NOT EXISTS (SELECT 1 FROM tracks t WHERE t.id = st.track_id))""")
        waiting["sessions"] = int(cur.fetchone()[0])

        # --- AI chats by creation time (microseconds; the tables have no
        # uuid). The agent session ids stay behind: they name a Claude Code /
        # Codex session on the other machine, and a merged conversation is
        # read here, then continued as a new one.
        progress("merging", what="chats")
        merged["chats"] = run("""
            INSERT INTO chat_sessions (title, created_at, updated_at)
            SELECT s.title, s.created_at, s.updated_at FROM _S_.chat_sessions s
             WHERE NOT EXISTS (SELECT 1 FROM chat_sessions c WHERE c.created_at = s.created_at)""")
        merged["chat_messages"] = run("""
            INSERT INTO chat_messages (session_id, role, content, tracks_data, model, filters_detected,
                                       retrieval_log, tracks_retrieved, is_not_relevant,
                                       feedback_comment, feedback_at, created_at, blocks_data)
            SELECT c.id, m.role, m.content, m.tracks_data, m.model, m.filters_detected,
                   m.retrieval_log, m.tracks_retrieved, m.is_not_relevant, m.feedback_comment,
                   m.feedback_at, m.created_at, m.blocks_data
              FROM _S_.chat_messages m
              JOIN _S_.chat_sessions s ON s.id = m.session_id
              JOIN chat_sessions c ON c.created_at = s.created_at
             WHERE NOT EXISTS (SELECT 1 FROM chat_messages x
                                WHERE x.session_id = c.id AND x.created_at = m.created_at
                                  AND x.role = m.role)""")

        # --- gear: the catalogue rows the owner's chain needs (deterministic
        # ids — the same model on two nodes is one row), then the chain and
        # the pair notes. Research columns of a model already here are its own.
        progress("merging", what="gear")
        _whole_rows(cur, "gear_brands")
        _whole_rows(cur, "gear_models",
                    where=sql.SQL("EXISTS (SELECT 1 FROM gear_brands b WHERE b.id = s.brand_id)"))
        merged["gear"] = _whole_rows(
            cur, "user_gear",
            where=sql.SQL("EXISTS (SELECT 1 FROM gear_models g WHERE g.id = s.gear_model_id)"))
        merged["pair_notes"] = _whole_rows(
            cur, "gear_pair_notes",
            where=sql.SQL("EXISTS (SELECT 1 FROM gear_models g WHERE g.id = s.model_a) "
                          "AND EXISTS (SELECT 1 FROM gear_models g WHERE g.id = s.model_b)"))

        # --- the profile: one row on both sides; what is empty here fills in.
        merged["profile"] = run("""
            UPDATE user_profile p
               SET display_name = COALESCE(NULLIF(p.display_name, ''), s.display_name),
                   city = COALESCE(NULLIF(p.city, ''), s.city),
                   bio = COALESCE(NULLIF(p.bio, ''), s.bio),
                   country = COALESCE(p.country, s.country),
                   avatar_cover_id = COALESCE(p.avatar_cover_id,
                                              (SELECT c.id FROM covers c WHERE c.id = s.avatar_cover_id))
              FROM _S_.user_profile s
             WHERE p.id = 1 AND s.id = 1
               AND ROW(p.display_name, p.city, p.bio, p.country, p.avatar_cover_id) IS DISTINCT FROM
                   ROW(COALESCE(NULLIF(p.display_name, ''), s.display_name),
                       COALESCE(NULLIF(p.city, ''), s.city),
                       COALESCE(NULLIF(p.bio, ''), s.bio),
                       COALESCE(p.country, s.country),
                       COALESCE(p.avatar_cover_id,
                                (SELECT c.id FROM covers c WHERE c.id = s.avatar_cover_id)))""")

        # --- the identity registry and bans: a ban on either side is a ban.
        progress("merging", what="identities")
        merged["identities"] = _whole_rows(cur, "p2p_identities")
        merged["bans"] = run("""
            INSERT INTO p2p_node_bans (pubkey, addr, reason, created_at)
            SELECT s.pubkey, s.addr, s.reason, s.created_at FROM _S_.p2p_node_bans s
             WHERE NOT EXISTS (SELECT 1 FROM p2p_node_bans b
                                WHERE b.pubkey IS NOT DISTINCT FROM s.pubkey
                                  AND b.addr IS NOT DISTINCT FROM s.addr)""")

        # --- preferences: the allowlist, missing keys only.
        merged["settings"] = run("""
            INSERT INTO user_settings (key, value, updated_at)
            SELECT s.key, s.value, s.updated_at FROM _S_.user_settings s
             WHERE s.key = ANY(%(allow)s)
            ON CONFLICT DO NOTHING""", {"allow": sorted(SETTINGS_ALLOWLIST)})
    return {"merged": merged, "waiting": waiting}


# ---------------------------------------------------------------------------
# Identity: the rotation archive
# ---------------------------------------------------------------------------

def merge_identity(identity_dir: Optional[Path], files: Dict[str, bytes]) -> int:
    """Union of rotation records: every retired identity the backup's
    node_info.json lists and this node's does not gets its archive directory
    (the notice, the certificate, the proof — a backup carries no key) and
    its entry. Nothing happens without a live node_info.json — a Docker node
    keeps none. Returns how many were added."""
    from desktop.node_identity import PREVIOUS_DIRNAME
    if identity_dir is None or "node_info.json" not in files:
        return 0
    live_path = identity_dir / "node_info.json"
    if not live_path.exists():
        return 0
    live = json.loads(live_path.read_text(encoding="utf-8"))
    theirs = json.loads(files["node_info.json"].decode("utf-8"))
    known = {str(e.get("public_key_hex", "")).lower() for e in live.get("previous", [])}
    known.add(str(live.get("public_key_hex", "")).lower())
    added = 0
    for entry in theirs.get("previous", []):
        pub = str(entry.get("public_key_hex", "")).lower()
        if not pub or pub in known or not entry.get("dir"):
            continue
        prefix = f"{PREVIOUS_DIRNAME}/{entry['dir']}/"
        for rel, data in files.items():
            if rel.startswith(prefix):
                path = identity_dir / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                if not path.exists():
                    path.write_bytes(data)
        live.setdefault("previous", []).append(entry)
        known.add(pub)
        added += 1
    if added:
        live["previous"].sort(key=lambda e: str(e.get("retired_at", "")))
        tmp = live_path.with_name(live_path.name + ".tmp")
        tmp.write_text(json.dumps(live, indent=2), encoding="utf-8")
        tmp.replace(live_path)
        logger.info("identity archive: %d retired identit%s added from the backup",
                    added, "y" if added == 1 else "ies")
    return added


# ---------------------------------------------------------------------------
# The whole thing
# ---------------------------------------------------------------------------

def merge_backup(path: Path, password: str, *, target: nb.PgTarget, pubkey: str,
                 previous: List[str], identity_dir: Optional[Path] = None,
                 progress: Optional[ProgressFn] = None, cancel: Optional[threading.Event] = None,
                 dry_run: bool = False, kdf: nb.KdfParams = nb.DEFAULT_KDF) -> dict:
    """Open `path` with `password`, insist it is this account's, load the
    life tables into the scratch schema and union them in — one transaction,
    committed unless `dry_run` (which reports the same counts and changes
    nothing). Returns {"merged", "waiting", "loaded", "manifest", "dry_run"}."""
    progress = progress or (lambda *_a, **_k: None)
    cancel = cancel or threading.Event()
    lock = threading.Lock()

    def report(phase: str, **fields) -> None:
        with lock:                                   # two threads speak; one line at a time
            progress(phase, **fields)

    file_size = path.stat().st_size
    with open(path, "rb") as fp:
        reader = nb.BackupReader(fp)
        report("unlocking")
        reader.unlock(password, kdf)
        manifest = reader.read_manifest()
        nb.check_compatible(manifest)
        check_same_account(manifest, pubkey=pubkey, previous=previous)
        in_dump = set((manifest.get("database") or {}).get("tables") or {})
        tables = [t for t in LIFE_TABLES if t in in_dump] if in_dump else list(LIFE_TABLES)
        conn = target.connect()
        conn.autocommit = False
        try:
            report("reading", bytes=reader.bytes_read, total=file_size)
            loaded, identity = load_scratch(reader, target, conn, tables=tables, progress=report,
                                            cancel=cancel, file_size=file_size)
            if cancel.is_set():
                raise nb.Cancelled("merge cancelled")
            result = merge_life(conn, progress=report)
            with conn.cursor() as cur:
                cur.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(SCRATCH)))
            if dry_run:
                conn.rollback()
            else:
                conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
    result["merged"]["rotations"] = 0 if dry_run else merge_identity(identity_dir, identity)
    result.update(loaded=loaded, manifest=manifest, dry_run=dry_run)
    logger.info("%s of %s's backup from %s: %s; waiting %s",
                "dry run" if dry_run else "merge", manifest["node"]["username"],
                manifest["created_at"], result["merged"], result["waiting"])
    return result
