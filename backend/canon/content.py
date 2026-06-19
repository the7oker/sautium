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
import re
import unicodedata

import psycopg2.extras
from sqlalchemy import text as _sql

import mb_backend as mb
from database import SessionLocal
from db_pool import db_execute, db_query, get_conn
from discography import release_match_key, title_residual
from canon.match import _candidates, _exact_mbids, match_whole_gids
from canon.identity import recanonicalize_album_variants, recanonicalize_artist
from canon.split import normalize_compound_artist
from release_groups import release_group_cluster, simplify_edition_name
from uuid_utils import album_uuid, artist_uuid, normalize

logger = logging.getLogger(__name__)

_NO_DUR = 9999     # duration tolerance that effectively ignores duration (title-only)

# Cascade passes (cheap→specialised), each re-tried only on the prior residue:
#   (label, exact_dur_tol, fuzzy_dur_tol, fuzzy_sim, min_matched, coverage_frac, cap_min_to_ntracks)
# cap=True lets a single tight (title+duration) match verify a 1-track album
# (min(minm, ntracks)); cap=False keeps minm as a hard floor so the loosest
# title-only pass never verifies a 1-track album on one unanchored match.
# Exact and fuzzy carry SEPARATE duration tolerances — "weaker name ⇒ tighter
# timing" (Valerii): an exact title plus edition/remaster drift is safe at ±25,
# a fuzzy title is not; duration tolerances serve CLEAN material (edition drift,
# MB's own length imprecision — measured: only 89% of accepted matches sit
# within ±2s), never vinyl.
_CASCADE = [
    ("strict",   8,       8,       0.50, 3, 0.60, True),   # bulk: exact-ish title + tight duration
    ("short",    8,       8,       0.50, 2, 0.60, True),   # short ambient/EP vs the min-3 gate (reuses strict)
    ("wide-dur", 25,      10,      0.45, 2, 0.55, True),   # edition/remaster drift: exact ±25, fuzzy ±10
    ("title",    _NO_DUR, _NO_DUR, 0.58, 2, 0.55, False),  # title-only → need >=2 (no 1-track on one loose match)
    # guest-heavy: OSTs/splits where vocal cuts are credited to OTHER artists in
    # MB while the tags credit the composer — his coverage ceiling sits below the
    # fracs above (Terminator: 6 scoreable of 11 = 0.545). Low frac, but a HARD
    # min of 4 duration-anchored matches (cap=False) — never verifies small albums.
    ("guest-heavy", 10,   10,      0.50, 4, 0.40, False),
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
UNION
-- per-release TRACK name variants (regional/romanized retitles): identity stays
-- the recording, only the matchable NAME comes from the track; the differs-filter
-- keeps just the rows that add information (most track names equal the recording's)
SELECT rec.id, rec.gid::text, cc.artist_mbid,
       {_norm('mt.name')}, coalesce(mt.length, rec.length)
FROM mb_track mt
JOIN cand_credit cc ON cc.artist_credit = mt.artist_credit
JOIN mb_recording rec ON rec.id = mt.recording
WHERE {_norm('mt.name')} <> {_norm('rec.name')}
"""

# Match owned tracks (scoped to %(albums)s) against the temp _recs.
# ta2: only the artist's own tracks — a mixed directory must not dilute coverage.
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
    JOIN track_artists ta2 ON ta2.track_id=t.id AND ta2.role='primary'
        AND ta2.artist_id=%(artist)s::uuid
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
        AND (r.length IS NULL OR abs(r.length/1000.0 - o.dur) <= %(fdurtol)s)
    UNION ALL
    -- timing arm, BIDIRECTIONAL containment: dirty owned title CONTAINS the
    -- recording's full name ("artist - fever" ⊃ "fever"), or the recording
    -- name contains the owned title — MB names OST cues with the album prefix
    -- ("The Terminator: Tunnel Chase" ⊃ owned "Tunnel Chase"). The weakest
    -- name evidence, so duration corroboration is REQUIRED and tight (never
    -- loosened by the pass durtol — on drifted vinyl rips the arm stays silent)
    SELECT o.album_id, o.track_id, r.rec_mbid, r.artist_mbid, FALSE,
           abs(r.length/1000.0 - o.dur)
    FROM owned o JOIN _recs r ON r.rname <> o.ntitle
        AND ((length(r.rname) >= 10 AND position(r.rname in o.ntitle) > 0)
             OR (length(o.ntitle) >= 10 AND position(o.ntitle in r.rname) > 0))
        AND r.length IS NOT NULL AND o.dur IS NOT NULL
        AND abs(r.length/1000.0 - o.dur) <= 5
)
SELECT DISTINCT ON (album_id, track_id) album_id, track_id, rec_mbid, artist_mbid
FROM cand
ORDER BY album_id, track_id, exact DESC, dd
"""


# Track-centric match — the layer for artists owning TRACKS but no albums (a
# single or two on a compilation owned by someone else). They are invisible to
# the album-centric resolve, which walks album_artists. Same trust chain (name
# candidates → content verification) and the same _recs scoping; grouping is per
# track instead of per album. Single-edit suffixes that MB recordings never
# carry ("(Original Mix)", "(Radio Edit)") are stripped before normalization.
_TRACK_STRIP = (r"\s*[\(\[](original mix|extended mix|radio edit|club mix|"
                r"original version|album version|original|"
                r"remaster(ed)?( \d{4})?)[\)\]]\s*$")
_TRACK_MATCH_SQL = f"""
WITH owned AS (
    SELECT t.id::text AS track_id,
           {_norm("regexp_replace(t.title, %(strip)s, '', 'i')")} AS ntitle,
           min(mf.duration_seconds::float) AS dur
    FROM tracks t
    JOIN track_artists ta ON ta.track_id = t.id AND ta.role = 'primary'
        AND ta.artist_id = %(artist)s::uuid
    JOIN media_files mf ON mf.track_id = t.id
    GROUP BY t.id, t.title
),
cand AS (
    SELECT o.track_id, r.rec_mbid, r.artist_mbid, TRUE AS exact,
           abs(coalesce(r.length, o.dur*1000)/1000.0 - o.dur) AS dd
    FROM owned o JOIN _recs r ON r.rname = o.ntitle
        AND (r.length IS NULL OR abs(r.length/1000.0 - o.dur) <= %(durtol)s)
    UNION ALL
    SELECT o.track_id, r.rec_mbid, r.artist_mbid, FALSE,
           abs(coalesce(r.length, o.dur*1000)/1000.0 - o.dur)
    FROM owned o JOIN _recs r ON r.rname %% o.ntitle
        AND (r.length IS NULL OR abs(r.length/1000.0 - o.dur) <= %(durtol)s)
    UNION ALL
    -- timing arm (see _MATCH_SQL): bidirectional containment + REQUIRED tight duration
    SELECT o.track_id, r.rec_mbid, r.artist_mbid, FALSE,
           abs(r.length/1000.0 - o.dur)
    FROM owned o JOIN _recs r ON r.rname <> o.ntitle
        AND ((length(r.rname) >= 10 AND position(r.rname in o.ntitle) > 0)
             OR (length(o.ntitle) >= 10 AND position(o.ntitle in r.rname) > 0))
        AND r.length IS NOT NULL AND o.dur IS NOT NULL
        AND abs(r.length/1000.0 - o.dur) <= 5
)
SELECT DISTINCT ON (track_id) track_id, rec_mbid, artist_mbid, exact
FROM cand ORDER BY track_id, exact DESC, dd
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
    JOIN track_artists ta2 ON ta2.track_id=t.id AND ta2.role='primary'
        AND ta2.artist_id=%(artist)s::uuid
    WHERE a.id = ANY(%(albums)s::uuid[])
    GROUP BY a.id, t.id, t.title, mf.duration_seconds
),
nt AS (SELECT album_id, count(DISTINCT track_id) AS total FROM owned GROUP BY album_id),
atitle AS (   -- atitle = normalized title; abase = the same with edition/disc suffixes
              -- stripped (release_match_key, built in _abase) so "X (Deluxe Edition)" and
              -- "X - Esper Definitive Edition - The Score" still corroborate RG "X" by name.
    SELECT a.id::text AS album_id, {_norm('a.title')} AS atitle,
           coalesce(ab.base, {_norm('a.title')}) AS abase
    FROM albums a
    LEFT JOIN _abase ab ON ab.album_id = a.id::text
    WHERE a.id = ANY(%(albums)s::uuid[])),
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
           GREATEST(similarity(rc.rgn, at.atitle), similarity(rc.rgn, at.abase)) AS namesim, rc.is_comp
    FROM rel_cov rc
    JOIN rel_total rt ON rt.rel_id = rc.rel_id
    JOIN atitle at ON at.album_id = rc.album_id
    WHERE rc.owned_cov::float >= 0.5 * rt.n   -- release-half: R is not mostly other tracks (owned-half pre-cut)
      AND ((rc.owned_cov::float >= {_RG_OWNED_STRONG} * rc.total   -- strong content (>= a few tracks), OR
            AND rc.owned_cov >= {_RG_MIN_CONTENT})
           OR GREATEST(similarity(rc.rgn, at.atitle), similarity(rc.rgn, at.abase)) >= {_RG_NAME_MIN})  -- name agrees (edition-stripped too)
      AND (NOT rc.is_comp                                         -- a comp ALSO needs the name (fuzzy tracklists
           OR GREATEST(similarity(rc.rgn, at.atitle), similarity(rc.rgn, at.abase)) >= {_RG_NAME_MIN})  -- overlap studio albums by content
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
    # Count only THIS artist's tracks (ta2): a shared/mixed album directory
    # otherwise inflates the denominator and the coverage gates become
    # unpassable — Alanis' 18 tracks inside a 38-file dir = 0.47 < every frac.
    rows = db_query("""
        SELECT a.id::text AS album_id, a.title, count(DISTINCT t.id) AS ntracks
        FROM albums a
        JOIN album_artists aa ON aa.album_id=a.id AND aa.role='primary'
            AND aa.artist_id=%(id)s::uuid
        JOIN album_variants av ON av.album_id=a.id
        JOIN media_files mf ON mf.album_variant_id=av.id
        JOIN tracks t ON t.id=mf.track_id
        JOIN track_artists ta2 ON ta2.track_id=t.id AND ta2.role='primary'
            AND ta2.artist_id=%(id)s::uuid
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
    cands = _candidates(name)
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
                for label, dur, fdur, sim, minm, frac, cap in _CASCADE:
                    if not pending:
                        break
                    key = (dur, fdur, sim)
                    if key not in cache:
                        cur.execute("SET pg_trgm.similarity_threshold = %s", (sim,))
                        cur.execute(_MATCH_SQL, {"artist": str(artist_id),
                                                 "albums": list(pending),
                                                 "durtol": dur, "fdurtol": fdur})
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
                    # edition-stripped titles for the RG name gate (single source of
                    # truth = release_match_key; SQL can't strip the edition word list)
                    cur.execute("DROP TABLE IF EXISTS _abase")
                    cur.execute("CREATE TEMP TABLE _abase (album_id text PRIMARY KEY, base text)")
                    psycopg2.extras.execute_values(cur,
                        "INSERT INTO _abase (album_id, base) VALUES %s",
                        [(aid, release_match_key(owned[aid][0])) for aid in resolved])
                    cur.execute("SET pg_trgm.similarity_threshold = %s", (_RG_SIM,))
                    cur.execute(_RG_SQL, {"artist": str(artist_id), "albums": list(resolved)})
                    rg = {r["album_id"]: r["rg_mbid"] for r in cur.fetchall()}
                    for aid, r in resolved.items():
                        r["rg"] = rg.get(aid)
            finally:
                cur.execute("RESET pg_trgm.similarity_threshold")  # don't leak to the pool
                cur.execute("DROP TABLE IF EXISTS _recs")
                cur.execute("DROP TABLE IF EXISTS _abase")

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


def canonicalize_trackonly(artist_id, name: str) -> dict:
    """Track-centric canon — resolve + materialize for an artist with no owned
    albums. Oracle-validated on the legacy set (17/17 correct when assigned, 0
    wrong). Gates: dominant artist must carry >=60% of the matched tracks, and a
    single lone match counts only when it is an EXACT title hit (a lone fuzzy hit
    proves nothing). Materializes artist_mbids + media_files.recording_mbid +
    track_mbids for the dominant artist's tracks; albums/RG don't apply here."""
    cand_mbids = _candidates(name)
    st = {"total": 0, "matched": 0, "artist_mbid": None, "files": 0}
    if not cand_mbids:
        return st
    aid = str(artist_id)
    with get_conn() as conn:
        conn.autocommit = False
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("DROP TABLE IF EXISTS _recs")
                cur.execute(_RECS_SQL, {"cands": cand_mbids})
                cur.execute("CREATE INDEX ON _recs (rname)")
                cur.execute("CREATE INDEX ON _recs USING gin (rname gin_trgm_ops)")
                cur.execute("SET pg_trgm.similarity_threshold = 0.55")
                cur.execute(_TRACK_MATCH_SQL,
                            {"artist": aid, "strip": _TRACK_STRIP, "durtol": 10})
                hits = cur.fetchall()
                cur.execute("""
                    SELECT count(DISTINCT ta.track_id) AS n FROM track_artists ta
                    WHERE ta.artist_id = %s::uuid AND ta.role = 'primary'
                """, (aid,))
                st["total"] = cur.fetchone()["n"]
                st["matched"] = len(hits)

                counts: dict = {}
                for h in hits:
                    counts[h["artist_mbid"]] = counts.get(h["artist_mbid"], 0) + 1
                dom = max(counts, key=counts.get) if counts else None
                dom_hits = [h for h in hits if h["artist_mbid"] == dom]
                ok = (dom is not None
                      and len(dom_hits) * 10 >= 6 * len(hits)        # >=60% dominance
                      and (len(dom_hits) > 1 or dom_hits[0]["exact"]))
                if ok:
                    st["artist_mbid"] = dom
                    # Stamp files FIRST (before any rename/merge): recording_mbid
                    # rides the media_files row, which survives a track-UUID move.
                    for h in dom_hits:
                        cur.execute(
                            "UPDATE media_files SET recording_mbid = %s WHERE track_id = %s::uuid",
                            (h["rec_mbid"], h["track_id"]))
                        st["files"] += cur.rowcount
                # inside the txn on purpose: commit clears them; a rollback
                # reverts the SET and the temp table along with the work
                cur.execute("RESET pg_trgm.similarity_threshold")
                cur.execute("DROP TABLE IF EXISTS _recs")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.autocommit = True
    if not st["artist_mbid"]:
        return st

    # Identity step. The mbid PK is one-MB-entity-one-artist: if another local row
    # already holds dom (a name-variant of the same entity — "UR" vs "Underground
    # Resistance"), plain INSERT silently no-ops and the artist stays orphaned. So
    # converge onto the MB-canonical name: rename (or merge into the holder, which
    # recanonicalize does when the canonical UUID exists) and re-point the mbid.
    canonical = db_query("SELECT name FROM mb_artist WHERE gid = %(g)s",
                         {"g": st["artist_mbid"]})[0]["name"]
    db = SessionLocal()
    try:
        canon_id = aid
        if str(artist_uuid(canonical)) != aid:
            canon_id = recanonicalize_artist(db, aid, canonical)
            st["canonical"] = canonical
        db.execute(_sql(
            "INSERT INTO artist_mbids (mbid, artist_id, confidence) "
            "VALUES (:m, :a, 'overlap_verified') "
            "ON CONFLICT (mbid) DO UPDATE SET artist_id = EXCLUDED.artist_id"),
            {"m": st["artist_mbid"], "a": canon_id})
        db.commit()
    finally:
        db.close()
    # track_mbids re-derived from the stamped files: track UUIDs may have just
    # moved in the rename/merge, media_files.track_id followed by CASCADE.
    db_execute("""
        INSERT INTO track_mbids (recording_mbid, track_id, confidence)
        SELECT DISTINCT mf.recording_mbid, mf.track_id, 'overlap_verified'::mb_match_confidence
        FROM media_files mf
        JOIN track_artists ta ON ta.track_id = mf.track_id AND ta.role = 'primary'
        WHERE ta.artist_id = %(a)s::uuid AND mf.recording_mbid IS NOT NULL
        ON CONFLICT (recording_mbid) DO NOTHING
    """, {"a": canon_id})
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


# Edition-grouping knobs.
_EDITION_MATCH_MIN = 0.5    # min Jaccard for a variant to claim a specific MB release
_EDITION_SAME_MIN = 0.8     # tracklist Jaccard above which two variants are ONE edition (drift)
# Trailing bracketed disc/channel marker — a per-disc rip of one edition ("The
# Fragile (Right)" = disc 2, "(LP03)"). Local to edition-folding: only ever
# compared between two siblings of the same album, so unlike the global
# release_match_key it can safely strip bare "left"/"right".
_CHANNEL_DISC_RE = re.compile(
    r"\s*[\(\[]\s*(?:left|right|side\s*[a-d]|disc|disk|cd|lp)\s*\.?\s*\d*\s*[\)\]]\s*$",
    re.IGNORECASE)


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if (a and b) else 0.0


def _disc_base(raw_title: str) -> str:
    return _CHANNEL_DISC_RE.sub("", (raw_title or "").strip()).strip().lower()


def _variant_fingerprints(album_id: str) -> list:
    """Per variant: its recording-MBID set (the name-independent tracklist
    fingerprint, stamped by apply_artist), normalized track-title set (fallback
    when recordings are sparse), raw_title and track count."""
    rows = db_query("""
        SELECT av.id AS vid, av.raw_title,
               array_remove(array_agg(DISTINCT mf.recording_mbid::text), NULL) AS recs,
               array_agg(DISTINCT lower(btrim(t.title))) AS titles,
               count(DISTINCT mf.track_id) AS ntracks
        FROM album_variants av
        JOIN media_files mf ON mf.album_variant_id = av.id
        JOIN tracks t ON t.id = mf.track_id
        WHERE av.album_id = %(a)s::uuid
        GROUP BY av.id, av.raw_title
    """, {"a": str(album_id)})
    return [{"vid": r["vid"], "raw_title": r["raw_title"] or "",
             "recs": set(r["recs"] or []),
             "titles": {t for t in (r["titles"] or []) if t},
             "ntracks": r["ntracks"], "rel": None} for r in rows]


def _prep_releases(rg_mbid: str, cache: dict) -> list:
    """All releases of the RG (bootlegs included — that is how a "Deck Art" /
    "Esper" edition gets its clean MB name), each with its recording-MBID and
    normalized-title sets precomputed for variant matching. Memoized per run."""
    if rg_mbid not in cache:
        rels = mb.fetch_release_tracklists(rg_mbid)
        for rel in rels:
            rel["recset"] = {t["recording_mbid"] for t in rel["tracks"] if t["recording_mbid"]}
            rel["titleset"] = {(t["title"] or "").strip().lower()
                               for t in rel["tracks"] if t["title"]}
        cache[rg_mbid] = rels
    return cache[rg_mbid]


def _best_release(v: dict, releases: list) -> tuple:
    """The RG release whose tracklist best matches variant ``v`` — recording-MBID
    Jaccard when the variant is well-canonized, else track-title Jaccard.
    Returns (release | None, score)."""
    use_recs = len(v["recs"]) >= max(1, v["ntracks"] // 2)
    best, bscore = None, 0.0
    for rel in releases:
        score = _jaccard(v["recs"], rel["recset"]) if use_recs \
            else _jaccard(v["titles"], rel["titleset"])
        if score > bscore:
            best, bscore = rel, score
    return best, bscore


def _group_editions(variants: list) -> list:
    """Cluster variants into editions: same matched release, OR near-identical
    tracklist (drift / different-tag true rips), OR a bare disc/channel marker
    apart (a multi-disc edition). Returns lists of variants."""
    parent = list(range(len(variants)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(len(variants)):
        for j in range(i + 1, len(variants)):
            a, b = variants[i], variants[j]
            # Identical scan tag = the same edition by the user's own labelling —
            # the decisive signal (matching runs AFTER grouping, so rel isn't known
            # here; this is what folds 3 rips tagged "X (Deluxe)" into one edition).
            same_raw = (a["raw_title"].strip() != ""
                        and a["raw_title"].strip().lower() == b["raw_title"].strip().lower())
            jac = _jaccard(a["recs"], b["recs"]) if (a["recs"] and b["recs"]) \
                else _jaccard(a["titles"], b["titles"])
            disc = (_disc_base(a["raw_title"]) == _disc_base(b["raw_title"])
                    and (_CHANNEL_DISC_RE.search(a["raw_title"])
                         or _CHANNEL_DISC_RE.search(b["raw_title"])))
            if same_raw or jac >= _EDITION_SAME_MIN or disc:
                parent[find(i)] = find(j)

    groups: dict = {}
    for i, v in enumerate(variants):
        groups.setdefault(find(i), []).append(v)
    return list(groups.values())


def _cluster_rg(album_id: str) -> str:
    """The release-group MBID of any owned sibling edition of this album (its
    cluster), so a scattered NULL-rg edition can attach to the group MB rejected
    on name/status grounds. None when no sibling carries an RG."""
    ids = release_group_cluster(album_id)
    rows = db_query("""
        SELECT musicbrainz_id::text AS mb FROM albums
        WHERE id = ANY(%(i)s::uuid[]) AND musicbrainz_id IS NOT NULL
        LIMIT 1
    """, {"i": ids}) if ids else []
    return rows[0]["mb"] if rows else None


def _album_is_owned(db, album_id: str) -> bool:
    return db.execute(_sql("SELECT 1 FROM album_variants WHERE album_id = :a LIMIT 1"),
                      {"a": album_id}).fetchone() is not None


def _split_album_editions(db, album_id: str, dry_run: bool, st: dict, rel_cache: dict) -> None:
    """Resolve one owned album into editions and (unless dry_run) apply the split.

    A single-edition rg'd album just adopts the RG name (the old rename). A
    conflated album splits its differing-tracklist variants onto their own rows.
    A scattered NULL-rg album attaches to its cluster's RG when its content (or
    base title) proves membership. Each non-primary edition is named from its
    matched MB release; the primary (the bare-named release) keeps the RG name.
    """
    arow = db.execute(_sql("""
        SELECT a.musicbrainz_id::text, a.title, ar.name
        FROM albums a
        JOIN album_artists aa ON aa.album_id = a.id AND aa.role = 'primary'
        JOIN artists ar ON ar.id = aa.artist_id
        WHERE a.id = :a
        ORDER BY ar.id LIMIT 1
    """), {"a": album_id}).fetchone()
    if not arow:
        return  # no primary artist → album UUID isn't recomputable; leave as-is
    rg, atitle, artist_name = arow
    attaching = rg is None
    if attaching:
        rg = _cluster_rg(album_id)
        if not rg:
            return  # standalone album with no release group — nothing to do
    rgrow = db.execute(_sql("SELECT name FROM mb_release_group WHERE gid = :g"),
                       {"g": rg}).fetchone()
    if not rgrow or not rgrow[0]:
        return
    rg_name = rgrow[0]

    variants = _variant_fingerprints(album_id)
    if not variants:
        return
    editions = _group_editions(variants)   # release-independent (recs / titles / disc)
    multi = len(editions) > 1
    rg_base = release_match_key(rg_name)
    bare_id = str(album_uuid(rg_name, artist_name))   # the standard-edition row
    # A row needs an edition qualifier when it is NOT the plain standard album:
    # attaching (cluster-borrowed RG), a multi-edition split, OR a lone row whose
    # bare RG name is already owned by another edition (a split edition seen on its
    # own — without this it would rename back to bare and re-merge every run). The
    # qualifier always comes from the matched MB release, so the result is STABLE
    # across re-runs. A true single album takes the bare name and skips the fetch.
    bare_taken = (bare_id != album_id) and _album_is_owned(db, bare_id)
    needs_qualifier = attaching or multi or bare_taken

    if needs_qualifier:
        releases = _prep_releases(rg, rel_cache)
        for v in variants:
            rel, score = _best_release(v, releases)
            v["rel"] = rel if score >= _EDITION_MATCH_MIN else None

    plan = []   # (canon_title, qualifier, variant_ids, release_mbid)
    used: set = set()
    for ed in editions:
        raw = ed[0]["raw_title"]
        matched = next((v["rel"] for v in ed if v["rel"]), None)
        if not needs_qualifier:
            qualifier = None   # true single-edition album → the bare RG name (no churn)
        else:
            # Membership is only uncertain for an attached (cluster-borrowed) RG or a
            # multi-edition variant that could be foreign content mis-tagged into the
            # album — gate those on a content match, the RG base as a title prefix, or
            # a bracket/dash edition suffix; a failing variant is left in place, never
            # minted as a junk edition. An already-rg'd single edition is trusted.
            if attaching or multi:
                rk = release_match_key(raw)
                if not (matched or rk == rg_base or rk.startswith(rg_base + " ")
                        or title_residual(raw, rg_name)):
                    st["anomalies"] += 1
                    logger.warning("edition anomaly on %s: variant %r not part of RG %r",
                                   album_id, raw, rg_name)
                    continue
            qualifier = simplify_edition_name(matched["name"] if matched else raw, rg_name)
        # The primary edition stays bare only when it owns the bare row; if another
        # row already holds it, this is a distinct edition and must keep a qualifier.
        if qualifier is None and bare_taken:
            qualifier = simplify_edition_name(matched["name"] if matched else raw, rg_name) or "Alt"
        canon = rg_name if qualifier is None else f"{rg_name} ({qualifier})"
        if canon in used:   # two distinct editions must not collapse back together
            qualifier = simplify_edition_name(matched["name"] if matched else raw, rg_name) or f"Alt {len(used)}"
            canon = f"{rg_name} ({qualifier})"
        used.add(canon)
        plan.append((canon, qualifier, [v["vid"] for v in ed],
                     matched["release_mbid"] if matched else None))

    # Skip pure no-ops: one edition already titled the RG name (the common case).
    if not plan or not (attaching or len(plan) > 1
                        or any(p[0] != atitle for p in plan)):
        return
    st["albums"] += 1
    if len(plan) > 1:
        st["editions_split"] += 1
    if attaching:
        st["attached"] += 1
    if dry_run:
        for canon, _q, vids, rel in plan:
            logger.info("[dry] %s -> edition %r (%d variants, rel=%s)",
                        album_id, canon, len(vids), rel)
        return

    if attaching:
        db.execute(_sql("UPDATE albums SET musicbrainz_id = :rg, "
                        "mb_match_confidence = 'overlap_verified' "
                        "WHERE id = :a AND musicbrainz_id IS NULL"),
                   {"rg": rg, "a": album_id})
    for canon, qualifier, vids, release_mbid in plan:
        if release_mbid:
            db.execute(_sql("UPDATE album_variants SET release_mbid = :r WHERE id = ANY(:v)"),
                       {"r": release_mbid, "v": vids})
        recanonicalize_album_variants(db, album_id, vids, canon, qualifier)


def apply_editions(dry_run: bool = True, artist_ids: list = None) -> dict:
    """APPLY increment 2b — resolve owned albums into editions.

    Supersedes the flat rename-to-RG: differing-tracklist variants wrongly
    collapsed as one album split onto their own rows (sharing the RG), scattered
    NULL-rg editions attach to their cluster's RG, and each edition is named from
    its matched MB release (the primary keeps the bare RG name) — while true rips
    (one tracklist) stay folded as the variants of an edition. Per-album commit
    (isolation: one pathological album can't abort the batch); reversible via
    album_variants.raw_title; local-only. artist_ids scopes to those primary
    artists; None = whole library. dry_run logs the plan without mutating."""
    db = SessionLocal()
    st = {"albums": 0, "editions_split": 0, "attached": 0, "anomalies": 0}
    rel_cache: dict = {}
    try:
        sql = """
            SELECT DISTINCT a.id::text
            FROM albums a
            JOIN album_artists aa ON aa.album_id = a.id AND aa.role = 'primary'
            WHERE EXISTS (SELECT 1 FROM album_variants av WHERE av.album_id = a.id)
        """
        params = {}
        if artist_ids is not None:
            if not artist_ids:
                return st
            sql += " AND aa.artist_id::text = ANY(:ids)"
            params["ids"] = [str(a) for a in artist_ids]
        album_ids = [r[0] for r in db.execute(_sql(sql), params).fetchall()]
        for album_id in album_ids:
            try:
                _split_album_editions(db, album_id, dry_run, st, rel_cache)
                db.commit() if not dry_run else db.rollback()
            except Exception as e:
                db.rollback()
                st["anomalies"] += 1
                logger.error("apply_editions failed for album %s: %s", album_id, e)
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
              AND am.confidence <> 'name_exact'   -- name-only evidence never renames
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
                # recanonicalize DELETEs the source row, and artist_mbids is ON
                # DELETE CASCADE — carry the rows to the canonical id (the same
                # dance merge_collisions does). Without this the rename orphans
                # the artist from its own MBID, and since recanonicalize stamps
                # last_mb_sync, later canon runs skip it as "done" forever.
                kept = db.execute(_sql(
                    "SELECT mbid::text, confidence::text FROM artist_mbids "
                    "WHERE artist_id::text = :a"), {"a": aid}).fetchall()
                canon_id = recanonicalize_artist(db, aid, canonical)
                for m, conf in kept:
                    # CAST(:c AS ...), not :c::... — SQLAlchemy's bind regex
                    # rejects a parameter immediately followed by '::'
                    db.execute(_sql(
                        "INSERT INTO artist_mbids (mbid, artist_id, confidence) "
                        "VALUES (:m, :a, CAST(:c AS mb_match_confidence)) "
                        "ON CONFLICT (mbid) DO UPDATE SET artist_id = EXCLUDED.artist_id"),
                        {"m": m, "a": canon_id, "c": conf})
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
    # a real band with this name → keep whole (its recordings may still carry a
    # 2-artist "member & member" credit on some releases; the band wins). Separator-
    # spelling aware: 'Jon & Vangelis' is the registered group 'Jon and Vangelis',
    # not a collaboration to split (see canon.match.match_whole_gids).
    if match_whole_gids(name):
        return []

    # Content path: the matched recordings' dominant credit (strongest evidence —
    # proven by the artist's own audio, immune to name-order differences).
    recs = [r for alb in plan["albums"] for r in alb["recordings"].values()]
    if recs:
        top = db_query("""
            SELECT rec.artist_credit AS cid, ac.artist_count AS n
            FROM mb_recording rec JOIN mb_artist_credit ac ON ac.id = rec.artist_credit
            WHERE rec.gid = ANY(%(recs)s::uuid[])
            GROUP BY rec.artist_credit, ac.artist_count
            ORDER BY count(*) DESC LIMIT 1
        """, {"recs": recs})
        if top and top[0]["n"] >= 2:
            return [(m["name"], m["mbid"]) for m in db_query("""
                SELECT mba.name AS name, mba.gid::text AS mbid
                FROM mb_artist_credit_name acn JOIN mb_artist mba ON mba.id = acn.artist
                WHERE acn.artist_credit = %(c)s ORDER BY acn.position
            """, {"c": top[0]["cid"]})]
        return []   # content spoke and said single-artist credit → not a collab

    # Name fallback — no recordings matched (tracks not in MB / on unmatched
    # compilations), so the credit can't testify. Deterministic name evidence
    # instead: the whole is NOT an mb_artist (guard above) AND every separator
    # part IS one (exact name/alias). Then the parts are the members, source
    # order = credit order (position 0 = primary). A part whose exact name is
    # ambiguous (several MB namesakes) still splits but gets NO mbid — identity
    # by name stays deterministic, the MBID waits for content evidence.
    parts = [p.strip() for p in re.split(r' & | and | with | / |,', name) if p.strip()]
    if len(parts) < 2:
        return []
    # "Kaji, Meiko" is a REORDERED person ("Meiko Kaji"), not a collaboration —
    # a comma-only 2-part name whose swap is an MB artist OR ALIAS must not be
    # split (梶芽衣子 carries "Meiko Kaji" only as an alias, not the name).
    if (len(parts) == 2 and "," in name
            and not re.search(r" & | and | with | / ", name)
            and db_query("""
                SELECT 1 FROM mb_artist WHERE lower(name) = lower(%(s)s)
                UNION ALL
                SELECT 1 FROM mb_artist_alias WHERE lower(name) = lower(%(s)s)
                LIMIT 1""", {"s": f"{parts[1]} {parts[0]}"})):
        return []
    members = []
    for p in parts:
        gids = {r["gid"] for r in db_query("""
            SELECT a.gid::text AS gid FROM mb_artist a WHERE lower(a.name) = lower(%(p)s)
            UNION
            SELECT a.gid::text FROM mb_artist a
            JOIN mb_artist_alias al ON al.artist = a.id WHERE lower(al.name) = lower(%(p)s)
        """, {"p": p})}
        if not gids:
            return []   # any unknown part → don't split on name evidence
        members.append((p, gids.pop() if len(gids) == 1 else None))
    return members


def _split_collab(artist_id, name: str, members: list) -> tuple:
    """Split a collaboration into its MB credit members (primary = position 0),
    each stamped with its own MBID. Reuses the deterministic compound-split
    primitive (tracks move to the primary, members recorded in artist_members).
    Returns (primary_id, primary_name) so the caller re-resolves the primary's
    albums; the featured members carry no primary album, only their MBID."""
    db = SessionLocal()
    try:
        parts = [m[0] for m in members]
        # 'verified_collab', NOT 'verified_split': the latter marks legacy Pass-1/2
        # splits and is what unsplit_pass2_compounds targets — MB-evidence splits
        # must be distinguishable so a future --unsplit can't destroy them.
        normalize_compound_artist(db, artist_id, name, parts, status='verified_collab')
        # The compound is now just a split marker; its single-member MBID (assigned
        # by an earlier apply_artist) must move OFF it onto the real member — hence
        # DELETE first, then DO UPDATE to re-point any MBID already held elsewhere.
        db.execute(_sql("DELETE FROM artist_mbids WHERE artist_id::text = :c"),
                   {"c": str(artist_id)})
        for mname, mmbid in members:
            if not mmbid:   # name-fallback member with ambiguous MB namesakes
                continue
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
          AND EXISTS (SELECT 1 FROM track_artists ta
                JOIN media_files mf ON mf.track_id = ta.track_id
                WHERE ta.artist_id = a.id AND ta.role = 'primary')
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


_VA_GID = "89ad4ac3-39f7-470e-963a-56509c546377"   # MB's special-purpose Various Artists
_VA_NAMES = ["various artists", "various", "va", "various artist"]


def _canonicalize_va() -> int:
    """Cheapest layer: placeholder VA rows ('Various', 'VA', …) ARE MB's official
    Various Artists entity — assign its mbid and merge the placeholders onto the
    canonical name. Deterministic; converges the 'Various'/'Various Artists'
    track-UUID fork across nodes. Per-track attribution of comp tracks to their
    real artists is a separate (timing) layer."""
    row = db_query("SELECT name FROM mb_artist WHERE gid = %(g)s", {"g": _VA_GID})
    if not row:
        return 0
    canonical = row[0]["name"]
    canon_id = str(artist_uuid(canonical))
    rows = db_query("""
        SELECT id::text AS id FROM artists
        WHERE lower(btrim(name)) = ANY(%(names)s)
          AND (last_mb_sync IS NULL
               OR NOT EXISTS (SELECT 1 FROM artist_mbids am WHERE am.artist_id = artists.id))
    """, {"names": _VA_NAMES})
    if not rows:
        return 0
    db = SessionLocal()
    try:
        for r in rows:
            if r["id"] != canon_id:
                recanonicalize_artist(db, r["id"], canonical)   # merge + watermark
            else:
                db.execute(_sql("UPDATE artists SET last_mb_sync = now() WHERE id = :i"),
                           {"i": canon_id})
        db.execute(_sql(
            "INSERT INTO artist_mbids (mbid, artist_id, confidence) "
            "VALUES (:m, :a, 'name_exact') "
            "ON CONFLICT (mbid) DO UPDATE SET artist_id = EXCLUDED.artist_id"),
            {"m": _VA_GID, "a": canon_id})
        db.commit()
    finally:
        db.close()
    return len(rows)


def assign_name_exact(artist_ids: list) -> int:
    """Slowest tier, runs LAST on the residue the content layers left mbid-less:
    when the name (or alias) exact-matches exactly ONE MB entity in the whole
    base, assign its mbid at confidence 'name_exact'. No rename, no stealing an
    mbid another artist holds (DO NOTHING) — name-only evidence stays humble.
    Replicates the legitimate part of the legacy name-matching experiments with
    a uniqueness guard against their failure mode (namesake/alias collisions)."""
    if not artist_ids:
        return 0
    rows = db_query("""
        SELECT a.id::text AS id, a.name FROM artists a
        WHERE a.id::text = ANY(%(ids)s)
          AND NOT EXISTS (SELECT 1 FROM artist_mbids am WHERE am.artist_id = a.id)
    """, {"ids": list(artist_ids)})
    n = 0
    for r in rows:
        gids = _exact_mbids(r["name"])
        if len(gids) == 1:
            ins = db_query(
                "INSERT INTO artist_mbids (mbid, artist_id, confidence) "
                "VALUES (%(m)s, %(a)s, 'name_exact') "
                "ON CONFLICT (mbid) DO NOTHING RETURNING 1",
                {"m": gids[0], "a": r["id"]})
            n += len(ins)
    return n


def promote_name_exact() -> dict:
    """name_exact lifecycle (the other end of assign_name_exact): a name-derived
    MBID is upgraded to 'overlap_verified' once the artist's OWN tracks carry a
    recording_mbid credited to that same MBID — content has caught up with the name
    guess (later track stamps, or matches below the resolve coverage gate). This
    promotes a humble name-only mapping to the re-verifiable tier P2P trusts,
    without re-running the full resolve. Idempotent; runs each canon pass, so a
    name_exact mapping strengthens automatically as content accrues."""
    with get_conn() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE artist_mbids am SET confidence = 'overlap_verified'
                WHERE am.confidence = 'name_exact'
                  AND EXISTS (
                    SELECT 1 FROM track_artists ta
                    JOIN media_files mf ON mf.track_id = ta.track_id
                        AND mf.recording_mbid IS NOT NULL
                    JOIN mb_recording rec ON rec.gid = mf.recording_mbid
                    JOIN mb_artist_credit_name acn ON acn.artist_credit = rec.artist_credit
                    JOIN mb_artist m ON m.id = acn.artist AND m.gid = am.mbid
                    WHERE ta.artist_id = am.artist_id)
            """)
            return {"promoted": cur.rowcount}


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

    va = _canonicalize_va()   # cheapest layer first — drops VA rows out of pending

    # Track-owned, not album-owned: an artist whose only presence is a track or
    # two on someone else's compilation has no album_artists row at all, yet is
    # exactly who the track-centric layer exists for.
    pending = db_query("""
        SELECT ar.id::text AS id, ar.name
        FROM artists ar
        WHERE EXISTS (
            SELECT 1 FROM track_artists ta
            JOIN media_files mf ON mf.track_id = ta.track_id
            WHERE ta.artist_id = ar.id AND ta.role = 'primary'
              AND (ar.last_mb_sync IS NULL OR mf.created_at > ar.last_mb_sync))
        ORDER BY ar.last_mb_sync NULLS FIRST, ar.name
        LIMIT %(lim)s
    """, {"lim": limit or 1_000_000})

    st = {"pending": len(pending), "artists": 0, "track_only": 0, "split": 0,
          "va": va, "errors": 0}
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
            if plan["albums"]:
                apply_artist(aid, aname, plan)   # materialize (reuses the resolve, atomic)
            else:
                canonicalize_trackonly(aid, aname)   # track-centric layer (no owned albums)
                st["track_only"] += 1
            done.append(aid)
            st["artists"] += 1
        except Exception as e:
            st["errors"] += 1
            logger.error("canon failed for %r: %s", aname, e)
    if done:
        apply_editions(dry_run=False, artist_ids=done)   # scoped edition split + RG rename
        # watermark BEFORE merge: rename leaves artist ids intact, merge re-ids the rare
        # collision survivors (they simply re-canon next scan — idempotent, cheap)
        db_execute("UPDATE artists SET last_mb_sync = now() WHERE id::text = ANY(%(ids)s)",
                   {"ids": done})
        merge_collisions(dry_run=False)   # cross-artist MBID collisions (a near-instant no-op at 0)
        # name → MB-canonical for the matched single-MBID artists (re-stamps via
        # recanonicalize), so the artist/track UUIDs are a pure function of the MBID
        # and converge across nodes regardless of how the source tags were spelled.
        st["renamed"] = rename_to_canonical(artist_ids=done)["renamed"]
        # slowest tier last — only the residue the content layers left mbid-less
        st["name_exact"] = assign_name_exact(done)
    return st
