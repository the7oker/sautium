"""Two positions of one tracklist are two tracks (CANON-TRACKS-1 §2, B-135).

A track's id is its title and its primary artist (`song:{artist}:{title}`),
album-free, so two movements a copy or a release titles alike — a box's
"Pulses" and its closing "Pulses", eleven "[untitled]", a disc of three
concertos each with an "I. Allegro" — were one track, shown once and played
for every position. The rule: the k-th position of a title in one tracklist
is the track `song:{artist}:{title}#k` (uuid_utils.track_uuid's
`occurrence`, stored on the row). A tracklist is a copy's files — a position
is (disc, track number, cue start), and a copy is one folder of an album, or
the sibling disc folders ("CD1", "Disc 2") the folder rule makes one album
of, as a MusicBrainz release is all its media — or a release the mint reads
(a slot is (disc, position)). Files at one position stay one track; a file
with no track number has no position and is the first.

`ordinals` is the one rule; its callers:
  - the scanner's import mints each file of a batch at its place among the
    batch's files of its copy (`batch_places`), and its re-read re-homes a
    file whose tags changed to its place (`places_of`); `settle_copies` then
    puts right what a batch could not see — a file of the copy read in
    another batch, two positions of one copy on one track (the tracks minted
    before the rule), a place that changed — each file from the last
    position down, so a track a later position leaves is free for the
    earlier one that takes its place;
  - with `complete`, a copy held in part (the canon's pass, the data step):
    where its files are bound to a MusicBrainz medium, the positions it
    holds no file of are read from that medium's release, so a node without
    track 1 gives track 14 the id a node with the whole copy gives it;
  - the phantom mint (discography), and `settle_slots` over the tracklist
    slots minted or imported before the rule (the seed's and a share's
    import run it on what they insert).

A place only grows on a copy held in part: there the files alone may not
see an earlier position. The B-134 fold (canon.content.fold_copy_twins)
never joins two positions of one copy, and the settle never moves a file off
a track whose title is not its own unless two positions of its copy share
it, so the two rules cannot undo each other."""

import logging
import unicodedata
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import psycopg2.extras
from sqlalchemy import text
from sqlalchemy.orm import Session

from album_identity import dir_disc, group_key
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


def folded(title: str) -> str:
    """A title as two copies' titles are compared: the identity's normalize
    over the accents taken off ("Gymnopédie" is "Gymnopedie")."""
    t = unicodedata.normalize("NFKD", title or "")
    return normalize("".join(ch for ch in t if not unicodedata.combining(ch)))


def _movement_of(name: str) -> str:
    """A classical title's movement: the part after the first `: `
    ("Music for 18 Musicians: Pulses" → "Pulses"), else the title."""
    return name.split(": ", 1)[1] if ": " in name else name


def titled_alike(a: str, b: str) -> bool:
    """Two titles of one thing once folded (accents, typography), or one a
    classical title whose movement is the other (the work prefix on one side
    only)."""
    fa, fb = folded(a), folded(b)
    return fa == fb or folded(_movement_of(a)) == fb or fa == folded(_movement_of(b))


def copy_root(directory_path: str) -> str:
    """The folder a copy is: a disc folder's parent ("Box/CD2" → "Box"),
    else the folder itself."""
    path = (directory_path or "").replace("\\", "/").rstrip("/")
    return path.rsplit("/", 1)[0] if dir_disc(path) and "/" in path else path


def _position(disc, number, cue_start) -> tuple:
    return (disc or 1, number, float(cue_start) if cue_start is not None else -1.0)


