"""Place the owner's imported scrobbles on canonical tracks (Last.fm history).

lastfm_history fills the waiting room (`pending_scrobbles`) with raw strings;
this module places them. A scrobble becomes a listen only on a CANONICAL track
— an owned file's, or a slot MusicBrainz minted — whose length is known:

  A  bind to a track this node already has: the name-derived track UUID of the
     scrobble's title and artist, or of their decoration-free forms, names an
     owned or minted track;
  B  resolve the artist name against MusicBrainz from the waiting scrobbles'
     evidence — the heard track titles and album titles must overlap the
     candidate's catalogue. It is the owned canon's anchor with heard albums in
     place of owned ones; Last.fm's MBIDs only break ties (they are its own
     name-based guesses, never anchors);
  C  mint the resolved artist's discography (discography.sync_artist_discography,
     its gates and "fully timed editions only" unchanged);
  D  bind through the artist's MB catalogue: a scrobble title matched against
     every track and recording name credited to the artist — edition
     decorations like "2011 Remaster" live there — gives the recording, and the
     recording the local track;
  E  write the listens (source 'lastfm'; the seconds listened are the track's
     length — Last.fm only knows the scrobble rule was met) and let the waiting
     rows go.

What cannot be placed waits — a name MusicBrainz does not know, a recording
nothing minted (a standalone single, a compilation, a live record) — and is
tried again when something changes for it: new scrobbles for the name, a slice
for it, a dump, a mint of its artist. A scan or a sync can bring any track, so
it re-runs A over everything. No timer: one consumer thread wakes on events.
"""

import hashlib
import json
import logging
import math
import re
import threading
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from psycopg2.extras import RealDictCursor, execute_values

from db_pool import db_execute, db_query, db_query_one, transaction
from play_stats import LISTENS_LOCK_KEY, SAME_LISTEN_S, refresh_play_stats, same_listen
from sql_queries import best_rip_order
from uuid_utils import artist_uuid, track_uuid

logger = logging.getLogger(__name__)

_BIND_CHUNK = 2000        # waiting scrobbles read and bound per transaction
_RESOLVE_PER_PASS = 200   # artist names decided per pass
_MINT_PER_PASS = 25       # discographies minted per pass
# Titles that name a place on a record rather than a song — evidence from them
# would fit any artist's catalogue.
_STOP_TITLES = frozenset({"intro", "outro", "interlude", "untitled", "skit", "hidden track",
                          "bonus track", "prelude", "reprise", "instrumental"})
_FEAT = re.compile(r"\s*[\(\[]\s*(?:feat\.?|ft\.?|featuring|with)\s[^\)\]]*[\)\]]",
                   re.IGNORECASE)
# The cut a store or a radio label names, not another performance: "(Radio
# Edit)", "- Single Version", "(Original Mix)".
_VERSION = re.compile(
    r"\s*(?:[\(\[]\s*(?:single|radio|album|lp|original)\s+(?:version|edit|mix)\s*[\)\]]"
    r"|[-–—]\s*(?:single|radio|album|lp|original)\s+(?:version|edit|mix)\s*$)",
    re.IGNORECASE)
# MB's special-purpose artists — [unknown], [traditional], Various Artists —
# credit every record on Earth; a name that is one of them places nothing.
_SPECIAL = re.compile(r"^\[.*\]$")
_VARIOUS = frozenset({"various artists", "various", "va", "various artist"})

# A track this node holds, with its lengths: the best rip's, or each slot's.
_TRACK_LENGTHS_SQL = f"""
    SELECT t.id::text AS track_id, f.id AS media_file_id, f.duration_seconds,
           at.length_ms, al.title AS album_title
      FROM tracks t
      LEFT JOIN LATERAL (SELECT mf.id, mf.duration_seconds FROM media_files mf
                          WHERE mf.track_id = t.id AND mf.duration_seconds > 0
                          ORDER BY {best_rip_order('mf')} LIMIT 1) f ON TRUE
      LEFT JOIN album_tracks at ON at.track_id = t.id AND at.length_ms > 0
      LEFT JOIN albums al ON al.id = at.album_id
     WHERE t.id = ANY(CAST(%(ids)s AS uuid[]))
       AND (f.id IS NOT NULL OR at.track_id IS NOT NULL)
"""

Row = Dict[str, Any]


# --------------------------------------------------------------------------
# Name and title forms
# --------------------------------------------------------------------------

def _title_variants(title: str) -> List[str]:
    """The title as scrobbled, then without a "(feat. …)" credit, without an
    edition marker ("- 2011 Remaster"), without a version tag ("- Radio
    Edit") — the most precise form first. A live, acoustic or remixed take is
    another recording and keeps its word."""
    from discography import split_edition
    out: List[str] = []

    def add(v: str) -> None:
        v = v.strip()
        if v and v not in out:
            out.append(v)

    for t in (title, _FEAT.sub("", title)):
        add(t)
        add(split_edition(t)[0])
        add(_VERSION.sub("", t))
        add(split_edition(_VERSION.sub("", t))[0])
    return out


