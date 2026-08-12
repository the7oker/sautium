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

P2P search for dump-less nodes was originally DEFERRED over dump-node load
and download-incentive concerns; REVERSED 2026-08-10 (Valerii) with the
load shaped out of it: artist-name-only, indexed-only matching on the serve
path (no trigram — mb_slice_queries.search_artists), requester-side node
ROTATION (spreads load, adds failover, and no single dump node sees the
full search stream) and a per-node COOLDOWN so unlimited search remains a
reason to download the dump. Click-to-mint fetches the artist's slice via
the existing signed slice protocol, after which the unchanged canon/mint
pipeline runs — remote is a thin acquisition layer, not a parallel one.
"""

import logging
import threading
import time
from typing import Optional

from db_pool import db_execute, db_query, db_query_one

logger = logging.getLogger(__name__)

# Requester-side politeness for remote search, shaped for the human
# "type → look → refine" pattern: a BURST of requests per node is free, the
# SUSTAINED rate matches the old fixed 10s interval. A fixed interval was
# wrong here — with a single reachable dump node (the common early-network
# topology) the second refinement inside 10s hit the cooldown wall. A
# script still feels the wall immediately: burst exhausts in seconds and
# refills at one request per 10s.
REMOTE_BURST = 3
REMOTE_BURST_WINDOW_S = 30.0
_remote_lock = threading.Lock()
_remote_hits: dict = {}          # url -> [monotonic ts of recent requests]
_remote_rr = 0                   # round-robin cursor
_remote_client = None            # pooled httpx.Client — TLS handshake per
                                 # search cost a measured ~0.2s otherwise


def _client():
    global _remote_client
    if _remote_client is None:
        import httpx
        _remote_client = httpx.Client(
            verify=False,
            # A stale source must cost a beat, not five seconds — connect
            # is where dead peers hang; live LAN/localhost peers connect
            # in milliseconds.
            timeout=httpx.Timeout(5.0, connect=1.5))
    return _remote_client

SOURCES_KEY = "mb.search_sources"


def remote_sources(kind: Optional[str] = None) -> list:
    """Peer slice/search sources persisted by the launcher's P2PManager
    (manual + LAN + DHT "mbdump" capability walk) on every slice run.
    Backend-side we only ever read the cache: peer discovery needs the
    P2P stack (DHT, LAN) that lives in the launcher process. Stale
    entries cost one failed probe and a rotation step. kind: "dump"
    (full dump, can search) or "replica" (blobs only, slice re-serve)."""
    row = db_query_one(
        "SELECT value FROM user_settings WHERE key = %(k)s", {"k": SOURCES_KEY})
    sources = (row or {}).get("value") or []
    if kind:
        sources = [s for s in sources if s.get("kind") == kind]
    return sources


def remote_available() -> bool:
    return bool(remote_sources("dump"))


def state() -> dict:
    """Live MB-scope capability for the UI chip: 'local' (full dump),
    'remote' (dump peers known), 'searching' (P2P is up but no source
    probe round has completed yet — the key is absent), 'none' (probed
    and found nothing, or P2P is off). Rides /api/events as {"t": "mb"}
    so the chip updates without a page reload."""
    if available():
        return {"available": True, "remote": False, "state": "local"}
    row = db_query_one(
        "SELECT value FROM user_settings WHERE key = %(k)s",
        {"k": SOURCES_KEY})
    if row is not None:
        dumps = [s for s in (row.get("value") or [])
                 if s.get("kind") == "dump"]
        if dumps:
            return {"available": True, "remote": True, "state": "remote"}
        return {"available": False, "remote": False, "state": "none"}
    p2p = db_query_one(
        "SELECT value FROM user_settings WHERE key = 'sync.p2p_enabled'")
    if p2p is not None and not p2p.get("value"):
        return {"available": False, "remote": False, "state": "none"}
    return {"available": False, "remote": False, "state": "searching"}


def remote_search(q: str, limit: int = 10) -> dict:
    """Search artist candidates on remote dump nodes, rotating across them.

    Returns {"status": "ok", "artists": [...], "node": url} |
    {"status": "no_peers"} | {"status": "cooldown", "retry_in": seconds}.
    Rotation + failover: start at the round-robin cursor, skip nodes whose
    burst is exhausted, take the first that answers; 429 or transport
    errors move to the next node. Failed attempts count as spend — a dead
    node must not be free to hammer."""
    global _remote_rr
    dumps = [s.get("url") for s in remote_sources("dump") if s.get("url")]
    if not dumps:
        return {"status": "no_peers"}

    now = time.monotonic()
    with _remote_lock:
        order = [dumps[(_remote_rr + i) % len(dumps)] for i in range(len(dumps))]
        _remote_rr = (_remote_rr + 1) % len(dumps)
        ready = []
        for u in order:
            stamps = [t for t in _remote_hits.get(u, [])
                      if now - t < REMOTE_BURST_WINDOW_S]
            _remote_hits[u] = stamps
            if len(stamps) < REMOTE_BURST:
                ready.append(u)
        if not ready:
            soonest = min(REMOTE_BURST_WINDOW_S - (now - min(_remote_hits[u]))
                          for u in order if _remote_hits.get(u))
            return {"status": "cooldown", "retry_in": round(max(soonest, 1.0))}

    for url in ready:
        with _remote_lock:
            _remote_hits.setdefault(url, []).append(time.monotonic())
        try:
            r = _client().get(f"{url}/api/mb/search", params={"q": q})
            if r.status_code == 429:
                continue
            r.raise_for_status()
            artists = (r.json() or {}).get("artists") or []
            return {"status": "ok", "artists": artists[:limit], "node": url}
        except Exception as e:
            logger.info(f"MB remote search via {url} failed: {e}")
            continue
    # Every known source failed — the "dump node disappeared" moment.
    # Ask the launcher's P2P layer to re-probe; it persists the fresh
    # verdict (likely []) and NOTIFYs sautium_mb_sources, which pushes an
    # {"t": "mb"} event to open tabs and flips the chip to disabled.
    try:
        db_execute("NOTIFY sautium_mb_sources_request")
    except Exception as e:
        logger.debug(f"mb sources re-probe notify failed: {e}")
    return {"status": "no_peers"}

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
    """FULL local dump — the search/browse/mint-locally authority.

    Deliberately NOT mb_backend.LOCAL_DUMP: that flag flips True after the
    first imported P2P slice (canon runs on partial data by design), and
    interactive search over a slice-partial world answers "not found" for
    every name it never saw — observed live ('sade' on a slice-holding
    dump-less node searched locally instead of asking a dump peer). The
    predicate is the loader's in-DB completion marker."""
    from routers.sync import mb_dump_version
    return bool(mb_dump_version())


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