def batch_places(entries, dir_albums) -> Dict[Tuple[str, float], int]:
    """{(file path, cue start or -1): its place among the batch's positions
    of its copy that carry its title} — a CUE image's slices share their
    image's path. A copy is the folder's album as the folder rule groups it
    (its group key), a disc folder's with its siblings'."""
    copies: Dict[tuple, list] = defaultdict(list)
    for dir_path, md in entries:
        title = md.get("title")
        artist = md.get("artist") or md.get("album_artist")
        album_title, album_artist, track_ov, disc_ov = dir_albums.get(
            md.get("file_path"), (None, None, None, None))
        number = track_ov if track_ov is not None else md.get("track_number")
        if not title or not artist or number is None or album_title is None:
            continue
        disc = disc_ov if disc_ov is not None else md.get("disc_number")
        pos = _position(disc, number, md.get("cue_start_seconds"))
        copies[(copy_root(dir_path), group_key(album_title, album_artist))].append(
            ((md["file_path"], pos[2]), pos, song_key(title, artist)))
    out: Dict[Tuple[str, float], int] = {}
    for files in copies.values():
        places = ordinals((pos, key) for _f, pos, key in files)
        for f, pos, key in files:
            out[f] = places[(pos, key)]
    return out


# The files of the copies of the albums asked, with what their ids are
# minted from: the file's own tags, as the scanner's import reads them (the
# track credit, else the album's); the track each is on, with its title, its
# occurrence and its primary credit.
_FILES_SQL = """
    WITH f AS (
        SELECT 'media_files' AS tbl, mf.id, mf.album_variant_id AS v, mf.track_id,
               mf.disc_number, mf.track_number AS n, mf.cue_start_seconds,
               mf.raw_track_name AS title,
               coalesce(nullif(mf.raw_artist, ''), mf.raw_album_artist) AS artist,
               mf.recording_mbid::text AS rec, mf.track_total, mf.disc_total
          FROM media_files mf
         WHERE mf.track_number IS NOT NULL AND mf.raw_track_name IS NOT NULL
        UNION ALL
        SELECT 'hqp_library_files', hf.id, hf.album_variant_id, hf.track_id,
               hf.disc_number, hf.track_number, NULL, hf.raw_track_name,
               coalesce(nullif(hf.raw_artist, ''), hf.raw_album_artist), hf.recording_mbid::text,
               NULL, NULL
          FROM hqp_library_files hf
         WHERE hf.track_number IS NOT NULL AND hf.raw_track_name IS NOT NULL)
    SELECT f.*, f.track_id::text AS track, av.album_id::text AS album_id, av.directory_path,
           av.hqp_endpoint_id, t.title AS track_title, t.occurrence,
           (SELECT a.name FROM track_artists ta JOIN artists a ON a.id = ta.artist_id
             WHERE ta.track_id = t.id AND ta.role = 'primary' ORDER BY a.id LIMIT 1) AS track_artist
      FROM f JOIN album_variants av ON av.id = f.v JOIN tracks t ON t.id = f.track_id
     WHERE CAST(:v AS int[]) IS NULL OR av.album_id IN (
           SELECT album_id FROM album_variants WHERE id = ANY(CAST(:v AS int[])))
"""


def _copies(db: Session, variant_ids: Optional[List[int]]) -> Dict[tuple, list]:
    """{copy: its files} — every copy (variant_ids None), or the copies that
    hold one of `variant_ids`, a disc folder's sibling folders with it."""
    copies: Dict[tuple, list] = defaultdict(list)
    for f in db.execute(text(_FILES_SQL), {"v": variant_ids}).mappings():
        f = dict(f)
        f["pos"] = _position(f["disc_number"], f["n"], f["cue_start_seconds"])
        f["key"] = song_key(f["title"], f["artist"])
        copies[(f["album_id"], copy_root(f["directory_path"]), f["hqp_endpoint_id"])].append(f)
    if variant_ids is not None:
        wanted = set(variant_ids)
        copies = {c: fs for c, fs in copies.items() if any(f["v"] in wanted for f in fs)}
    return copies