def _head(artist: str) -> str:
    """The head of a safe compound credit ("A feat. B" → "A"), else the credit."""
    from canon.split import detect_compound_type
    compound = detect_compound_type(artist)
    return compound[2][0] if compound else artist


def _artist_variants(artist: str) -> List[str]:
    head = _head(artist)
    return [artist] + ([head] if head != artist else [])


def _lower_forms(title: str) -> List[str]:
    """What an MB track or recording name may read as, lowered: every variant,
    in the scrobbler's and in MB's punctuation, the most precise first."""
    from canon.match import _mb_spelling
    out: List[str] = []
    for v in _title_variants(title):
        for f in (v.lower(), _mb_spelling(v).lower()):
            if f not in out:
                out.append(f)
    return out


def _unplaceable(name_key: str) -> bool:
    return name_key in _VARIOUS or bool(_SPECIAL.match(name_key))


# --------------------------------------------------------------------------
# The waiting room, the lengths, the listens
# --------------------------------------------------------------------------

def _pending(cur, name_keys: Optional[List[str]], after: Optional[Tuple]) -> List[Row]:
    """The next chunk of waiting scrobbles — of `name_keys`, or of all — newest
    first, after the keyset position `after` (the last row of the previous
    chunk: bound rows leave, so the position is what moves the reading on)."""
    cur.execute(f"""
        SELECT played_at, artist, title, album, name_key, artist_mbid::text AS artist_mbid,
               album_mbid::text AS album_mbid, track_mbid::text AS track_mbid
          FROM pending_scrobbles
         WHERE {'name_key = ANY(%(names)s)' if name_keys is not None else 'TRUE'}
           AND (%(p)s::timestamptz IS NULL OR (played_at, artist, title) < (%(p)s, %(a)s, %(t)s))
         ORDER BY played_at DESC, artist DESC, title DESC
         LIMIT %(lim)s""",
        {"names": name_keys, "p": after[0] if after else None, "a": after[1] if after else None,
         "t": after[2] if after else None, "lim": _BIND_CHUNK})
    return cur.fetchall()


def _chunks(name_keys: Optional[List[str]]):
    """(cursor, rows) for every chunk of the waiting scrobbles of `name_keys`,
    each in its own transaction under the listens lock."""
    after = None
    while True:
        with transaction(RealDictCursor) as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (LISTENS_LOCK_KEY,))
            rows = _pending(cur, name_keys, after)
            if not rows:
                return
            yield cur, rows
        if len(rows) < _BIND_CHUNK:
            return
        last = rows[-1]
        after = (last["played_at"], last["artist"], last["title"])


def _track_lengths(cur, track_ids: Iterable[str]) -> Dict[str, Row]:
    """track id → {media_file_id, file_seconds, slots: [(album title, seconds)]}
    for the tracks this node can place a listen on."""
    ids = sorted(set(track_ids))
    out: Dict[str, Row] = {}
    if not ids:
        return out
    cur.execute(_TRACK_LENGTHS_SQL, {"ids": ids})
    for r in cur.fetchall():
        e = out.setdefault(r["track_id"], {"media_file_id": r["media_file_id"],
                                           "file_seconds": r["duration_seconds"], "slots": []})
        if r["length_ms"]:
            e["slots"].append((r["album_title"], r["length_ms"] / 1000.0))
    return out


def _seconds(entry: Row, album: Optional[str]) -> Optional[float]:
    """The length a scrobble of this track stands for: the owned rip's, else
    the slot on the scrobbled album, else the length most of its slots agree on."""
    from discography import release_match_key
    if entry["file_seconds"]:
        return float(entry["file_seconds"])
    slots = entry["slots"]
    if not slots:
        return None
    key = release_match_key(album) if album else ""
    for title, seconds in slots:
        if key and release_match_key(title or "") == key:
            return seconds
    return Counter(s for _, s in slots).most_common(1)[0][0]


