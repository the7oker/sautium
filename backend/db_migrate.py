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
     it (backend/play_stats.py) instead of counters kept in place.

The launcher's own P2P sync server is a separate process: on a launcher
node with old-rule data a peer import racing this rewrite is a known
window — keep P2P off for the first start after such an upgrade.
"""

import logging

import psycopg2

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

        # Cold-start seed: after the identity pass, so the bundle's rule
        # check compares against a fully renormalized database. The marker
        # lands only on a COMPLETE import — a skip (no bundle yet, phantom
        # layer off) or a shortfall re-evaluates on the next start. seed_v2
        # (2026-09-18) carries the audio records under payload v3; a node
        # that imported v1 lost those rows to migration 018 and takes them
        # again here.
        if not _marked(conn, "seed_v2"):
            import seed_import
            out["seed"] = seed_import.apply_seed(conn, settings.database_url)
            if out["seed"].get("complete"):
                _mark(conn, "seed_v2")
    finally:
        conn.close()
    logger.info("db_migrate: %s", out)
    return out