def places_of(db: Session, variant_ids: Sequence[int], read: Dict[int, tuple]) -> Dict[int, int]:
    """{media file id: its place in its copy} for the rip files of `read`
    ({id: (disc, track number, title, artist)} — what a re-read now reads),
    among their copies' other files as they stand."""
    out: Dict[int, int] = {}
    for files in _copies(db, sorted(set(variant_ids))).values():
        for f in files:
            if f["tbl"] == "media_files" and f["id"] in read:
                disc, number, title, artist = read[f["id"]]
                f["pos"] = _position(disc, number, f["cue_start_seconds"])
                f["key"] = song_key(title, artist)
        places = ordinals((f["pos"], f["key"]) for f in files)
        for f in files:
            if f["tbl"] == "media_files" and f["id"] in read:
                out[f["id"]] = places[(f["pos"], f["key"])]
    return out


def _lacking(files: list) -> set:
    """The (disc, position) pairs a copy lacks: below the highest position
    held or the count the tags declare on each disc it holds, and the first
    of each disc below the highest held or the count declared that it lacks
    whole. A copy that lacks none is held whole."""
    by_disc: Dict[int, set] = defaultdict(set)
    totals: Dict[int, int] = defaultdict(int)
    disc_total = 0
    for f in files:
        by_disc[f["pos"][0]].add(f["n"])
        totals[f["pos"][0]] = max(totals[f["pos"][0]], f["track_total"] or 0)
        disc_total = max(disc_total, f["disc_total"] or 0)
    out = {(d, n) for d, ns in by_disc.items() for n in range(1, max(max(ns), totals[d]) + 1)
           if n not in ns}
    return out | {(d, 1) for d in range(1, max(max(by_disc), disc_total) + 1) if d not in by_disc}


def _completion(db: Session, partial: List[Tuple[tuple, list]]) -> Dict[int, int]:
    """{id(file dict): how many earlier positions of its copy the node holds
    no file of carry its title}, read off MusicBrainz: per disc of a copy
    held in part, the medium every bound file of it sits on at its own
    position (the smallest such medium's id where several do); the earlier
    positions are that medium's before the file's, and — where the copy's
    disc is that medium's position on its release and the copy holds another
    disc of that release — the release's earlier media whole. A position
    counts when titled alike (titled_alike) and credited as the file's own
    track is. Only files above a position the
    node lacks are read, and the recordings' rows once (mb_recording has no
    index on its gid)."""
    asked = []
    for _copy, files in partial:
        lacking = _lacking(files)
        asked.extend((files, f) for f in files
                     if f["rec"] and any(p < f["pos"][:2] for p in lacking))
    gids = sorted({f["rec"] for _fs, f in asked})
    if not gids:
        return {}
    on = defaultdict(set)   # recording gid → {(medium, position, credit)}
    for r in db.execute(text("""
            SELECT r.gid::text AS gid, mt.medium, mt.position, mt.artist_credit
              FROM mb_recording r JOIN mb_track mt ON mt.recording = r.id
             WHERE r.gid = ANY(CAST(:g AS uuid[]))"""), {"g": gids}):
        on[r.gid].add((r.medium, r.position, r.artist_credit))
    picked: Dict[tuple, int] = {}
    for _copy, files in partial:
        for disc in {f["pos"][0] for f in files}:
            media = None
            for f in files:
                if f["pos"][0] == disc and f["rec"] in on:
                    here = {m for m, p, _c in on[f["rec"]] if p == f["n"]}
                    media = here if media is None else media & here
            if media:
                picked[(id(files), disc)] = min(media)
    if not picked:
        return {}
    rows = db.execute(text("""
        SELECT md.id AS medium, md.release, md.position AS mpos FROM mb_medium md
         WHERE md.id = ANY(CAST(:m AS int[]))"""), {"m": sorted(set(picked.values()))}).fetchall()
    medium_of = {r.medium: (r.release, r.mpos) for r in rows}
    tracklist = defaultdict(list)   # release → [((medium position, position), name, credit)]
    for r in db.execute(text("""
            SELECT md.release, md.position AS mpos, mt.position, mt.name, mt.artist_credit
              FROM mb_medium md JOIN mb_track mt ON mt.medium = md.id
             WHERE md.release = ANY(CAST(:r AS int[]))"""),
            {"r": sorted({rel for rel, _p in medium_of.values()})}):
        tracklist[r.release].append(((r.mpos, r.position), r.name, r.artist_credit))
    out: Dict[int, int] = {}
    for files, f in asked:
        medium = picked.get((id(files), f["pos"][0]))
        if medium not in medium_of:
            continue
        release, mpos = medium_of[medium]
        own = {c for m, p, c in on.get(f["rec"], ()) if m == medium and p == f["n"]}
        # the copy's discs are the release's media only where the copy holds
        # another disc of that release: one disc of a box kept as an album of
        # its own is that disc's tracklist alone
        whole = f["pos"][0] == mpos and any(
            d != f["pos"][0] and medium_of.get(m, (None,))[0] == release
            for (fid, d), m in picked.items() if fid == id(files))
        held = {fl["pos"][:2] for fl in files}
        here = (mpos, f["n"])
        before = 0
        for (mp, p), name, credit in tracklist[release]:
            if (mp, p) >= here or (mp != mpos and not whole) or credit not in own:
                continue
            if ((mp, p) if whole else (f["pos"][0], p)) in held:
                continue
            before += titled_alike(name, f["title"])
        if before:
            out[id(f)] = before
    return out


