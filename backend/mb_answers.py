"""MusicBrainz's answers, kept by MBID, and the queue that asks for them.

On a node with catalogue slices and no MusicBrainz database (`required()`),
any node's signed slice may nominate what the canon merges, renames or
mints. backend/canon/confirm.py re-makes each such decision on MusicBrainz's
own answers; this module is where those answers come from and live:

- `mbws_*` tables hold them, normalized by MBID (001_initial.sql). An answer
  is reused for FRESH; `unknown` is a definitive 404 only, and a merged MBID
  is `forwarded` to the entity it now leads to, whose rows are stored under
  its own MBID (and answered as found too).
- `mbws_queue` is what is still to ask, with a priority and, when a kept
  slice raised it, the slice's author; `mbws_waiters` names the subjects
  (an owned artist, a discography, an imported-scrobble name…) waiting on
  each row. A decision that lacks an answer collects it in an `Asks`, flushes
  it here and waits; nothing it would have done happens meanwhile.
- One consumer thread asks MusicBrainz (backend/musicbrainz.py, one pace per
  process): the lowest priority value first — the person's own act, the
  owned library, imported listening, discovery, the checks before a slice is
  passed on — and within a priority the authors take turns, so one author's
  flood cannot starve the rest. A NOTIFY on `sautium_mbws` wakes it; nothing
  polls. When every row a subject waits on is answered, the subject's waker
  (`on_ready`) is called with its keys.
- While MusicBrainz cannot be reached the queue holds and the decisions
  wait; imports, shown data, sync and playback go on. An item whose own
  request fails (backend/musicbrainz.Failed) — or whose answer cannot be
  kept — backs off on its own (`attempts`, `not_before`: a minute, then four
  times longer each time, at most a day) and the rest of the queue goes on;
  its subjects keep waiting. A browse of an artist's groups is asked a page a
  step, so a person's own act is asked between two pages of a long one, and a
  failure resumes it where it stopped.

A node holding the MusicBrainz database never asks (`required()` is false):
its canon reads MetaBrainz's own data, and it queues nothing.
"""

import logging
import random
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Iterable, List, NamedTuple, Optional, Set, Tuple

from psycopg2.extras import execute_values

from db_pool import Listener, db_execute, db_query, db_query_one, transaction
from desktop.p2p.mb_asks import PRIORITY_PERSON   # one value for both runtimes

logger = logging.getLogger(__name__)

FRESH = timedelta(days=30)

PRIORITY_OWNED = 2
PRIORITY_LISTENING = 3
PRIORITY_DISCOVERY = 4
PRIORITY_CHECK = 5

KINDS = ("artist", "release_group", "release", "recording", "artist_release_groups")
ITEM_BACKOFF_S = 60
ITEM_BACKOFF_CAP_S = 24 * 3600
# Different items failing in a row are MusicBrainz's trouble, not theirs: the
# queue pauses, from QUEUE_PAUSE_S doubling to QUEUE_PAUSE_CAP_S.
FAILED_IN_A_ROW = 3
QUEUE_PAUSE_S = 30.0
QUEUE_PAUSE_CAP_S = 1800.0

Item = Tuple[str, str]          # (kind, mbid)


class Answer(NamedTuple):
    status: str                 # found | forwarded | unknown
    mbid: str                   # the MBID asked
    target: Optional[str]       # the entity answered for it; None when unknown
    answered_at: datetime

    @property
    def fresh(self) -> bool:
        return self.answered_at > datetime.now(timezone.utc) - FRESH

    @property
    def known(self) -> bool:
        return self.status != "unknown"


def required() -> bool:
    """This node decides on MusicBrainz's answers: its `mb_*` rows came from
    slices (mb_backend.LOCAL_DUMP) and it holds no completed full dump (the
    loader's in-database marker) — desktop/p2p/mb_asks.node_asks, with the
    catalogue's presence as mb_backend keeps it."""
    import mb_backend
    from desktop.p2p import mb_asks
    if not mb_backend.LOCAL_DUMP:
        return False
    return db_query_one(mb_asks.DUMP_HELD_SQL) is None


# --------------------------------------------------------------------------
# Reading what is kept
# --------------------------------------------------------------------------

def answer(kind: str, mbid: str) -> Optional[Answer]:
    row = db_query_one("""SELECT status::text AS status, target::text AS target, answered_at
                            FROM mbws_answers WHERE kind = %(k)s AND mbid = %(m)s::uuid""",
                       {"k": kind, "m": str(mbid)})
    return Answer(row["status"], str(mbid), row["target"], row["answered_at"]) if row else None


