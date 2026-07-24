"""
MusicBrainz-scope Discovery: search the local MB dump and mint a chosen
artist into phantom entities.

The dump (optional, ~19GB) holds the whole catalog — 2.9M artists, 4.4M
release groups — so a user can search BEYOND their library and stream what
they find (the built-in YouTube provider needs a title + duration, which
only materialized phantom entities carry; the closed Deezer module is not
in the distribution). Flow: search → click → the artist's whole slice runs
through the UNCHANGED canon pipeline (discography.sync_artist_discography:
phantom albums + tracklists with durations + genres + CAA covers) → the
user lands on the entity they clicked and streams it.

Search reuses the canon-era trigram/lower indexes on mb_artist /
mb_artist_alias / mb_release_group — fuzzy matching costs nothing extra
(typo "radiohed" finds Radiohead; the alias arm finds "On a Friday").
mb_recording (39M) is deliberately NOT searched: no name index, and the
import unit is the artist slice anyway — the UI scope says "Artist or
album".

Release groups are pre-filtered to what the canon pipeline will actually
materialize (_ALLOWED_PRIMARY minus _DISQUALIFYING_SECONDARY) so search
never shows a click-dead compilation/live row it would then refuse to mint.

P2P slice search for dump-less nodes was considered and DEFERRED (possible
Pro feature): serving strangers' searches would load dump nodes and remove
the incentive to download the dump.
"""

import logging
from typing import Optional

from db_pool import db_execute, db_query, db_query_one

logger = logging.getLogger(__name__)

_CAA_FRONT_URL = "https://coverartarchive.org/release-group/{rg}/front-500"

_ARTIST_SQL = """
WITH cand AS (
    SELECT a.id, a.gid::text AS gid, a.name, a.comment,
           GREATEST(similarity(lower(a.name), lower(%(q)s)),
                    CASE WHEN lower(a.name) LIKE lower(%(q)s) || '%%' THEN 0.85 ELSE 0 END) AS sim
    FROM mb_artist a
    WHERE lower(a.name) %% lower(%(q)s) OR lower(a.name) LIKE lower(%(q)s) || '%%'
    UNION ALL
    SELECT a.id, a.gid::text, a.name, a.comment,
           similarity(lower(al.name), lower(%(q)s)) AS sim
    FROM mb_artist_alias al JOIN mb_artist a ON a.id = al.artist
    WHERE lower(al.name) %% lower(%(q)s)
),
top AS (
    SELECT DISTINCT ON (id) id, gid, name, comment, sim
    FROM cand ORDER BY id, sim DESC
),
pool AS (SELECT * FROM top ORDER BY sim DESC LIMIT 50)
SELECT t.gid, t.name, t.comment, t.sim,
       (SELECT COUNT(DISTINCT rg.id) FROM mb_artist_credit_name acn
        JOIN mb_release_group rg ON rg.artist_credit = acn.artist_credit
        WHERE acn.artist = t.id) AS rg_count,
       am.artist_id::text AS local_artist_id
FROM pool t
LEFT JOIN artist_mbids am ON am.mbid = t.gid::uuid
ORDER BY t.sim DESC, rg_count DESC, t.name
LIMIT %(limit)s
"""

# local_album_id via a correlated subselect, NOT a join: owned editions
# share one RG mbid across several albums rows and a join multiplies the
# result (Dark Side of the Moon came back four times).
_RG_SQL = """
WITH pool AS (
    SELECT rg.id, rg.gid::text AS gid, rg.name, rg.artist_credit,
           GREATEST(similarity(lower(rg.name), lower(%(q)s)),
                    CASE WHEN lower(rg.name) LIKE lower(%(q)s) || '%%' THEN 0.85 ELSE 0 END) AS sim
    FROM mb_release_group rg
    JOIN mb_release_group_primary_type pt ON pt.id = rg.type
        AND pt.name = ANY(%(allowed)s)
    WHERE (lower(rg.name) %% lower(%(q)s) OR lower(rg.name) LIKE lower(%(q)s) || '%%')
      AND NOT EXISTS (
          SELECT 1 FROM mb_release_group_secondary_type_join j
          JOIN mb_release_group_secondary_type st ON st.id = j.secondary_type
          WHERE j.release_group = rg.id AND st.name = ANY(%(disq)s))
    ORDER BY sim DESC LIMIT 50
)
SELECT t.gid, t.name, t.sim,
       ac.name AS credit,
       (SELECT a.gid::text FROM mb_artist_credit_name acn
        JOIN mb_artist a ON a.id = acn.artist
        WHERE acn.artist_credit = t.artist_credit AND acn.position = 0
        LIMIT 1) AS artist_gid,
       (SELECT LEAST(MIN(rc.date_year), MIN(ruc.date_year)) FROM mb_release r
        LEFT JOIN mb_release_country rc ON rc.release = r.id
        LEFT JOIN mb_release_unknown_country ruc ON ruc.release = r.id
        WHERE r.release_group = t.id) AS year,
       (SELECT COUNT(*) FROM mb_release r WHERE r.release_group = t.id) AS n_rel,
       (SELECT al.id::text FROM albums al
        WHERE al.musicbrainz_id = t.gid::uuid LIMIT 1) AS local_album_id
FROM pool t
JOIN mb_artist_credit ac ON ac.id = t.artist_credit
ORDER BY t.sim DESC, n_rel DESC, year NULLS LAST
LIMIT %(limit)s
"""


