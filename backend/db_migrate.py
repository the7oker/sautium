"""Bring this node's database to the running code's version.

Runs at backend startup (main.py lifespan, before anything serves) on every
node — the Docker master as much as a launcher install — so a code update
is never applied to a database on an older schema or an older identity
rule. Two layers, one tracking table (`_schema_migrations`):

  1. Schema deltas — `desktop/migrations/NNN_*.sql`, applied by the same
     runner the launcher uses (`desktop.db_init.apply_migrations`). A
     database that got its schema before this surface ran the runner (the
     master, 001 by hand) adopts 001 as applied first.
  2. Data migrations — Python, keyed by marker rows (no file): the identity
     rule (`uuid_utils.IDENTITY_RULE`, `identity_rule_v{N}`) re-normalizes
     every uuid5 entity through `canon.migrations.renormalize_identities`
     when the recorded rule is older than the code's. Idempotent, so a fresh
     node just records the marker; a node with old-rule data is rewritten
     here, before it can mint on the new rule beside old rows. That rewrite
     sheds every seal — the node's own sign_audio cadence re-seals. The
     play-stats step (`play_stats_derived_v1`) re-derives `local_play_stats`
     from `listening_history` once, the day the table became a function of
     it (backend/play_stats.py) instead of counters kept in place. The
     sub-floor step (`sub_floor_analysis_v1`) drops the node's own analysis
     of material under provenance.MIN_MATERIAL_SECONDS, computed before the
     floor existed (2026-09-18). The listen-times step (`listen_times_utc_v1`)
     moves the starts a launcher's tracker wrote as naive local time back to
     the instant they meant (play_stats.repaired_start, 2026-09-24). The
     scrobble step (`scrobble_title_key_v1`) reopens every waiting
     imported-scrobble name once, the day titles came to be compared by a
     folded key (accents, typography, a store's annotation — 2026-09-24).
     The support step (`diag_scrubbed_v1`) scrubs what was kept for support
     before its text was: the node's events, the master's received reports
     (no path, address or email — 2026-11-03). The consent step
     (`diag_consent_v1`) makes every bundle staged and not yet sent count for
     nothing, the day the person came to decide on the content scopes
     (2026-11-04): an earlier release's carried the events' log tails
     unasked; the warrant is collected again on its next delivery, under the
     new rule, and one the switch withdrew is closed.
     The slice step (`slice_authors_v1`) dropped what slices of a key the
     release did not name had left behind, the day a slice came to count
     only under its author (2026-11-03); its code went on 2026-11-07, when
     any dump holder came to author slices — the nodes that ran it keep the
     marker. The master step (`master_stub_bound_v1`) binds the support
     contact still waiting unbound to the master's own key, the day a contact
     came to carry its key's fingerprint (2026-11-07). The rebuild step
     (`slice_rebuild_v1`) rebuilds a node's catalogue rows once from the
     slices it keeps, under the import's rules of that day — a slice's own
     subtree only, its rows of their columns' kinds, an artist or a group one
     row per MBID — and indexes the slices kept before the subject index, on
     a node without the dump (2026-11-07).

Before the schema deltas, where this node's own role administers the
cluster (Docker), the assistant's read-only login is re-stated
(_assistant_role); the runner states its grants after the deltas.

The launcher's own P2P sync server is a separate process: on a launcher
node with old-rule data a peer import racing this rewrite is a known
window — keep P2P off for the first start after such an upgrade.

Beside the migrations, analyze_unrecorded gives back what a crash recovery
takes from autovacuum (below); the lifespan runs it in the background.
"""

import logging
import time

import psycopg2
from psycopg2 import errors, sql

from config import settings
from desktop import db_init
from uuid_utils import IDENTITY_RULE

logger = logging.getLogger(__name__)

_IDENTITY_MARK = "identity_rule_v{}"


