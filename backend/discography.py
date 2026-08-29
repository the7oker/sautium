"""Missing-album discovery for canonized local artists (Phantom Discovery).

Derives a *canonized* artist's missing albums from the local MusicBrainz
dump: every Album/EP release-group the artist is credited on that the
user doesn't own becomes a **phantom album row** — `albums` +
`album_artists` with a Cover Art Archive `cover_url` and NO
`album_variants`/`media_files`. The artist screen surfaces them in a
"Missing albums" shelf.

Canonized = has a row in `artist_mbids` (set by the MB canonicalization
pass). Non-canonized artists get no shelf — and deliberately no
`last_album_sync` stamp, so the first screen view after canonization
lands syncs immediately.

Each sync is a full **reconcile**, not an append: newly-missing groups
are upserted, rows that stopped being missing (ripped since, MB
reclassified, pre-MB legacy) are unlinked and garbage-collected, and a
stale external cover on an album that became owned is cleared (the
frontend prefers `cover_url` over the local file cover).

Two callers share `sync_artist_discography`:
  - the background enrichment step (monthly per artist), and
  - the fetch-on-view endpoint (daily gate on `artists.last_album_sync`).

Both go through `db_pool` (own connection per call) so neither needs to
thread a session in. With the dump loaded this is pure local-DB work —
no network, no cooldowns; dump-less nodes fall back to the MB HTTP API
via `mb_backend` and surface its rate limit as `status="rate_limited"`.

The own-check is two-channel: precise release-group MBID equality
(`albums.musicbrainz_id`, set by canonicalization for most owned albums)
plus `release_match_key` title matching for the rest. The deterministic
`album_uuid` can't be the match key: MB titles and local tags differ in
articles, punctuation and edition suffixes, so the same album yields
different UUIDs on each side. The stored UUID stays
`album_uuid(title, artist)` so a later local rip can still collapse onto
it; across artists the rg MBID is the real identity (a collaboration
synced from the other side only gains an `album_artists` link).
"""

import logging
from collections import Counter
import re
import unicodedata
from typing import Any, Dict, List, Optional

import mb_backend as mb  # module-attr access only — refresh() rebinds the source
from psycopg2.extras import execute_values

from db_pool import db_execute, db_query, db_query_one, get_conn
from mb_dump_load import MB_LOAD_LOCK_KEY
from uuid_utils import album_uuid, artist_uuid, normalize, track_uuid

logger = logging.getLogger(__name__)

# Release-group primary types surfaced on the "Missing albums" shelf.
# Singles add too much noise (pre-release duplicates of album tracks).
_ALLOWED_PRIMARY = {"Album", "EP"}

# Secondary types that disqualify a release-group from the shelf — not part
# of the artist's core studio discography. `Soundtrack` is deliberately
# absent: an artist-credited film score is a first-class studio release
# (VA soundtracks never appear here — the fetch is per artist credit).
_DISQUALIFYING_SECONDARY = {
    "Compilation", "Live", "Remix", "DJ-mix", "Mixtape/Street",
    "Demo", "Interview", "Audiobook", "Spokenword", "Field recording",
}

# Cover Art Archive front image by release-group MBID, written verbatim into
# `albums.cover_url`. The CAA image API has no rate limit, so the browser
# resolves it directly (the tile's onerror hides a 404's broken image).
_CAA_FRONT_URL = "https://coverartarchive.org/release-group/{rg}/front-500"

# Edition/reissue markers — same album, different packaging; stripped so
# variants collapse. Deliberately EXCLUDES 'live', 'remix', 'acoustic',
# 'demo', 'instrumental' — those are genuinely distinct releases and must
# survive as separate albums.
_EDITION_WORDS = (
    "deluxe", "remaster", "remastered", "anniversary", "expanded",
    "super deluxe", "bonus", "reissue", "collector", "collectors",
    "edition", "mono", "stereo", "redux",
)
_EDITION_BRACKET_RE = re.compile(
    r"\s*[\(\[][^\)\]]*\b(?:" + "|".join(_EDITION_WORDS) + r")\b[^\)\]]*[\)\]]",
    re.IGNORECASE,
)
_EDITION_SUFFIX_RE = re.compile(
    r"\s*[-–—:]\s*[^-–—:]*\b(?:" + "|".join(_EDITION_WORDS) + r")\b.*$",
    re.IGNORECASE,
)
_ARTICLE_RE = re.compile(r"^(?:the|a|an)\s+")
_NONALNUM_RE = re.compile(r"[^a-z0-9]+")
# Multi-disc rip suffixes — "disc 1", "(Disc 2)", "cd1", "CD 2", "(LP03)". A
# per-disc directory split is the same album: without this, "Catch a Fire … disc
# 1" never match-keys onto MB's "Catch a Fire" and overlap-verification silently
# misses every multi-disc release. 'disc one' (word) is rarer; digits only. 'lp'
# needs the digit too, so a title ending in "Help" never trips it.
_DISC_RE = re.compile(
    r"\s*[\(\[]?\s*\b(?:disc|disk|cd|lp)\s*\.?\s*\d+\s*[\)\]]?",
    re.IGNORECASE,
)


