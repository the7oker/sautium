"""Test helpers for a node with catalogue slices and no MusicBrainz database.

- `FakeMusicBrainz`: WS/2's JSON for the entities a test declares, a 404 for
  every other MBID, a merged MBID answered with the entity it was merged
  into, a browse a page (`page`) at a time, a switch that makes MusicBrainz
  unreachable, and failures for one MBID (`fail`). Installed over
  backend/musicbrainz.py's two calls, so nothing leaves the machine.
- `drain()`: the answers' queue worked through in the test's own thread, in
  the consumer's order and by its steps (mb_answers._step) — what its
  consumer thread does at MusicBrainz's pace.
- `Slice`: rows in the `mb_*` tables, as a slice brings them — the plan the
  canon makes before MusicBrainz is asked.
- `Library`: an owned artist with albums, tracks and files.
- `slice_fed(dsn, monkeypatch)`: the pool, the SQLAlchemy sessions and
  mb_backend pointed at a scratch database whose node holds slices and no
  dump marker.
"""

import copy
import itertools
import uuid
from typing import Iterable, List, Optional, Sequence

_ids = itertools.count(1000)


def mbid() -> str:
    return str(uuid.uuid4())


def credit(*members) -> list:
    """WS/2's artist-credit: members as (name, mbid) or (name, mbid, join)."""
    return [{"name": m[0], "joinphrase": m[2] if len(m) > 2 else "",
             "artist": {"id": m[1], "name": m[0]}} for m in members]


class FakeMusicBrainz:
    def __init__(self):
        self.entities = {"artist": {}, "release_group": {}, "release": {}, "recording": {}}
        self.merged = {}
        self.credited = {}
        self.asked: List[tuple] = []
        self.down = False
        self.page = 100
        # (kind, mbid) -> exceptions raised, one per request, before it answers;
        # a browse page is ("artist_release_groups", artist, offset)
        self.fail: dict = {}

    # ---- declaring what MusicBrainz holds

    def artist(self, mbid_: str, name: str, *, aliases: Sequence[str] = (), sort_name=None,
               genres: Sequence[str] = (), member_of: Sequence[str] = (),
               members: Sequence[str] = (), urls: Sequence[str] = (), disambiguation="",
               type_=None, area=None) -> str:
        rels = ([{"type": "member of band", "direction": "forward", "target-type": "artist",
                  "artist": {"id": g}} for g in member_of]
                + [{"type": "member of band", "direction": "backward", "target-type": "artist",
                    "artist": {"id": m}} for m in members]
                + [{"type": "bandcamp", "target-type": "url", "url": {"resource": u}} for u in urls])
        self.entities["artist"][mbid_] = {
            "id": mbid_, "name": name, "sort-name": sort_name or name,
            "disambiguation": disambiguation, "type": type_,
            "area": {"name": area} if area else None,
            "aliases": [{"name": a, "sort-name": a, "locale": None} for a in aliases],
            "genres": [{"name": g, "count": 1} for g in genres], "relations": rels}
        return mbid_

    def release_group(self, mbid_: str, title: str, artist_credit: list, *, primary="Album",
                      secondary: Sequence[str] = (), first_release_date=None) -> str:
        self.entities["release_group"][mbid_] = {
            "id": mbid_, "title": title, "primary-type": primary,
            "secondary-types": list(secondary), "first-release-date": first_release_date or "",
            "artist-credit": copy.deepcopy(artist_credit)}
        for c in artist_credit:
            self.credited.setdefault(c["artist"]["id"], []).append(mbid_)
        return mbid_

    def recording(self, mbid_: str, title: str, length: Optional[int], artist_credit: list) -> str:
        self.entities["recording"][mbid_] = {"id": mbid_, "title": title, "length": length,
                                             "artist-credit": copy.deepcopy(artist_credit)}
        return mbid_

    def release(self, mbid_: str, title: str, group: str, artist_credit: list,
                tracks: Iterable[dict], *, status="Official", barcode=None,
                urls: Sequence[str] = (), date=None, media_format="CD") -> str:
        """tracks: {"title", "length", "recording"} with optional "credit"
        (the release's by default), "disc" and "position"."""
        media: dict = {}
        for i, t in enumerate(tracks):
            disc = t.get("disc", 1)
            medium = media.setdefault(disc, {"position": disc, "format": media_format, "tracks": []})
            tcredit = t.get("credit") or artist_credit
            rec = self.entities["recording"].get(t["recording"]) or {
                "id": t["recording"], "title": t.get("recording_title", t["title"]),
                "length": t["length"], "artist-credit": copy.deepcopy(tcredit)}
            self.entities["recording"].setdefault(t["recording"], rec)
            medium["tracks"].append({"id": t.get("id") or mbid(),
                                     "position": t.get("position", len(medium["tracks"]) + 1),
                                     "title": t["title"], "length": t["length"],
                                     "artist-credit": copy.deepcopy(tcredit),
                                     "recording": copy.deepcopy(rec)})
        self.entities["release"][mbid_] = {
            "id": mbid_, "title": title, "status": status, "barcode": barcode, "date": date,
            "artist-credit": copy.deepcopy(artist_credit),
            "release-group": copy.deepcopy(self.entities["release_group"][group]),
            "media": [media[d] for d in sorted(media)],
            "relations": [{"type": "bandcamp", "target-type": "url", "url": {"resource": u}}
                          for u in urls]}
        return mbid_

    def merge(self, kind: str, old: str, new: str) -> None:
        self.merged[(kind, old)] = new
        self.entities[kind].pop(old, None)

    # ---- what backend/musicbrainz.py answers

    def _failing(self, key: tuple) -> None:
        import musicbrainz
        if self.down:
            raise musicbrainz.Unavailable("down")
        pending = self.fail.get(key)
        if pending:
            raise pending.pop(0)

    def lookup(self, kind: str, mbid_: str) -> dict:
        import musicbrainz
        self.asked.append((kind, mbid_))
        self._failing((kind, mbid_))
        target = self.merged.get((kind, mbid_), mbid_)
        entity = self.entities[kind].get(target)
        if entity is None:
            raise musicbrainz.NotFound(f"{kind}/{mbid_}")
        return copy.deepcopy(entity)

    def browse_release_groups_page(self, artist_mbid: str, offset: int) -> tuple:
        import musicbrainz
        self.asked.append(("artist_release_groups", artist_mbid))
        self._failing(("artist_release_groups", artist_mbid, offset))
        if artist_mbid not in self.entities["artist"]:
            raise musicbrainz.NotFound(f"artist/{artist_mbid}")
        groups = [copy.deepcopy(self.entities["release_group"][g])
                  for g in self.credited.get(artist_mbid, []) if g in self.entities["release_group"]]
        return groups[offset:offset + self.page], len(groups)

    def install(self, monkeypatch) -> "FakeMusicBrainz":
        import musicbrainz
        monkeypatch.setattr(musicbrainz, "lookup", self.lookup)
        monkeypatch.setattr(musicbrainz, "browse_release_groups_page", self.browse_release_groups_page)
        return self