def _commit_binds(cur, binds: List[Row]) -> int:
    """Write placed scrobbles as listens and release their waiting rows. Two
    scrobbles of one play (two scrobblers reporting it) become one listen; a
    record of the same listen already here wins (play_stats.same_listen)."""
    if not binds:
        return 0
    binds = sorted(binds, key=lambda b: b["played_at"])
    kept: List[Row] = []
    for b in binds:
        if kept and (b["played_at"] - kept[-1]["played_at"]).total_seconds() <= SAME_LISTEN_S:
            continue
        kept.append(b)
    rows = execute_values(cur, f"""
        INSERT INTO listening_history (media_file_id, track_id, started_at, ended_at,
                                       duration_listened, percent_listened, completed,
                                       skipped, source)
        SELECT v.media_file_id, v.track_id, v.played_at,
               v.played_at + v.seconds * interval '1 second', v.seconds, NULL, TRUE, FALSE, 'lastfm'
          FROM (VALUES %s) AS v(media_file_id, track_id, played_at, seconds)
         WHERE NOT EXISTS (SELECT 1 FROM listening_history h
                            WHERE {same_listen('h', 'v.played_at')})
        RETURNING track_id::text AS track_id""",
        [(b["media_file_id"], b["track_id"], b["played_at"], round(b["seconds"], 2)) for b in kept],
        template="(%s::int, %s::uuid, %s::timestamptz, %s::numeric)", fetch=True)
    execute_values(cur, """
        DELETE FROM pending_scrobbles p USING (VALUES %s) AS v(played_at, artist, title)
         WHERE p.played_at = v.played_at AND p.artist = v.artist AND p.title = v.title""",
        [(b["played_at"], b["artist"], b["title"]) for b in binds],
        template="(%s::timestamptz, %s, %s)")
    refresh_play_stats(cur, sorted({r["track_id"] for r in rows}))
    return len(rows)


def drop_echoes() -> int:
    """Waiting scrobbles a record of the same listen has reached since they
    were read (this node's own play, written after the walk passed it) are
    that listen already."""
    with transaction() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (LISTENS_LOCK_KEY,))
        cur.execute(f"""DELETE FROM pending_scrobbles p
                         WHERE EXISTS (SELECT 1 FROM listening_history h
                                        WHERE {same_listen('h', 'p.played_at')})""")
        return cur.rowcount


# --------------------------------------------------------------------------
# A — tracks this node already has
# --------------------------------------------------------------------------

def _bind_known(cur, rows: List[Row]) -> List[Row]:
    wanted = [[str(track_uuid(t, a)) for a in _artist_variants(r["artist"])
               for t in _title_variants(r["title"])] for r in rows]
    lengths = _track_lengths(cur, (u for us in wanted for u in us))
    binds = []
    for r, uuids in zip(rows, wanted):
        for u in uuids:
            entry = lengths.get(u)
            seconds = _seconds(entry, r["album"]) if entry else None
            if seconds:
                binds.append({**r, "track_id": u, "seconds": seconds,
                              "media_file_id": entry["media_file_id"]})
                break
    return binds


def bind_known(name_keys: Optional[List[str]] = None) -> int:
    """Stage A over the waiting scrobbles of `name_keys` (all when None)."""
    return sum(_commit_binds(cur, _bind_known(cur, rows)) for cur, rows in _chunks(name_keys))


# --------------------------------------------------------------------------
# B — the artist, from the scrobbles' evidence
# --------------------------------------------------------------------------

def _evidence(name_keys: List[str]) -> Dict[str, Row]:
    return _evidence_of(db_query("""
        SELECT name_key, artist, title, album, artist_mbid::text AS artist_mbid
          FROM pending_scrobbles WHERE name_key = ANY(%(n)s)
    """, {"n": name_keys}))


def _evidence_of(rows: Iterable[Row]) -> Dict[str, Row]:
    """name_key → its scrobbles' title forms (lowered form → title key), the
    title and album keys, the credits carried, and Last.fm's artist MBIDs."""
    from discography import release_match_key
    out: Dict[str, Row] = {}
    for r in rows:
        e = out.setdefault(r["name_key"], {"forms": {}, "titles": set(), "albums": set(),
                                           "credits": Counter(), "hints": set()})
        e["credits"][r["artist"]] += 1
        if r["artist_mbid"]:
            e["hints"].add(r["artist_mbid"])
        key = r["title"].strip().lower()
        if key and key not in _STOP_TITLES:
            e["titles"].add(key)
            for f in _lower_forms(r["title"]):
                e["forms"].setdefault(f, key)
        if r["album"]:
            k = release_match_key(r["album"])
            if k:
                e["albums"].add(k)
    return out


def _candidates(credit: str) -> List[str]:
    """MB artist gids the credit's head may denote — the phantom canon's
    deterministic set (name, &↔and, MB punctuation, alias for non-compounds,
    unaccent) — minus MB's special-purpose artists."""
    from canon.match import phantom_candidate_gids
    gids = phantom_candidate_gids(_head(credit))
    if not gids:
        return []
    return [r["gid"] for r in db_query("""
        SELECT gid::text AS gid FROM mb_artist
         WHERE gid = ANY(%(g)s::uuid[])
           AND name !~ '^\\[.*\\]$' AND lower(name) <> 'various artists'
    """, {"g": gids})]


