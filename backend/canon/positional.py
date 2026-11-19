"""The positional release tier (CANON-CLASSICAL-DESIGN §3, §12.5–§12.6): an
owned copy the cascade left without a release group is bound by its positions
and their lengths to the release of an artist already bound on it.

A pass of its own over the unbound copies, after the cascade
(canon.content.canonicalize_pending), never inside resolve_artist: the
copy's candidate artists are the bound artists of its tracks (primary or
featured) and of its album's credit — an ensemble's album whose tracks credit
its leader (the owner's Polarity, §12.1) is seen from both. MusicBrainz's
special-purpose artists are never candidates (Q14: an album bound only to
Various Artists stays with the cascade).

The rule (§3.1, the tolerances of §12.6 Q15):
  - candidates: the releases whose credit names a candidate artist, Bootleg
    and Pseudo-Release excluded, with the copy's position count (data tracks
    are no position) — and each medium of such a release of several media,
    with that count (§12.7: a copy of one disc of a box fits the box's group
    as well as a one-disc release of the same recordings, and so abstains);
  - fit: every position has a length (the track's, else the recording's) and
    it agrees with the file's within the medium's tolerance — Digital Media
    to 0.5 s where MusicBrainz keeps the length to the millisecond, and in
    [−0.5, 1.5) s where it keeps whole seconds (a store's length is the floor
    of the file's); every other medium within 2 s;
  - the fitting releases in one release group bind it; under three
    positions only with a second witness (the group's name like the album's
    title, or the barcode or catalogue number); across groups only when the
    barcode or the catalogue number names exactly one — a witness never
    makes a candidate and never binds alone (§3.3);
  - an album whose copies fit different groups is bound to none (§3.1.6).

What it binds: the group (`mb_match_confidence = 'positional'`), the copy's
release, each position's recording on its file and in track_mbids. No artist
binding (§3.1.5). An album it bound is not the cascade's to re-decide
(resolve_artist leaves it out).

Idempotent and resumable: a bound copy leaves the unbound set; a copy it
abstained on keeps the candidates it was asked with
(`album_variants.positional_asked`, a digest of MusicBrainz's data here — the
dump's version, the newest slice —, the releases asked, the positions'
lengths and the witnesses) and is asked again only when they change. On a node without the dump the bind waits for MusicBrainz's answer
for the release (canon.confirm), whose tracklist must still fit.
"""
import hashlib
import logging

import psycopg2.extras

import mb_answers
from canon import confirm
from db_pool import get_conn
from discography import release_match_key
from record_sig import MATCHER_VERSION

logger = logging.getLogger(__name__)

# MusicBrainz's special-purpose artists: Various Artists, [anonymous],
# [data], [dialogue], [no artist], [traditional], [unknown] — the server's
# own list (SPECIAL_ARTIST_GIDS in musicbrainz-server's
# lib/MusicBrainz/Server/Constants.pm, read 2026-11-18; the style page
# "Special purpose artist" answered 404 that day). The dump carries no flag
# for them, so the list is the code's.
SPECIAL_PURPOSE_ARTISTS = (
    "89ad4ac3-39f7-470e-963a-56509c546377",
    "f731ccc4-e22a-43af-a747-64213329e088",
    "33cf029c-63b0-41a0-9855-be2a3665fb3b",
    "314e1c25-dde7-4e4d-b2f4-0a7b9f7c56dc",
    "eec63d3c-3b81-4ad4-b1e4-7c147d4d2b61",
    "9be7f096-97ec-4615-8957-8d40b5dcbc41",
    "125ec42a-7229-4250-afc5-e057484327fe",
)

# Q15's starting values for the artist-led tier (DESIGN §12.6); the replay
# of the harness decides between them and 2 s (CANON-CLASSICAL-PROPOSAL).
DIGITAL_MS_TOL = 0.5
DIGITAL_WHOLE_LO, DIGITAL_WHOLE_HI = -0.5, 1.5
OTHER_TOL = 2.0
WITNESS_UNDER = 3        # _RG_MIN_CONTENT's three: fewer positions need a second witness
NAME_MIN = 0.30          # _RG_NAME_MIN: the group's name like the album's title

