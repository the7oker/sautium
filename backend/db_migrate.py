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
     the instant they meant (play_stats.repaired_start, 2026-09-24).

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


def apply_pending() -> dict:
    """Schema deltas, then data migrations. Returns what happened."""
    out = {"adopted_baseline": False, "sql_applied": 0, "identity_renormalized": False}
    conn = psycopg2.connect(settings.database_url)
    conn.autocommit = False
    try:
        out["adopted_baseline"] = db_init.adopt_baseline(conn)
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
