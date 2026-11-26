"""Two positions of one tracklist are two tracks (CANON-TRACKS-1 §2, B-135).

A track's id is its title and its primary artist (`song:{artist}:{title}`),
album-free, so two movements a copy or a release titles alike — a box's
"Pulses" and its closing "Pulses", eleven "[untitled]", a disc of three
concertos each with an "I. Allegro" — were one track, shown once and played
for every position. The rule: the k-th position of a title in one tracklist
is the track `song:{artist}:{title}#k` (uuid_utils.track_uuid's
`occurrence`, stored on the row). A tracklist is a copy's files (a position
is (disc, track number, cue start)) or a MusicBrainz release the mint reads
(a slot is (disc, position)). Files at one position stay one track; a file
with no track number has no position and is the first.

`ordinals` is the one rule; its callers:
  - the scanner's import mints each file of a batch at its place in the
    batch's copy, and `settle_copies` then puts right what a batch could not
    see — a file of the same copy read in another batch, two positions of
    one copy on one track (the tracks minted before this rule): the later
    positions move to their own track, with the analysis made from their
    material and their own listens (rehome_file_track's split);
  - `complete_copies`: a copy the node holds only in part shows its files a
    first "Pulses" where the release's first position is missing here; where
    the copy's files are bound to a medium of MusicBrainz's, the positions it
    holds no file of are read from that medium, so a node without track 1
    gives track 14 the id a node with the whole copy gives it;
  - the phantom mint (discography), and `settle_slots` over the tracklist
    slots minted or imported before the rule (the seed's and a share's
    import run it on what they insert).

The B-134 fold (canon.content.fold_copy_twins) never joins two positions of
one copy, so the two rules cannot undo each other."""

import logging
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import psycopg2.extras
from sqlalchemy import text
from sqlalchemy.orm import Session

from transliterate import latinize
from uuid_utils import normalize, song_key, track_uuid

logger = logging.getLogger(__name__)


def ordinals(pairs: Iterable[Tuple[tuple, tuple]]) -> Dict[Tuple[tuple, tuple], int]:
    """{(position, key): k} — the place of each position among the
    tracklist's positions that carry its key, in position order. One
    position with one key is one entry, however many files hold it."""
    seen: Dict[tuple, int] = defaultdict(int)
    out: Dict[Tuple[tuple, tuple], int] = {}
    for pos, key in sorted(set(pairs)):
        seen[key] += 1
        out[(pos, key)] = seen[key]
    return out


def _movement_of(name: str) -> str:
    """A classical title's movement: the part after the first `: `
    ("Music for 18 Musicians: Pulses" → "Pulses"), else the title."""
    return name.split(": ", 1)[1] if ": " in name else name


def titled_alike(a: str, b: str) -> bool:
    """Two titles of one thing once folded: equal under the identity's
    normalize, or one a classical title whose movement is the other (the
    work prefix on one side only)."""
    na, nb = normalize(a), normalize(b)
    return na == nb or normalize(_movement_of(a)) == nb or na == normalize(_movement_of(b))


# The files of the copies asked, with what their ids are minted from: the
# file's own tags, as the scanner's import reads them (the track credit, else
# the album's).
_FILES_SQL = """
    SELECT 'media_files' AS tbl, mf.id, mf.album_variant_id AS v, mf.track_id::text AS track_id,
           coalesce(mf.disc_number, 1) AS d, mf.track_number AS n,
           coalesce(mf.cue_start_seconds, -1)::float AS cs, mf.raw_track_name AS title,
           coalesce(nullif(mf.raw_artist, ''), mf.raw_album_artist) AS artist,
           mf.recording_mbid::text AS rec, mf.track_total
      FROM media_files mf
     WHERE mf.track_number IS NOT NULL AND mf.raw_track_name IS NOT NULL
       AND (CAST(:v AS int[]) IS NULL OR mf.album_variant_id = ANY(CAST(:v AS int[])))
    UNION ALL
    SELECT 'hqp_library_files', hf.id, hf.album_variant_id, hf.track_id::text,
           coalesce(hf.disc_number, 1), hf.track_number, -1, hf.raw_track_name,
           coalesce(nullif(hf.raw_artist, ''), hf.raw_album_artist), hf.recording_mbid::text, NULL
      FROM hqp_library_files hf
     WHERE hf.track_number IS NOT NULL AND hf.raw_track_name IS NOT NULL
       AND (CAST(:v AS int[]) IS NULL OR hf.album_variant_id = ANY(CAST(:v AS int[])))
"""