_FIT = f"""CASE WHEN f.name = 'Digital Media' AND p.len % 1000 <> 0
               THEN abs(c.dur - p.len / 1000.0) <= {DIGITAL_MS_TOL}
           WHEN f.name = 'Digital Media'
               THEN c.dur - p.len / 1000.0 >= {DIGITAL_WHOLE_LO} AND c.dur - p.len / 1000.0 < {DIGITAL_WHOLE_HI}
           ELSE abs(c.dur - p.len / 1000.0) <= {OTHER_TOL} END"""


def _norm(col: str) -> str:
    from canon.content import _norm as norm
    return norm(col)


# The copies of albums no release group holds whose files (here or at an
# HQPlayer) give each position once: (disc, track) numbered on every file.
_COPIES_SQL = """
WITH f AS (
    SELECT av.id AS v, av.album_id, 'media_files' AS tbl, mf.id AS fid, mf.track_id,
           coalesce(mf.disc_number, 1) AS dn, mf.track_number AS tn,
           mf.duration_seconds::float AS dur, mf.barcode
      FROM album_variants av JOIN albums al ON al.id = av.album_id AND al.musicbrainz_id IS NULL
      JOIN media_files mf ON mf.album_variant_id = av.id
    UNION ALL
    SELECT av.id, av.album_id, 'hqp_library_files', hf.id, hf.track_id,
           coalesce(hf.disc_number, 1), hf.track_number, hf.duration_seconds::float, NULL
      FROM album_variants av JOIN albums al ON al.id = av.album_id AND al.musicbrainz_id IS NULL
      JOIN hqp_library_files hf ON hf.album_variant_id = av.id),
ok AS (
    SELECT v FROM f GROUP BY v
    HAVING count(*) FILTER (WHERE tn IS NULL OR dur IS NULL) = 0
       AND count(*) = count(DISTINCT (dn, tn)))
SELECT f.v, f.album_id::text AS album_id, al.title, al.catalog_number, av.positional_asked,
       array_agg(f.dur ORDER BY f.dn, f.tn) AS durs,
       array_agg(f.tbl ORDER BY f.dn, f.tn) AS tbls,
       array_agg(f.fid ORDER BY f.dn, f.tn) AS fids,
       array_agg(f.track_id::text ORDER BY f.dn, f.tn) AS tracks,
       CASE WHEN count(DISTINCT f.barcode) = 1
             AND count(*) FILTER (WHERE f.barcode IS NULL AND f.tbl = 'media_files') = 0
            THEN min(f.barcode) END AS barcode,
       ARRAY(SELECT DISTINCT am.mbid::text
               FROM (SELECT ta.artist_id FROM f f2 JOIN track_artists ta ON ta.track_id = f2.track_id
                      WHERE f2.v = f.v AND ta.role IN ('primary', 'featured')
                     UNION
                     SELECT aa.artist_id FROM album_artists aa WHERE aa.album_id = f.album_id) x
               JOIN artist_mbids am ON am.artist_id = x.artist_id
              WHERE am.mbid::text <> ALL(%(special)s)
              ORDER BY 1) AS cands,
       (SELECT ta.artist_id::text FROM f f2 JOIN track_artists ta ON ta.track_id = f2.track_id
         WHERE f2.v = f.v AND ta.role = 'primary' LIMIT 1) AS artist
  FROM f JOIN ok ON ok.v = f.v
  JOIN albums al ON al.id = f.album_id JOIN album_variants av ON av.id = f.v
 GROUP BY f.v, f.album_id, al.title, al.catalog_number, av.positional_asked
"""

