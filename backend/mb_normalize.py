"""Album-centric MB normalization (Ф3) — recording-centric, SQL-pushed, cascaded.

Per artist, in essentially ONE Postgres query per pass: match owned tracks → MB
**recordings** (the release-independent song identity = exactly our `tracks`
notion) by EXACT normalized-title+duration (a hash join — fast bulk) then FUZZY
only for the leftover. From the matched recordings we derive each album's
release-group (album canon) and the artist's MBID(s); the recording gid is the
track-norm (sync win). Content-based → resolves freely-named (vinyl) albums.

**Progressive distillation (cascade):** a strict cheap pass runs on everything;
each later pass re-matches only the unverified RESIDUE with looser parameters,
each targeting a failure mode (short ambient/EP vs the min gate; live/remaster
duration drift; title-only). The expensive passes touch only what's left. Floor =
"not in MB" (demos/bootlegs) — distilled toward, not broken.

The artist's candidate recordings are materialised ONCE into a session TEMP TABLE
(`_recs`) so the cascade's passes reuse them instead of re-scanning mb_recording
(39M) each time. Heavy matching runs in SQL, never Python (CLAUDE.md). Local-only.
Read-only `resolve_artist`; apply is separate. Indexes: `mb_recording(artist_credit)`,
`mb_track(recording)`, `mb_artist(lower(name))`.
"""
import logging

import psycopg2.extras
from sqlalchemy import text as _sql

import mb_backend as mb
from database import SessionLocal
from db_pool import db_execute, db_query, get_conn
from discography import split_edition, title_residual
from normalize_artists import (
    normalize_compound_artist, recanonicalize_album, recanonicalize_artist,
)
from uuid_utils import album_uuid, artist_uuid, normalize

logger = logging.getLogger(__name__)

_CAND_SCORE = 70   # candidate name-score floor — drops generic partial namesakes
_NO_DUR = 9999     # duration tolerance that effectively ignores duration (title-only)

# Cascade passes (cheap→specialised), each re-tried only on the prior residue:
#   (label, dur_tol_seconds, fuzzy_sim, min_matched, coverage_frac, cap_min_to_ntracks)
# cap=True lets a single tight (title+duration) match verify a 1-track album
# (min(minm, ntracks)); cap=False keeps minm as a hard floor so the loosest
# title-only pass never verifies a 1-track album on one unanchored match.
_CASCADE = [
    ("strict",   8,       0.50, 3, 0.60, True),   # bulk: exact-ish title + tight duration
    ("short",    8,       0.50, 2, 0.60, True),   # short ambient/EP vs the min-3 gate (reuses strict)
    ("wide-dur", 25,      0.45, 2, 0.55, True),   # live / remaster / vinyl duration drift
    ("title",    _NO_DUR, 0.58, 2, 0.55, False),  # title-only → need >=2 (no 1-track on one loose match)
]


def _norm(col: str) -> str:
    # Unicode-safe: keep alnum (incl Cyrillic/accented); punctuation → space so
    # "Part 1-5" → "part 1 5" (not "15"); then collapse + trim.
    return f"btrim(regexp_replace(lower(btrim({col})),'[^[:alnum:]]+',' ','g'))"


# Materialise the candidate artist(s)' recordings into a session temp table once.
_RECS_SQL = f"""
CREATE TEMP TABLE _recs AS
WITH cand AS (SELECT id, gid FROM mb_artist WHERE gid = ANY(%(cands)s::uuid[])),
cand_credit AS (
    SELECT DISTINCT acn.artist_credit, c.gid::text AS artist_mbid
    FROM cand c JOIN mb_artist_credit_name acn ON acn.artist = c.id
)
SELECT rec.id, rec.gid::text AS rec_mbid, cc.artist_mbid,
       {_norm('rec.name')} AS rname, rec.length
FROM mb_recording rec JOIN cand_credit cc ON cc.artist_credit = rec.artist_credit
"""