def _title_matches(gids: List[str], forms: List[str]) -> Dict[str, Set[str]]:
    """gid → the lowered track/recording names credited to it that are among
    `forms` — matched in SQL over the artist's credited catalogue, so a
    prolific artist's names never leave the database."""
    out: Dict[str, Set[str]] = defaultdict(set)
    if not gids or not forms:
        return out
    for r in db_query("""
        WITH cand AS (SELECT id, gid FROM mb_artist WHERE gid = ANY(%(g)s::uuid[])),
             cred AS (SELECT DISTINCT c.gid, acn.artist_credit
                        FROM cand c JOIN mb_artist_credit_name acn ON acn.artist = c.id)
        SELECT cr.gid::text AS gid, lower(t.name) AS n
          FROM cred cr JOIN mb_track t ON t.artist_credit = cr.artist_credit
         WHERE lower(t.name) = ANY(%(f)s)
        UNION
        SELECT cr.gid::text, lower(r.name)
          FROM cred cr JOIN mb_recording r ON r.artist_credit = cr.artist_credit
         WHERE lower(r.name) = ANY(%(f)s)
    """, {"g": gids, "f": forms}):
        out[r["gid"]].add(r["n"])
    return out


def decide(e: Row, gids: List[str], anchored: Set[str]) -> Optional[str]:
    """The candidate the evidence names, or None. A candidate needs a score —
    heard titles plus heard albums found in its catalogue — of at least
    min(3, max(1, ⌈10 % of the evidence⌉)), and the top score must be unique:
    a tie is broken only by this node's own anchoring of one of them, then by
    Last.fm's artist MBID; otherwise the name waits."""
    from canon.match import release_title_keys
    matched = _title_matches(gids, list(e["forms"]))
    albums = release_title_keys(gids) if e["albums"] else {}
    scores = {g: len({e["forms"][n] for n in matched.get(g, ())})
              + len(e["albums"] & albums.get(g, set())) for g in gids}
    need = min(3, max(1, math.ceil(0.1 * (len(e["titles"]) + len(e["albums"])))))
    top = max(scores.values(), default=0)
    if top < need:
        return None
    best = [g for g in gids if scores[g] == top]
    for tie_break in (anchored, e["hints"]):
        narrowed = [g for g in best if g in tie_break]
        if len(best) > 1 and len(narrowed) == 1:
            best = narrowed
    return best[0] if len(best) == 1 else None


def _anchor(gid: str) -> Optional[str]:
    """The local artist an MB gid belongs to: this node's existing decision,
    else the artist row of MusicBrainz's own name (born canonical) — unless
    that row already stands for another MB entity. Two namesakes on one row
    would pour both discographies onto it; the name waits instead."""
    row = db_query_one("SELECT artist_id::text AS id FROM artist_mbids WHERE mbid = %(g)s::uuid",
                       {"g": gid})
    if row:
        return row["id"]
    mb = db_query_one("SELECT name FROM mb_artist WHERE gid = %(g)s::uuid", {"g": gid})
    if not mb:
        return None
    if db_query_one("SELECT 1 AS x FROM artist_mbids WHERE artist_id = %(a)s::uuid",
                    {"a": str(artist_uuid(mb["name"]))}):
        return None
    from canon.identity import _ensure_artist
    from database import get_db_context
    with get_db_context() as db:
        artist_id = str(_ensure_artist(db, mb["name"]))
        db.commit()
    db_execute("INSERT INTO artist_mbids (mbid, artist_id, confidence) VALUES (%s, %s, 'phantom') "
               "ON CONFLICT (mbid) DO NOTHING", (gid, artist_id))
    return artist_id