# The candidates of each copy in _pcopies: a release (`med` NULL), and each
# medium of a release of several (§12.7: a copy of one disc of a box fits the
# box's group too, so a one-disc release carrying the same recordings is no
# longer the only group it fits). A candidate's positions are its non-data
# tracks in (medium, track) order.
_CANDIDATES_SQL = """
CREATE TEMP TABLE _pcand ON COMMIT DROP AS
WITH cr AS (
    SELECT DISTINCT c.v, r.id AS rel
      FROM _pcopies c
      JOIN mb_artist a ON a.gid = ANY(c.cands)
      JOIN mb_artist_credit_name acn ON acn.artist = a.id
      JOIN mb_release r ON r.artist_credit = acn.artist_credit
       AND (r.status IS NULL OR r.status NOT IN (3, 4))),
mn AS (
    SELECT md.release AS rel, md.id AS med, count(*) AS n
      FROM (SELECT DISTINCT rel FROM cr) d JOIN mb_medium md ON md.release = d.rel
      JOIN mb_track mt ON mt.medium = md.id
     WHERE NOT coalesce(mt.is_data_track, false)
     GROUP BY md.release, md.id),
rn AS (SELECT rel, sum(n) AS n, count(*) AS media FROM mn GROUP BY rel)
SELECT cr.v, cr.rel, NULL::int AS med
  FROM cr JOIN rn ON rn.rel = cr.rel JOIN _pcopies c ON c.v = cr.v
 WHERE rn.n = cardinality(c.durs)
UNION ALL
SELECT cr.v, cr.rel, mn.med
  FROM cr JOIN rn ON rn.rel = cr.rel AND rn.media > 1 JOIN mn ON mn.rel = cr.rel
  JOIN _pcopies c ON c.v = cr.v
 WHERE mn.n = cardinality(c.durs)
"""

_FITS_SQL = f"""
WITH p AS (
    SELECT k.v, k.rel, k.med, mt.recording,
           row_number() OVER (PARTITION BY k.v, k.rel, k.med ORDER BY md.position, mt.position) AS pos,
           coalesce(mt.length, rc.length) AS len, md.format
      FROM _pcand k JOIN mb_medium md ON md.release = k.rel AND (k.med IS NULL OR md.id = k.med)
      JOIN mb_track mt ON mt.medium = md.id AND NOT coalesce(mt.is_data_track, false)
      JOIN mb_recording rc ON rc.id = mt.recording),
c AS (SELECT v, pos, dur FROM _pcopies, unnest(durs) WITH ORDINALITY AS u(dur, pos)),
fit AS (
    SELECT p.v, p.rel, p.med, array_agg(p.recording ORDER BY p.pos) AS recs,
           sum(abs(c.dur - p.len / 1000.0)) AS err
      FROM p JOIN c ON c.v = p.v AND c.pos = p.pos
      LEFT JOIN mb_medium_format f ON f.id = p.format
     GROUP BY p.v, p.rel, p.med
    HAVING bool_and(p.len IS NOT NULL AND {_FIT}))
SELECT fit.v, fit.rel, (SELECT md.position FROM mb_medium md WHERE md.id = fit.med) AS disc,
       r.gid::text AS rel_mbid, rg.gid::text AS rg_mbid, fit.err,
       (SELECT array_agg(rec.gid::text ORDER BY u.i) FROM unnest(fit.recs) WITH ORDINALITY u(id, i)
          JOIN mb_recording rec ON rec.id = u.id) AS recordings,
       regexp_replace(coalesce(r.barcode, ''), '\\D', '', 'g') AS barcode,
       ARRAY(SELECT lower(regexp_replace(rl.catalog_number, '[^[:alnum:]]', '', 'g'))
               FROM mb_release_label rl WHERE rl.release = r.id AND rl.catalog_number IS NOT NULL) AS catnos,
       GREATEST(similarity({_norm('rg.name')}, c.atitle), similarity({_norm('rg.name')}, c.abase)) AS namesim
  FROM fit JOIN mb_release r ON r.id = fit.rel JOIN mb_release_group rg ON rg.id = r.release_group
  JOIN _pcopies c ON c.v = fit.v
"""


def _catno(value):
    return "".join(ch for ch in (value or "").lower() if ch.isalnum()) or None


# What MusicBrainz's rows here are: the dump's version, and the slices' newest
# import — a reload or a slice can change a release's lengths under its id.
_MB_DATA_SQL = """
SELECT coalesce((SELECT value::text FROM user_settings WHERE key = 'musicbrainz.db_version'), '')
       || '/' || coalesce((SELECT max(fetched_at)::text FROM mb_slice_fetches), '') AS v"""