def _credits(owner_kind: str, owners: List[str]) -> Dict[str, List[dict]]:
    out: Dict[str, List[dict]] = defaultdict(list)
    if not owners:
        return out
    for r in db_query("""
        SELECT owner::text AS owner, position, artist::text AS artist, artist_name,
               credited_as, join_phrase
          FROM mbws_credits WHERE owner_kind = %(k)s AND owner = ANY(%(o)s::uuid[])
         ORDER BY owner, position""", {"k": owner_kind, "o": list(owners)}):
        out[r["owner"]].append({"artist": r["artist"], "name": r["artist_name"],
                                "credited_as": r["credited_as"], "join": r["join_phrase"]})
    return out


def artist(target: str) -> Optional[dict]:
    """The artist MusicBrainz answered under its own MBID: name, sort name,
    disambiguation, type, area, aliases, genres, its "member of band" links
    and its Bandcamp pages."""
    row = db_query_one("""SELECT mbid::text AS mbid, name, sort_name, disambiguation, type, area
                            FROM mbws_artists WHERE mbid = %(m)s::uuid""", {"m": target})
    if row is None:
        return None
    p = {"m": target}
    row["aliases"] = db_query("""SELECT name, sort_name, locale FROM mbws_artist_aliases
                                  WHERE artist = %(m)s::uuid ORDER BY ord""", p)
    row["genres"] = {r["genre"]: r["count"] for r in db_query(
        "SELECT genre, count FROM mbws_artist_genres WHERE artist = %(m)s::uuid", p)}
    # (member, group), as _put_artist keeps them.
    row["members"] = [(r["m"], r["g"]) for r in db_query(
        """SELECT group_mbid::text AS g, member_mbid::text AS m FROM mbws_artist_members
            WHERE artist = %(m)s::uuid""", p)]
    row["urls"] = [r["url"] for r in db_query(
        "SELECT url FROM mbws_artist_urls WHERE artist = %(m)s::uuid ORDER BY url", p)]
    return row


def release_group(target: str) -> Optional[dict]:
    row = db_query_one("""SELECT mbid::text AS mbid, title, primary_type, first_release_date
                            FROM mbws_release_groups WHERE mbid = %(m)s::uuid""", {"m": target})
    if row is None:
        return None
    row["secondary_types"] = [r["t"] for r in db_query(
        """SELECT secondary_type AS t FROM mbws_release_group_types
            WHERE release_group = %(m)s::uuid ORDER BY 1""", {"m": target})]
    row["credit"] = _credits("release_group", [target]).get(target, [])
    return row


def release(target: str) -> Optional[dict]:
    """A release as MusicBrainz answered it: title, status, barcode, date, its
    group (with the group's credit when the answer carried it), its credit,
    its Bandcamp pages and its tracklist — each track with its title, length,
    recording and credit."""
    row = db_query_one("""SELECT mbid::text AS mbid, title, status, barcode,
                                 release_group::text AS release_group, date
                            FROM mbws_releases WHERE mbid = %(m)s::uuid""", {"m": target})
    if row is None:
        return None
    row["credit"] = _credits("release", [target]).get(target, [])
    row["urls"] = [r["url"] for r in db_query(
        "SELECT url FROM mbws_release_urls WHERE release = %(m)s::uuid ORDER BY url", {"m": target})]
    tracks = db_query("""SELECT disc, position, mbid::text AS mbid, title, length_ms,
                                recording::text AS recording, media_format
                           FROM mbws_tracks WHERE release = %(m)s::uuid
                          ORDER BY disc, position""", {"m": target})
    credits = _credits("track", [t["mbid"] for t in tracks if t["mbid"]])
    for t in tracks:
        t["credit"] = credits.get(t["mbid"], [])
    row["tracks"] = tracks
    row["group"] = release_group(row["release_group"]) if row["release_group"] else None
    return row


def recording(target: str) -> Optional[dict]:
    row = db_query_one("""SELECT mbid::text AS mbid, title, length_ms FROM mbws_recordings
                           WHERE mbid = %(m)s::uuid""", {"m": target})
    if row is None:
        return None
    row["credit"] = _credits("recording", [target]).get(target, [])
    return row