def _pos(f) -> tuple:
    return (f.d, f.n, f.cs)


def _move(db: Session, f, k: int, st: dict) -> str:
    """One file to the track of its title's k-th place: a rip by
    rehome_file_track's split (its analysis and its own listens go with
    it), a file at an HQPlayer by its row; the tracklist slot the old track
    held at its position follows. Returns the track."""
    from canon.identity import _ensure_artist, rehome_file_track
    from credits import derive_composers
    if f.tbl == "media_files":
        new = rehome_file_track(db, f.id, f.track_id, f.title, f.artist, split=True, occurrence=k)
        db.execute(text("UPDATE media_files SET content_at = now() WHERE id = :id"), {"id": f.id})
    else:
        new = str(track_uuid(f.title, f.artist, k))
        artist = str(_ensure_artist(db, f.artist))
        db.execute(text("""INSERT INTO tracks (id, title, title_latin, occurrence)
                           VALUES (:id, :t, :tl, :k) ON CONFLICT (id) DO NOTHING"""),
                   {"id": new, "t": f.title, "tl": latinize(f.title), "k": k})
        db.execute(text("""INSERT INTO track_artists (track_id, artist_id, role)
                           VALUES (:t, :a, 'primary') ON CONFLICT DO NOTHING"""),
                   {"t": new, "a": artist})
        db.execute(text("UPDATE hqp_library_files SET track_id = :t WHERE id = :id"),
                   {"t": new, "id": f.id})
    db.execute(text("""
        UPDATE album_tracks at SET track_id = :new
          FROM album_variants av
         WHERE av.id = :v AND at.album_id = av.album_id AND at.disc = :d
           AND at.position = :n AND at.track_id = :old"""),
        {"new": new, "old": f.track_id, "v": f.v, "d": f.d, "n": f.n})
    derive_composers(db, [f.track_id, new])
    st["moved"] += 1
    return new


def settle_copies(db: Session, variant_ids: Optional[Sequence[int]] = None) -> dict:
    """Within each copy (all, or `variant_ids`), the files at two positions
    or more of one track: each later position moves to its title's place in
    the copy. Idempotent: a copy whose positions are each their own track
    is not read past the first statement. A later position whose own title
    is the first of its kind in the copy (the canon folded two titles into
    one track) has no place to move to and stays, counted."""
    st = {"copies": 0, "moved": 0, "unsplittable": 0}
    vs = None if variant_ids is None else sorted({int(v) for v in variant_ids})
    if vs == []:
        return st
    shared = [r.v for r in db.execute(text(f"""
        SELECT DISTINCT v FROM ({_FILES_SQL}) f
         GROUP BY v, track_id HAVING count(DISTINCT (d, n, cs)) > 1"""), {"v": vs})]
    if not shared:
        return st
    by_copy: Dict[int, list] = defaultdict(list)
    for f in db.execute(text(_FILES_SQL), {"v": shared}):
        by_copy[f.v].append(f)
    for v, files in by_copy.items():
        st["copies"] += 1
        places = ordinals((_pos(f), song_key(f.title, f.artist)) for f in files)
        first: Dict[str, tuple] = {}
        for f in sorted(files, key=_pos):
            first.setdefault(f.track_id, _pos(f))
        for f in files:
            if _pos(f) == first[f.track_id]:
                continue
            k = places[(_pos(f), song_key(f.title, f.artist))]
            if k == 1:
                st["unsplittable"] += 1
                logger.warning("copy %s: positions %s and %s share track %s under two titles; "
                               "left as they are", v, first[f.track_id], _pos(f), f.track_id)
                continue
            _move(db, f, k, st)
    if st["moved"]:
        logger.info("tracks of one copy at two positions split: %s", st)
    return st