def resolve_names(limit: int = _RESOLVE_PER_PASS, *, create: bool = True) -> Row:
    """Stage B for the names due a decision — never tried, new evidence, or new
    MB data (a slice for the name, a dump) since the last try — and covered by
    MB data at all. With `create` off (the phantom layer switched off) only a
    gid this node already anchors is taken. Returns counts, the artists
    resolved, and whether names are left for another pass."""
    from discography import _MB_SOURCE_COVERS_SQL
    stats: Row = {"decided": 0, "resolved": 0, "not_in_mb": 0, "undecided": 0}
    due = [r["name_key"] for r in db_query(f"""
        SELECT n.name_key
          FROM pending_scrobble_artists n JOIN pending_scrobbles p USING (name_key)
         WHERE n.artist_id IS NULL
           AND (n.checked_at IS NULL OR n.checked_at < n.touched_at
                OR EXISTS (SELECT 1 FROM mb_slice_fetches f
                            WHERE f.name_key = n.name_key AND f.fetched_at > n.checked_at)
                OR n.checked_at < (SELECT updated_at FROM user_settings
                                    WHERE key = 'musicbrainz.db_version'))
           AND ({_MB_SOURCE_COVERS_SQL.format(name='n.name_key')})
         GROUP BY n.name_key
         ORDER BY count(*) DESC, n.name_key
         LIMIT %(lim)s""", {"lim": limit})]
    resolved: Dict[str, str] = {}
    evidence = _evidence(due) if due else {}
    for nk in due:
        stats["decided"] += 1
        e = evidence.get(nk)
        if e is None or _unplaceable(nk):
            continue
        gids = _candidates(e["credits"].most_common(1)[0][0])
        if not gids:
            stats["not_in_mb"] += 1
            continue
        anchored = {r["gid"] for r in db_query(
            "SELECT mbid::text AS gid FROM artist_mbids WHERE mbid = ANY(%(g)s::uuid[])",
            {"g": gids})}
        if not create:
            gids = [g for g in gids if g in anchored]
        gid = decide(e, gids, anchored) if gids else None
        artist_id = _anchor(gid) if gid else None
        if artist_id is None:
            stats["undecided"] += 1
            continue
        resolved[nk] = artist_id
        stats["resolved"] += 1
    if due:
        db_execute("""UPDATE pending_scrobble_artists
                         SET checked_at = now(),
                             artist_id = COALESCE((CAST(%(r)s AS jsonb) ->> name_key)::uuid, artist_id)
                       WHERE name_key = ANY(%(n)s)""", {"n": due, "r": json.dumps(resolved)})
    stats["artists"] = sorted(set(resolved.values()))
    stats["drain"] = len(due) >= limit
    return stats


# --------------------------------------------------------------------------
# C — the resolved artists' discographies
# --------------------------------------------------------------------------

def mint_resolved(limit: int = _MINT_PER_PASS) -> Row:
    """Stage C: the discography of every resolved artist with scrobbles waiting
    that was never shelved, most-scrobbled first. Outside the canon lock — the
    mint probes the dump lock itself, and would read our hold as a reload."""
    from discography import sync_artist_discography
    stats: Row = {"statuses": Counter(), "artists": []}
    rows = db_query("""
        SELECT a.id::text AS id, a.name
          FROM pending_scrobble_artists n
          JOIN pending_scrobbles p USING (name_key)
          JOIN artists a ON a.id = n.artist_id
         WHERE a.last_album_sync IS NULL
         GROUP BY a.id, a.name
         ORDER BY count(*) DESC, a.name
         LIMIT %(lim)s""", {"lim": limit})
    for r in rows:
        status = sync_artist_discography(r["id"], r["name"])["status"]
        stats["statuses"][status] += 1
        if status == "success":
            stats["artists"].append(r["id"])
        if status in ("mb_loading", "rate_limited"):
            break
    if stats["statuses"]["no_source"]:
        # A slice node holds the name the scrobbles carried, not always the
        # canonical one the mint asks about: the "canonized, never shelved"
        # tier fetches it.
        db_execute("NOTIFY sautium_mb_pending")
    stats["drain"] = len(rows) >= limit
    return stats


# --------------------------------------------------------------------------
# D — through the resolved artist's catalogue
# --------------------------------------------------------------------------

def _catalogue(gids: List[str], forms: List[str], track_hints: List[str]) -> List[Row]:
    """The artist's credited tracks and recordings whose lowered name is one of
    `forms` (or whose MB track gid Last.fm hinted): name, recording gid, track
    gid, and whether the artist heads the credit."""
    return db_query("""
        WITH a AS (SELECT id FROM mb_artist WHERE gid = ANY(%(g)s::uuid[])),
             cred AS (SELECT acn.artist_credit, min(acn.position) AS pos
                        FROM mb_artist_credit_name acn JOIN a ON acn.artist = a.id
                       GROUP BY acn.artist_credit)
        SELECT lower(t.name) AS n, rec.gid::text AS recording, t.gid::text AS track_gid,
               cr.pos = 0 AS head
          FROM cred cr JOIN mb_track t ON t.artist_credit = cr.artist_credit
          JOIN mb_recording rec ON rec.id = t.recording
         WHERE lower(t.name) = ANY(%(f)s) OR t.gid = ANY(CAST(%(h)s AS uuid[]))
        UNION
        SELECT lower(r.name), r.gid::text, NULL, cr.pos = 0
          FROM cred cr JOIN mb_recording r ON r.artist_credit = cr.artist_credit
         WHERE lower(r.name) = ANY(%(f)s)
    """, {"g": gids, "f": forms, "h": track_hints})