def artist_release_groups(target: str) -> List[dict]:
    """Every group MusicBrainz credits to the artist (its browse), each with
    title, types, first release date and credit."""
    groups = db_query("""
        SELECT g.mbid::text AS mbid, g.title, g.primary_type, g.first_release_date
          FROM mbws_artist_release_groups a JOIN mbws_release_groups g ON g.mbid = a.release_group
         WHERE a.artist = %(m)s::uuid ORDER BY g.mbid""", {"m": target})
    ids = [g["mbid"] for g in groups]
    types: Dict[str, List[str]] = defaultdict(list)
    for r in db_query("""SELECT release_group::text AS g, secondary_type AS t
                           FROM mbws_release_group_types WHERE release_group = ANY(%(i)s::uuid[])
                          ORDER BY 1, 2""", {"i": ids}) if ids else []:
        types[r["g"]].append(r["t"])
    credits = _credits("release_group", ids)
    for g in groups:
        g["secondary_types"] = types.get(g["mbid"], [])
        g["credit"] = credits.get(g["mbid"], [])
    return groups


# --------------------------------------------------------------------------
# What a decision still needs
# --------------------------------------------------------------------------

class Asks:
    """The answers one decision needs. `need(kind, mbid)` returns the fresh
    answer, or notes the MBID as missing and returns None; `flush()` queues
    what is missing for the subject (`subject`, `key`) to wait on."""

    def __init__(self, subject: Optional[str], key: Optional[str],
                 priority: int, author: Optional[str] = None):
        self.subject, self.key = subject, (str(key) if key is not None else None)
        self.priority, self.author = priority, author
        self.missing: Set[Item] = set()

    def need(self, kind: str, mbid: str) -> Optional[Answer]:
        a = answer(kind, mbid)
        if a is None or not a.fresh:
            self.missing.add((kind, str(mbid)))
            return None
        return a

    @property
    def waiting(self) -> bool:
        return bool(self.missing)

    def flush(self) -> None:
        if self.missing:
            enqueue(sorted(self.missing), self.priority, self.subject, self.key, self.author)
            self.missing = set()


def enqueue(items: Iterable[Item], priority: int, subject: Optional[str] = None,
            key: Optional[str] = None, author: Optional[str] = None) -> None:
    """Queue `items` (kind, mbid) at `priority` — an item already queued keeps
    the more urgent of the two — with the subject that waits on them, and wake
    the consumer. The one SQL both runtimes queue by (desktop/p2p/mb_asks.py).
    A node that does not ask queues nothing: its consumer would only drop it."""
    from desktop.p2p import mb_asks
    if not required():
        return
    with transaction() as cur:
        mb_asks.enqueue(cur.connection, items, priority, subject, key, author)
    _worker.wake()


def waiting(subject: str, key: str) -> bool:
    return db_query_one("""SELECT 1 AS x FROM mbws_waiters
                            WHERE subject = %(s)s AND subject_key = %(k)s LIMIT 1""",
                        {"s": subject, "k": str(key)}) is not None


WAITING_SQL = """EXISTS (SELECT 1 FROM mbws_waiters w
                          WHERE w.subject = '{subject}' AND w.subject_key = {key})"""


# --------------------------------------------------------------------------
# Storing what MusicBrainz answered
# --------------------------------------------------------------------------

def _credit_rows(owner_kind: str, owner: str, credit: Optional[list]) -> list:
    rows = []
    for i, c in enumerate(credit or []):
        a = c.get("artist") or {}
        if not a.get("id"):
            continue
        rows.append((owner_kind, owner, i, a["id"], a.get("name") or c.get("name") or "",
                     c.get("name") or a.get("name") or "", c.get("joinphrase") or ""))
    return rows


def _put_credits(cur, rows: list) -> None:
    if rows:
        execute_values(cur, """INSERT INTO mbws_credits (owner_kind, owner, position, artist,
                                                         artist_name, credited_as, join_phrase)
                               VALUES %s ON CONFLICT DO NOTHING""", rows,
                       template="(%s::mbws_credit_owner, %s::uuid, %s, %s::uuid, %s, %s, %s)")


def _clear_credits(cur, owner_kind: str, owners: List[str]) -> None:
    if owners:
        cur.execute("DELETE FROM mbws_credits WHERE owner_kind = %s AND owner = ANY(%s::uuid[])",
                    (owner_kind, owners))


def _mark(cur, kind: str, mbid: str, status: str, target: Optional[str]) -> None:
    cur.execute("""
        INSERT INTO mbws_answers (kind, mbid, status, target, answered_at)
        VALUES (%s, %s::uuid, %s, %s::uuid, now())
        ON CONFLICT (kind, mbid) DO UPDATE
           SET status = EXCLUDED.status, target = EXCLUDED.target, answered_at = now()""",
                (kind, mbid, status, target))