def release_match_key(title: str) -> str:
    """Canonical comparison key for collapsing reissues and for matching
    an MB release-group against an owned album.

    lowercase + NFC → drop bracketed/trailing edition markers → drop multi-disc
    suffixes → strip a leading article → collapse punctuation to spaces.
    'Live'/'remix'/etc. are not edition markers, so they survive and stay
    distinct.
    """
    s = unicodedata.normalize("NFC", (title or "").strip().lower())
    s = _EDITION_BRACKET_RE.sub("", s)
    s = _EDITION_SUFFIX_RE.sub("", s)
    s = _DISC_RE.sub(" ", s)
    # "&" vs "and": dirty tags use "Hunting High & Low", MB stores "… and …".
    # Normalise to the spelled-out form on both sides so they overlap-match.
    s = s.replace("&", " and ")
    s = _ARTICLE_RE.sub("", s)
    s = _NONALNUM_RE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def _clean_edition(raw: str) -> str:
    """Edition name from a matched bracket/suffix chunk: drop the wrapping
    brackets / leading separator, keep the original case for display."""
    return raw.strip().strip("()[]").lstrip("-–—: ").strip()


def split_edition(title: str) -> tuple[str, Optional[str]]:
    """Split a dirty album title into ``(canonical_title, edition)``.

    Extracts only a BRACKETED or dash/colon-suffix edition marker, so an inline
    edition word survives ("Hellbilly Deluxe" → no split). Disc suffixes are
    stripped from the canonical part but are NOT an edition (a disc is a
    position, captured by ``media_files.disc_number``) → edition stays None for
    those. Case/spacing of the canonical part is preserved — this is the rename
    target, not the lowercase match-key.

        "Forever Young (Super Deluxe Edition)" -> ("Forever Young", "Super Deluxe Edition")
        "I Am The Night [Remaster]"            -> ("I Am The Night", "Remaster")
        "Quadrophenia (Disc 1)"                -> ("Quadrophenia", None)
        "Hellbilly Deluxe"                     -> ("Hellbilly Deluxe", None)
    """
    s = (title or "").strip()
    edition = None
    m = _EDITION_BRACKET_RE.search(s)
    if m:
        edition = _clean_edition(m.group(0))
        s = _EDITION_BRACKET_RE.sub("", s)
    else:
        m = _EDITION_SUFFIX_RE.search(s)
        if m:
            edition = _clean_edition(m.group(0))
            s = _EDITION_SUFFIX_RE.sub("", s)
    s = _DISC_RE.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip(" -–—:")
    return (s or title.strip()), edition


def _plain_key(s: str) -> str:
    return _NONALNUM_RE.sub(" ", unicodedata.normalize("NFC", (s or "").strip().lower())).strip()


# Trailing pressing/edition markers split_edition's keyword list misses: a bracketed
# group, then a separator-suffix (separator must be space-padded so "Candy-O" keeps its
# "-O"). Used only with a known canonical name to validate the cut (see title_residual).
_TRAIL_MARKER_RES = (
    re.compile(r"\s*[\(\[]([^\)\]]+)[\)\]]\s*$"),
    re.compile(r"\s*[-–—:]\s+(.+?)\s*$"),
)


def title_residual(dirty: str, canonical: str) -> Optional[str]:
    """The pressing/edition marker a dirty title carries BEYOND its canonical name —
    catalog number, region, format, bare year — the non-keyword cases split_edition
    leaves on the title. Returns the trailing bracket/suffix ONLY when removing it
    leaves exactly the canonical (so a regional retitle or pure typography yields None,
    never a spurious edition): "Animals (2011, 50999 028951 2 3)" vs "Animals" ->
    "2011, 50999 028951 2 3"; "It's a Heartache" vs "Natural Force" -> None."""
    d = (dirty or "").strip()
    if not d or _plain_key(d) == _plain_key(canonical):
        return None
    for rx in _TRAIL_MARKER_RES:
        m = rx.search(d)
        if m and _plain_key(rx.sub("", d)) == _plain_key(canonical):
            return m.group(1).strip()
    return None


def _stamp_sync(artist_id: str) -> None:
    db_execute(
        "UPDATE artists SET last_album_sync = now() WHERE id = %(id)s::uuid",
        {"id": artist_id},
    )