def _move(db: Session, f: dict, new: str, k: int, title: str, artist: str,
          collapsed: bool, st: dict) -> None:
    """One file to the track of its place: a rip by rehome_file_track's split
    (its analysis and its own listens go with it), a file at an HQPlayer by
    its row; the tracklist slot the old track held at its position follows,
    the credits are derived again and both tracks' artists' canon is woken.
    A file leaving a track two positions of its copy shared carries no
    binding: one recording stood for both, wrong for all but one and
    arbitrary for that one, so the track's bindings and its files' stamps go
    and the canon binds each on its own evidence (as the re-read's split
    does). A file whose place alone changed takes this node's binding of its
    recording along where no other file of the old track carries it (a
    peer's is its author's: the node's own binding replaces it)."""
    from canon.identity import _ensure_artist, rehome_file_track
    from credits import derive_composers, lock_credits
    # the credits' lock before any row: the order every writer of credits
    # takes (credits.CREDITS_LOCK_KEY)
    lock_credits(db)
    old = str(f["track_id"])
    p = {"old": old, "new": new, "id": f["id"], "rec": f["rec"], "v": f["v"]}
    if collapsed:
        db.execute(text("DELETE FROM track_mbids WHERE track_id = :old"), p)
        for table in ("media_files", "hqp_library_files"):
            db.execute(text(f"UPDATE {table} SET recording_mbid = NULL "
                            f"WHERE track_id = :old AND recording_mbid IS NOT NULL"), p)
        db.execute(text("""
            UPDATE album_variants av SET positional_bound = FALSE, release_mbid = NULL,
                   positional_asked = NULL
             WHERE av.positional_bound AND (av.id = :v OR EXISTS (
                   SELECT 1 FROM owned_files o WHERE o.album_variant_id = av.id AND o.track_id = :old))"""), p)
    if f["tbl"] == "media_files":
        got = rehome_file_track(db, f["id"], old, title, artist, split=True, occurrence=k)
        assert got == new, (got, new)
        db.execute(text("UPDATE media_files SET content_at = now() WHERE id = :id"), p)
    else:
        a = str(_ensure_artist(db, artist))
        db.execute(text("""INSERT INTO tracks (id, title, title_latin, occurrence)
                           VALUES (:id, :t, :tl, :k) ON CONFLICT (id) DO NOTHING"""),
                   {"id": new, "t": title, "tl": latinize(title), "k": k})
        db.execute(text("""INSERT INTO track_artists (track_id, artist_id, role)
                           VALUES (:t, :a, 'primary') ON CONFLICT DO NOTHING"""), {"t": new, "a": a})
        db.execute(text("UPDATE hqp_library_files SET track_id = :new WHERE id = :id"), p)
    if not collapsed and f["rec"]:
        db.execute(text("""
            UPDATE track_mbids SET track_id = :new
             WHERE recording_mbid = CAST(:rec AS uuid) AND track_id = :old AND NOT imported
               AND NOT EXISTS (SELECT 1 FROM owned_files o
                                WHERE o.track_id = :old AND o.recording_mbid = CAST(:rec AS uuid))"""), p)
    db.execute(text("""
        UPDATE album_tracks at SET track_id = :new
          FROM album_variants av
         WHERE av.id = :v AND at.album_id = av.album_id AND at.disc = :d
           AND at.position = :n AND at.track_id = :old"""), {**p, "d": f["pos"][0], "n": f["n"]})
    db.execute(text("""UPDATE artists SET last_mb_sync = NULL
                        WHERE id IN (SELECT artist_id FROM track_artists
                                      WHERE role = 'primary' AND track_id IN (:old, :new))"""), p)
    derive_composers(db, [old, new])
    st["moved"] += 1