def _put_artist(cur, d: dict) -> str:
    from desktop.p2p.mb_slice_queries import bandcamp_page
    mbid = d["id"]
    cur.execute("DELETE FROM mbws_artists WHERE mbid = %s::uuid", (mbid,))
    cur.execute("""INSERT INTO mbws_artists (mbid, name, sort_name, disambiguation, type, area)
                   VALUES (%s::uuid, %s, %s, %s, %s, %s)""",
                (mbid, d.get("name") or "", d.get("sort-name"), d.get("disambiguation") or "",
                 d.get("type"), (d.get("area") or {}).get("name")))
    aliases = [(mbid, i, a.get("name"), a.get("sort-name"), a.get("locale"))
               for i, a in enumerate(d.get("aliases") or []) if a.get("name")]
    if aliases:
        execute_values(cur, """INSERT INTO mbws_artist_aliases (artist, ord, name, sort_name, locale)
                               VALUES %s""", aliases)
    genres = {}
    for g in d.get("genres") or []:
        if g.get("name"):
            genres[g["name"].lower()] = int(g.get("count") or 0)
    if genres:
        execute_values(cur, "INSERT INTO mbws_artist_genres (artist, genre, count) VALUES %s",
                       [(mbid, g, n) for g, n in genres.items()])
    members, urls = set(), set()
    for rel in d.get("relations") or []:
        other = (rel.get("artist") or {}).get("id")
        if rel.get("type") == "member of band" and other:
            # (member, group): "forward" is this artist in the other, "backward"
            # the other in this one
            members.add((other, mbid) if rel.get("direction") == "backward" else (mbid, other))
        resource = (rel.get("url") or {}).get("resource")
        if resource and bandcamp_page(resource):
            urls.add(resource)
    if members:
        execute_values(cur, """INSERT INTO mbws_artist_members (artist, group_mbid, member_mbid)
                               VALUES %s ON CONFLICT DO NOTHING""",
                       [(mbid, group, member) for member, group in sorted(members)])
    if urls:
        execute_values(cur, "INSERT INTO mbws_artist_urls (artist, url) VALUES %s",
                       [(mbid, u) for u in sorted(urls)])
    return mbid


def _put_release_group(cur, d: dict, with_credit: bool = True) -> str:
    mbid = d["id"]
    cur.execute("""
        INSERT INTO mbws_release_groups (mbid, title, primary_type, first_release_date)
        VALUES (%s::uuid, %s, %s, %s)
        ON CONFLICT (mbid) DO UPDATE SET title = EXCLUDED.title,
               primary_type = EXCLUDED.primary_type,
               first_release_date = EXCLUDED.first_release_date""",
                (mbid, d.get("title") or "", d.get("primary-type"), d.get("first-release-date")))
    cur.execute("DELETE FROM mbws_release_group_types WHERE release_group = %s::uuid", (mbid,))
    types = sorted({t for t in d.get("secondary-types") or [] if t})
    if types:
        execute_values(cur, "INSERT INTO mbws_release_group_types (release_group, secondary_type) VALUES %s",
                       [(mbid, t) for t in types])
    if with_credit:
        _clear_credits(cur, "release_group", [mbid])
        _put_credits(cur, _credit_rows("release_group", mbid, d.get("artist-credit")))
    return mbid


def _put_recording(cur, d: dict) -> str:
    mbid = d["id"]
    cur.execute("""
        INSERT INTO mbws_recordings (mbid, title, length_ms) VALUES (%s::uuid, %s, %s)
        ON CONFLICT (mbid) DO UPDATE SET title = EXCLUDED.title, length_ms = EXCLUDED.length_ms""",
                (mbid, d.get("title") or "", d.get("length")))
    if "artist-credit" in d:
        _clear_credits(cur, "recording", [mbid])
        _put_credits(cur, _credit_rows("recording", mbid, d.get("artist-credit")))
    return mbid


