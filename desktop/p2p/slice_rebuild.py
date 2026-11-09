"""Dropping what a penalized key brought, and rebuilding the catalogue rows
from the slices that are left.

A slice's rows land in the shared `mb_*` tables, first writer winning a
conflict, and carry no author; only the blobs (kept verbatim) and the subject
index (desktop/p2p/slice_trust.py) know who said what. So dropping an
author's or a relay's slices (`drop`) is forgetting its blobs, ledger rows,
index rows, clashes and check plans in the transaction that penalized the key,
and — when that removed any — asking for a rebuild (`request`): every `mb_*`
table of the slice protocol rebuilt in full from the blobs still kept
(`rebuild`), deleted and re-inserted in one transaction under the loader's
exclusive lock, so a reader sees the catalogue before it or after it, never
half of it, and is never blocked by it. The backend's rebuilder runs it off
every other thread (`run_pending`, woken by NOTIFY), once per request however
many came, and again after a restart until it commits; a clash MusicBrainz's
answers settled asks for one too. Every name a drop or a rebuild leaves
without its rows is asked again, the slice cycle woken for it.

What the canon made from dropped slices stays: every irreversible decision
was re-made on MusicBrainz's answers. What only the dropped slices supported
goes: the bindings to MBIDs the catalogue carried before the rebuild and no
kept slice carries after it, with the copies shown from them (the genres of
those groups, the year and cover of their phantom albums, the name and
caption the artist_mbids trigger wrote) — and the owned artists those
bindings held are taken by the canon again, as are those of a name whose
contested blob a rebuild landed.
"""

import gzip
import json
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterable, List, Optional

logger = logging.getLogger(__name__)

PENDING_KEY = "mb_slice.rebuild_pending"
CHANNEL = "sautium_slice_rebuild"


@contextmanager
def atomic(conn):
    """The caller's transaction, or — on an autocommit connection — one of
    its own: a penalty, the drop it causes and the rebuild it asks for commit
    together or not at all."""
    if not conn.autocommit:
        yield
        return
    conn.autocommit = False
    try:
        yield
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.autocommit = True


@contextmanager
def verdict(conn):
    """A verdict's transaction (`atomic`), the loader's lock taken shared
    first: a verdict waits for a rebuild that runs, and a rebuild for the
    verdicts under way, never each for the other's rows."""
    from desktop.p2p.mb_slice_queries import MB_LOAD_LOCK_KEY
    with atomic(conn):
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock_shared(%s)", (MB_LOAD_LOCK_KEY,))
        yield