# Match owned tracks (scoped to %(albums)s) against the temp _recs.
_MATCH_SQL = f"""
WITH owned AS (
    SELECT a.id::text AS album_id, t.id::text AS track_id,
           {_norm('t.title')} AS ntitle, mf.duration_seconds::float AS dur
    FROM albums a
    JOIN album_artists aa ON aa.album_id=a.id AND aa.role='primary'
        AND aa.artist_id=%(artist)s::uuid
    JOIN album_variants av ON av.album_id=a.id
    JOIN media_files mf ON mf.album_variant_id=av.id
    JOIN tracks t ON t.id=mf.track_id
    WHERE a.id = ANY(%(albums)s::uuid[])
    GROUP BY a.id, t.id, t.title, mf.duration_seconds
),
cand AS (   -- recording candidates per owned track within the pass's duration tolerance;
            -- exact (=) and fuzzy (%%) are separate scans so each uses its _recs(rname)
            -- index. `matched` collapses to the single best pick per track (recording-norm
            -- + coverage gate); RG resolution is a SEPARATE concern (see _RG_SQL).
    SELECT o.album_id, o.track_id, r.rec_mbid, r.artist_mbid, TRUE AS exact,
           abs(coalesce(r.length, o.dur*1000)/1000.0 - o.dur) AS dd
    FROM owned o JOIN _recs r ON r.rname = o.ntitle
        AND (r.length IS NULL OR abs(r.length/1000.0 - o.dur) <= %(durtol)s)
    UNION ALL
    SELECT o.album_id, o.track_id, r.rec_mbid, r.artist_mbid, FALSE,
           abs(coalesce(r.length, o.dur*1000)/1000.0 - o.dur)
    FROM owned o JOIN _recs r ON r.rname %% o.ntitle
        AND (r.length IS NULL OR abs(r.length/1000.0 - o.dur) <= %(durtol)s)
)
SELECT DISTINCT ON (album_id, track_id) album_id, track_id, rec_mbid, artist_mbid
FROM cand
ORDER BY album_id, track_id, exact DESC, dd
"""


# RG resolution, run ONCE post-cascade over the verified albums — decoupled from which pass
# verified them. Title-only candidates (no duration filter): bootlegs are excluded by
# release.status, so studio-vs-live/superset is settled by the BIDIRECTIONAL size check, not
# duration. (Coupling RG to the verifying pass mis-fired on heavily duration-drifted rips that
# verify on a tight pass via non-studio matches and never reach the title-width candidate set.)
_RG_SIM = 0.55
_RG_DURTOL = 120     # generous band: keeps studio/remaster/vinyl drift, cuts long-live-jam blow-up
_RG_OWNED_MIN = 0.5    # hard min owned-coverage (also the perf early-cut on the comp-explosion join)
_RG_OWNED_STRONG = 0.66  # owned-coverage that ALONE proves "owned album = release R" (regional retitles
                         # share no name yet cover ~all of themselves); below it, the name must agree
_RG_NAME_MIN = 0.30      # name-corroboration floor: separates edition-noisy real albums (sim >= .32:
                         # "Blackdance (Remastered 2017)" vs "Blackdance") from comps sampling a studio
                         # album under a different name (sim ~0: "Star Profile" vs "I Am What I Am")
_RG_MIN_CONTENT = 3      # the content-only path needs this many matched tracks — 1-2 track fragments
                         # trivially "cover" any release holding that track, so they must match by name