def _put_release(cur, d: dict) -> List[Item]:
    """The release, its group, its tracks and the recordings they carry; returns
    the items answered by it beside the release (its group, when the answer
    carried the group's credit, and its recordings)."""
    from desktop.p2p.mb_slice_queries import bandcamp_page
    mbid = d["id"]
    cur.execute("SELECT mbid::text FROM mbws_tracks WHERE release = %s::uuid AND mbid IS NOT NULL",
                (mbid,))
    _clear_credits(cur, "track", [r[0] for r in cur.fetchall()])
    _clear_credits(cur, "release", [mbid])
    cur.execute("DELETE FROM mbws_releases WHERE mbid = %s::uuid", (mbid,))
    group = d.get("release-group") or {}
    also: List[Item] = []
    if group.get("id"):
        has_credit = "artist-credit" in group
        _put_release_group(cur, group, with_credit=has_credit)
        if has_credit:
            also.append(("release_group", group["id"]))
    cur.execute("""INSERT INTO mbws_releases (mbid, title, status, barcode, release_group, date)
                   VALUES (%s::uuid, %s, %s, %s, %s::uuid, %s)""",
                (mbid, d.get("title") or "", d.get("status"), d.get("barcode") or None,
                 group.get("id"), d.get("date")))
    _put_credits(cur, _credit_rows("release", mbid, d.get("artist-credit")))
    urls = sorted({(r.get("url") or {}).get("resource") for r in d.get("relations") or []}
                  - {None})
    urls = [u for u in urls if bandcamp_page(u)]
    if urls:
        execute_values(cur, "INSERT INTO mbws_release_urls (release, url) VALUES %s",
                       [(mbid, u) for u in urls])
    tracks, credits = [], []
    for medium in d.get("media") or []:
        disc = int(medium.get("position") or 1)
        fmt = medium.get("format")
        listed = ([medium["pregap"]] if medium.get("pregap") else []) \
            + list(medium.get("tracks") or []) + list(medium.get("data-tracks") or [])
        for t in listed:
            rec = t.get("recording") or {}
            if rec.get("id"):
                _put_recording(cur, rec)
                if "artist-credit" in rec:
                    also.append(("recording", rec["id"]))
            position = int(t.get("position") if t.get("position") is not None else 0)
            tracks.append((mbid, disc, position, t.get("id"), t.get("title") or rec.get("title") or "",
                           t.get("length") if t.get("length") is not None else rec.get("length"),
                           rec.get("id"), fmt))
            if t.get("id"):
                credits.extend(_credit_rows("track", t["id"],
                                            t.get("artist-credit") or rec.get("artist-credit")))
    if tracks:
        execute_values(cur, """INSERT INTO mbws_tracks (release, disc, position, mbid, title,
                                                        length_ms, recording, media_format)
                               VALUES %s ON CONFLICT DO NOTHING""", tracks,
                       template="(%s::uuid, %s, %s, %s::uuid, %s, %s, %s::uuid, %s)")
    _put_credits(cur, credits)
    return also


def store(kind: str, mbid: str, data: Optional[dict]) -> List[Item]:
    """Keep MusicBrainz's answer for (kind, mbid): `data` is its JSON, None a
    definitive 404. Returns the other items the answer settled — the entity a
    merged MBID leads to, a release's group and recordings."""
    if data is None:
        with transaction() as cur:
            _mark(cur, kind, mbid, "unknown", None)
        if _unknown_waker is not None:
            _unknown_waker(kind, mbid)
        return []
    with transaction() as cur:
        target = data["id"]
        also: List[Item] = []
        if kind == "artist":
            _put_artist(cur, data)
        elif kind == "release_group":
            _put_release_group(cur, data)
        elif kind == "release":
            also = _put_release(cur, data)
        elif kind == "recording":
            _put_recording(cur, data)
        _mark(cur, kind, mbid, "found" if target == mbid else "forwarded", target)
        if target != mbid:
            also.append((kind, target))
        for k, m in also:
            _mark(cur, k, m, "found", m)
    return also


def _browse_answered(cur, mbid: str, target: Optional[str]) -> List[Item]:
    if target is None:
        _mark(cur, "artist_release_groups", mbid, "unknown", None)
        return [("artist_release_groups", mbid)]
    _mark(cur, "artist_release_groups", mbid, "found" if target == mbid else "forwarded", target)
    if target == mbid:
        return [("artist_release_groups", mbid)]
    _mark(cur, "artist_release_groups", target, "found", target)
    return [("artist_release_groups", mbid), ("artist_release_groups", target)]


def store_browse_page(mbid: str, target: str, started: datetime, offset: int,
                      groups: List[dict], count: int) -> List[Item]:
    """Keep one page of the browse of the artist answered under `target`
    (for `mbid`), begun at `started`: its groups, each answered too. The last
    page answers the browse and removes the groups an earlier browse listed
    and this one did not. Returns the items settled."""
    with transaction() as cur:
        settled: List[Item] = []
        for g in groups:
            if g.get("id"):
                _put_release_group(cur, g)
                _mark(cur, "release_group", g["id"], "found", g["id"])
                settled.append(("release_group", g["id"]))
        if settled:
            execute_values(cur, """
                INSERT INTO mbws_artist_release_groups (artist, release_group, browsed_at)
                VALUES %s ON CONFLICT (artist, release_group)
                DO UPDATE SET browsed_at = EXCLUDED.browsed_at""",
                           [(target, m, started) for _k, m in settled])
        following = offset + len(groups)
        if groups and following < count:
            cur.execute("""UPDATE mbws_queue SET browse_offset = %s
                            WHERE kind = 'artist_release_groups' AND mbid = %s::uuid""",
                        (following, mbid))
            return settled
        cur.execute("""DELETE FROM mbws_artist_release_groups
                        WHERE artist = %s::uuid AND browsed_at < %s""", (target, started))
        return settled + _browse_answered(cur, mbid, target)