def _marked(conn, name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(f"SELECT 1 FROM {db_init.MIGRATIONS_TABLE} WHERE filename = %s", (name,))
        return cur.fetchone() is not None


def _mark(conn, name: str) -> None:
    with conn.cursor() as cur:
        cur.execute(f"INSERT INTO {db_init.MIGRATIONS_TABLE} (filename) VALUES (%s) "
                    "ON CONFLICT (filename) DO NOTHING", (name,))
    conn.commit()


def _renormalize_identities() -> dict:
    from canon.migrations import renormalize_identities
    from database import get_db_context
    with get_db_context() as db:
        return renormalize_identities(db)


def _rederive_play_stats(conn) -> int:
    from play_stats import refresh_play_stats
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT track_id::text FROM listening_history")
        return refresh_play_stats(cur, [r[0] for r in cur.fetchall()])


def _repair_naive_listen_times(conn) -> int:
    from play_stats import refresh_play_stats, repair_naive_listen_times
    with conn.cursor() as cur:
        tracks = repair_naive_listen_times(cur)
        refresh_play_stats(cur, tracks)
    conn.commit()
    return len(tracks)


def _reopen_waiting_scrobble_names(conn) -> int:
    """Every name with scrobbles still waiting is due again: decided and
    placed under exact lowered titles, it gets another look under the folded
    title key — the resolver's consumer starts on them at startup."""
    with conn.cursor() as cur:
        cur.execute("UPDATE pending_scrobble_artists SET checked_at = NULL "
                    "WHERE checked_at IS NOT NULL")
        reopened = cur.rowcount
    conn.commit()
    return reopened


def _drop_sub_floor_analysis(conn) -> dict:
    """The node's own analysis of owned material under the floor — rows the
    passes no longer produce and the coverage figures no longer count, left
    from before the floor existed (twenty stings and intros on the master).
    The track set is the queues' own rule (the analysis-source file under
    MIN_MATERIAL_SECONDS); only file-backed first-hand rows and unlinked
    legacy rows go — an imported row describes a peer's material, a stream
    row its own. Deleting sealed rows is fine: a seal is per row, and a
    signing batch stays valid for its other leaves."""
    from provenance import MIN_MATERIAL_SECONDS
    own_file = """(x.analysis_source_id IS NULL
                   OR EXISTS (SELECT 1 FROM analysis_sources src
                               WHERE src.id = x.analysis_source_id
                                 AND NOT src.imported
                                 AND src.media_file_id IS NOT NULL))"""
    counts = {}
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TEMP TABLE sub_floor ON COMMIT DROP AS
            SELECT DISTINCT mf.track_id FROM media_files mf
             WHERE mf.is_analysis_source AND mf.duration_seconds < %(min)s
        """, {"min": MIN_MATERIAL_SECONDS})
        for table in ("embeddings", "audio_features"):
            cur.execute(f"""
                DELETE FROM {table} x USING sub_floor s
                 WHERE x.track_id = s.track_id AND {own_file}""")
            counts[table] = cur.rowcount
        cur.execute("""
            DELETE FROM analysis_sources src USING sub_floor s
             WHERE src.track_id = s.track_id
               AND NOT src.imported AND src.media_file_id IS NOT NULL""")
        counts["analysis_sources"] = cur.rowcount
    conn.commit()
    return counts


def _scrub_support_records(conn) -> dict:
    """What was kept for support before its text was scrubbed — every event
    here, and on the master every report received — through the scrubber the
    recorder and the master's ingest now apply (diag_protocol.scrub_detail),
    so nothing stored for support names a path, an address or an email."""
    from psycopg2.extras import Json, execute_batch

    from desktop.p2p.diag_protocol import scrub_detail
    counts = {}
    with conn.cursor() as cur:
        for table in ("diag_events", "support_reports"):
            cur.execute(f"SELECT id, detail FROM {table}")
            changed = [(Json(clean), rid) for rid, detail in cur.fetchall()
                       if (clean := scrub_detail(detail)) != detail]
            execute_batch(cur, f"UPDATE {table} SET detail = %s WHERE id = %s", changed,
                          page_size=500)
            counts[table] = len(changed)
        # A bundle collected before, staged and not yet uploaded, was not
        # scrubbed and is boxed: it never leaves (resumable_warrants skips it).
        cur.execute("""
            UPDATE diag_warrants SET error = 'withdrawn: collected before support bundles were scrubbed'
             WHERE collected_at IS NOT NULL AND uploaded_at IS NULL AND error IS NULL""")
        counts["staged_bundles_withdrawn"] = cur.rowcount
    conn.commit()
    return counts


def _recollect_staged_bundles(conn) -> dict:
    """A bundle collected and staged, not yet sent, counts for nothing: it
    was collected before the person decided on the content scopes, and even
    a request for facts about the node alone carried the events' log tails.
    Its warrant is collected again on its next delivery — one that names a
    content scope only once the person decided (diag_events.AWAITS_THE_PERSON)
    — unless the switch moved since its collection or decision: that one the
    switch withdrew, and it is closed, never collected again."""
    from desktop.p2p.diag_events import SWITCH_ALLOWS, WITHDRAWN
    staged = "collected_at IS NOT NULL AND uploaded_at IS NULL AND error IS NULL"
    with conn.cursor() as cur:
        cur.execute(f"""
            UPDATE diag_warrants SET error = %s
             WHERE {staged}
               AND NOT {SWITCH_ALLOWS.format(since="coalesce(decided_at, collected_at)")}
        """, (WITHDRAWN,))
        counts = {"withdrawn": cur.rowcount}
        cur.execute(f"UPDATE diag_warrants SET collected_at = NULL, size_bytes = NULL WHERE {staged}")
        counts["recollected"] = cur.rowcount
    conn.commit()
    return counts


def _rebuild_slices(conn) -> dict:
    """The catalogue rows of a node without the dump rebuilt from the slices
    it keeps (desktop/p2p/slice_rebuild.py): the import's rules apply to rows
    that landed before them, and every blob kept before the subject index
    existed is indexed and its carried MBIDs noted. Asked for here and run by
    the backend's rebuilder once it is up — the start never waits on the
    loader's lock a slice import or a load holds. A dump node's own cache is
    no slice of anyone's, and the rebuild leaves a dump node alone."""
    from desktop.p2p import slice_rebuild
    with conn.cursor() as cur:
        cur.execute("SELECT EXISTS (SELECT 1 FROM mb_slice_blobs)")
        if not cur.fetchone()[0]:
            return {}
    slice_rebuild.request(conn, "upgrade")
    conn.commit()
    return {"requested": True}


def _bind_master_stub(conn) -> int:
    """The support contact, where it still waits for its key with none
    named (seeded or added before contacts carried a fingerprint), bound to
    the master's own: a key that merely shares the 48 bits of its code
    resolves it no more."""
    from master_node import MASTER_INVITE_CODE, MASTER_PUBKEY_HEX
    if not (MASTER_INVITE_CODE and MASTER_PUBKEY_HEX):
        return 0
    from desktop.p2p import chat_wire
    with conn.cursor() as cur:
        cur.execute("UPDATE friends SET key_fingerprint = %s WHERE public_key_hex = %s AND key_fingerprint IS NULL",
                    (bytes.fromhex(chat_wire.key_fingerprint(MASTER_PUBKEY_HEX)),
                     f"pending:{MASTER_INVITE_CODE}"))
        bound = cur.rowcount
    conn.commit()
    return bound


def _assistant_role(conn) -> None:
    """The cluster administrator's part of the assistant's login
    (desktop/db_init.ASSISTANT_ROLE): the login itself, and large objects
    taken from PUBLIC in this database — here when this node's own role is
    that administrator (Docker's bootstrap superuser), else the launcher's
    setup did both before the backend started. Before the migrations, whose
    runner states the login's grants."""
    with conn.cursor() as cur:
        cur.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
        if cur.fetchone()[0]:
            db_init.ensure_assistant_role(cur, settings.postgres_password)
            db_init.revoke_large_object_creation(cur)
    conn.commit()


def apply_pending() -> dict:
    """Schema deltas, then data migrations. Returns what happened."""
    out = {"adopted_baseline": False, "sql_applied": 0, "identity_renormalized": False}
    conn = psycopg2.connect(settings.database_url)
    conn.autocommit = False
    try:
        out["adopted_baseline"] = db_init.adopt_baseline(conn)
        _assistant_role(conn)
        out["sql_applied"] = db_init.apply_migrations(conn, logger.info)

        mark = _IDENTITY_MARK.format(IDENTITY_RULE)
        if not _marked(conn, mark):
            logger.info("identity rule v%d not recorded — re-normalizing identities", IDENTITY_RULE)
            stats = _renormalize_identities()
            logger.info("identity re-normalization: %s", stats)
            _mark(conn, mark)
            out["identity_renormalized"] = True

        if not _marked(conn, "play_stats_derived_v1"):
            out["play_stats_rederived"] = _rederive_play_stats(conn)
            _mark(conn, "play_stats_derived_v1")

        if not _marked(conn, "sub_floor_analysis_v1"):
            out["sub_floor_dropped"] = _drop_sub_floor_analysis(conn)
            _mark(conn, "sub_floor_analysis_v1")

        if not _marked(conn, "listen_times_utc_v1"):
            out["listen_tracks_repaired"] = _repair_naive_listen_times(conn)
            _mark(conn, "listen_times_utc_v1")

        if not _marked(conn, "scrobble_title_key_v1"):
            out["scrobble_names_reopened"] = _reopen_waiting_scrobble_names(conn)
            _mark(conn, "scrobble_title_key_v1")

        if not _marked(conn, "diag_scrubbed_v1"):
            out["support_records_scrubbed"] = _scrub_support_records(conn)
            _mark(conn, "diag_scrubbed_v1")

        if not _marked(conn, "diag_consent_v1"):
            out["staged_bundles"] = _recollect_staged_bundles(conn)
            _mark(conn, "diag_consent_v1")

        if not _marked(conn, "slice_rebuild_v1"):
            out["slices_rebuilt"] = _rebuild_slices(conn)
            _mark(conn, "slice_rebuild_v1")

        if not _marked(conn, "master_stub_bound_v1"):
            out["master_stub_bound"] = _bind_master_stub(conn)
            _mark(conn, "master_stub_bound_v1")

        # Cold-start seed: after the identity pass, so the bundle's rule
        # check compares against a fully renormalized database. The marker
        # lands only on a COMPLETE import — a skip (no bundle yet, phantom
        # layer off) or a shortfall re-evaluates on the next start. The
        # marker follows the bundle version in backend/seed/bundle.json, so a
        # new bundle is a new import with no code change: seed_v2
        # (2026-09-18) carries the audio records under payload v3, and a node
        # that imported v1 lost those rows to migration 018 and takes them
        # again here.
        import seed_import
        info = seed_import.bundle_info()
        if info is not None and not _marked(conn, f"seed_v{info['version']}"):
            out["seed"] = seed_import.apply_seed(conn, settings.database_url, info)
            if out["seed"].get("complete"):
                _mark(conn, f"seed_v{info['version']}")
    finally:
        conn.close()
    logger.info("db_migrate: %s", out)
    return out


def analyze_unrecorded(dsn: str) -> int:
    """ANALYZE every table PostgreSQL holds no analysis on record for;
    returns how many.

    A crash recovery (a killed container, a Windows session that ended
    under the launcher, a power cut) discards PostgreSQL's cumulative
    statistics, and those counters are what autovacuum schedules from: with
    them gone a large table waits for another 10-20 % of churn before it is
    analyzed or vacuumed again, and every recovery starts that count anew.
    PostgreSQL's remedy for lost counters is a database-wide ANALYZE, which
    also re-estimates each table's dead rows and so puts the vacuum backlog
    back in front of autovacuum. A table with no analysis on record is one
    whose counters were lost, or one nothing has analyzed yet — after a
    clean restart there is nothing to do."""
    started = time.monotonic()
    analyzed = 0
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT schemaname, relname FROM pg_stat_user_tables
                WHERE last_analyze IS NULL AND last_autoanalyze IS NULL
            """)
            for schema, table in cur.fetchall():
                try:
                    cur.execute(sql.SQL("ANALYZE (SKIP_LOCKED) {}.{}").format(
                        sql.Identifier(schema), sql.Identifier(table)))
                except errors.UndefinedTable:
                    # Dropped since the listing: a dump load's swap, a merge's
                    # scratch schema.
                    continue
                analyzed += 1
    finally:
        conn.close()
    if analyzed:
        logger.info("analyzed %d tables with no analysis on record (%.0f s)",
                    analyzed, time.monotonic() - started)
    return analyzed