_RG_SQL = f"""
WITH owned AS (
    SELECT a.id::text AS album_id, t.id::text AS track_id, {_norm('t.title')} AS ntitle,
           mf.duration_seconds::float AS dur
    FROM albums a
    JOIN album_artists aa ON aa.album_id=a.id AND aa.role='primary'
        AND aa.artist_id=%(artist)s::uuid
    JOIN album_variants av ON av.album_id=a.id
    JOIN media_files mf ON mf.album_variant_id=av.id
    JOIN tracks t ON t.id=mf.track_id
    WHERE a.id = ANY(%(albums)s::uuid[])
    GROUP BY a.id, t.id, t.title, mf.duration_seconds
),
nt AS (SELECT album_id, count(DISTINCT track_id) AS total FROM owned GROUP BY album_id),
atitle AS (SELECT a.id::text AS album_id, {_norm('a.title')} AS atitle
           FROM albums a WHERE a.id = ANY(%(albums)s::uuid[])),
cand AS (   -- title candidates within a GENEROUS duration band (the band is a perf bound on
            -- mega-bootlegged artists, not a correctness lever — studio recordings sit within
            -- seconds of the owned file); exact (=) and fuzzy (%%) each use their _recs index
    SELECT o.album_id, o.track_id, r.id AS rec_id FROM owned o JOIN _recs r ON r.rname = o.ntitle
        AND (r.length IS NULL OR abs(r.length/1000.0 - o.dur) <= {_RG_DURTOL})
    UNION
    SELECT o.album_id, o.track_id, r.id FROM owned o JOIN _recs r ON r.rname %% o.ntitle
        AND (r.length IS NULL OR abs(r.length/1000.0 - o.dur) <= {_RG_DURTOL})
),
rel_cov AS (   -- per (owned album, OFFICIAL release of ANY studio-primary type incl comp/live):
               -- owned tracks covered. Comps are NOT pruned — an owned compilation must be free to
               -- match its OWN comp release (which holds all its tracks, so owned_cov wins) instead
               -- of being forced onto a studio/live album it merely shares songs with. Early-cut to
               -- releases covering >= half the owned album: that is half the bidirectional gate, and
               -- it bounds the "a hit song rides hundreds of comps" join blow-up.
    SELECT album_id, rg_mbid, rel_id, owned_cov, total, is_comp, rgn FROM (
        SELECT c.album_id, rg.gid::text AS rg_mbid, r.id AS rel_id, nt.total,
               {_norm('rg.name')} AS rgn,
               count(DISTINCT c.track_id) AS owned_cov,
               EXISTS (SELECT 1 FROM mb_release_group_secondary_type_join j
                       JOIN mb_release_group_secondary_type st ON st.id = j.secondary_type
                       WHERE j.release_group = rg.id
                         AND st.name IN ('Compilation', 'DJ-mix', 'Mixtape/Street')) AS is_comp
        FROM cand c
        JOIN mb_track mt ON mt.recording = c.rec_id
        JOIN mb_medium md ON md.id = mt.medium
        JOIN mb_release r ON r.id = md.release
            AND (r.status IS NULL OR r.status NOT IN (3, 4))  -- drop Bootleg + Pseudo-Release
        JOIN mb_release_group rg ON rg.id = r.release_group
        JOIN mb_release_group_primary_type pt ON pt.id = rg.type
            AND pt.name IN ('Album', 'EP', 'Single')
        JOIN nt ON nt.album_id = c.album_id
        GROUP BY c.album_id, rg.gid, rg.id, r.id, nt.total
    ) q WHERE owned_cov::float >= {_RG_OWNED_MIN} * total
),
rel_total AS (   -- true track count of just the candidate releases (the other half of the
                 -- bidirectional check), scoped to releases that surfaced in rel_cov
    SELECT d.rel_id, count(*) AS n
    FROM (SELECT DISTINCT rel_id FROM rel_cov) d
    JOIN mb_medium md ON md.release = d.rel_id
    JOIN mb_track mt ON mt.medium = md.id
    GROUP BY d.rel_id
),
valid AS (   -- candidates passing the FULL bidirectional gate BEFORE ranking, so the name tie-break
             -- picks the best-named VALID release and a high-name-sim near-miss that fails the gate
             -- can't shadow a real lower-named match (which would wrongly NULL the album).
    SELECT rc.album_id, rc.rg_mbid, rc.owned_cov, rc.total, rt.n AS rel_total,
           similarity(rc.rgn, at.atitle) AS namesim, rc.is_comp
    FROM rel_cov rc
    JOIN rel_total rt ON rt.rel_id = rc.rel_id
    JOIN atitle at ON at.album_id = rc.album_id
    WHERE rc.owned_cov::float >= 0.5 * rt.n   -- release-half: R is not mostly other tracks (owned-half pre-cut)
      AND ((rc.owned_cov::float >= {_RG_OWNED_STRONG} * rc.total   -- strong content (>= a few tracks), OR
            AND rc.owned_cov >= {_RG_MIN_CONTENT})
           OR similarity(rc.rgn, at.atitle) >= {_RG_NAME_MIN})     -- the name agrees (same album, imperfect match)
      AND (NOT rc.is_comp                                         -- a comp ALSO needs the name (fuzzy tracklists
           OR similarity(rc.rgn, at.atitle) >= {_RG_NAME_MIN})    -- overlap many studio albums by content alone)
)
SELECT album_id, rg_mbid FROM (   -- among content-valid releases, the one whose RG NAME is most like the
                                  -- owned title (tie-break, never a gate: a regional retitle with a lone
                                  -- match still wins on content; two sibling comps split by name). Then
                                  -- most covered, closest in size (rejects supersets / medleys), studio.
    SELECT album_id, rg_mbid,
           row_number() OVER (PARTITION BY album_id
               ORDER BY namesim DESC, owned_cov DESC, abs(rel_total - total), is_comp) AS rn
    FROM valid
) x WHERE x.rn = 1
"""