# --------------------------------------------------------------------------
# The consumer
# --------------------------------------------------------------------------

_wakers: Dict[str, Callable[[List[str]], None]] = {}
_answered = threading.Condition()


def on_ready(subject: str, fn: Callable[[List[str]], None]) -> None:
    """`fn(keys)` runs on the consumer's thread when every row the subjects
    `keys` of `subject` waited on is answered."""
    _wakers[subject] = fn


_unknown_waker: Optional[Callable[[str, str], None]] = None


def on_unknown(fn: Callable[[str, str], None]) -> None:
    """`fn(kind, mbid)` runs on the consumer's thread once an answer that
    MusicBrainz does not know (kind, mbid) is kept, after its commit — the
    kept slices about it are checked again (slice_checks.uncheck_subject),
    on a thread that can wait for their rows; `fn` itself must not."""
    global _unknown_waker
    _unknown_waker = fn


_READY = "(q.not_before IS NULL OR q.not_before <= now())"
_NEXT_SQL = f"""
    SELECT kind::text AS kind, mbid::text AS mbid, author FROM (
        SELECT q.*, row_number() OVER (PARTITION BY coalesce(q.author, '')
                                       ORDER BY q.queued_at, q.kind, q.mbid) AS turn
          FROM mbws_queue q
         WHERE {_READY}
           AND q.priority = (SELECT min(q.priority) FROM mbws_queue q WHERE {_READY})
    ) x
    ORDER BY turn, (coalesce(author, '') <= %(last)s), coalesce(author, ''), queued_at
    LIMIT 1"""
# Over every backed-off item: one that fell due since the consumer looked is
# taken at once (zero), never waited past.
_NEXT_DUE_SQL = """SELECT greatest(0, extract(epoch FROM min(not_before) - now())) AS seconds
                     FROM mbws_queue WHERE not_before IS NOT NULL"""


def _lookup(kind: str, mbid: str) -> List[Item]:
    import musicbrainz
    try:
        data = musicbrainz.lookup(kind, mbid)
    except musicbrainz.NotFound:
        data = None
    return [(kind, mbid)] + store(kind, mbid, data)


def _browse_step(mbid: str) -> List[Item]:
    """One step of the browse asked under `mbid`: the artist's own answer
    first, then one page at a time from where the last step stopped. Until
    its last page the browse is not answered, so nothing reads the rows it has
    put down — every reader asks for a fresh browse answer first."""
    import musicbrainz
    a = answer("artist", mbid)
    if a is None or not a.fresh:
        return _lookup("artist", mbid)
    if not a.known:
        with transaction() as cur:
            return _browse_answered(cur, mbid, None)
    if a.target != mbid:
        whole = answer("artist_release_groups", a.target)
        if whole is not None and whole.fresh:
            with transaction() as cur:
                return _browse_answered(cur, mbid, a.target)
    with transaction() as cur:
        # A browse begun for another artist (the MBID now leads elsewhere)
        # starts over.
        cur.execute("""
            UPDATE mbws_queue
               SET browse_target = %(t)s::uuid, browse_offset = 0, browse_started = now()
             WHERE kind = 'artist_release_groups' AND mbid = %(m)s::uuid
               AND browse_target IS DISTINCT FROM %(t)s::uuid""", {"t": a.target, "m": mbid})
        cur.execute("""SELECT browse_offset, browse_started FROM mbws_queue
                        WHERE kind = 'artist_release_groups' AND mbid = %s::uuid""", (mbid,))
        row = cur.fetchone()
    if row is None:
        return []
    offset, started = row
    try:
        groups, count = musicbrainz.browse_release_groups_page(a.target, offset)
    except musicbrainz.NotFound:
        with transaction() as cur:
            return _browse_answered(cur, mbid, None)
    return store_browse_page(mbid, a.target, started, offset, groups, count)


def _ask(kind: str, mbid: str) -> List[Item]:
    """One step on a queued item: the items it answered — the item itself once
    it is complete, and the others its answer settled. Raises
    musicbrainz.Unavailable (RateLimited included) or musicbrainz.Failed when
    MusicBrainz did not answer."""
    a = answer(kind, mbid)
    if a is not None and a.fresh:
        # Settled meanwhile by another item's answer (a release's recordings,
        # a browse's groups, a merged MBID's target).
        return [(kind, mbid)]
    if kind == "artist_release_groups":
        return _browse_step(mbid)
    return _lookup(kind, mbid)