def _digest(cands: list, copy: dict, mb_data: str) -> str:
    parts = [mb_data, ",".join(map(str, sorted(cands))), ",".join(f"{d:.2f}" for d in copy["durs"]),
             copy["barcode"] or "", _catno(copy["catalog_number"]) or "", copy["title"] or ""]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def decide(copy: dict, fits: list):
    """(release row, why) for one copy's fitting releases, or (None, why)."""
    if not fits:
        return None, "none_fit"
    groups = {f["rg_mbid"] for f in fits}
    barcode, catno = copy["barcode"], _catno(copy["catalog_number"])

    def named(f):
        return bool((barcode and f["barcode"] == barcode) or (catno and catno in (f["catnos"] or [])))

    by_witness = [f for f in fits if named(f)]
    if len(groups) > 1:
        witnessed = {f["rg_mbid"] for f in by_witness}
        if len(witnessed) != 1:
            return None, "several_groups"
        fits = [f for f in fits if f["rg_mbid"] in witnessed]
        by_witness = [f for f in fits if named(f)]
    elif len(copy["durs"]) < WITNESS_UNDER and not by_witness \
            and max(f["namesim"] or 0 for f in fits) < NAME_MIN:
        return None, "no_second_witness"
    pick = min(by_witness or fits, key=lambda f: (f["err"], f["rel"], f["disc"] or 0))
    return pick, "bound"


def run(waiting: set = None) -> dict:
    """The pass over every unbound copy. Returns its counts."""
    st = {"copies": 0, "asked": 0, "bound_albums": 0, "bound_copies": 0, "abstained": 0,
          "album_conflict": 0, "waiting": 0}
    with get_conn() as conn:
        conn.autocommit = False
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(_COPIES_SQL, {"special": list(SPECIAL_PURPOSE_ARTISTS)})
                copies = {r["v"]: r for r in cur.fetchall() if r["cands"]}
                st["copies"] = len(copies)
                if not copies:
                    conn.commit()
                    return st
                cur.execute("""CREATE TEMP TABLE _pcopies (v int PRIMARY KEY, durs float8[], cands uuid[],
                                                            atitle text, abase text) ON COMMIT DROP""")
                psycopg2.extras.execute_values(cur, "INSERT INTO _pcopies VALUES %s", [
                    (v, c["durs"], c["cands"], None, release_match_key(c["title"] or ""))
                    for v, c in copies.items()], template="(%s, %s, %s::uuid[], %s, %s)")
                # the edition-stripped base as resolve_artist's _abase keeps it
                cur.execute(f"UPDATE _pcopies p SET atitle = {_norm('al.title')}, "
                            f"abase = coalesce(nullif(p.abase, ''), {_norm('al.title')}) "
                            "FROM album_variants av JOIN albums al ON al.id = av.album_id WHERE av.id = p.v")
                cur.execute(_CANDIDATES_SQL)
                cur.execute("SELECT v, array_agg(rel || ':' || coalesce(med, 0) ORDER BY rel, med) AS rels "
                            "FROM _pcand GROUP BY v")
                rels = {r["v"]: r["rels"] for r in cur.fetchall()}
                cur.execute(_MB_DATA_SQL)
                mb_data = cur.fetchone()["v"]
                digests = {v: _digest(rels.get(v, []), c, mb_data) for v, c in copies.items()}
                ask = [v for v, c in copies.items() if c["positional_asked"] != digests[v]]
                st["asked"] = len(ask)
                cur.execute("DELETE FROM _pcopies WHERE v <> ALL(%s)", (ask,))
                cur.execute("DELETE FROM _pcand WHERE v <> ALL(%s)", (ask,))
                cur.execute(_FITS_SQL)
                fits: dict = {}
                for r in cur.fetchall():
                    fits.setdefault(r["v"], []).append(r)
                decisions = {v: decide(copies[v], fits.get(v, [])) for v in ask}
                st.update(_apply(cur, copies, decisions, digests, waiting))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.autocommit = True
    logger.info("positional tier: %s", st)
    return st