def _owned_counts(artist_id):
    rows = db_query("""
        SELECT a.id::text AS album_id, a.title, count(DISTINCT t.id) AS ntracks
        FROM albums a
        JOIN album_artists aa ON aa.album_id=a.id AND aa.role='primary'
            AND aa.artist_id=%(id)s::uuid
        JOIN album_variants av ON av.album_id=a.id
        JOIN media_files mf ON mf.album_variant_id=av.id
        JOIN tracks t ON t.id=mf.track_id
        GROUP BY a.id, a.title
    """, {"id": str(artist_id)})
    return {r["album_id"]: (r["title"], r["ntracks"]) for r in rows}


def _group(rows):
    by_album: dict = {}
    for r in rows:
        m = by_album.setdefault(r["album_id"], {"recs": {}, "amb": {}})
        m["recs"][r["track_id"]] = r["rec_mbid"]
        if r["artist_mbid"]:
            m["amb"][r["artist_mbid"]] = m["amb"].get(r["artist_mbid"], 0) + 1
    return by_album


def resolve_artist(artist_id, name: str) -> dict:
    """Read-only cascaded recording-centric resolve. Materialises the candidate
    recordings once (TEMP _recs), then each cascade pass re-matches only the
    unverified residue with looser parameters; the pass that verifies wins."""
    candidates = mb.search_artist(name, limit=8)
    strong = [c for c in candidates if (c.get("score") or 0) >= _CAND_SCORE] or candidates[:1]
    cands = [c["mbid"] for c in strong]
    owned = _owned_counts(artist_id)

    resolved: dict = {}
    if cands and owned:
        pending = set(owned)
        with get_conn() as conn, conn.cursor(
                cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("DROP TABLE IF EXISTS _recs")
            cur.execute(_RECS_SQL, {"cands": cands})
            cur.execute("CREATE INDEX ON _recs (rname)")                       # exact (=)
            cur.execute("CREATE INDEX ON _recs USING gin (rname gin_trgm_ops)")  # fuzzy (%)
            cur.execute("ANALYZE _recs")
            try:
                cache: dict = {}
                for label, dur, sim, minm, frac, cap in _CASCADE:
                    if not pending:
                        break
                    key = (dur, sim)
                    if key not in cache:
                        cur.execute("SET pg_trgm.similarity_threshold = %s", (sim,))
                        cur.execute(_MATCH_SQL, {"artist": str(artist_id),
                                                 "albums": list(pending), "durtol": dur})
                        cache[key] = _group(cur.fetchall())
                    for aid in list(pending):
                        m = cache[key].get(aid)
                        ntr = owned[aid][1]
                        nm = len(m["recs"]) if m else 0
                        floor = min(minm, ntr) if cap else minm
                        if m and nm >= floor and nm / ntr >= frac:
                            resolved[aid] = {**m, "matched": nm, "pass": label}
                            pending.discard(aid)
                # RG once over the verified albums — decoupled from the verifying pass
                if resolved:
                    cur.execute("SET pg_trgm.similarity_threshold = %s", (_RG_SIM,))
                    cur.execute(_RG_SQL, {"artist": str(artist_id), "albums": list(resolved)})
                    rg = {r["album_id"]: r["rg_mbid"] for r in cur.fetchall()}
                    for aid, r in resolved.items():
                        r["rg"] = rg.get(aid)
            finally:
                cur.execute("RESET pg_trgm.similarity_threshold")  # don't leak to the pool
                cur.execute("DROP TABLE IF EXISTS _recs")

    albums = []
    for aid, (title, ntr) in owned.items():
        r = resolved.get(aid)
        albums.append({
            "album_id": aid, "title": title, "ntracks": ntr,
            "matched": r["matched"] if r else 0,
            "rg_mbid": r["rg"] if r else None,
            "artist_mbid": max(r["amb"], key=r["amb"].get) if (r and r["amb"]) else None,
            "recordings": r["recs"] if r else {},
            "verified": bool(r), "pass": r["pass"] if r else None,
        })
    artist_mbids = sorted({a["artist_mbid"] for a in albums if a["artist_mbid"]})
    return {"artist_id": str(artist_id), "name": name,
            "albums": albums, "artist_mbids": artist_mbids}


def apply_artist(artist_id, name: str, plan: dict = None) -> dict:
    """Persist a resolve plan (materialize, increment 1): albums.musicbrainz_id (RG)
    + album_artists.mbid + media_files.recording_mbid + artist_mbids + track_mbids.
    Atomic per artist, idempotent (overwrites / ON CONFLICT DO NOTHING). Unique-PK
    collisions (an MBID already held by another artist/song) are SKIPPED here —
    they are the merge worklist for the structural pass (increment 2: merge +
    rename to MB-canonical). No rename/merge in this pass."""
    plan = plan or resolve_artist(artist_id, name)
    aid = str(artist_id)
    st = {"albums": 0, "album_artists": 0, "files": 0, "artist_mbids": 0, "track_mbids": 0}
    with get_conn() as conn:
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                for a in plan["albums"]:
                    if not a["verified"]:
                        continue
                    # always set (incl NULL) so re-apply clears a wrong/comp RG
                    cur.execute("UPDATE albums SET musicbrainz_id=%s, mb_match_confidence=%s "
                                "WHERE id=%s",
                                (a["rg_mbid"],
                                 'overlap_verified' if a["rg_mbid"] else None,
                                 a["album_id"]))
                    if a["rg_mbid"]:
                        st["albums"] += cur.rowcount
                    if a["artist_mbid"]:
                        cur.execute("UPDATE album_artists SET mbid=%s WHERE album_id=%s "
                                    "AND artist_id=%s AND role='primary'",
                                    (a["artist_mbid"], a["album_id"], aid))
                        st["album_artists"] += cur.rowcount
                    for tid, rec in a["recordings"].items():
                        cur.execute("UPDATE media_files mf SET recording_mbid=%s "
                                    "FROM album_variants av WHERE mf.album_variant_id=av.id "
                                    "AND av.album_id=%s AND mf.track_id=%s",
                                    (rec, a["album_id"], tid))
                        st["files"] += cur.rowcount
                        cur.execute("INSERT INTO track_mbids (recording_mbid, track_id, "
                                    "confidence) VALUES (%s,%s,'overlap_verified') "
                                    "ON CONFLICT (recording_mbid) DO NOTHING", (rec, tid))
                        st["track_mbids"] += cur.rowcount
                for mbid in plan["artist_mbids"]:
                    cur.execute("INSERT INTO artist_mbids (mbid, artist_id, confidence) "
                                "VALUES (%s,%s,'overlap_verified') "
                                "ON CONFLICT (mbid) DO NOTHING", (mbid, aid))
                    st["artist_mbids"] += cur.rowcount
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.autocommit = True
    return st


def merge_collisions(dry_run: bool = True) -> list:
    """APPLY increment 2 — merge-on-collision. Sautium artists that resolved to the
    SAME MBID are un-merged name-variants (diacritics/typos/articles/sort-order/
    transliteration/homoglyph — content-proven the same artist). For each colliding
    MBID, recanonicalize every involved artist onto the MB-canonical name (merging
    tracks/albums, preserving history) and re-point all their MBIDs to the survivor.
    dry_run=True returns the plan without mutating. Returns
    [{mbid, canonical, artists:[names]}]."""
    db = SessionLocal()
    plan = []
    try:
        cols = db.execute(_sql("""
            SELECT DISTINCT am.mbid::text AS mbid, mba.name AS canonical
            FROM artist_mbids am
            JOIN mb_artist mba ON mba.gid = am.mbid
            WHERE EXISTS (SELECT 1 FROM album_artists aa
                WHERE aa.mbid = am.mbid AND aa.role = 'primary'
                  AND aa.artist_id != am.artist_id)
        """)).fetchall()
        for mbid, canonical in cols:
            inv = db.execute(_sql("""
                SELECT ar.id::text, ar.name FROM artists ar WHERE ar.id IN (
                    SELECT artist_id FROM artist_mbids WHERE mbid = :m
                    UNION
                    SELECT artist_id FROM album_artists WHERE mbid = :m AND role = 'primary'
                )
            """), {"m": mbid}).fetchall()
            ids = [r[0] for r in inv]
            plan.append({"mbid": mbid, "canonical": canonical, "artists": [r[1] for r in inv]})
            if dry_run or len(ids) < 2:
                continue
            # preserve every involved artist's MBIDs (namesake MBIDs survive the merge)
            keep = [r[0] for r in db.execute(_sql(
                "SELECT DISTINCT mbid::text FROM artist_mbids WHERE artist_id::text = ANY(:ids)"),
                {"ids": ids}).fetchall()]
            for aid in ids:
                recanonicalize_artist(db, aid, canonical)
            canon_id = str(artist_uuid(canonical))
            for m in set(keep) | {mbid}:
                db.execute(_sql("INSERT INTO artist_mbids (mbid, artist_id, confidence) "
                                "VALUES (:m, :a, 'overlap_verified') "
                                "ON CONFLICT (mbid) DO UPDATE SET artist_id = :a"),
                           {"m": m, "a": canon_id})
        db.commit() if not dry_run else db.rollback()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return plan


def rename_albums(dry_run: bool = True, artist_ids: list = None) -> dict:
    """APPLY increment 2b — rename owned albums to their MB-canonical release-group
    title (the RG name), collapsing editions onto one album with named variants.
    For each album with musicbrainz_id whose title differs from the RG name,
    recanonicalize_album renames (or merges if the canonical UUID exists) and tags
    the variant's edition. The original title survives on album_variants.raw_title
    (set at scan) so a wrong rename is reversible — albums are local, never synced.
    artist_ids scopes the pass to those primary artists' albums (incremental canon);
    None = whole library. dry_run=True returns the scope without mutating."""
    from collections import Counter
    db = SessionLocal()
    try:
        sql = """
            SELECT a.id::text AS album_id, a.title, rg.name AS canonical, ar.name AS artist
            FROM albums a
            JOIN mb_release_group rg ON rg.gid = a.musicbrainz_id
            JOIN album_artists aa ON aa.album_id = a.id AND aa.role = 'primary'
            JOIN artists ar ON ar.id = aa.artist_id
            WHERE a.musicbrainz_id IS NOT NULL
        """
        params = {}
        if artist_ids:
            sql += " AND aa.artist_id::text = ANY(:ids)"
            params["ids"] = [str(a) for a in artist_ids]
        rows = db.execute(_sql(sql), params).fetchall()
        new_ids = Counter(str(album_uuid(c, ar)) for _, _, c, ar in rows)
        changes = [(aid, title, canon, ar) for aid, title, canon, ar in rows
                   if str(album_uuid(canon, ar)) != aid]
        st = {"rg_albums": len(rows), "changes": len(changes),
              "merge_groups": sum(1 for v in new_ids.values() if v > 1),
              "samples": [(t[:34], c[:34]) for _, t, c, _ in changes[:18]]}
        if not dry_run:
            for aid, title, canon, _ in changes:
                edition = split_edition(title)[1] or title_residual(title, canon)
                recanonicalize_album(db, aid, canon, edition)
            db.commit()
        else:
            db.rollback()
    finally:
        db.close()
    return st


def rename_to_canonical(artist_ids: list = None, dry_run: bool = False) -> dict:
    """Rename every single-MBID artist to its MB-canonical name (`mb_artist.name`).

    This is what makes MB convergence hold across nodes. `apply_artist` assigns the
    MBID but keeps the SCANNED name; `merge_collisions` renames only when two local
    variants collide on one MBID. So a node that scanned just "Beach Boys" keeps it
    while a node that also had "The Beach Boys" (collision → merge) ends up canonical
    — same MBID, different name, hence a different artist (and track) UUID. Pinning
    the name to the MBID's canonical form makes it a pure function of the MBID, so any
    two nodes that resolve the same artist converge regardless of source-tag spelling.

    Multi-MBID artists (namesakes conflated under one name) are skipped — there's no
    single canonical name; merge_collisions / manual review own those. recanonicalize_
    artist recomputes the artist + its track/album UUIDs and merges if the canonical
    name already exists (history preserved). Idempotent: a name already equal to its
    canonical form is not in the worklist. Pass artist_ids to scope (post-canon batch),
    or None to sweep the whole library (one-shot retrofit of pre-existing matches).
    """
    db = SessionLocal()
    out = {"renamed": 0, "plan": []}
    scope, params = "", {}
    if artist_ids is not None:
        if not artist_ids:
            return out
        scope = "AND a.id::text = ANY(:ids)"
        params["ids"] = list(artist_ids)
    try:
        rows = db.execute(_sql(f"""
            SELECT a.id::text, a.name, mba.name AS canonical
            FROM artists a
            JOIN artist_mbids am ON am.artist_id = a.id
            JOIN mb_artist mba ON mba.gid = am.mbid
            WHERE a.name <> mba.name
              AND (SELECT count(*) FROM artist_mbids x WHERE x.artist_id = a.id) = 1
              {scope}
        """), params).fetchall()
        for aid, cur, canonical in rows:
            # Skip case/normalize-only differences ("SATeN" -> "Saten"): normalize()
            # already collapses them to the same UUID, so there's nothing to converge
            # and recanonicalize would no-op anyway.
            if artist_uuid(cur) == artist_uuid(canonical):
                continue
            out["plan"].append({"from": cur, "to": canonical})
            if not dry_run:
                recanonicalize_artist(db, aid, canonical)
                out["renamed"] += 1
        db.commit() if not dry_run else db.rollback()
    finally:
        db.close()
    return out


def _collab_members(artist_id, name: str, plan: dict) -> list:
    """The MB-deterministic 'split this collaboration' signal, or [].

    MB models a collaboration as an `artist_credit` of N entities (Bei Bei [&]
    Shawn Lee), NOT a single artist — while a real band (Simon & Garfunkel) IS a
    single `mb_artist`. So: if the artist's matched recordings predominantly carry
    a multi-artist credit AND no single `mb_artist` bears this name (not a band),
    the credit's ordered members [(name, mbid)] are returned for splitting. This is
    the deterministic, content-verified replacement for the removed Last.fm Pass 2.
    """
    recs = [r for alb in plan["albums"] for r in alb["recordings"].values()]
    if not recs:
        return []
    # a real band with this exact name → keep whole (its recordings may still carry
    # a 2-artist "member & member" credit on some releases; the band wins).
    if db_query("SELECT 1 FROM mb_artist WHERE lower(name) = lower(%(n)s) LIMIT 1",
                {"n": name}):
        return []
    top = db_query("""
        SELECT rec.artist_credit AS cid, ac.artist_count AS n
        FROM mb_recording rec JOIN mb_artist_credit ac ON ac.id = rec.artist_credit
        WHERE rec.gid = ANY(%(recs)s::uuid[])
        GROUP BY rec.artist_credit, ac.artist_count
        ORDER BY count(*) DESC LIMIT 1
    """, {"recs": recs})
    if not top or top[0]["n"] < 2:
        return []
    return [(m["name"], m["mbid"]) for m in db_query("""
        SELECT mba.name AS name, mba.gid::text AS mbid
        FROM mb_artist_credit_name acn JOIN mb_artist mba ON mba.id = acn.artist
        WHERE acn.artist_credit = %(c)s ORDER BY acn.position
    """, {"c": top[0]["cid"]})]


def _split_collab(artist_id, name: str, members: list) -> tuple:
    """Split a collaboration into its MB credit members (primary = position 0),
    each stamped with its own MBID. Reuses the deterministic compound-split
    primitive (tracks move to the primary, members recorded in artist_members).
    Returns (primary_id, primary_name) so the caller re-resolves the primary's
    albums; the featured members carry no primary album, only their MBID."""
    db = SessionLocal()
    try:
        parts = [m[0] for m in members]
        normalize_compound_artist(db, artist_id, name, parts, status='verified_split')
        # The compound is now just a split marker; its single-member MBID (assigned
        # by an earlier apply_artist) must move OFF it onto the real member — hence
        # DELETE first, then DO UPDATE to re-point any MBID already held elsewhere.
        db.execute(_sql("DELETE FROM artist_mbids WHERE artist_id::text = :c"),
                   {"c": str(artist_id)})
        for mname, mmbid in members:
            db.execute(_sql("INSERT INTO artist_mbids (mbid, artist_id, confidence) "
                            "VALUES (:m, :a, 'overlap_verified') "
                            "ON CONFLICT (mbid) DO UPDATE SET artist_id = EXCLUDED.artist_id"),
                       {"m": mmbid, "a": str(artist_uuid(mname))})
        db.commit()
        return str(artist_uuid(parts[0])), parts[0]
    finally:
        db.close()


def split_collaborations(dry_run: bool = False) -> dict:
    """One-shot retrofit: split every owned collaboration into its MB credit members.

    canonicalize_pending does this inline for freshly-scanned artists; this sweeps
    PRE-EXISTING data (already past their last_mb_sync watermark) without disturbing
    the rename/merge machinery. Scoped to compound-named owned artists (the only
    split candidates) so it resolves a few hundred, not the whole library. Returns
    the plan either way (dry_run shows it without mutating).
    """
    if not mb.LOCAL_DUMP:
        return {"skipped": "no local MB dump", "split": 0, "plan": []}
    owned = db_query("""
        SELECT DISTINCT a.id::text AS id, a.name FROM artists a
        WHERE (a.name ~ ' & ' OR a.name ~ ', ' OR a.name ~ ' and '
               OR a.name ~ ' with ' OR a.name ~ ' / ')
          AND EXISTS (SELECT 1 FROM album_artists aa
                JOIN album_variants av ON av.album_id = aa.album_id
                JOIN media_files mf ON mf.album_variant_id = av.id
                WHERE aa.artist_id = a.id AND aa.role = 'primary')
    """)
    out = {"candidates": len(owned), "split": 0, "errors": 0, "plan": []}
    for r in owned:
        try:
            plan = resolve_artist(r["id"], r["name"])
            members = _collab_members(r["id"], r["name"], plan)
            if not members:
                continue
            out["plan"].append({"from": r["name"], "to": [m[0] for m in members]})
            if not dry_run:
                pid, pname = _split_collab(r["id"], r["name"], members)
                apply_artist(pid, pname)   # materialize the primary member's albums/MBID
                out["split"] += 1
        except Exception as e:
            out["errors"] += 1
            logger.error("split failed for %r: %s", r["name"], e)
    return out


def canonicalize_pending(limit: int = None) -> dict:
    """Incremental canon stage — the post-scan / post-import entry point. For every OWNED
    artist whose content is newer than its last canon (the `artists.last_mb_sync` watermark
    vs `media_files.created_at`): resolve + materialize (apply_artist) + scoped rename/edition-
    collapse, then merge any MBID collisions surfaced across the batch.

    MB-OPTIONAL — a NO-OP without the local dump (sync/enrich proceed on tier-3 deterministic
    names; MB never blocks). Idempotent: bumps last_mb_sync so a stable re-scan is free, and a
    crash mid-run leaves un-bumped artists to retry. Album renames are reversible (raw_title)
    and local (albums are not synced), so this auto-applies without a review gate."""
    if not mb.LOCAL_DUMP:
        mb.refresh()   # the dump may have been loaded since this process started
        if not mb.LOCAL_DUMP:
            return {"skipped": "no local MB dump", "artists": 0}

    pending = db_query("""
        SELECT ar.id::text AS id, ar.name
        FROM artists ar
        WHERE EXISTS (
            SELECT 1 FROM album_artists aa
            JOIN album_variants av ON av.album_id = aa.album_id
            JOIN media_files mf ON mf.album_variant_id = av.id
            WHERE aa.artist_id = ar.id AND aa.role = 'primary'
              AND (ar.last_mb_sync IS NULL OR mf.created_at > ar.last_mb_sync))
        ORDER BY ar.last_mb_sync NULLS FIRST, ar.name
        LIMIT %(lim)s
    """, {"lim": limit or 1_000_000})

    st = {"pending": len(pending), "artists": 0, "split": 0, "errors": 0}
    done = []
    # Worklist (not a flat for): a collaboration is split into its members, and the
    # primary member is re-queued so its albums resolve under the now-split identity.
    worklist = [(a["id"], a["name"]) for a in pending]
    seen = set()
    while worklist:
        aid, aname = worklist.pop(0)
        if aid in seen:
            continue
        seen.add(aid)
        try:
            plan = resolve_artist(aid, aname)
            members = _collab_members(aid, aname, plan)
            if members:
                primary = _split_collab(aid, aname, members)   # → (id, name) of pos-0
                st["split"] += 1
                worklist.append(primary)
                continue
            apply_artist(aid, aname, plan)   # materialize (reuses the resolve, atomic)
            done.append(aid)
            st["artists"] += 1
        except Exception as e:
            st["errors"] += 1
            logger.error("canon failed for %r: %s", aname, e)
    if done:
        rename_albums(dry_run=False, artist_ids=done)   # scoped rename + edition-collapse
        # watermark BEFORE merge: rename leaves artist ids intact, merge re-ids the rare
        # collision survivors (they simply re-canon next scan — idempotent, cheap)
        db_execute("UPDATE artists SET last_mb_sync = now() WHERE id::text = ANY(%(ids)s)",
                   {"ids": done})
        merge_collisions(dry_run=False)   # cross-artist MBID collisions (a near-instant no-op at 0)
        # name → MB-canonical for the matched single-MBID artists (re-stamps via
        # recanonicalize), so the artist/track UUIDs are a pure function of the MBID
        # and converge across nodes regardless of how the source tags were spelled.
        st["renamed"] = rename_to_canonical(artist_ids=done)["renamed"]
    return st