def available() -> bool:
    import mb_backend as mb
    return mb.LOCAL_DUMP


def search(q: str, limit: int = 20) -> dict:
    """Artists + release groups matching `q` in the local dump, ranked by
    name similarity then by discography size / release count (the only
    popularity proxy the dump carries — the real Sade has 80 release
    groups, the Valencian punk namesake has 0)."""
    from discography import _ALLOWED_PRIMARY, _DISQUALIFYING_SECONDARY
    artists = db_query(_ARTIST_SQL, {"q": q, "limit": limit})
    albums = db_query(_RG_SQL, {"q": q, "limit": limit,
                                "allowed": sorted(_ALLOWED_PRIMARY),
                                "disq": sorted(_DISQUALIFYING_SECONDARY)})
    return {
        "artists": [{
            "gid": r["gid"],
            "name": r["name"],
            "comment": r["comment"],          # MB disambiguation line
            "rg_count": r["rg_count"],
            "similarity": round(float(r["sim"]), 2),
            "local_artist_id": r["local_artist_id"],
        } for r in artists],
        "albums": [{
            "gid": r["gid"],
            "title": r["name"],
            "artist": r["credit"],
            "artist_gid": r["artist_gid"],
            "year": r["year"],
            "cover_url": _CAA_FRONT_URL.format(rg=r["gid"]),
            "similarity": round(float(r["sim"]), 2),
            "local_album_id": r["local_album_id"],
        } for r in albums],
    }


def mint(artist_gid: str, rg_gid: Optional[str] = None) -> dict:
    """Materialize an MB artist as phantom entities and return local ids
    for navigation.

    Same-name artists collapse onto one local row by design (the namesake
    model): the new mbid joins artist_mbids under the existing uuid5 row
    and album_artists.mbid attributes each album to its namesake. The
    'user' confidence marks an explicit user pick — ground truth, above
    every automatic tier. Idempotent end to end (ON CONFLICT + the
    discography sync's own skip logic)."""
    row = db_query_one(
        "SELECT name FROM mb_artist WHERE gid = %(g)s::uuid", {"g": artist_gid})
    if not row:
        return {"status": "unknown_artist"}
    name = row["name"]

    link = db_query_one(
        "SELECT artist_id::text AS aid FROM artist_mbids WHERE mbid = %(g)s::uuid",
        {"g": artist_gid})
    if link:
        artist_id = link["aid"]
    else:
        from transliterate import latinize
        from uuid_utils import artist_uuid
        artist_id = str(artist_uuid(name))
        db_execute(
            "INSERT INTO artists (id, name, name_latin) "
            "VALUES (%(id)s, %(n)s, %(nl)s) ON CONFLICT (id) DO NOTHING",
            {"id": artist_id, "n": name, "nl": latinize(name)})
        db_execute(
            "INSERT INTO artist_mbids (mbid, artist_id, confidence, name) "
            "VALUES (%(m)s::uuid, %(a)s::uuid, 'user', %(n)s) "
            "ON CONFLICT (mbid) DO NOTHING",
            {"m": artist_gid, "a": artist_id, "n": name})
        logger.info(f"MB mint: linked {name} ({artist_gid}) -> {artist_id}")

    from discography import sync_artist_discography
    stats = sync_artist_discography(artist_id, name)

    out = {"status": stats.get("status", "success"),
           "artist_id": artist_id,
           "new_albums": stats.get("new", 0)}
    if rg_gid:
        alb = db_query_one(
            "SELECT id::text AS id FROM albums WHERE musicbrainz_id = %(rg)s::uuid LIMIT 1",
            {"rg": rg_gid})
        # None happens only if canon declined the group (e.g. it turned out
        # owned under another credit) — the client falls back to the artist.
        out["album_id"] = alb["id"] if alb else None
    return out