def _back_off(kind: str, mbid: str, reason: str) -> None:
    row = db_execute("""
        UPDATE mbws_queue
           SET attempts = attempts + 1,
               not_before = now() + make_interval(
                   secs => least(%(cap)s, %(base)s * power(4, least(attempts, 12))) * (0.75 + random() * 0.5))
         WHERE kind = %(k)s AND mbid = %(m)s::uuid
        RETURNING attempts, not_before""",
                     {"cap": ITEM_BACKOFF_CAP_S, "base": ITEM_BACKOFF_S, "k": kind, "m": mbid})
    if row is not None:
        logger.warning("MusicBrainz: %s %s failed (%s), attempt %d — asked again after %s UTC",
                       kind, mbid, reason, row["attempts"],
                       row["not_before"].strftime("%Y-%m-%d %H:%M"))


def _step(kind: str, mbid: str) -> Tuple[str, int]:
    """One consumer step on the queued item, and how many queued items it
    settled: 'answered', or 'progress' (a browse with pages to go); 'failed'
    (MusicBrainz's request for this entity failed) or 'unkept' (its answer
    could not be kept) — either way the item backs off on its own and its
    subjects keep waiting. Raises musicbrainz.Unavailable (RateLimited
    included): MusicBrainz did not answer, and nothing about the item changes."""
    import musicbrainz
    try:
        answered = _ask(kind, mbid)
    except musicbrainz.Unavailable:
        raise
    except musicbrainz.Failed as e:
        _back_off(kind, mbid, str(e))
        return "failed", 0
    except Exception:
        logger.exception("MusicBrainz: the answer for %s %s could not be kept", kind, mbid)
        _back_off(kind, mbid, "its answer could not be kept")
        return "unkept", 0
    settled = sum(_answered_item(k, m) for k, m in _queued(answered))
    return ("answered" if (kind, mbid) in answered else "progress"), settled


def _queued(items: List[Item]) -> List[Item]:
    """Those of `items` the queue holds. One queued after this read is taken
    by the consumer in its turn and, answered already, leaves at once."""
    if not items:
        return []
    kinds, mbids = zip(*dict.fromkeys(items))
    return [(r["kind"], r["mbid"]) for r in db_query("""
        SELECT q.kind::text AS kind, q.mbid::text AS mbid
          FROM mbws_queue q
          JOIN unnest(%(k)s::text[], %(m)s::uuid[]) AS i(kind, mbid)
            ON q.kind::text = i.kind AND q.mbid = i.mbid""", {"k": list(kinds), "m": list(mbids)})]


def _answered_item(kind: str, mbid: str, skip: Tuple[str, ...] = ()) -> bool:
    """The item leaves the queue, if it is queued; the subjects with nothing
    left to wait on are woken, but those of `skip`. True when it was queued."""
    ready: Dict[str, List[str]] = defaultdict(list)
    with transaction() as cur:
        # The row is locked before its waiters are read: a subject queuing on
        # it at this moment is either read here or waits, and queues it anew.
        cur.execute("SELECT 1 FROM mbws_queue WHERE kind = %s AND mbid = %s::uuid FOR UPDATE",
                    (kind, mbid))
        queued = cur.fetchone() is not None
        if queued:
            cur.execute("""DELETE FROM mbws_waiters WHERE kind = %s AND mbid = %s::uuid
                           RETURNING subject::text, subject_key""", (kind, mbid))
            waited = sorted(set(cur.fetchall()))
            cur.execute("DELETE FROM mbws_queue WHERE kind = %s AND mbid = %s::uuid", (kind, mbid))
            for subject, key in waited:
                if subject in skip:
                    continue
                cur.execute("""SELECT 1 FROM mbws_waiters WHERE subject = %s AND subject_key = %s
                                LIMIT 1""", (subject, key))
                if cur.fetchone() is None:
                    ready[subject].append(key)
    with _answered:
        _answered.notify_all()
    for subject, keys in ready.items():
        fn = _wakers.get(subject)
        if fn is None:
            continue
        try:
            fn(sorted(set(keys)))
        except Exception:
            logger.exception("MusicBrainz answers: waking %s %s failed", subject, keys[:5])
    return queued