def _local_tracks(cur, recordings: List[str]) -> Dict[str, List[str]]:
    """recording gid → the local tracks bound to it: a recording binding, an
    owned file's materialized recording, a minted slot."""
    out: Dict[str, List[str]] = defaultdict(list)
    if not recordings:
        return out
    cur.execute("""
        SELECT recording_mbid::text AS rec, track_id::text AS track_id FROM track_mbids
         WHERE recording_mbid = ANY(CAST(%(r)s AS uuid[]))
        UNION
        SELECT recording_mbid::text, track_id::text FROM media_files
         WHERE recording_mbid = ANY(CAST(%(r)s AS uuid[]))
        UNION
        SELECT recording_mbid::text, track_id::text FROM album_tracks
         WHERE recording_mbid = ANY(CAST(%(r)s AS uuid[]))
    """, {"r": recordings})
    for r in cur.fetchall():
        out[r["rec"]].append(r["track_id"])
    return out


def _pick(row: Row, names: List[Row], local: Dict[str, List[str]],
          fallback: Dict[str, str], lengths: Dict[str, Row]) -> Tuple[Optional[str], bool]:
    """(track id, named) for one scrobble: the most precise title form that
    names a recording this node holds a track for — the recording Last.fm's
    track MBID points at first, the artist's own credits before guest spots.
    `named` tells a recording MB has but nothing minted from one MB lacks."""
    forms = _lower_forms(row["title"])
    ranked = []
    for n in names:
        if row["track_mbid"] and n["track_gid"] == row["track_mbid"]:
            rank = -1
        elif n["n"] in forms:
            rank = forms.index(n["n"])
        else:
            continue
        ranked.append((rank, not n["head"], n["recording"]))
    for _, _, rec in sorted(ranked):
        for track_id in local.get(rec, []) + ([fallback[rec]] if rec in fallback else []):
            entry = lengths.get(track_id)
            if entry and _seconds(entry, row["album"]):
                return track_id, True
    return None, bool(ranked)


def bind_catalogue(extra_artists: Iterable[str] = ()) -> Row:
    """Stage D for the resolved names due a placement attempt — never tried
    since resolution, new scrobbles, or a mint of their artist since — plus
    the artists in `extra_artists`. Stamps each name's attempt."""
    stats: Row = {"bound": 0, "not_in_catalogue": 0, "not_minted": 0}
    artists = db_query("""
        SELECT n.artist_id::text AS artist_id, a.name, array_agg(DISTINCT n.name_key) AS names,
               (SELECT array_agg(am.mbid::text) FROM artist_mbids am
                 WHERE am.artist_id = n.artist_id) AS gids
          FROM pending_scrobble_artists n
          JOIN artists a ON a.id = n.artist_id
         WHERE EXISTS (SELECT 1 FROM pending_scrobbles p WHERE p.name_key = n.name_key)
           AND (n.checked_at IS NULL OR n.checked_at < n.touched_at
                OR n.checked_at < a.last_album_sync
                OR n.artist_id = ANY(CAST(%(x)s AS uuid[])))
         GROUP BY n.artist_id, a.name""", {"x": sorted(set(extra_artists))})
    for art in artists:
        if not art["gids"]:
            continue
        for cur, rows in _chunks(art["names"]):
            forms = sorted({f for r in rows for f in _lower_forms(r["title"])})
            hints = sorted({r["track_mbid"] for r in rows if r["track_mbid"]})
            names = _catalogue(art["gids"], forms, hints)
            local = _local_tracks(cur, sorted({n["recording"] for n in names}))
            fallback = {n["recording"]: str(track_uuid(n["n"], art["name"])) for n in names}
            lengths = _track_lengths(cur, [t for ts in local.values() for t in ts]
                                     + list(fallback.values()))
            binds = []
            for r in rows:
                track_id, named = _pick(r, names, local, fallback, lengths)
                if track_id is None:
                    stats["not_minted" if named else "not_in_catalogue"] += 1
                    continue
                entry = lengths[track_id]
                binds.append({**r, "track_id": track_id, "seconds": _seconds(entry, r["album"]),
                              "media_file_id": entry["media_file_id"]})
            stats["bound"] += _commit_binds(cur, binds)
        db_execute("UPDATE pending_scrobble_artists SET checked_at = now() WHERE name_key = ANY(%(n)s)",
                   {"n": art["names"]})
    return stats


# --------------------------------------------------------------------------
# The pass, and the consumer that runs it
# --------------------------------------------------------------------------

_wake_event = threading.Event()
_scope_lock = threading.Lock()
_scope: Row = {"full": False, "names": set(), "artists": set()}
_started = False


