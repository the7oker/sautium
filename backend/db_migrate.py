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
     here, before it can mint on the new rule beside old rows — unless the
     records step below fails closed: the rewrite sheds every seal, which
     that step reads, so it waits too (the `migration.waiting` event), the
     node mints on the new rule beside old rows meanwhile, and the rewrite's
     later run merges them. The rewrite's shed of every seal is re-sealed by
     the node's own sign_audio cadence. The
     play-stats step (`play_stats_derived_v1`) re-derives `local_play_stats`
     from `listening_history` once, the day the table became a function of
     it (backend/play_stats.py) instead of counters kept in place. The
     sub-floor step (`sub_floor_analysis_v1`) drops the node's own analysis
     of material under provenance.MIN_MATERIAL_SECONDS, computed before the
     floor existed (2026-09-18). The listen-times step (`listen_times_utc_v1`)
     moves the starts a launcher's tracker wrote as naive local time back to
     the instant they meant (play_stats.repaired_start, 2026-09-24). The
     variant-group step (`variant_raw_group_v1`, 2026-11-17, LIBRARY-WATCH)
     writes each copy's raw title (unwritten before 2026-09-28) and album
     credit as the folder rule makes them of its rows' tags
     (scanner.backfill_variant_groups), for copies made before they were
     kept: the rule finds a folder's copy by them. The
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
     came to carry its key's fingerprint (2026-11-07). The origins step
     (`slice_origins_v1`) marks the slice blobs kept before a blob came to
     carry its origin by the rules a fetch reads now: those that name the
     imported history (for ListenBrainz, any artist neither owned nor played
     to the end here) serve the node alone from then on, on a node without
     the family's dump (2026-11-14). The slice-records step
     (`slice_records_cut_v1`) cuts once what a node kept of who asked which
     artists: the contact rows of the requests about artists, a dump node's
     builds kept under a requester's spelling, and each kept build's moment
     of first ask, left to the day (2026-11-14). The rebuild step
     (`slice_rebuild_v1`) rebuilds a node's catalogue rows once from the
     slices it keeps, under the import's rules of that day — a slice's own
     subtree only, its rows of their columns' kinds, an artist or a group one
     row per MBID — and indexes the slices kept before the subject index, on
     a node without the dump (2026-11-07). The records step
     (`analysis_records_v4`) is the cutover to a record version per kind —
     features v4 without the zero-shot and instrument fields, instruments a
     v1 record of their own, segments untouched (2026-11-10): with the
     node's keys it keeps the features no other key sealed and no imported
     source holds, deletes the rest with their instruments, sheds every
     features seal left for the notary to sign them anew, splits the grids
     another key sealed whole under a first-hand row back onto its imported
     twin (one with a segment of this node's or an unsealed one deleted),
     joins the source rows from before the material groups, and deletes the
     unsealed grids under imported rows of tracks with a file here; it fails
     closed — no marker — while no key resolves and seals stand on records
     no imported source holds, and the seed waits for it. It runs first of
     the data steps: the seals are how it tells this node's records from
     another author's, and the identity re-normalization sheds every one.
     The labels step
     (`zero_shot_labels_v1`) loads the release's zero-shot label embeddings
     (backend/zero_shot.py) and scores every bundle of segments the node
     holds, its own and imported, the day moods, the vocal score and
     danceability came to be derived from the segments instead of analysed
     (2026-11-10); from then on the triggers on embedding_segments keep the
     scores.

Right after the schema deltas, once per database, the birth preset
(`birth_preset_v1`, _apply_birth_preset): the switches a node's setup chose
for the machine it read (SAUTIUM_BIRTH_PRESET=nano — no models, no streaming
library, nothing passed on), written only where the owner's row does not
exist, before the seed step reads the streaming library's switch.

Before the schema deltas, where this node's own role administers the
cluster (Docker), the assistant's read-only login is re-stated
(_assistant_role); the runner states its grants after the deltas.

The launcher's own P2P sync server is a separate process: on a launcher
node with old-rule data a peer import racing this rewrite is a known
window — keep P2P off for the first start after such an upgrade.

Beside the migrations, analyze_unrecorded gives back what a crash recovery
takes from autovacuum (below); the lifespan runs it in the background.
"""

import json
import logging
import os
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


# What a node is born with, by the name its setup read off the machine and
# wrote to .env (SAUTIUM_BIRTH_PRESET; PI-NODE-2B). "nano": no model runs, the
# streaming library is off, and the node passes nothing on — each an owner's
# switch from then on, changed in its own place in the Web UI.
BIRTH_PRESETS = {
    "nano": {"compute.models": False, "discovery.phantom_layer": False, "sync.pass_on": False},
}


def _apply_birth_preset(conn) -> dict:
    """The preset's switches, each written only where the owner's row does not
    exist. Read once, at the first migration pass of a database (the marker
    lands with or without a preset): a variable added to .env later changes
    nothing on a node that already lives. Before the seed step, which reads
    the streaming library's switch."""
    name = os.environ.get("SAUTIUM_BIRTH_PRESET", "").strip().lower()
    if not name:
        return {"preset": None, "written": []}
    if name not in BIRTH_PRESETS:
        raise ValueError(f"SAUTIUM_BIRTH_PRESET names no preset: {name!r} "
                         f"(known: {', '.join(sorted(BIRTH_PRESETS))})")
    written = []
    with conn.cursor() as cur:
        for key, value in BIRTH_PRESETS[name].items():
            cur.execute("INSERT INTO user_settings (key, value) VALUES (%s, %s::jsonb) "
                        "ON CONFLICT (key) DO NOTHING RETURNING key", (key, json.dumps(value)))
            if cur.fetchone():
                written.append(key)
    conn.commit()
    return {"preset": name, "written": written}


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
    from provenance import ANALYSIS_KINDS, MIN_MATERIAL_SECONDS
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
        for table in ANALYSIS_KINDS:
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


def _mark_local_slices(conn) -> dict:
    """The slice blobs this node kept before a blob came to carry its origin
    (2026-11-14) marked once by the rules a fetch reads now
    (mb_slice_queries / lb_slice_queries LOCAL_ONLY_SQL): those that name
    the imported history serve this node alone from here on (a lookup of the
    owner's own before then left nothing to tell it by, an opened page's
    ListenBrainz statistics aside). Every blob of a family is a fetched one on a node without
    that family's dump, whatever its `relay` says — the column came with no
    backfill (052), so a blob kept before it names none. A dump node's are
    its own builds, nobody's history: each load empties the family's blobs
    (an MB load keeps those of its own version, and a dump node passes
    another author's on only rebuilt as its own) — what an MB dump node kept
    from before its load that it did not build (no match key) goes, as a load
    drops it now: one a same-account sibling built would pass for its own."""
    from desktop.p2p import lb_slice_queries, mb_slice_queries
    out = {}
    with conn.cursor() as cur:
        if not mb_slice_queries.local_dump_available(conn):
            cur.execute("UPDATE mb_slice_blobs b SET local_only = "
                        + mb_slice_queries.LOCAL_ONLY_SQL.format(nk="b.name_key"))
            out["mb_marked"] = cur.rowcount
        else:
            cur.execute("DELETE FROM mb_slice_blobs WHERE match_key IS NULL")
            out["mb_dropped"] = cur.rowcount
        if not lb_slice_queries.local_dump_available(conn):
            cur.execute("UPDATE lb_slice_blobs b SET local_only = "
                        + lb_slice_queries.LOCAL_ONLY_SQL.format(mbid="b.artist_mbid"))
            out["lb_marked"] = cur.rowcount
        cur.execute("SELECT (SELECT count(*) FROM mb_slice_blobs WHERE local_only), "
                    "       (SELECT count(*) FROM lb_slice_blobs WHERE local_only)")
        out["mb_local"], out["lb_local"] = cur.fetchone()
    conn.commit()
    return out


def _cut_slice_records(conn) -> dict:
    """What a node kept of who asked which artists, cut once (2026-11-14):
    the contact rows of the requests about artists (slices of either family,
    the catalogue search — each with its requester's key and address and the
    moment it asked; the code keeps none from now on), the builds a dump
    node kept under a requester's spelling rather than one MusicBrainz spells
    (mb_slice_queries.MUSICBRAINZ_NAME_SQL), and the moment of each kept
    build's first ask, of each check of a kept blob and of each check's
    plan — another node's ask drew it —, left to the day."""
    from desktop.p2p import contact_log, lb_slice_queries, mb_slice_queries
    out = {}
    with conn.cursor() as cur:
        cur.execute("DELETE FROM p2p_contact_events WHERE endpoint = ANY(%s)",
                    (sorted(contact_log.ARTIST_FAMILIES),))
        out["contact_rows"] = cur.rowcount
        if mb_slice_queries.local_dump_available(conn):
            cur.execute("DELETE FROM mb_slice_blobs b WHERE b.match_key IS NOT NULL AND NOT "
                        + mb_slice_queries.MUSICBRAINZ_NAME_SQL.format(nk="b.name_key"))
            out["spellings_dropped"] = cur.rowcount
        cur.execute("UPDATE mb_slice_blobs SET created_at = date_trunc('day', created_at) "
                    "WHERE match_key IS NOT NULL")
        out["mb_builds_dated"] = cur.rowcount
        if lb_slice_queries.local_dump_available(conn):
            cur.execute("UPDATE lb_slice_blobs SET created_at = date_trunc('day', created_at) "
                        "WHERE relay IS NULL")
            out["lb_builds_dated"] = cur.rowcount
        out["checks_dated"] = 0
        for table in ("mb_slice_blobs", "lb_slice_blobs"):
            cur.execute(f"UPDATE {table} SET checked_at = date_trunc('day', checked_at) "
                        "WHERE checked_at IS NOT NULL")
            out["checks_dated"] += cur.rowcount
        cur.execute("UPDATE slice_check_plans SET planned_at = date_trunc('day', planned_at)")
        out["plans_dated"] = cur.rowcount
    conn.commit()
    return out


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


def _own_keys() -> frozenset:
    """This node's keys, lower-case hex: the one sign() signs with
    (p2p_identity.load_signing_key) and every key it retired (node_info.json's
    `previous`, witness.own_keys); empty when no identity resolves. No
    certificate is read for a key: one left in the identity folder from an
    earlier identity names that identity's key, and the folder's certificate
    counts only when it names the key that signs."""
    from pathlib import Path

    from desktop.p2p import witness
    from p2p_identity import load_signing_key
    from sign_audio import _pubkey_hex
    key = load_signing_key(settings)
    if key is None:
        return frozenset()
    folder = Path(settings.p2p_identity_dir) if settings.p2p_identity_dir else None
    return witness.own_keys(_pubkey_hex(key), folder)


# A record's seal names its author: one of the node's keys, or another's.
_OTHERS = ("({t}.signature IS NOT NULL"
           " AND NOT lower({t}.author_pubkey) = ANY(CAST(:keys AS text[])))")

def _split_flipped(db, keys: list) -> dict:
    """The grids another author sealed under this node's own source row — a
    registration flipped an imported row first-hand until 2026-11-10
    (provenance) — moved to that row's imported twin: the same declaration,
    imported, put in the first-hand row's group. Only a bundle every segment
    of which another key sealed moves: one holding a segment of this node's
    key or an unsealed one is no author's whole record, and is deleted — the
    passes derive it again from a file here, or a pull brings it back (a
    stream row's, which nothing here re-derives). The flip wrote this node's
    duration over the author's, and a seal binds the author's: a bundle whose
    seals do not verify under the twin's declaration is deleted too, and its
    next pull brings it under its author's."""
    from sqlalchemy import text

    import record_sig as rs
    from sign_audio import _vector_bytes
    others = _OTHERS.format(t="es")
    flipped = db.execute(text(f"""
        SELECT e.id, e.track_id::text AS track, e.model_id::text AS model, s.chromaprint,
               s.duration_seconds, s.grid_version, COALESCE(s.material_of, s.id) AS root,
               bool_and({others}) AS whole
          FROM embeddings e JOIN analysis_sources s ON s.id = e.analysis_source_id
          JOIN embedding_segments es ON es.embedding_id = e.id
         WHERE NOT s.imported
         GROUP BY e.id, s.id
        HAVING bool_or({others})
         ORDER BY e.id"""), {"keys": keys}).fetchall()
    moved = failed = mixed = 0
    for f in flipped:
        if not f.whole:
            db.execute(text("DELETE FROM embeddings WHERE id = :e"), {"e": f.id})
            mixed += 1
            continue
        twin = db.execute(text("""
            INSERT INTO analysis_sources (track_id, chromaprint, duration_seconds, grid_version,
                                          imported, material_of)
            VALUES (CAST(:t AS uuid), :fp, :dur, :grid, true, :root)
            ON CONFLICT (track_id, chromaprint_key, duration_seconds, imported, copy_rank) DO NOTHING
            RETURNING id"""), {"t": f.track, "fp": f.chromaprint, "dur": f.duration_seconds,
                               "grid": f.grid_version, "root": f.root}).scalar()
        if twin is None:
            twin = db.execute(text("""
                SELECT id FROM analysis_sources
                 WHERE track_id = CAST(:t AS uuid) AND chromaprint = :fp AND imported
                   AND duration_seconds IS NOT DISTINCT FROM :dur"""),
                {"t": f.track, "fp": f.chromaprint, "dur": f.duration_seconds}).scalar()
        db.execute(text("UPDATE embeddings SET analysis_source_id = :twin WHERE id = :e"),
                   {"twin": twin, "e": f.id})
        segments = db.execute(text("""
            SELECT segment_index, vector::text AS vec, author_pubkey, signature
              FROM embedding_segments WHERE embedding_id = :e AND signature IS NOT NULL"""),
            {"e": f.id}).fetchall()
        if all(rs.verify(rs.segment_payload(
                s.author_pubkey, f.track, f.chromaprint, f.duration_seconds, f.model,
                s.segment_index, rs.vector_hash(_vector_bytes(s.vec)),
                grid_version=f.grid_version), s.signature, s.author_pubkey) for s in segments):
            moved += 1
        else:
            db.execute(text("DELETE FROM embeddings WHERE id = :e"), {"e": f.id})
            failed += 1
    return {"bundles_split": moved, "bundles_failing_the_twin": failed,
            "bundles_mixed_deleted": mixed}


def _reseal_analysis_records() -> dict:
    """The cutover to the per-kind records (record_sig: features v4,
    instruments v1), at the node's first start on that release, in one
    transaction; no audio decoded. Delta 056 copied every features row's
    instruments into track_instruments, unsigned; this step tells the node's
    own records from another author's by the seal, with the node's keys:

      - kept is a features row no other key sealed and no imported source
        holds — unsealed, or sealed by one of this node's keys, under a
        first-hand source or under none; every other row goes, with the
        instruments copied from it (and any instruments row another key
        sealed or an imported source holds): a row under an imported source
        goes even when this node's key sealed it — a node of the same
        account shares the key, and sign() never re-signs under an imported
        source. Deleted, a row is either the node's own pass's work (it holds
        the file) or comes again from its author;
      - every seal left on a features row is shed (a v3 seal), for the
        notary's startup wake to sign the features in v4 and the instruments
        in v1 (sign_audio);
      - the grids another key sealed whole under a first-hand row are split
        back onto that row's imported twin, and one that also holds a
        segment of this node's or an unsealed one is deleted
        (_split_flipped);
      - the source rows from before the material groups are joined
        (provenance.join_existing_rows);
      - a grid all unsealed under an imported row, on a track with an
        analysis-source file here, goes: an import arrives sealed, so it is
        this node's own analysis an identity merge re-pointed onto the
        imported row before a shed unsealed it (or a foreign one a shed
        unsealed — the two cannot be told apart), never re-signed and never
        re-derived as it stood; deleted, the passes derive it again.

    It fails closed: with no key resolving while records no imported source
    holds carry seals, the node had an identity the step cannot see (an
    environment not where it looks), and deciding would delete the node's
    own sealed records as another's — `done` False, an error logged, no
    marker, and the next start tries again. A node whose records carry no
    such seal never had a key, and the step runs. Counts what it did, and
    the first-hand embeddings without segments (no segment, no derived
    zero-shot values)."""
    from sqlalchemy import text

    import provenance
    from database import SessionLocal
    from desktop.node_identity import SEALED_FIRST_HAND_SQL

    keys = sorted(_own_keys())
    started = time.monotonic()
    db = SessionLocal()
    try:
        # Seals on records no imported source holds — the node's own, unless
        # it never had a key: what deciding without the node's keys would
        # take for another author's.
        if not keys and db.execute(text(SEALED_FIRST_HAND_SQL)).scalar():
            logger.error("the analysis records are not re-sealed: records no imported source holds "
                         "carry seals, and no key of this node resolves (P2P_USERNAME/P2P_PASSWORD, "
                         "or the identity folder) — the node's own would go as another's; the next "
                         "start tries again")
            return {"done": False, "keys": 0}
        p = {"keys": keys}
        db.execute(text(f"""
            CREATE TEMP TABLE records_v4_gone ON COMMIT DROP AS
            SELECT a.id, a.track_id FROM audio_features a
              LEFT JOIN analysis_sources s ON s.id = a.analysis_source_id
             WHERE {_OTHERS.format(t='a')} OR s.imported"""), p)
        counts = {"keys": len(keys)}
        counts["features_deleted"] = db.execute(text(
            "DELETE FROM audio_features a USING records_v4_gone g WHERE a.id = g.id")).rowcount
        counts["instruments_deleted"] = db.execute(text(f"""
            DELETE FROM track_instruments t
             WHERE t.track_id IN (SELECT track_id FROM records_v4_gone)
                OR {_OTHERS.format(t='t')}
                OR EXISTS (SELECT 1 FROM analysis_sources s
                            WHERE s.id = t.analysis_source_id AND s.imported)"""), p).rowcount
        counts["features_kept"] = db.execute(text("SELECT count(*) FROM audio_features")).scalar()
        counts["feature_seals_shed"] = db.execute(text("""
            UPDATE audio_features
               SET author_pubkey = NULL, signature = NULL, batch_root = NULL, merkle_proof = NULL
             WHERE signature IS NOT NULL""")).rowcount
        counts.update(_split_flipped(db, keys))
        counts["sources_joined"] = provenance.join_existing_rows(db)
        counts["unsealed_under_imported_deleted"] = db.execute(text("""
            DELETE FROM embeddings e USING analysis_sources s
             WHERE s.id = e.analysis_source_id AND s.imported
               AND EXISTS (SELECT 1 FROM embedding_segments es WHERE es.embedding_id = e.id)
               AND NOT EXISTS (SELECT 1 FROM embedding_segments es
                                WHERE es.embedding_id = e.id AND es.signature IS NOT NULL)
               AND EXISTS (SELECT 1 FROM media_files f
                            WHERE f.track_id = e.track_id AND f.is_analysis_source)""")).rowcount
        counts["own_embeddings_without_segments"] = db.execute(text("""
            SELECT count(*) FROM embeddings e JOIN analysis_sources s ON s.id = e.analysis_source_id
             WHERE NOT s.imported
               AND NOT EXISTS (SELECT 1 FROM embedding_segments es
                                WHERE es.embedding_id = e.id)""")).scalar()
        db.commit()
    finally:
        db.close()
    return {"done": True, **counts, "seconds": round(time.monotonic() - started, 1)}


def _load_zero_shot_labels(conn) -> dict:
    """The release's label embeddings into zero_shot_labels, and the scores
    of every embedding that has segments (zero_shot_derive — the triggers'
    own derivation). The labels' model row is made here when the node has
    none yet: a fresh node gets it at its first analysis or import, and the
    labels reference it now. The lock keeps segment writers out from before
    the labels exist until they are committed with the scores: an import
    committing in between would have run its trigger with no labels, after
    this step's snapshot. One transaction; run again, it writes the same
    rows.

    `unscored` counts the bundles the derivation gave nothing: those holding
    a segment that is not a unit vector, which no later write will score
    either. A row holding the model's name under another id (an import of a
    release that took a peer's word for the pair) leaves the step undone —
    `loaded` False, no marker, the next start tries again — and the node
    without derived scores, not without its start."""
    import base64

    from psycopg2.extras import execute_values

    import zero_shot
    from record_sig import vector_from_bytes
    from uuid_utils import embedding_model_uuid

    doc = zero_shot.load_labels()
    model_id = str(embedding_model_uuid(doc["model"]))
    labels = [(model_id, label_set, label["key"], label["prompt"],
               str(vector_from_bytes(base64.b64decode(label["vector"]))))
              for label_set, members in doc["sets"].items() for label in members]
    started = time.monotonic()
    with conn.cursor() as cur:
        cur.execute("SELECT id::text FROM embedding_models WHERE name = %s AND id <> %s",
                    (doc["model"], model_id))
        other = cur.fetchone()
        if other:
            conn.rollback()
            logger.error("the zero-shot labels are not loaded: embedding_models holds the name %s "
                         "under %s, not under the id of that name (%s); moods, the vocal score and "
                         "danceability stay underived until that row is gone",
                         doc["model"], other[0], model_id)
            return {"loaded": False, "model_named_under": other[0]}
        cur.execute("""
            INSERT INTO embedding_models (id, name, description, dimension, logit_scale)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET logit_scale = EXCLUDED.logit_scale
        """, (model_id, doc["model"], f"CLAP audio embedding model ({doc['dimension']}d)",
              doc["dimension"], doc["logit_scale"]))
        cur.execute("LOCK TABLE embedding_segments IN SHARE ROW EXCLUSIVE MODE")
        execute_values(cur, """
            INSERT INTO zero_shot_labels (model_id, label_set, key, prompt, vector) VALUES %s
            ON CONFLICT (model_id, key) DO UPDATE
               SET label_set = EXCLUDED.label_set, prompt = EXCLUDED.prompt,
                   vector = EXCLUDED.vector
        """, labels, template="(%s, %s::zero_shot_set, %s, %s, %s::vector)")
        cur.execute("""
            INSERT INTO zero_shot_scores (embedding_id, label_id, score)
            SELECT d.embedding_id, d.label_id, d.score
              FROM zero_shot_derive(ARRAY(SELECT e.id FROM embeddings e
                                           WHERE e.model_id = %s)) d
            ON CONFLICT (embedding_id, label_id) DO UPDATE SET score = EXCLUDED.score
        """, (model_id,))
        scores = cur.rowcount
        cur.execute("""
            SELECT count(*) FROM embeddings e
             WHERE e.model_id = %s
               AND EXISTS (SELECT 1 FROM embedding_segments es WHERE es.embedding_id = e.id)
               AND NOT EXISTS (SELECT 1 FROM zero_shot_scores s WHERE s.embedding_id = e.id)
        """, (model_id,))
        unscored = cur.fetchone()[0]
    conn.commit()
    if unscored:
        logger.warning("%d bundles hold a segment that is not a unit vector: they have no "
                       "derived scores", unscored)
    return {"loaded": True, "labels": len(labels), "scores": scores, "unscored": unscored,
            "seconds": round(time.monotonic() - started, 1)}


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

        if not _marked(conn, "birth_preset_v1"):
            out["birth_preset"] = _apply_birth_preset(conn)
            _mark(conn, "birth_preset_v1")

        # First: it reads the seals to tell this node's records from another
        # author's, and the identity re-normalization below sheds every seal
        # (a node from before 2026-08-25, or its backup, runs both at once) —
        # so while it fails closed, the re-normalization waits for it too: a
        # rule's duplicates merge at its later run, a foreign grid signed as
        # this node's never comes back.
        if not _marked(conn, "analysis_records_v4"):
            out["analysis_records_v4"] = _reseal_analysis_records()
            logger.info("analysis records v4: %s", out["analysis_records_v4"])
            if out["analysis_records_v4"]["done"]:
                _mark(conn, "analysis_records_v4")
            else:
                # Failed closed: it waits for a start that finds the node's
                # key, with the identity re-normalization and the seed — a
                # state that can last, so support sees it, not only the log.
                from desktop.p2p import diag_events
                diag_events.record(conn, "migration.waiting", {
                    "step": "analysis_records_v4", "why": "no_key",
                    "holds_identity_rule": not _marked(
                        conn, _IDENTITY_MARK.format(IDENTITY_RULE))})

        mark = _IDENTITY_MARK.format(IDENTITY_RULE)
        if not _marked(conn, mark) and not _marked(conn, "analysis_records_v4"):
            logger.warning("identity rule v%d not recorded — the re-normalization waits for the "
                           "analysis records' cutover, which reads the seals it sheds",
                           IDENTITY_RULE)
        elif not _marked(conn, mark):
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

        if not _marked(conn, "slice_origins_v1"):
            out["slice_origins"] = _mark_local_slices(conn)
            _mark(conn, "slice_origins_v1")

        if not _marked(conn, "slice_records_cut_v1"):
            out["slice_records_cut"] = _cut_slice_records(conn)
            _mark(conn, "slice_records_cut_v1")

        if not _marked(conn, "variant_raw_group_v1"):
            from scanner import backfill_variant_groups
            with conn.cursor() as cur:
                out["variant_groups"] = backfill_variant_groups(cur)
            conn.commit()
            _mark(conn, "variant_raw_group_v1")

        # Before the seed: its segments get their scores as they land.
        if not _marked(conn, "zero_shot_labels_v1"):
            out["zero_shot"] = _load_zero_shot_labels(conn)
            if out["zero_shot"]["loaded"]:
                _mark(conn, "zero_shot_labels_v1")

        # Cold-start seed: after the identity pass, so the bundle's rule
        # check compares against a fully renormalized database. The marker
        # lands only on a COMPLETE import — a skip (no bundle yet, phantom
        # layer off) or a shortfall re-evaluates on the next start. The
        # marker follows the bundle version in backend/seed/bundle.json, so a
        # new bundle is a new import with no code change: seed_v2
        # (2026-09-18) carries the audio records under payload v3, and a node
        # that imported v1 lost those rows to migration 018 and takes them
        # again here; seed_v4 (2026-11-10) the records of features v4 and
        # instruments v1, and a node that imported v3 lost its features to
        # the records' cutover. Only on a node past that cutover: a seed
        # landed before a cutover that failed closed would be deleted by it,
        # its imported sources' records being another author's.
        import seed_import
        info = seed_import.bundle_info()
        if (info is not None and _marked(conn, "analysis_records_v4")
                and not _marked(conn, f"seed_v{info['version']}")):
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