# The copies held in part: a disc of the copy whose positions leave a gap
# below its highest, or fewer than the tags declare.
_PARTIAL_SQL = f"""
    WITH f AS ({_FILES_SQL}),
    disc AS (SELECT v, d FROM f GROUP BY v, d
              HAVING count(DISTINCT n) < greatest(max(n), coalesce(max(track_total), 0)))
    SELECT f.* FROM f JOIN disc USING (v, d)
"""


def complete_copies(db: Session, variant_ids: Optional[Sequence[int]] = None) -> dict:
    """The copies held in part whose files are bound to MusicBrainz's
    recordings: per disc, the medium every bound file of it sits on at its
    own position (the smallest such medium's id where several do — one
    tracklist, read alike on every node); its tracks at the positions the
    node holds no file of count toward a held file's place when titled alike
    (titled_alike) and credited as that file's own track is. A file whose
    place so grows moves to it — only one still on its own first-hand
    identity (the occurrence its copy's files give, its own title): a track
    the canon folded or renamed is the canon's. Idempotent."""
    st = {"copies": 0, "moved": 0}
    vs = None if variant_ids is None else sorted({int(v) for v in variant_ids})
    if vs == []:
        return st
    files = db.execute(text(_PARTIAL_SQL), {"v": vs}).fetchall()
    gids = sorted({f.rec for f in files if f.rec})
    if not gids:
        return st
    on = defaultdict(set)   # recording gid → {(medium, position, credit)}
    for r in db.execute(text("""
            SELECT r.gid::text AS gid, mt.medium, mt.position, mt.artist_credit
              FROM mb_recording r JOIN mb_track mt ON mt.recording = r.id
             WHERE r.gid = ANY(CAST(:g AS uuid[]))"""), {"g": gids}):
        on[r.gid].add((r.medium, r.position, r.artist_credit))
    by_disc: Dict[tuple, list] = defaultdict(list)
    for f in files:
        by_disc[(f.v, f.d)].append(f)
    picked: Dict[tuple, int] = {}
    for key, fs in by_disc.items():
        bound = [f for f in fs if f.rec in on]
        if not bound:
            continue
        media = None
        for f in bound:
            here = {m for m, p, _c in on[f.rec] if p == f.n}
            media = here if media is None else media & here
        if media:
            picked[key] = min(media)
    if not picked:
        return st
    tracklist = defaultdict(dict)   # medium → {position: (name, credit)}
    for r in db.execute(text("""SELECT medium, position, name, artist_credit FROM mb_track
                                 WHERE medium = ANY(CAST(:m AS int[]))"""),
                        {"m": sorted(set(picked.values()))}):
        tracklist[r.medium][r.position] = (r.name, r.artist_credit)
    current = {r.id: r for r in db.execute(text(
        "SELECT id::text AS id, title, occurrence FROM tracks WHERE id = ANY(CAST(:t AS uuid[]))"),
        {"t": sorted({f.track_id for fs in by_disc.values() for f in fs})})}
    for key, medium in picked.items():
        fs = by_disc[key]
        st["copies"] += 1
        held = {f.n for f in fs}
        missing = {p: tracklist[medium][p] for p in tracklist[medium] if p not in held}
        places = ordinals((_pos(f), song_key(f.title, f.artist)) for f in fs)
        for f in fs:
            own = {c for m, p, c in on.get(f.rec, ()) if m == medium and p == f.n}
            before = sum(1 for p, (name, credit) in missing.items()
                         if p < f.n and credit in own and titled_alike(name, f.title))
            if not before:
                continue
            k_files = places[(_pos(f), song_key(f.title, f.artist))]
            t = current.get(f.track_id)
            if t is None or t.occurrence != k_files or normalize(t.title) != normalize(f.title):
                continue
            _move(db, f, k_files + before, st)
    if st["moved"]:
        logger.info("tracks of copies held in part moved to their place on the release: %s", st)
    return st


def settle_slots(conn, album_ids: Optional[Sequence[str]] = None) -> dict:
    """Within each album (all, or `album_ids`), the tracklist slots of one
    track at two positions or more: each later slot moves to its title's
    place in the album's tracklist — the track minted for it (title, its
    credits, `occurrence`) where none is. A slot is re-pointed in place, its
    seal shed by the table's guard (the notary signs this node's again).
    Set-based for the phantom layer's size; idempotent. On `conn`'s
    transaction; the caller commits."""
    st = {"albums": 0, "slots": 0, "tracks": 0}
    ids = None if album_ids is None else sorted({str(a) for a in album_ids})
    if ids == []:
        return st
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        return _settle_slots(cur, ids, st)