def wake(names: Optional[Iterable[str]] = None, artist_ids: Optional[Iterable[str]] = None) -> None:
    """Ask for a pass (any thread). With no scope, stage A covers every waiting
    scrobble — something happened that can bring any track (a scan, a sync, a
    dump, slices, the phantom layer switched on); a scoped wake names what
    just changed: names that got scrobbles, artists a mint just shelved."""
    with _scope_lock:
        if names is None and artist_ids is None:
            _scope["full"] = True
        _scope["names"].update(names or ())
        _scope["artists"].update(str(a) for a in (artist_ids or ()))
    _wake_event.set()


def start() -> None:
    """Start the consumer (backend startup); a waiting room the last run left
    is worked through at once."""
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_consume, daemon=True, name="scrobble-canon").start()
    if db_query_one("SELECT 1 AS x FROM pending_scrobbles LIMIT 1"):
        wake()


def _consume() -> None:
    while True:
        _wake_event.wait()
        _wake_event.clear()
        with _scope_lock:
            scope = {"full": _scope["full"], "names": sorted(_scope["names"]),
                     "artists": sorted(_scope["artists"])}
            _scope.update(full=False, names=set(), artists=set())
        try:
            drain = run_pass(scope)
        except Exception as e:
            logger.error("scrobble canon pass failed: %s", e, exc_info=True)
            continue
        if drain:
            wake(names=())


def run_pass(scope: Row) -> bool:
    """One pass: A over the scope, then B, C and D where MusicBrainz data
    allows, then what the new listens wake. Returns whether a stage hit its cap
    with work left (the consumer runs another pass)."""
    import mb_backend
    from canon import algo_canon
    from discography import _mb_load_in_progress
    from routers.settings import _read as read_setting
    stats: Row = {"echoes": drop_echoes(),
                  "bound_known": bind_known(None if scope["full"] else scope["names"])}
    drain = False
    if mb_backend.LOCAL_DUMP and not _mb_load_in_progress():
        create = bool(read_setting("discovery.phantom_layer"))
        with algo_canon() as ok:
            resolve = resolve_names(create=create) if ok else {"artists": [], "drain": False}
        mint = mint_resolved() if create else {"statuses": Counter(), "artists": [], "drain": False}
        catalogue = bind_catalogue(set(scope["artists"]) | set(resolve["artists"])
                                   | set(mint["artists"]))
        drain = resolve["drain"] or mint["drain"]
        stats.update(resolved=resolve.get("resolved", 0), minted=len(mint["artists"]),
                     mint=dict(mint["statuses"]), catalogue=catalogue)
        if stats["bound_known"] + catalogue["bound"]:
            _after_binding(minted=bool(mint["artists"]))
    elif stats["bound_known"]:
        _after_binding(minted=False)
    db_execute("DELETE FROM pending_scrobble_artists n WHERE NOT EXISTS "
               "(SELECT 1 FROM pending_scrobbles p WHERE p.name_key = n.name_key)")
    import lastfm_history
    lastfm_history.resume_below_budget()
    logger.info("scrobble canon pass: %s", stats)
    return drain


def _after_binding(minted: bool) -> None:
    """Listens landed: the engaged set grew. Wake what feeds on it — the
    background loop (bios, similars), the DB steps (credit heads, shelves),
    the P2P walk (analysis for the new canonical tracks), the LB slices — and
    the screens."""
    import background_enrichment
    from routers.settings import notify_library_subscribers
    background_enrichment.wake("lastfm import")
    background_enrichment.wake_db_steps("lastfm import")
    notify_library_subscribers()
    db_execute("NOTIFY sautium_sync_request")
    db_execute("NOTIFY sautium_lb_pending")
    if minted:
        import notary
        notary.wake("scrobble canon", full=True)
        db_execute("NOTIFY sautium_enrich_done")


# --------------------------------------------------------------------------
# Measuring before trusting: read-only report and evaluation
# --------------------------------------------------------------------------