class _Worker:
    def __init__(self) -> None:
        self._event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._listener: Optional[Listener] = None
        self._last_author = ""
        self._resume_at = 0.0           # time.monotonic(): a pause in force
        self._failed_in_a_row = 0
        self._crashes = 0               # unexpected failures since the queue last answered

    def wake(self) -> None:
        self._event.set()

    def start(self) -> None:
        if self._thread is not None:
            return
        from desktop.p2p import mb_asks
        self._listener = Listener("mb-answers-listen", (mb_asks.CHANNEL,),
                                  on_notify=lambda _n: self.wake(), on_connect=self.wake)
        self._listener.start()
        self._thread = threading.Thread(target=self._run, daemon=True, name="mb-answers")
        self._thread.start()

    def _run(self) -> None:
        import api_cooldown
        import musicbrainz
        while True:
            self._event.clear()
            try:
                if not required():
                    self._drop_all()
                    self._event.wait()
                    continue
                deadline = api_cooldown.cooling_down(musicbrainz.COOLDOWN_SOURCE)
                if deadline is not None:
                    # With jitter: the nodes one outage stopped do not all ask
                    # again in the same second.
                    self._event.wait(max(1.0, (deadline - datetime.now(timezone.utc))
                                         .total_seconds()) + random.uniform(0, 60))
                    continue
                pause = self._resume_at - time.monotonic()
                if pause > 0:
                    self._event.wait(pause)
                    continue
                row = db_query_one(_NEXT_SQL, {"last": self._last_author})
                self._crashes = 0
                if row is None:
                    due = db_query_one(_NEXT_DUE_SQL)
                    self._event.wait(float(due["seconds"]) if due and due["seconds"] is not None
                                     else None)
                    continue
                self._one(row["kind"], row["mbid"], row["author"] or "")
            except Exception:
                # A dropped connection wakes no one: try again after a growing
                # delay, or at the next wake if that comes first.
                self._crashes += 1
                delay = min(QUEUE_PAUSE_CAP_S, QUEUE_PAUSE_S * 2 ** min(self._crashes - 1, 16))
                logger.warning("MusicBrainz answers: the consumer failed — it tries again in %.0f s",
                               delay, exc_info=True)
                self._event.wait(delay)

    def _one(self, kind: str, mbid: str, author: str) -> None:
        import musicbrainz
        self._last_author = author
        try:
            outcome, _settled = _step(kind, mbid)
        except musicbrainz.RateLimited as e:
            logger.info("MusicBrainz asks for a slower pace (%s): nothing is asked for %.0f s",
                        e, e.retry_after)
            self._resume_at = time.monotonic() + e.retry_after
            return
        except musicbrainz.Unavailable as e:
            logger.info("MusicBrainz did not answer (%s): the queue waits", e)
            return
        if outcome == "failed":
            self._failed_in_a_row += 1
            if self._failed_in_a_row >= FAILED_IN_A_ROW:
                pause = min(QUEUE_PAUSE_CAP_S, QUEUE_PAUSE_S * 2 ** min(
                    self._failed_in_a_row - FAILED_IN_A_ROW, 16)) * random.uniform(0.5, 1.5)
                logger.warning("MusicBrainz failed %d different items in a row: the queue "
                               "pauses %.0f s", self._failed_in_a_row, pause)
                self._resume_at = time.monotonic() + pause
        elif outcome != "unkept":
            self._failed_in_a_row = 0

    def _drop_all(self) -> None:
        """No longer a node that asks (a full dump arrived, or the slices went):
        what the queue held leaves it, and the subjects that waited are woken
        to decide under the node's own rules — all but the checks of slices
        to pass on, which only a node that asks makes
        (desktop/p2p/slice_checks.py) and which would only ask again."""
        from desktop.p2p import slice_checks
        for r in db_query("SELECT kind::text AS kind, mbid::text AS mbid FROM mbws_queue"):
            _answered_item(r["kind"], r["mbid"], skip=(slice_checks.SUBJECT,))


_worker = _Worker()


def start() -> None:
    """Start the consumer (backend startup)."""
    _worker.start()


def ask_now(items: Iterable[Item], priority: int = PRIORITY_PERSON,
            timeout: float = 15.0) -> bool:
    """Queue `items` and wait — the person's own act — until each has a fresh
    answer or `timeout` passes; True when all were answered."""
    items = sorted(set((k, str(m)) for k, m in items))
    if not required():
        return False

    def missing() -> list:
        return [i for i in items if not ((a := answer(*i)) and a.fresh)]

    left = missing()
    if not left:
        return True
    enqueue(left, priority)
    deadline = time.monotonic() + timeout
    with _answered:
        while True:
            left = missing()
            remaining = deadline - time.monotonic()
            if not left or remaining <= 0:
                return not left
            _answered.wait(remaining)