def _fetch_remote_slice(artist_name: str) -> bool:
    """Pull one name's signed slice from peer sources and import it locally.

    Replicas first — the slice serve-order philosophy (spreads entry-load
    off the few dump nodes; a miss falls through to a full holder). After a
    successful import mb_backend.refresh() flips LOCAL_DUMP so the regular
    mint/discography path reads the freshly-landed rows."""
    try:
        from desktop.api_client import BackendAPIClient
        from desktop.mb_slice_client import MBSliceClient
    except ImportError as e:
        logger.warning(f"MB slice client unavailable in this runtime: {e}")
        return False
    from config import settings as app_settings

    sources = remote_sources()
    ordered = ([s["url"] for s in sources if s.get("kind") == "replica"]
               + [s["url"] for s in sources if s.get("kind") == "dump"])
    for url in ordered:
        client = MBSliceClient(BackendAPIClient(url),
                               db_dsn=app_settings.database_url,
                               source_node=url)
        try:
            stats = client.run([artist_name])
            if stats.get("error") or artist_name in (stats.get("missing") or []):
                continue
            client.finalize()          # ANALYZE the hot canon tables
            import mb_backend as mb
            mb.refresh()
            logger.info(f"MB remote mint: slice for {artist_name!r} via {url}")
            return True
        except Exception as e:
            logger.info(f"MB slice fetch via {url} failed: {e}")
        finally:
            try:
                client.close()
            except Exception:
                pass
    return False


def mint(artist_gid: str, rg_gid: Optional[str] = None,
         artist_name: Optional[str] = None) -> dict:
    """Materialize an MB artist as phantom entities and return local ids
    for navigation.

    Same-name artists collapse onto one local row by design (the namesake
    model): the new mbid joins artist_mbids under the existing uuid5 row
    and album_artists.mbid attributes each album to its namesake. The
    'user' confidence marks an explicit user pick — ground truth, above
    every automatic tier. Idempotent end to end (ON CONFLICT + the
    discography sync's own skip logic).

    On a dump-less node the clicked artist's rows are acquired first via
    the P2P slice protocol (artist_name is the slice key — the protocol
    speaks names, and its closed-world answer delivers every namesake, so
    the clicked gid lands with them)."""
    row = db_query_one(
        "SELECT name FROM mb_artist WHERE gid = %(g)s::uuid", {"g": artist_gid})
    if not row and artist_name and remote_sources():
        if _fetch_remote_slice(artist_name):
            row = db_query_one(
                "SELECT name FROM mb_artist WHERE gid = %(g)s::uuid",
                {"g": artist_gid})
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