def evaluate(limit: int = 300, titles: Optional[int] = None) -> Row:
    """Stage B's decision against answers already known: owned artists whose
    MBID the owned canon verified by content. Each plays a scrobbled name —
    its owned titles and albums as the evidence, no local anchoring to lean
    on — and the decision is compared with the verified gid. `titles` keeps
    only that many titles and no albums: the long tail's sparse evidence.
    Read-only."""
    stats: Row = Counter()
    wrong: List[str] = []
    for a in db_query("""
        SELECT a.id::text AS id, a.name, array_agg(am.mbid::text) AS gids
          FROM artists a JOIN artist_mbids am ON am.artist_id = a.id
         WHERE am.confidence = 'overlap_verified'
         GROUP BY a.id, a.name
         ORDER BY md5(a.id::text)
         LIMIT %(lim)s""", {"lim": limit}):
        rows = [{"name_key": "x", "artist": a["name"], "title": r["title"], "album": r["album"],
                 "artist_mbid": None} for r in db_query("""
            SELECT DISTINCT t.title, al.title AS album
              FROM track_artists ta
              JOIN tracks t ON t.id = ta.track_id
              JOIN media_files mf ON mf.track_id = t.id
              JOIN album_variants av ON av.id = mf.album_variant_id
              JOIN albums al ON al.id = av.album_id
             WHERE ta.artist_id = %(a)s::uuid AND ta.role = 'primary'""", {"a": a["id"]})]
        if not rows:
            continue
        if titles is not None:
            rows = [dict(r, album=None) for r in
                    sorted(rows, key=lambda r: hashlib.md5(r["title"].encode()).hexdigest())[:titles]]
        e = _evidence_of(rows)["x"]
        gids = _candidates(a["name"])
        gid = decide(e, gids, set()) if gids else None
        if gid is None:
            stats["undecided"] += 1
        elif gid in a["gids"]:
            stats["correct"] += 1
        else:
            stats["wrong"] += 1
            wrong.append(f"{a['name']} → {gid}")
    decided = stats["correct"] + stats["wrong"]
    stats["precision"] = round(stats["correct"] / decided, 4) if decided else None
    return {**stats, "wrong_examples": wrong[:15]}


def report(pages: int = 25) -> Row:
    """What the stages would do with the owner's newest scrobbles, read into
    memory — nothing is written. Buckets: bound to a track this node has
    (A), names resolved or not (B), and of the resolved names' scrobbles,
    those whose title names a recording with a local track, a recording
    nothing minted (a single, a compilation, a live record — C decides), or
    no recording at all."""
    from datetime import datetime, timezone
    import lastfm_history
    from config import settings
    from lastfm import LastFmService
    svc = LastFmService(session_key=settings.lastfm_session_key)
    rows, before = [], datetime.now(timezone.utc)
    for _ in range(pages):
        page = svc.recent_tracks_page(settings.lastfm_username, before, None)
        rows += [dict(i, name_key=lastfm_history.name_key(i["artist"])) for i in page["items"]]
        if len(page["items"]) < lastfm_history.PAGE_SIZE:
            break
        before = min(i["played_at"] for i in page["items"])
    stats: Row = Counter(scrobbles=len(rows))

    def key(r):
        return r["played_at"], r["artist"], r["title"]

    with transaction(RealDictCursor) as cur:
        bound = {key(b) for b in _bind_known(cur, rows)}
    stats["A_bound_known"] = len(bound)
    waiting = [r for r in rows if key(r) not in bound]
    evidence = _evidence_of(waiting)
    stats["names_waiting"] = len(evidence)
    for nk, e in evidence.items():
        these = [r for r in waiting if r["name_key"] == nk]
        gids = [] if _unplaceable(nk) else _candidates(e["credits"].most_common(1)[0][0])
        gid = decide(e, gids, set()) if gids else None
        if not gids:
            stats["B_not_in_mb_names"] += 1
            stats["B_not_in_mb_scrobbles"] += len(these)
            continue
        if gid is None:
            stats["B_undecided_names"] += 1
            stats["B_undecided_scrobbles"] += len(these)
            continue
        stats["B_resolved_names"] += 1
        forms = sorted({f for r in these for f in _lower_forms(r["title"])})
        hints = sorted({r["track_mbid"] for r in these if r["track_mbid"]})
        names = _catalogue([gid], forms, hints)
        with transaction(RealDictCursor) as cur:
            local = _local_tracks(cur, sorted({n["recording"] for n in names}))
            artist = db_query_one("SELECT name FROM mb_artist WHERE gid = %(g)s::uuid", {"g": gid})
            fallback = {n["recording"]: str(track_uuid(n["n"], artist["name"])) for n in names}
            lengths = _track_lengths(cur, [t for ts in local.values() for t in ts]
                                     + list(fallback.values()))
        for r in these:
            track_id, named = _pick(r, names, local, fallback, lengths)
            stats["D_bound_now" if track_id else
                  "D_recording_not_minted" if named else "D_no_recording"] += 1
    return dict(stats)


if __name__ == "__main__":
    import argparse
    import pprint
    parser = argparse.ArgumentParser(description="Imported-scrobble canon: read-only checks")
    parser.add_argument("--eval", action="store_true", help="stage B against verified owned artists")
    parser.add_argument("--report", action="store_true", help="the stages over the newest scrobbles")
    parser.add_argument("--limit", type=int, default=300)
    parser.add_argument("--pages", type=int, default=25)
    parser.add_argument("--titles", type=int, default=None, help="eval: sparse evidence")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    if args.eval:
        pprint.pprint(evaluate(args.limit, args.titles))
    if args.report:
        import lastfm_auth
        lastfm_auth.load_from_db()
        pprint.pprint(report(args.pages))