def request(conn, reason: str) -> None:
    """Ask for a rebuild, in the caller's transaction; the backend's
    rebuilder is woken when it commits. A request made while a rebuild runs
    is a new one: that rebuild leaves it (run_pending)."""
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO user_settings (key, value) VALUES (%s, %s::jsonb)
                       ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value,
                                                       updated_at = CURRENT_TIMESTAMP""",
                    (PENDING_KEY, json.dumps({"reason": reason,
                                              "at": datetime.now(timezone.utc).isoformat()})))
        cur.execute(f"NOTIFY {CHANNEL}")


def _forget(conn, keys: List[str]) -> dict:
    """Every slice authored by one of `keys` or passed on by one: its blob,
    ledger row, index row, clashes and check plan; an LB slice's numbers too.
    The names and artists left unanswered are asked again: the slice cycles
    are woken."""
    with conn.cursor() as cur:
        cur.execute("""DELETE FROM mb_slice_fetches
                        WHERE lower(btrim(source_pubkey)) = ANY(%(k)s) OR lower(btrim(relay)) = ANY(%(k)s)""",
                    {"k": keys})
        names = cur.rowcount
        cur.execute("""DELETE FROM mb_slice_blobs
                        WHERE lower(btrim(author_pubkey)) = ANY(%(k)s) OR lower(btrim(relay)) = ANY(%(k)s)
                    RETURNING name_key""", {"k": keys})
        mb_keys = [r[0] for r in cur.fetchall()]
        cur.execute("""DELETE FROM lb_slice_fetches
                        WHERE lower(btrim(source_pubkey)) = ANY(%(k)s) OR lower(btrim(relay)) = ANY(%(k)s)
                    RETURNING artist_mbid::text""", {"k": keys})
        lb_lost = [r[0] for r in cur.fetchall()]
        cur.execute("""DELETE FROM lb_slice_blobs
                        WHERE lower(btrim(author_pubkey)) = ANY(%(k)s) OR lower(btrim(relay)) = ANY(%(k)s)
                    RETURNING artist_mbid::text""", {"k": keys})
        lb_keys = [r[0] for r in cur.fetchall()]
        if lb_lost:
            # The numbers those slices set go with them; the artists are asked again.
            cur.execute("DELETE FROM lb_artist WHERE artist_mbid = ANY(%s::uuid[])", (lb_lost,))
            cur.execute("DELETE FROM lb_recording WHERE artist_mbids <@ %s::uuid[]", (lb_lost,))
        cur.execute("DELETE FROM slice_clashes WHERE family = 'mb' AND key = ANY(%s)", (mb_keys,))
        cur.execute("""DELETE FROM slice_check_plans
                        WHERE (family = 'mb' AND key = ANY(%s)) OR (family = 'lb' AND key = ANY(%s))""",
                    (mb_keys, lb_keys))
        cur.execute("""DELETE FROM slice_index WHERE author = ANY(%(k)s) OR relay = ANY(%(k)s)
                    RETURNING family::text""", {"k": keys})
        indexed = [r[0] for r in cur.fetchall()]
        if names:
            cur.execute("NOTIFY sautium_mb_pending")
        if lb_lost:
            cur.execute("NOTIFY sautium_lb_pending")
    return {"names": names, "blobs": len(mb_keys), "lb_names": len(lb_lost), "lb_blobs": len(lb_keys),
            "indexed": indexed.count("mb")}


def drop(conn, keys: Iterable[str]) -> dict:
    """Forget every slice authored by one of `keys` or passed on by one, and —
    when the subject index shows one of them brought MB rows (it is
    append-only: a blob or a ledger row another answer replaced still left its
    rows) — ask for the rebuild that takes their rows out, in the caller's
    transaction (`verdict`, with the penalty). An LB slice's numbers go with
    its forgetting; a key that never brought an MB slice costs no rebuild.
    Returns counts."""
    keys = sorted({k.lower() for k in keys})
    if not keys:
        return {}
    out = _forget(conn, keys)
    rebuilt = bool(out["names"] or out["blobs"] or out["indexed"])
    if rebuilt:
        request(conn, "penalty")
    logger.warning("slices of %d penalized key(s) dropped (%s)%s", len(keys), out,
                   "; the catalogue is rebuilt" if rebuilt else "")
    return out


def _withdraw_lost(conn) -> dict:
    """The bindings to MBIDs the catalogue carried before the rebuild
    (_bound_artists, _bound_groups) and no kept slice carries after it, what
    was shown from them, and the owned artists they bound unstamped — the
    canon takes them again, and the slice cycle asks again for every name the
    rebuild left without its rows (`_rebuild`)."""
    with conn.cursor() as cur:
        cur.execute("""SELECT mbid::text FROM _bound_artists b
                        WHERE NOT EXISTS (SELECT 1 FROM mb_artist a WHERE a.gid = b.mbid)""")
        artists = [r[0] for r in cur.fetchall()]
        cur.execute("""SELECT mbid::text FROM _bound_groups b
                        WHERE NOT EXISTS (SELECT 1 FROM mb_release_group g WHERE g.gid = b.mbid)""")
        groups = [r[0] for r in cur.fetchall()]
        cur.execute("DELETE FROM artist_mbids WHERE mbid = ANY(%s::uuid[]) RETURNING artist_id::text",
                    (artists,))
        unbound = {r[0] for r in cur.fetchall()}
        bindings = len(unbound)
        cur.execute("""UPDATE album_artists SET mbid = NULL WHERE mbid = ANY(%s::uuid[])
                    RETURNING artist_id::text""", (artists,))
        unbound |= {r[0] for r in cur.fetchall()}
        cur.execute("""DELETE FROM album_genres ag USING albums al
                        WHERE ag.album_id = al.id AND al.musicbrainz_id = ANY(%s::uuid[])
                          AND NOT EXISTS (SELECT 1 FROM album_variants av WHERE av.album_id = al.id)""",
                    (groups,))
        cur.execute("""UPDATE albums al SET cover_url = NULL, release_year = NULL
                        WHERE al.musicbrainz_id = ANY(%s::uuid[])
                          AND NOT EXISTS (SELECT 1 FROM album_variants av WHERE av.album_id = al.id)""",
                    (groups,))
        phantoms = cur.rowcount
        cur.execute("""SELECT DISTINCT aa.artist_id::text FROM album_artists aa
                         JOIN albums al ON al.id = aa.album_id
                        WHERE al.musicbrainz_id = ANY(%s::uuid[])""", (groups,))
        unbound |= {r[0] for r in cur.fetchall()}
        cur.execute("UPDATE artists SET last_mb_sync = NULL WHERE id = ANY(%s::uuid[])",
                    (sorted(unbound),))
    return {"bindings_withdrawn": bindings, "phantom_albums_cleared": phantoms,
            "artists_retaken": len(unbound)}


def rebuild(conn) -> dict:
    """Every `mb_*` table of the slice protocol rebuilt from the kept blobs,
    in the caller's transaction under the loader's exclusive lock (taken for
    the transaction; a full load holds it until its completion marker, so a
    rebuild that waited for one finds the dump and leaves it alone). First
    what a penalized key still has here goes (a drop a crash cut short); then
    each kept blob is imported the way the slice client imports it — its own
    subtree only, its rows as the import checks them, shop pages only — but
    none whose author or source is banned, none that lost a clash. Of blobs
    whose rows contradict one another the first kept lands, the rest are
    contested anew and their clashes queued for MusicBrainz's answers. A name
    whose blob the rebuild leaves out is asked again (its ledger row void),
    of another author first. `mb_genre` comes from MusicBrainz's genre list,
    never a slice, and stays."""
    with atomic(conn):
        return _rebuild(conn)


def _rebuild(conn) -> dict:
    from desktop.mb_slice_client import _note_clashes, _rows, _shop_pages_only, carried, insert_rows
    from desktop.p2p import slice_trust, wire
    from desktop.p2p.mb_slice_queries import MB_LOAD_LOCK_KEY, SLICE_TABLES, local_dump_available
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (MB_LOAD_LOCK_KEY,))
    if local_dump_available(conn):
        return {"rebuilt": 0}
    forgotten = _forget(conn, sorted(slice_trust.penalized_keys(conn)))
    with conn.cursor() as cur:
        cur.execute("""CREATE TEMP TABLE _bound_artists ON COMMIT DROP AS
                       SELECT DISTINCT am.mbid FROM artist_mbids am
                         JOIN mb_artist a ON a.gid = am.mbid""")
        cur.execute("""CREATE TEMP TABLE _bound_groups ON COMMIT DROP AS
                       SELECT DISTINCT al.musicbrainz_id AS mbid FROM albums al
                         JOIN mb_release_group g ON g.gid = al.musicbrainz_id""")
        cur.execute("SELECT name_key FROM mb_slice_blobs WHERE contested")
        contested_before = {r[0] for r in cur.fetchall()}
        cur.execute("SELECT name_key FROM mb_slice_blobs")
        left_out = {r[0] for r in cur.fetchall()}
        # DELETE, not TRUNCATE: a TRUNCATE holds every table ACCESS EXCLUSIVE
        # until the commit, and every reader of the catalogue — the canon, the
        # peer surface's health, a slice served — would wait the whole rebuild
        # out; deleted rows leave readers the old snapshot meanwhile.
        for table in SLICE_TABLES:
            cur.execute(f"DELETE FROM {table}")
    kept = refused = contested = 0
    landed: List[str] = []
    with conn.cursor(name="slice_rebuild_blobs") as blobs:
        blobs.itersize = 100
        blobs.execute("""
            SELECT b.name_key, b.blob_gz, btrim(b.author_pubkey), btrim(b.relay), b.dump_version,
                   coalesce(b.payload_sha256, encode(sha256(b.blob_gz), 'hex'))
              FROM mb_slice_blobs b
             WHERE NOT (b.suspect AND b.contested)
               AND NOT EXISTS (
                     SELECT 1 FROM p2p_node_bans n
                      WHERE n.pubkey IS NOT NULL
                        AND lower(n.pubkey) IN (lower(btrim(b.author_pubkey)),
                                                lower(coalesce(btrim(b.relay), btrim(b.author_pubkey)))))
               AND NOT EXISTS (
                     SELECT 1 FROM p2p_identities f
                      WHERE f.status = 'failed'
                        AND f.pubkey IN (lower(btrim(b.author_pubkey)),
                                         lower(coalesce(btrim(b.relay), btrim(b.author_pubkey)))))
             ORDER BY b.created_at, b.name_key""")
        for name_key, blob_gz, author, relay, version, payload in blobs:
            core = wire.loads(gzip.decompress(bytes(blob_gz)))
            rows = _rows(core)
            if rows is None or not slice_trust.subtree_ok(core, rows):
                # Kept before the import's checks: held back, never passed on.
                with conn.cursor() as cur:
                    cur.execute("UPDATE mb_slice_blobs SET suspect = TRUE WHERE name_key = %s",
                                (name_key,))
                refused += 1
                continue
            _received, _inserted, clashed = insert_rows(conn, name_key, _shop_pages_only(rows))
            left_out.discard(name_key)
            with conn.cursor() as cur:
                cur.execute("""UPDATE mb_slice_blobs SET contested = %s, payload_sha256 = %s,
                                                         mbids = %s::uuid[]
                                WHERE name_key = %s""",
                            (bool(clashed), payload, carried(rows), name_key))
                cur.execute("""SELECT 1 FROM slice_index WHERE family = 'mb' AND key = %s
                                  AND payload_sha256 = %s""", (name_key, payload))
                indexed = cur.fetchone() is not None
            if not indexed:
                # A blob kept before the subject index existed.
                artists, groups = slice_trust.subjects(core, rows)
                slice_trust.index(conn, "mb", name_key, author, relay or author, None, payload,
                                  version or "", artists, groups)
            if clashed:
                contested += 1
                _note_clashes(conn, name_key, payload, clashed, author)
            else:
                kept += 1
                if name_key in contested_before:
                    landed.append(name_key)
    withdrawn = _withdraw_lost(conn)
    from desktop.p2p import slice_checks
    # Left out (lost a clash, a banned key's, refused by the import's checks):
    # the name is asked again, of another author first.
    slice_checks.reopen(conn, sorted(left_out))
    with conn.cursor() as cur:
        if landed:
            # A name whose contested blob landed: its owned artists, stamped
            # while their rows were missing, are taken by the canon again.
            cur.execute("UPDATE artists SET last_mb_sync = NULL WHERE lower(btrim(name)) = ANY(%s)",
                        (landed,))
        # A name whose rows no kept blob holds any more is asked again.
        cur.execute("""DELETE FROM mb_slice_fetches f
                        WHERE f.matched_ids > 0
                          AND NOT EXISTS (SELECT 1 FROM mb_slice_blobs b WHERE b.name_key = f.name_key)""")
        reask = cur.rowcount
        if reask:
            cur.execute("NOTIFY sautium_mb_pending")
    out = {"rebuilt": kept, "refused": refused, "contested": contested, "landed": len(landed),
           "names_reasked": reask, "left_out": len(left_out), **withdrawn}
    if forgotten.get("blobs") or forgotten.get("names"):
        out["forgotten"] = forgotten
    logger.info("slice catalogue rebuilt: %s", out)
    return out


def run_pending(conn) -> Optional[dict]:
    """The rebuild asked for (`request`), when one waits — or when a
    penalized key's MB slices are still kept (a drop a crash cut short, an
    import that landed after the verdict) — in one transaction with the
    request's removal: the deletes and the re-import commit together, and a
    crash or a failure leaves the request for the next run. A request made
    meanwhile changes the marker, so it stays for another run. A penalized
    key's LB slices left kept are forgotten (no rebuild: their numbers go
    with them). None when there was nothing to do."""
    from desktop.p2p import slice_trust
    with atomic(conn):
        with conn.cursor() as cur:
            cur.execute("SELECT value::text FROM user_settings WHERE key = %s", (PENDING_KEY,))
            row = cur.fetchone()
            if row is None:
                cur.execute("""SELECT EXISTS (SELECT 1 FROM mb_slice_blobs b JOIN slice_penalties p
                                               ON p.pubkey IN (btrim(b.author_pubkey), btrim(b.relay))),
                                      EXISTS (SELECT 1 FROM lb_slice_blobs b JOIN slice_penalties p
                                               ON p.pubkey IN (btrim(b.author_pubkey), btrim(b.relay)))""")
                mb_left, lb_left = cur.fetchone()
                if not mb_left:
                    if not lb_left:
                        return None
                    return {"forgotten": _forget(conn, sorted(slice_trust.penalized_keys(conn)))}
        out = rebuild(conn)
        if row is not None:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM user_settings WHERE key = %s AND value::text = %s",
                            (PENDING_KEY, row[0]))
    return out