def _mb_load_in_progress() -> bool:
    """True while mb_dump_load.stream_load holds the reload lock. The
    reconcile must not run against half-TRUNCATEd mb_* tables — it would
    read an empty discography and strip the artist's phantom shelf.
    try-lock + unlock must happen on the SAME pooled connection."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (MB_LOAD_LOCK_KEY,))
            got = cur.fetchone()[0]
            if got:
                cur.execute("SELECT pg_advisory_unlock(%s)", (MB_LOAD_LOCK_KEY,))
            return not got


def _artist_mbids(artist_id: str) -> List[str]:
    """All MB artist MBIDs for a library artist (>1 = conflated namesakes;
    their discographies are unioned). Empty = not canonized."""
    return [r["mbid"] for r in db_query(
        "SELECT mbid::text FROM artist_mbids WHERE artist_id = %(id)s::uuid",
        {"id": artist_id},
    )]


def _upsert_phantom_album(artist_id: str, artist_name: str, rg: dict,
                          artist_mbid: str) -> tuple[Optional[str], bool]:
    """Persist one missing release-group as a phantom album + artist link.
    Returns ``(album_id, inserted)``; album_id is None when the UUID
    collided with an owned row (the guard suppressed the upsert).

    A release-group already in the DB under another UUID (a collaboration
    synced from the other member first) only gains the artist link — the
    rg MBID is the real identity, the name-keyed UUID is just the row key.
    The ON CONFLICT guard (`WHERE NOT EXISTS album_variants`) makes the
    upsert a no-op on an owned album, so an owned record's data can never
    be clobbered even if a UUID somehow collides.
    """
    existing = db_query_one(
        "SELECT id::text FROM albums WHERE musicbrainz_id = %(rg)s::uuid LIMIT 1",
        {"rg": rg["rg_mbid"]},
    )
    if existing:
        db_execute("""
            INSERT INTO album_artists (album_id, artist_id, role, mbid)
            VALUES (%(al)s::uuid, %(ar)s::uuid, 'primary', %(mbid)s::uuid)
            ON CONFLICT DO NOTHING
        """, {"al": existing["id"], "ar": artist_id, "mbid": artist_mbid})
        # Refresh phantom metadata that arrived after the row was minted —
        # e.g. release years appear only once a dump reload fills
        # mb_release_country. Owned rows stay untouched (same guard).
        db_execute("""
            UPDATE albums SET
                release_year = COALESCE(%(year)s, release_year),
                updated_at = now()
            WHERE id = %(al)s::uuid
              AND COALESCE(%(year)s, release_year) IS DISTINCT FROM release_year
              AND NOT EXISTS (SELECT 1 FROM album_variants av
                              WHERE av.album_id = albums.id)
        """, {"al": existing["id"], "year": rg.get("first_year")})
        return existing["id"], False

    from transliterate import latinize
    aid = str(album_uuid(rg["title"], artist_name))
    res = db_execute("""
        WITH up AS (
            INSERT INTO albums (id, title, title_latin, release_year, musicbrainz_id, cover_url)
            VALUES (%(id)s::uuid, %(title)s, %(title_latin)s, %(year)s, %(rg)s::uuid, %(cover)s)
            ON CONFLICT (id) DO UPDATE
                SET musicbrainz_id = EXCLUDED.musicbrainz_id,
                    cover_url = EXCLUDED.cover_url,
                    release_year = COALESCE(EXCLUDED.release_year, albums.release_year),
                    updated_at = now()
                WHERE NOT EXISTS (
                    SELECT 1 FROM album_variants av WHERE av.album_id = albums.id
                )
            RETURNING id, (xmax = 0) AS inserted
        ),
        link AS (
            INSERT INTO album_artists (album_id, artist_id, role, mbid)
            SELECT id, %(ar)s::uuid, 'primary', %(mbid)s::uuid FROM up
            ON CONFLICT DO NOTHING
        )
        SELECT inserted FROM up
    """, {
        "id": aid,
        "title": rg["title"],
        "title_latin": latinize(rg["title"]),
        "year": rg.get("first_year"),  # None until a dump refresh loads mb_release_country
        "rg": rg["rg_mbid"],
        "cover": _CAA_FRONT_URL.format(rg=rg["rg_mbid"]),
        "ar": artist_id,
        "mbid": artist_mbid,
    })
    if not res:
        return None, False
    return aid, bool(res.get("inserted"))


# mb_release.status id (MB InsertDefaultRows): 1 = Official.
_MB_RELEASE_STATUS_OFFICIAL = 1


def _pick_canonical_release(releases: List[dict]) -> Optional[dict]:
    """The release whose tracklist represents the phantom album: Official
    first, then the most complete tracklist, then lowest gid (determinism —
    a re-sync must pick the same release)."""
    if not releases:
        return None
    official = [r for r in releases
                if r.get("status") == _MB_RELEASE_STATUS_OFFICIAL]
    pool = official or releases
    return sorted(pool, key=lambda r: (-len(r["tracks"]), r["release_mbid"]))[0]


def _carry_rekeyed_slots(album_id: str, slot_rows: list, slot_artists: dict) -> int:
    """Slots whose current track is not the one about to claim them: move the
    old track's data onto the new uuid BEFORE the upsert — a rename, or a
    merge when the new uuid already exists (the rip the user made since) —
    so the slot's DO UPDATE is a no-op and nothing analysed, listened to or
    owned is stranded on a row no tracklist points at. The moved track takes
    the MB title its new uuid was derived from and exactly the slot's artist
    as primary (the rename carried the old link along)."""
    current = {(r["disc"], r["position"]): r["track_id"] for r in db_query("""
        SELECT disc, position, track_id::text AS track_id
        FROM album_tracks WHERE album_id = %(al)s::uuid""", {"al": album_id})}
    moves = [(current[(d, pos)], tid, title)
             for (_, tid, d, pos, _, _), title in slot_rows
             if (d, pos) in current and current[(d, pos)] != tid]
    if not moves:
        return 0
    # Only a row this album holds ALONE is this album's to rename.
    # _update_track_uuid renames the ROW, and every slot that points at it
    # follows by cascade — so renaming a row that other slots still reference
    # (another album's, an owned album's, or a slot of this album bound for
    # a different uuid) drags all of them onto a uuid derived from THIS
    # slot's credit. That is how the 2026-08-25 "Various Artists" re-mint
    # turned one compilation slot's "Pictures of You" into Joy Electric's
    # uuid and took 112 other recordings — The Cure's own among them — with
    # it. A shared row keeps its data and its other slots; the slot simply
    # re-points to its own row in the upsert below.
    wanted: dict = {}
    for old, new, _ in moves:
        wanted.setdefault(old, set()).add(new)
    refs = {r["track_id"]: r["n"] for r in db_query("""
        SELECT track_id::text AS track_id, count(*) AS n
        FROM album_tracks WHERE track_id = ANY(CAST(%(olds)s AS uuid[]))
        GROUP BY track_id""", {"olds": list(wanted)})}
    leaving = Counter(old for old, _, _ in moves)
    exclusive = {old for old, news in wanted.items()
                 if len(news) == 1 and refs.get(old, 0) == leaving[old]}
    moves = list({(old, new): (old, new, title) for old, new, title in moves
                  if old in exclusive}.values())
    if not moves:
        return 0
    from transliterate import latinize
    from canon.identity import _update_track_uuid
    from database import SessionLocal
    from sqlalchemy import text as _sql
    db = SessionLocal()
    try:
        for old, new, title in moves:
            _update_track_uuid(db, old, new)
            db.execute(_sql("UPDATE tracks SET title = :t, title_latin = :tl "
                            "WHERE id = :id AND title <> :t"),
                       {"t": title, "tl": latinize(title), "id": new})
            db.execute(_sql("DELETE FROM track_artists WHERE track_id = :id "
                            "AND role = 'primary' AND artist_id <> :ar"),
                       {"id": new, "ar": slot_artists[new]})
        db.commit()
    finally:
        db.close()
    return len(moves)


def _persist_phantom_tracklist(album_id: str, artist_id: str,
                               artist_name: str, rg_mbid: str) -> int:
    """Persist the canonical MB tracklist of one phantom album as
    tracks + track_artists + album_tracks rows — deliberately NO
    media_files (that is the owned/phantom discriminator; a later rip
    collapses onto the same `track_uuid` row and simply gains files).

    Each slot is keyed the way a correctly tagged rip of the same release
    ends up keyed after canon: title + the HEAD of the track's own artist
    credit, not the album's. The printed credit is split exactly as Pass 1
    splits a rip's tag — on the separators no band name uses (feat./ft./
    vs./pres./aka/meets, canon.split.SAFE_SEPARATORS) — so the head is the
    slot's artist and the rest ride as featured credits, and there is no
    compound artist row for a later pass to dissolve. '&' stays whole here
    as it does there: the identity must follow from the string alone (a
    node without the dump derives the same uuid), and '&' names a duo as
    often as a collaboration. Until 2026-08-29 the slot carried the whole
    printed credit — 19k phantom "X feat. Y" artist rows that only a
    scan-triggered Pass 1 would have split, and never did.

    A head that is the album artist (same MB gid, or the same name) uses
    that artist row; any other head — a compilation entry, an alias, a guest
    on a split — gets its own artist row, name-only. No MB anchor for those:
    an anchor would put the row into stale_canonized_artists' discography
    re-derive, and a compilation's fifteen credits times their discographies
    is the fan-out the engagement rule forbids; the anchor arrives the day
    the user engages with the artist. Until 2026-08-25 every slot carried the
    album artist, so a "Various Artists" phantom could never collapse with a
    rip — 530 same-recording pairs on the master lived under two uuids.

    Idempotent: slot upserts on the (album, disc, position) PK; track rows
    never clobber existing titles. A slot that moves to another uuid (a
    re-mint after a credit or normalization change, an MB retitle) carries
    its analysis, listens and files along instead of orphaning them. Returns
    slots written.
    """
    from transliterate import latinize
    from canon.split import detect_compound_type
    best = _pick_canonical_release(mb.fetch_release_tracklists(rg_mbid))
    if not best:
        return 0

    album_mbids = {r["mbid"] for r in db_query("""
        SELECT mbid::text AS mbid FROM album_artists
        WHERE album_id = %(al)s::uuid AND mbid IS NOT NULL
        UNION
        SELECT mbid::text FROM artist_mbids WHERE artist_id = %(ar)s::uuid
    """, {"al": album_id, "ar": artist_id})}
    album_key = normalize(artist_name)

    track_rows, artist_rows, credit_rows, slot_rows = [], [], [], []
    slot_artists: dict = {}
    for tr in best["tracks"]:
        # MB has pathological titles past the column's 500 chars; clamp
        # BEFORE the UUID so identity keys on the stored value.
        title = (tr["title"] or "").strip()[:500]
        if not title:
            continue
        credit = (tr.get("credit") or "").strip()[:500]
        compound = detect_compound_type(credit) if credit else None
        head, featured = ((compound[2][0], compound[2][1:]) if compound
                          else (credit, []))
        if (not head or tr.get("credit_artist_mbid") in album_mbids
                or normalize(head) == album_key):
            slot_artist_id, slot_artist_name = artist_id, artist_name
        else:
            slot_artist_id, slot_artist_name = str(artist_uuid(head)), head
            credit_rows.append((slot_artist_id, head, latinize(head)))
        tid = str(track_uuid(title, slot_artist_name))
        track_rows.append((tid, title, latinize(title)))
        artist_rows.append((tid, "primary", slot_artist_id))
        for name in featured:
            fid = str(artist_uuid(name))
            credit_rows.append((fid, name, latinize(name)))
            artist_rows.append((tid, "featured", fid))
        slot_artists[tid] = slot_artist_id
        slot_rows.append(((album_id, tid, tr["disc"] or 1, tr["position"],
                           tr["recording_mbid"], tr["length_ms"]), title))
    if not slot_rows:
        return 0

    _carry_rekeyed_slots(album_id, slot_rows, slot_artists)

    with get_conn() as conn:
        with conn.cursor() as cur:
            if credit_rows:
                execute_values(cur, """
                    INSERT INTO artists (id, name, name_latin) VALUES %s
                    ON CONFLICT (id) DO NOTHING
                """, credit_rows, template="(%s::uuid, %s, %s)")
            execute_values(cur, """
                INSERT INTO tracks (id, title, title_latin) VALUES %s
                ON CONFLICT (id) DO NOTHING
            """, track_rows, template="(%s::uuid, %s, %s)")
            execute_values(cur, """
                INSERT INTO track_artists (track_id, role, artist_id) VALUES %s
                ON CONFLICT DO NOTHING
            """, artist_rows, template="(%s::uuid, %s::credit_role, %s::uuid)")
            execute_values(cur, """
                INSERT INTO album_tracks
                    (album_id, track_id, disc, position, recording_mbid, length_ms)
                VALUES %s
                ON CONFLICT (album_id, disc, position)
                DO UPDATE SET track_id = EXCLUDED.track_id,
                              recording_mbid = EXCLUDED.recording_mbid,
                              length_ms = EXCLUDED.length_ms,
                              updated_at = now()
            """, [row for row, _ in slot_rows],
                template="(%s::uuid, %s::uuid, %s, %s, %s::uuid, %s)")
        conn.commit()
    return len(slot_rows)


def remint_tracklists(artist_name: Optional[str] = None,
                      limit: Optional[int] = None,
                      album_ids: Optional[List[str]] = None) -> Dict[str, int]:
    """Re-derive the tracklist of every phantom album — optionally only one
    album artist's ("Various Artists" is the case that motivated it), or an
    explicit set of albums — under the current mint rules, carrying analysis
    across re-keyed slots, then drop the old rows nothing points at any
    more. The one-off that follows a mint-rule or normalization change: the
    incremental path never revisits an album that already has a tracklist.
    Per-album isolation, resumable."""
    from canon.identity import delete_orphan_tracks
    from database import SessionLocal
    rows = db_query(f"""
        SELECT DISTINCT ON (al.id)
               al.id::text AS album_id, aa.artist_id::text AS artist_id,
               a.name AS artist_name, al.musicbrainz_id::text AS rg_mbid
        FROM albums al
        JOIN album_artists aa ON aa.album_id = al.id AND aa.role = 'primary'
        JOIN artists a ON a.id = aa.artist_id
        WHERE al.musicbrainz_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM album_variants av WHERE av.album_id = al.id)
          {"AND a.name = %(name)s" if artist_name else ""}
          {"AND al.id = ANY(CAST(%(ids)s AS uuid[]))" if album_ids is not None else ""}
        ORDER BY al.id, aa.artist_id
        {f"LIMIT {int(limit)}" if limit else ""}
    """, {"name": artist_name, "ids": list(album_ids or [])})
    totals = {"albums": len(rows), "processed": 0, "slots": 0, "errors": 0}
    logger.info(f"remint_tracklists: {len(rows)} phantom albums"
                f"{' of ' + artist_name if artist_name else ''}")
    for row in rows:
        try:
            totals["slots"] += _persist_phantom_tracklist(
                row["album_id"], row["artist_id"], row["artist_name"], row["rg_mbid"])
        except Exception as e:
            totals["errors"] += 1
            logger.error(f"remint failed for album {row['album_id']} "
                         f"({row['artist_name']}, rg {row['rg_mbid']}): {e}")
        totals["processed"] += 1
        if totals["processed"] % 1000 == 0:
            logger.info(f"remint_tracklists: {totals['processed']}/{len(rows)} albums, "
                        f"{totals['slots']} slots, {totals['errors']} errors")
    db = SessionLocal()
    try:
        totals["orphans_deleted"] = delete_orphan_tracks(db)
        db.commit()
    finally:
        db.close()
    logger.info(f"remint_tracklists done: {totals}")
    return totals


def _reconcile_phantoms(artist_id: str, missing_rgs: List[str]) -> None:
    """Drop this artist's phantom links that no longer correspond to a
    missing release-group (ripped since the last sync, MB reclassified,
    pre-MB legacy), garbage-collect phantom albums nobody links to, and
    clear a stale external cover on albums that became owned — the
    frontend prefers `cover_url` over the local file cover.

    A phantom that carries analysis is ALWAYS spared — whether the node
    streamed-then-enriched it itself or a CGNAT peer push-seeded its
    segments here (carry). Both are analysis someone spent GPU time on,
    the track cascade would take analysis_sources and embeddings with
    it, and neither can be re-derived without the audio. This guard used
    to be opt-in (lite prune only), which left the discography hot path
    free to delete carried rows the moment their artist got canonized."""
    # Streaming-minted phantoms are OUTSIDE the MB source-of-truth: the user
    # clicked a provider tile (explicit intent), the album has no rg MBID, and
    # this reconcile used to nuke it on the very first artist-page view.
    analyzed_guard = """
              AND NOT EXISTS (SELECT 1 FROM album_tracks at
                              JOIN embeddings e ON e.track_id = at.track_id
                              WHERE at.album_id = al.id)
    """
    if missing_rgs:
        db_execute(f"""
            DELETE FROM album_artists aa
            USING albums al
            WHERE al.id = aa.album_id
              AND aa.artist_id = %(id)s::uuid
              AND NOT EXISTS (SELECT 1 FROM album_variants av WHERE av.album_id = al.id)
              AND NOT EXISTS (SELECT 1 FROM streaming_mints sm WHERE sm.album_id = al.id)
              {analyzed_guard}
              AND (al.musicbrainz_id IS NULL
                   OR NOT (al.musicbrainz_id::text = ANY(%(rgs)s)))
        """, {"id": artist_id, "rgs": missing_rgs})
    else:
        db_execute(f"""
            DELETE FROM album_artists aa
            USING albums al
            WHERE al.id = aa.album_id
              AND aa.artist_id = %(id)s::uuid
              AND NOT EXISTS (SELECT 1 FROM album_variants av WHERE av.album_id = al.id)
              AND NOT EXISTS (SELECT 1 FROM streaming_mints sm WHERE sm.album_id = al.id)
              {analyzed_guard}
        """, {"id": artist_id})

    db_execute("""
        DELETE FROM albums al
        WHERE NOT EXISTS (SELECT 1 FROM album_variants av WHERE av.album_id = al.id)
          AND NOT EXISTS (SELECT 1 FROM album_artists aa WHERE aa.album_id = al.id)
    """)

    db_execute("""
        UPDATE albums al
        SET cover_url = NULL, updated_at = now()
        WHERE al.cover_url IS NOT NULL
          AND EXISTS (SELECT 1 FROM album_variants av WHERE av.album_id = al.id)
          AND EXISTS (SELECT 1 FROM album_artists aa
                      WHERE aa.album_id = al.id AND aa.artist_id = %(id)s::uuid)
    """, {"id": artist_id})

    # Orphan phantom tracks of this artist: their album was GC'd above and
    # no file ever materialized (a ripped phantom track has media_files and
    # is protected). Enrichment rows cascade with the track — the embedding
    # guard is the secondary net behind the album-level analyzed one.
    db_execute("""
        DELETE FROM tracks t
        USING track_artists ta
        WHERE ta.track_id = t.id
          AND ta.artist_id = %(id)s::uuid
          AND NOT EXISTS (SELECT 1 FROM media_files mf WHERE mf.track_id = t.id)
          AND NOT EXISTS (SELECT 1 FROM album_tracks at WHERE at.track_id = t.id)
          AND NOT EXISTS (SELECT 1 FROM embeddings e WHERE e.track_id = t.id)
    """, {"id": artist_id})


def prune_phantom_layer(cancel_flag=None, progress_cb=None) -> Dict[str, Any]:
    """Delete the MB-derived phantom album/track layer. This is the owner's
    explicit "Remove phantom layer" action (Settings › Library) and nothing
    else starts it — a hardware profile governs compute, never retention.
    ~3.1M phantom track rows + album_tracks cost ~3.6 GB of heap+index on
    the reference library.

    Spares: owned rows, streaming mints (explicit user intent), and
    analysis-carrying phantoms (the node's own streamed-enrichment
    contribution). Similar-artist stubs stay — `similar_artists` edges hang
    off them. Everything deleted is re-derivable from the MB dump / Last.fm,
    at the cost of hours. Per-artist commits: resumable and error-isolated.
    `progress_cb(done, total)` fires every 250 artists and at the end; the
    planner statistics are refreshed afterwards so the Settings footprint
    row reads the new size at once."""
    artists = db_query("""
        SELECT DISTINCT aa.artist_id
        FROM album_artists aa
        JOIN albums al ON al.id = aa.album_id
        WHERE NOT EXISTS (SELECT 1 FROM album_variants av WHERE av.album_id = al.id)
          AND NOT EXISTS (SELECT 1 FROM streaming_mints sm WHERE sm.album_id = al.id)
    """)
    total = len(artists)
    stats = {"artists": 0, "total": total, "cancelled": False}
    for row in artists:
        if cancel_flag and cancel_flag():
            stats["cancelled"] = True
            break
        _reconcile_phantoms(str(row["artist_id"]), [])
        stats["artists"] += 1
        if progress_cb and (stats["artists"] % 250 == 0 or stats["artists"] == total):
            progress_cb(stats["artists"], total)
    if stats["artists"]:
        db_execute("ANALYZE tracks, albums, album_tracks")
    logger.info("phantom prune: reconciled %d/%d artists%s", stats["artists"], total,
                " (cancelled)" if stats["cancelled"] else "")
    return stats


def sync_artist_discography(artist_id, artist_name: str) -> Dict[str, int]:
    """Derive `artist_name`'s missing albums from the MB dump and reconcile
    the phantom rows. Canonized artists only; idempotent; stamps
    `last_album_sync`.

    Returns a stats dict: ``{status, found, new, skipped_owned}``.
    `status` is success / not_canonized (no `artist_mbids` row — gate, not
    stamped) / mb_loading (dump reload in flight — skipped, not stamped) /
    rate_limited (dump-less nodes on the MB HTTP API).
    """
    artist_id = str(artist_id)
    stats = {"status": "success", "found": 0, "new": 0, "skipped_owned": 0,
             "tracks": 0}

    from routers.settings import _read as _read_setting
    if not bool(_read_setting("discovery.phantom_layer")):
        # The owner switched the phantom layer off: nothing new is minted
        # and what exists stays until they remove it — the shelf shows
        # owned albums only.
        stats["status"] = "disabled"
        return stats

    mbids = _artist_mbids(artist_id)
    if not mbids:
        # Not canonized (yet): no shelf. Self-heal a revoked canonization —
        # phantom rows minted under a former MBID must not linger.
        if db_query_one("""
            SELECT 1 FROM album_artists aa
            WHERE aa.artist_id = %(id)s::uuid
              AND NOT EXISTS (SELECT 1 FROM album_variants av
                              WHERE av.album_id = aa.album_id)
            LIMIT 1
        """, {"id": artist_id}):
            _reconcile_phantoms(artist_id, [])
        stats["status"] = "not_canonized"
        return stats

    if _mb_load_in_progress():
        # Dump reload in flight — candidates would come from TRUNCATEd
        # tables. Not stamped: retried on the next view/batch.
        stats["status"] = "mb_loading"
        return stats

    try:
        candidates: Dict[str, tuple] = {}
        for mbid in mbids:
            for rg in mb.fetch_album_release_groups(mbid):
                if rg["primary_type"] not in _ALLOWED_PRIMARY:
                    continue
                if set(rg["secondary_types"]) & _DISQUALIFYING_SECONDARY:
                    continue
                if not rg["title"]:
                    continue
                # same 500-char clamp as tracks: identity keys on the
                # stored value (albums.title is VARCHAR(500))
                rg["title"] = rg["title"][:500]
                candidates.setdefault(rg["rg_mbid"], (rg, mbid))
    except mb.MBRateLimited:
        logger.warning(f"MB API rate-limited on discography for {artist_name}")
        stats["status"] = "rate_limited"
        return stats

    owned = db_query("""
        SELECT DISTINCT al.title, al.musicbrainz_id::text AS rg_mbid
        FROM albums al
        JOIN album_variants av ON av.album_id = al.id
        JOIN media_files mf ON mf.album_variant_id = av.id
        JOIN track_artists ta ON ta.track_id = mf.track_id
        WHERE ta.artist_id = %(id)s::uuid
    """, {"id": artist_id})
    owned_rg = {r["rg_mbid"] for r in owned if r["rg_mbid"]}
    owned_keys = {release_match_key(r["title"]) for r in owned}

    def _is_owned(rg: dict) -> bool:
        if rg["rg_mbid"] in owned_rg:
            return True
        if release_match_key(rg["title"]) in owned_keys:
            return True
        # A dirty owned tag often matches a regional/alternate RELEASE
        # title, not the RG's canonical name (same channel mb_audit
        # overlap-verifies through).
        return any(release_match_key(rt) in owned_keys
                   for rt in rg["release_titles"])

    missing = {}
    for rg_mbid, (rg, mbid) in candidates.items():
        if _is_owned(rg):
            stats["skipped_owned"] += 1
        else:
            missing[rg_mbid] = (rg, mbid)

    # Cross-artist ownership: a collaboration album owned through the other
    # member's credit carries the same rg MBID on its owned row.
    if missing:
        rows = db_query("""
            SELECT musicbrainz_id::text AS rg_mbid
            FROM albums
            WHERE musicbrainz_id::text = ANY(%(rgs)s)
              AND EXISTS (SELECT 1 FROM album_variants av WHERE av.album_id = albums.id)
        """, {"rgs": list(missing.keys())})
        for r in rows:
            missing.pop(r["rg_mbid"], None)
            stats["skipped_owned"] += 1

    stats["found"] = len(missing)
    phantoms: List[tuple] = []  # (album_id, rg_mbid)
    for rg, mbid in missing.values():
        album_id, inserted = _upsert_phantom_album(artist_id, artist_name, rg, mbid)
        if inserted:
            stats["new"] += 1
        if album_id:
            phantoms.append((album_id, rg["rg_mbid"]))

    # Tracklists — only for phantom albums that don't have one yet, so a
    # monthly re-sync doesn't re-read tens of releases per album.
    if phantoms:
        have = {r["album_id"] for r in db_query("""
            SELECT DISTINCT album_id::text AS album_id FROM album_tracks
            WHERE album_id = ANY(%(ids)s::uuid[])
        """, {"ids": [a for a, _ in phantoms]})}
        for album_id, rg_mbid in phantoms:
            if album_id not in have:
                stats["tracks"] += _persist_phantom_tracklist(
                    album_id, artist_id, artist_name, rg_mbid)
        # Genres for the freshly-minted shelf (release_group_tag) — so a new
        # phantom album carries genres at creation, not only after a dump
        # reload runs the full refresh_album_mb_genres sweep.
        from canon.genres import materialize_album_genres
        materialize_album_genres([a for a, _ in phantoms])

    _reconcile_phantoms(artist_id, list(missing.keys()))
    _stamp_sync(artist_id)
    return stats


def fetch_new_albums(artist_id, mbid=None, include_unattributed=False) -> List[dict]:
    """The phantom albums for an artist — those linked via `album_artists`
    with no local files — newest first. Tile shape for the artist screen.

    `mbid` scopes to one namesake (album_artists.mbid) when a display name maps
    to several MB artists; `include_unattributed` also folds NULL-mbid residue
    under it (used for the dominant namesake so nothing is orphaned).
    """
    scope = ""
    params = {"id": str(artist_id)}
    if mbid is not None:
        scope = ("AND (aa.mbid = %(mbid)s::uuid OR aa.mbid IS NULL)"
                 if include_unattributed else "AND aa.mbid = %(mbid)s::uuid")
        params["mbid"] = str(mbid)
    return db_query(f"""
        SELECT al.id::text AS id,
               al.title,
               al.release_year AS year,
               al.cover_url
        FROM albums al
        JOIN album_artists aa ON aa.album_id = al.id
        WHERE aa.artist_id = %(id)s::uuid
          {scope}
          AND NOT EXISTS (
              SELECT 1 FROM album_variants av WHERE av.album_id = al.id
          )
        ORDER BY al.release_year DESC NULLS LAST, al.title
    """, params)


# Listening recency window that defines "artists I listen to" for the
# update priority (same 6 months the Deezer-era step used).
_LISTENED_MONTHS = 6


def stale_canonized_artists(limit: Optional[int] = None,
                            stale_days: int = 30) -> List[dict]:
    """Canonized artists whose new-album data is missing or older than
    `stale_days`, in update-priority order:

      tier 0 — artists actually listened to in the last 6 months,
      tier 1 — artists similar to those (both edge directions — Last.fm
               similarity is stored directed, see the artist-screen note),
      tier 2 — the rest (so a post-dump-refresh re-derive eventually
               covers everyone, relevant shelves first).

    Within a tier: oldest data first. Shared by the background step and
    the CLI backfill — one source of truth for the priority.
    """
    sql = f"""
        WITH listened AS (
            SELECT DISTINCT ta.artist_id
            FROM track_artists ta
            JOIN listening_history lh ON lh.track_id = ta.track_id
            WHERE ta.role = 'primary'
              AND lh.started_at > now() - interval '{_LISTENED_MONTHS} months'
        ),
        similar_of_listened AS (
            SELECT sa.similar_artist_id AS artist_id
            FROM similar_artists sa JOIN listened l ON l.artist_id = sa.artist_id
            UNION
            SELECT sa.artist_id
            FROM similar_artists sa JOIN listened l ON l.artist_id = sa.similar_artist_id
        )
        SELECT a.id::text AS id, a.name
        FROM artists a
        WHERE EXISTS (SELECT 1 FROM artist_mbids am WHERE am.artist_id = a.id)
          AND (a.last_album_sync IS NULL
               OR a.last_album_sync < now() - make_interval(days => %(days)s))
        ORDER BY
          CASE WHEN a.id IN (SELECT artist_id FROM listened) THEN 0
               WHEN a.id IN (SELECT artist_id FROM similar_of_listened) THEN 1
               ELSE 2 END,
          a.last_album_sync ASC NULLS FIRST,
          a.name
    """
    params: Dict = {"days": stale_days}
    if limit:
        sql += " LIMIT %(lim)s"
        params["lim"] = int(limit)
    return db_query(sql, params)


def run_backfill(limit: Optional[int] = None, stale_days: int = 30) -> Dict[str, int]:
    """One-shot sweep: reconcile every canonized artist whose new-album data
    is missing or older than `stale_days`, listened-first. Pure local-DB
    work with the dump loaded. Returns aggregate stats."""
    rows = stale_canonized_artists(limit=limit, stale_days=stale_days)

    totals = {"processed": 0, "found": 0, "new": 0, "skipped_owned": 0,
              "rate_limited": 0}
    for row in rows:
        result = sync_artist_discography(row["id"], row["name"])
        totals["processed"] += 1
        if result["status"] in ("rate_limited", "mb_loading"):
            totals["rate_limited"] += 1
            logger.warning(f"Backfill: {result['status']} — stopping early")
            break
        totals["found"] += result["found"]
        totals["new"] += result["new"]
        totals["skipped_owned"] += result["skipped_owned"]
        if totals["processed"] % 200 == 0:
            logger.info(f"Backfill: {totals['processed']}/{len(rows)} artists, "
                        f"{totals['new']} phantom albums so far")
    logger.info(f"Backfill done: {totals}")
    return totals


def backfill_tracklists(limit: Optional[int] = None) -> Dict[str, int]:
    """One-shot sweep: persist tracklists for phantom albums that lack one
    (albums created before Stage B, or whose fetch failed). The track-UUID
    namespace keys on the album's first-linked artist name — the same
    artist whose sync minted the album row for non-collabs."""
    if _mb_load_in_progress():
        logger.warning("Tracklist backfill: dump reload in flight — aborting")
        return {"processed": 0, "tracks": 0}

    rows = db_query(f"""
        SELECT al.id::text AS album_id, al.musicbrainz_id::text AS rg_mbid,
               a.id::text AS artist_id, a.name AS artist_name
        FROM albums al
        JOIN LATERAL (
            SELECT ar.id, ar.name
            FROM album_artists aa
            JOIN artists ar ON ar.id = aa.artist_id
            WHERE aa.album_id = al.id
            ORDER BY ar.name
            LIMIT 1
        ) a ON true
        WHERE al.musicbrainz_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM album_variants av WHERE av.album_id = al.id)
          AND NOT EXISTS (SELECT 1 FROM album_tracks at WHERE at.album_id = al.id)
        ORDER BY al.id
        {f"LIMIT {int(limit)}" if limit else ""}
    """)

    totals = {"processed": 0, "tracks": 0, "errors": 0}
    for row in rows:
        try:
            totals["tracks"] += _persist_phantom_tracklist(
                row["album_id"], row["artist_id"], row["artist_name"], row["rg_mbid"])
        except Exception as e:
            # per-album isolation (enrichment convention): one pathological
            # tracklist must not kill the remaining tens of thousands
            totals["errors"] += 1
            logger.error(f"Tracklist backfill failed for album {row['album_id']} "
                         f"({row['artist_name']}, rg {row['rg_mbid']}): {e}")
        totals["processed"] += 1
        if totals["processed"] % 2000 == 0:
            logger.info(f"Tracklist backfill: {totals['processed']}/{len(rows)} albums, "
                        f"{totals['tracks']} tracks")
    logger.info(f"Tracklist backfill done: {totals}")
    return totals


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(
        description="Backfill missing-album discovery for canonized artists")
    parser.add_argument("--limit", type=int, default=None,
                        help="max artists/albums to process (default: all)")
    parser.add_argument("--stale-days", type=int, default=30,
                        help="re-sync artists older than this (default 30)")
    parser.add_argument("--tracklists", action="store_true",
                        help="persist tracklists for phantom albums lacking one")
    parser.add_argument("--remint-tracklists", action="store_true",
                        help="re-derive EVERY phantom tracklist under the current "
                             "mint rules (one-off after a rule change)")
    parser.add_argument("--artist", default=None,
                        help="with --remint-tracklists: only albums of this album "
                             "artist, e.g. 'Various Artists'")
    args = parser.parse_args()
    if args.remint_tracklists:
        print(remint_tracklists(artist_name=args.artist, limit=args.limit))
    elif args.tracklists:
        print(backfill_tracklists(limit=args.limit))
    else:
        print(run_backfill(limit=args.limit, stale_days=args.stale_days))