# The (album, track) pairs of one track at two slots or more whose album
# holds no copy with a file of it: an owned copy's positions are its files'
# (settle_copies), and two copies that are one performance at two positions
# (canon.content.fold_copy_twins) hold one track at two slots by design.
_SHARED_SLOTS_SQL = """
    SELECT at.album_id, at.track_id FROM album_tracks at
     WHERE (CAST(%(a)s AS uuid[]) IS NULL OR at.album_id = ANY(CAST(%(a)s AS uuid[])))
     GROUP BY at.album_id, at.track_id HAVING count(*) > 1
       AND NOT EXISTS (SELECT 1 FROM owned_files f JOIN album_variants av ON av.id = f.album_variant_id
                        WHERE av.album_id = at.album_id AND f.track_id = at.track_id)
"""


def _settle_slots(cur, ids, st: dict) -> dict:
    cur.execute(f"""
        WITH g AS ({_SHARED_SLOTS_SQL})
        SELECT at.id, at.album_id::text AS album_id, at.disc, at.position,
               at.track_id::text AS track_id, t.title,
               gs.track_id IS NOT NULL AS shared,
               (SELECT a.name FROM track_artists ta JOIN artists a ON a.id = ta.artist_id
                 WHERE ta.track_id = t.id AND ta.role = 'primary' ORDER BY a.id LIMIT 1) AS artist
          FROM album_tracks at JOIN (SELECT DISTINCT album_id FROM g) ga ON ga.album_id = at.album_id
          LEFT JOIN g gs ON gs.album_id = at.album_id AND gs.track_id = at.track_id
          JOIN tracks t ON t.id = at.track_id
         ORDER BY at.album_id, at.disc, at.position""", {"a": ids})
    by_album: Dict[str, list] = defaultdict(list)
    for r in cur.fetchall():
        by_album[r["album_id"]].append(r)
    moves: List[tuple] = []
    new_tracks: Dict[str, tuple] = {}
    for album, slots in by_album.items():
        st["albums"] += 1
        named = [s for s in slots if s["artist"]]
        places = ordinals(((s["disc"], s["position"]), song_key(s["title"], s["artist"]))
                          for s in named)
        seen = set()
        for s in named:
            if not s["shared"]:
                continue
            if s["track_id"] not in seen:
                seen.add(s["track_id"])
                continue
            k = places[((s["disc"], s["position"]), song_key(s["title"], s["artist"]))]
            target = str(track_uuid(s["title"], s["artist"], k))
            if k == 1 or target == s["track_id"]:
                continue
            moves.append((s["id"], target))
            new_tracks.setdefault(target, (s["track_id"], s["title"], k))
    if not moves:
        return st
    rows = [(t, title, latinize(title), k) for t, (_old, title, k) in new_tracks.items()]
    st["tracks"] = len(psycopg2.extras.execute_values(cur, """
        INSERT INTO tracks (id, title, title_latin, occurrence) VALUES %s
        ON CONFLICT (id) DO NOTHING RETURNING id""", rows,
        template="(%s::uuid, %s, %s, %s)", page_size=500, fetch=True))
    psycopg2.extras.execute_values(cur, """
        INSERT INTO track_artists (track_id, artist_id, role)
        SELECT x.new, ta.artist_id, ta.role
          FROM (VALUES %s) AS x(new, old) JOIN track_artists ta ON ta.track_id = x.old
        ON CONFLICT DO NOTHING""",
        [(t, old) for t, (old, _title, _k) in new_tracks.items()],
        template="(%s::uuid, %s::uuid)", page_size=500)
    psycopg2.extras.execute_values(cur, """
        UPDATE album_tracks at SET track_id = x.track_id
          FROM (VALUES %s) AS x(id, track_id) WHERE at.id = x.id""",
        moves, template="(%s::bigint, %s::uuid)", page_size=500)
    st["slots"] = len(moves)
    logger.info("tracklist slots of one track at two positions split: %s", st)
    return st