def drain(limit: int = 10_000) -> int:
    """Answer what the queue holds, now, in the consumer's order and by its
    steps; returns how many queued items were answered (MusicBrainz silent
    stops it, the rest stay; an item that failed waits out its backoff)."""
    import mb_answers
    import musicbrainz
    from db_pool import db_query_one
    done, last = 0, ""
    while done < limit:
        row = db_query_one(mb_answers._NEXT_SQL, {"last": last})
        if row is None:
            return done
        last = row["author"] or ""
        try:
            _outcome, settled = mb_answers._step(row["kind"], row["mbid"])
        except musicbrainz.Unavailable:
            return done
        done += settled
    return done


class Slice:
    """mb_* rows as a slice delivers them (MB's internal ids made up here)."""

    def __init__(self, cur):
        self.cur = cur

    def artist(self, gid: str, name: str, aliases: Sequence[str] = (), sort_name=None) -> int:
        aid = next(_ids)
        self.cur.execute("INSERT INTO mb_artist (id, gid, name, sort_name) VALUES (%s, %s, %s, %s)",
                         (aid, gid, name, sort_name or name))
        for a in aliases:
            self.cur.execute("INSERT INTO mb_artist_alias (id, artist, name, sort_name) "
                             "VALUES (%s, %s, %s, %s)", (next(_ids), aid, a, a))
        return aid

    def credit(self, *artists) -> int:
        """artists: (mb id, credited name) in credit order."""
        cid = next(_ids)
        self.cur.execute("INSERT INTO mb_artist_credit (id, name, artist_count) VALUES (%s, %s, %s)",
                         (cid, " & ".join(n for _, n in artists), len(artists)))
        for pos, (aid, name) in enumerate(artists):
            self.cur.execute("INSERT INTO mb_artist_credit_name (artist_credit, position, artist, name, "
                             "join_phrase) VALUES (%s, %s, %s, %s, %s)",
                             (cid, pos, aid, name, " & " if pos < len(artists) - 1 else ""))
        return cid

    def _type(self, name: str) -> int:
        self.cur.execute("SELECT id FROM mb_release_group_primary_type WHERE name = %s", (name,))
        row = self.cur.fetchone()
        if row:
            return row[0]
        tid = {"Album": 1, "Single": 2, "EP": 3, "Other": 11, "Broadcast": 12}.get(name, next(_ids))
        self.cur.execute("INSERT INTO mb_release_group_primary_type (id, name) VALUES (%s, %s) "
                         "ON CONFLICT DO NOTHING", (tid, name))
        return tid

    def release_group(self, gid: str, title: str, credit_id: int, primary="Album") -> int:
        rid = next(_ids)
        self.cur.execute("INSERT INTO mb_release_group (id, gid, name, artist_credit, type) "
                         "VALUES (%s, %s, %s, %s, %s)", (rid, gid, title, credit_id, self._type(primary)))
        return rid

    def release(self, gid: str, title: str, group_id: int, credit_id: int,
                tracks: Iterable[tuple], status: int = 1) -> int:
        """tracks: (title, length ms, recording gid[, recording credit id])."""
        rid = next(_ids)
        self.cur.execute("INSERT INTO mb_release (id, gid, name, artist_credit, release_group, status) "
                         "VALUES (%s, %s, %s, %s, %s, %s)", (rid, gid, title, credit_id, group_id, status))
        mid = next(_ids)
        tracks = list(tracks)
        self.cur.execute("INSERT INTO mb_medium (id, release, position, track_count) VALUES (%s, %s, 1, %s)",
                         (mid, rid, len(tracks)))
        for pos, t in enumerate(tracks, start=1):
            title_, length, rec_gid = t[0], t[1], t[2]
            rec_credit = t[3] if len(t) > 3 else credit_id
            self.cur.execute("SELECT id FROM mb_recording WHERE gid = %s", (rec_gid,))
            row = self.cur.fetchone()
            if row:
                rec_id = row[0]
            else:
                rec_id = next(_ids)
                self.cur.execute("INSERT INTO mb_recording (id, gid, name, artist_credit, length) "
                                 "VALUES (%s, %s, %s, %s, %s)", (rec_id, rec_gid, title_, rec_credit, length))
            self.cur.execute("INSERT INTO mb_track (id, gid, recording, medium, position, number, name, "
                             "artist_credit, length) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                             (next(_ids), mbid(), rec_id, mid, pos, str(pos), title_, rec_credit, length))
        return rid


class Library:
    """An owned artist: albums, tracks with their files."""

    def __init__(self, cur, name: str):
        from uuid_utils import artist_uuid
        self.cur = cur
        self.name = name
        self.id = str(artist_uuid(name))
        cur.execute("INSERT INTO artists (id, name) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (self.id, name))

    def album(self, title: str, tracks: Sequence[tuple], directory: Optional[str] = None) -> str:
        """tracks: (title, seconds). Returns the album id."""
        from uuid_utils import album_uuid, track_uuid
        album_id = str(album_uuid(title, self.name))
        self.cur.execute("INSERT INTO albums (id, title) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                         (album_id, title))
        self.cur.execute("INSERT INTO album_artists (album_id, artist_id, role) VALUES (%s, %s, 'primary') "
                         "ON CONFLICT DO NOTHING", (album_id, self.id))
        directory = directory or f"/music/{self.name}/{title}"
        self.cur.execute("INSERT INTO album_variants (album_id, directory_path, raw_title) "
                         "VALUES (%s, %s, %s) RETURNING id", (album_id, directory, title))
        variant = self.cur.fetchone()[0]
        for n, (ttitle, seconds) in enumerate(tracks, start=1):
            tid = str(track_uuid(ttitle, self.name))
            self.cur.execute("INSERT INTO tracks (id, title) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                             (tid, ttitle))
            self.cur.execute("INSERT INTO track_artists (track_id, artist_id, role) "
                             "VALUES (%s, %s, 'primary') ON CONFLICT DO NOTHING", (tid, self.id))
            self.cur.execute("INSERT INTO media_files (track_id, album_variant_id, file_path, sample_rate, "
                             "bit_depth, duration_seconds, track_number, disc_number) "
                             "VALUES (%s, %s, %s, 44100, 16, %s, %s, 1)",
                             (tid, variant, f"{directory}/{n:02d}.flac", seconds, n))
        return album_id


def slice_fed(dsn: str, monkeypatch):
    """Point the pool, the sessions and mb_backend at `dsn` as a node with
    slices and no dump; returns an autocommit connection with the catalogue,
    the library and the answers emptied."""
    import psycopg2
    import psycopg2.pool
    from sqlalchemy.orm import sessionmaker
    import database
    import db_pool
    import mb_backend
    pool = psycopg2.pool.ThreadedConnectionPool(1, 8, dsn=dsn, options="-c timezone=UTC")
    engine = database.make_engine(dsn)
    monkeypatch.setattr(db_pool, "_pool", pool)
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=engine))
    import canon.content
    import canon.identity
    monkeypatch.setattr(canon.content, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr(mb_backend, "LOCAL_DUMP", True)
    conn = psycopg2.connect(dsn, options="-c timezone=UTC")
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("""TRUNCATE tracks, albums, artists, mb_artist, mb_artist_alias, mb_artist_credit,
                                mb_artist_credit_name, mb_recording, mb_track, mb_medium,
                                mb_release_group, mb_release, mbws_answers, mbws_artists,
                                mbws_release_groups, mbws_releases, mbws_recordings, mbws_credits,
                                mbws_artist_release_groups, mbws_queue, mb_refuted_bindings,
                                external_api_cooldown CASCADE""")
        cur.execute("DELETE FROM user_settings WHERE key = 'musicbrainz.db_version'")
    import api_cooldown
    api_cooldown._cache.clear()
    return conn, pool, engine