def _confirmed(copy: dict, pick: dict, asks) -> object:
    """On a node without the dump: the release as MusicBrainz answers it —
    filed under the group the slice said, credited to a candidate artist, and
    its tracklist (the picked medium's, for a medium) still fitting the copy
    within 2 s. WAIT while the answer is
    missing; False when it refutes the slice."""
    rel = confirm.release(pick["rel_mbid"], asks)
    if rel is confirm.WAIT:
        return confirm.WAIT
    if not rel or not rel.known or not rel.data.get("group"):
        return False
    same = confirm.same("release_group", pick["rg_mbid"], rel.data["group"]["mbid"], asks)
    if same is confirm.WAIT:
        return confirm.WAIT
    tracks = [t for t in rel.data.get("tracks") or [] if pick["disc"] is None or t["disc"] == pick["disc"]]
    if not same or not confirm.credited(rel.data.get("credit") or [], copy["cands"]) \
            or len(tracks) != len(copy["durs"]):
        return False
    if any(t.get("length_ms") is None or abs(t["length_ms"] / 1000.0 - d) > OTHER_TOL
           for t, d in zip(tracks, copy["durs"])):
        return False
    return [t["recording"] for t in tracks]


def _apply(cur, copies: dict, decisions: dict, digests: dict, waiting) -> dict:
    from canon.content import _LOCAL_BINDING_WINS, _PRUNE_STALE_BINDINGS
    st = {"bound_albums": 0, "bound_copies": 0, "abstained": 0, "album_conflict": 0, "waiting": 0}
    by_album: dict = {}
    for v, (pick, _why) in decisions.items():
        by_album.setdefault(copies[v]["album_id"], []).append(v)
    for album, vs in by_album.items():
        picks = {v: decisions[v][0] for v in vs if decisions[v][0] is not None}
        groups = {p["rg_mbid"] for p in picks.values()}
        if len(groups) > 1:
            st["album_conflict"] += 1
            picks = {}
        recordings, waits = {}, set()
        for v, pick in list(picks.items()):
            if confirm.required():
                asks = mb_answers.Asks("owned_artist", copies[v]["artist"], 1)
                got = _confirmed(copies[v], pick, asks)
                if got is confirm.WAIT:
                    asks.flush()
                    waits.add(v)
                    st["waiting"] += 1
                    if waiting is not None and copies[v]["artist"]:
                        waiting.add(copies[v]["artist"])
                    del picks[v]
                    continue
                if got is False:
                    del picks[v]
                    continue
                recordings[v] = got
            else:
                recordings[v] = pick["recordings"]
        for v in vs:
            # an abstention keeps what it was asked with; a wait is asked again
            if v not in picks and v not in waits:
                cur.execute("UPDATE album_variants SET positional_asked = %s WHERE id = %s",
                            (digests[v], v))
                st["abstained"] += 1
        if not picks:
            continue
        rg = next(iter(groups))
        cur.execute("UPDATE albums SET musicbrainz_id = %s, mb_match_confidence = 'positional' "
                    "WHERE id = %s::uuid AND musicbrainz_id IS NULL", (rg, album))
        if not cur.rowcount:
            continue
        st["bound_albums"] += 1
        bound_tracks = []
        for v, pick in picks.items():
            c = copies[v]
            cur.execute("UPDATE album_variants SET release_mbid = %s, positional_asked = NULL WHERE id = %s",
                        (pick["rel_mbid"], v))
            for tbl, fid, track, rec in zip(c["tbls"], c["fids"], c["tracks"], recordings[v]):
                cur.execute(f"UPDATE {tbl} SET recording_mbid = %s WHERE id = %s", (rec, fid))
                cur.execute("INSERT INTO track_mbids (recording_mbid, track_id, confidence, matcher_version) "
                            "VALUES (%s, %s, 'positional', %s)" + _LOCAL_BINDING_WINS,
                            (rec, track, MATCHER_VERSION))
                bound_tracks.append(track)
            st["bound_copies"] += 1
        cur.execute(_PRUNE_STALE_BINDINGS, {"tracks": bound_tracks})
    return st