def settle_copies(db: Session, variant_ids: Optional[Sequence[int]] = None,
                  complete: bool = False, fresh: frozenset = frozenset()) -> dict:
    """Every file of each copy (all, or those holding `variant_ids`) on the
    track of its place. A file moves when two positions of its copy share its
    track and it is not the first of them, or when its track is its own
    title's (not one the canon folded or renamed it into) at another place —
    a place grown always, a place shrunk only in a copy held whole (in part,
    the files alone may not see an earlier position). From the last position
    down, until nothing moves: a track a later position leaves is free for
    the one that takes its place. `complete` reads the positions a copy held
    in part lacks off MusicBrainz (_completion). Idempotent.

    `fresh` names the files ((table, id)) this transaction wrote — an
    import's: two positions on one track are a collapse to undo, its bindings
    and stamps gone, only where two of them are older than this
    transaction; a fresh file at a position its copy held already on that
    track is no collapse, and the file whose place it changed keeps its
    binding."""
    st = {"copies": 0, "moved": 0, "unsplittable": 0}
    vs = None if variant_ids is None else sorted({int(v) for v in variant_ids})
    if vs == []:
        return st
    for _round in range(4):
        moved = st["moved"]
        copies = _copies(db, vs)
        partial = [(c, fs) for c, fs in copies.items() if _lacking(fs)]
        extra = _completion(db, partial) if complete else {}
        part = {c for c, _fs in partial}
        for c, files in copies.items():
            places = ordinals((f["pos"], f["key"]) for f in files)
            on: Dict[str, set] = defaultdict(set)
            older: Dict[str, set] = defaultdict(set)
            for f in files:
                on[str(f["track_id"])].add(f["pos"])
                if (f["tbl"], f["id"]) not in fresh:
                    older[str(f["track_id"])].add(f["pos"])
            touched = False
            for f in sorted(files, key=lambda f: f["pos"], reverse=True):
                k = places[(f["pos"], f["key"])] + extra.get(id(f), 0)
                t = str(f["track_id"])
                shared = len(on[t]) > 1 and f["pos"] != min(on[t])
                own = normalize(f["track_title"]) == normalize(f["title"])
                if own and f["track_artist"]:
                    title, artist = f["track_title"], f["track_artist"]
                else:
                    title, artist = f["title"], f["artist"]
                new = str(track_uuid(title, artist, k))
                wrong_place = own and (k > f["occurrence"] or (k < f["occurrence"] and c not in part))
                if new == t or not (shared or wrong_place):
                    if shared:
                        st["unsplittable"] += 1
                        logger.info("copy %s: position %s shares track %s under another title; "
                                    "left as it is", c, f["pos"], t)
                    continue
                _move(db, f, new, k, title, artist, shared and len(older[t]) > 1, st)
                on[t].discard(f["pos"])
                on[new].add(f["pos"])
                touched = True
            st["copies"] += touched
        if st["moved"] == moved:
            break
    if st["moved"]:
        logger.info("tracks of a copy at their places: %s", st)
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
