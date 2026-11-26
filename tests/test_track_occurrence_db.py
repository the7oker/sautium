"""Two positions of one tracklist are two tracks (CANON-TRACKS-1 §2, B-135).

A track's id was its title and its primary artist alone, so the box's Disc 4
— "Pulses" (326 s) at track 1 and "Pulses" (371 s) at track 14 — was one
track: 14 files, 13 tracks, the page one row short and the closing Pulses
played as the opening one. The k-th position of a title in one tracklist is
now `song:{artist}:{title}#k` (tracks.occurrence): the scanner's import and
re-read, the copies held in part (completed from the medium their files are
bound to), the phantom mint, the slots minted or imported before, and every
rule that recomputes an id from a row.

On a real PostgreSQL — the module's scratch database, the pool and the
SQLAlchemy sessions pointed at it (test_tracklist_source's fixtures).
"""

import sys
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BACKEND = REPO / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

psycopg2 = pytest.importorskip("psycopg2")
pytest.importorskip("sqlalchemy")

import scanner  # noqa: E402
from canon import occurrence  # noqa: E402
from tests.test_tracklist_source import cur, library  # noqa: E402,F401
from uuid_utils import track_uuid  # noqa: E402

REICH = "Steve Reich"
DISC4 = [("Pulses", 326.47)] + [(f"Section {n}", 200.0 + i) for i, n in enumerate(
    ("I", "II", "IIIA", "IIIB", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI"), 1)] + [("Pulses", 370.76)]


def _md(root, folder, album, number, title, seconds, artist=REICH, fmt="FLAC", disc=1):
    ext = fmt.lower()
    return (f"{root}/{folder}",
            {"file_path": f"{root}/{folder}/{disc}-{number:02d}. {title}.{ext}",
             "title": title, "artist": artist, "album_artist": artist, "album": album,
             "track_number": number, "disc_number": disc, "duration_seconds": seconds,
             "file_format": fmt, "is_lossless": fmt == "FLAC", "sample_rate": 44100, "bit_depth": 16})


def _import(entries):
    scanner.import_metadata(entries, sink=scanner.LOCAL_FILES, stats={})


def _disc4(root, positions=None, folder="Works/Disc 4", album="Music for 18 Musicians"):
    return [_md(root, folder, album, n, t, sec) for n, (t, sec) in enumerate(DISC4, 1)
            if positions is None or n in positions]


def _on(cur, number, folder="Works/Disc 4"):
    cur.execute("""SELECT mf.id, mf.track_id::text AS track_id, t.occurrence FROM media_files mf
                     JOIN tracks t ON t.id = mf.track_id
                    WHERE mf.track_number = %s AND mf.file_path LIKE %s""", (number, f"%/{folder}/%"))
    return cur.fetchone()


def _tracks_of_copy(cur, folder="Works/Disc 4"):
    cur.execute("SELECT count(DISTINCT track_id) AS n FROM media_files WHERE file_path LIKE %s",
                (f"%/{folder}/%",))
    return cur.fetchone()["n"]


def test_two_movements_titled_alike_in_one_copy_are_two_tracks(cur, library):
    _import(_disc4(library))
    assert _tracks_of_copy(cur) == 14
    first, last = _on(cur, 1), _on(cur, 14)
    assert first["track_id"] == str(track_uuid("Pulses", REICH))           # every old id stays
    assert last["track_id"] == str(track_uuid("Pulses", REICH, 2)) and last["occurrence"] == 2
    from database import get_db_context
    with get_db_context() as db:                                              # a second settle moves nothing
        assert occurrence.settle_copies(db, complete=True)["moved"] == 0


def test_a_later_batch_puts_right_what_an_earlier_one_could_not_see(cur, library):
    """Track 14 read alone mints the first "Pulses"; track 1, read later,
    takes that place, and 14 moves to the second with its own listen."""
    _import(_disc4(library, {14}))
    alone = _on(cur, 14)
    assert alone["track_id"] == str(track_uuid("Pulses", REICH))
    cur.execute("""INSERT INTO listening_history (track_id, media_file_id, started_at, completed)
                   VALUES (%s, %s, now(), TRUE)""", (alone["track_id"], alone["id"]))
    _import(_disc4(library, {1}))
    assert _on(cur, 1)["track_id"] == str(track_uuid("Pulses", REICH))
    assert _on(cur, 14)["track_id"] == str(track_uuid("Pulses", REICH, 2))
    cur.execute("SELECT track_id::text AS t FROM listening_history")
    assert [r["t"] for r in cur.fetchall()] == [str(track_uuid("Pulses", REICH, 2))]


def test_one_position_in_two_formats_is_one_track_and_two_untitled_apart_by_04s_are_two(cur, library):
    """Thomas Köner's shape: positions 6 and 9 untitled, 0.4 s apart — two
    pieces, two tracks; a FLAC and an MP3 of one position stay one."""
    koner = "Thomas Köner"
    _import([_md(library, "Nunatak", "Nunatak Gongamur", 6, "[untitled]", 208.8, koner),
             _md(library, "Nunatak", "Nunatak Gongamur", 9, "[untitled]", 208.4, koner),
             _md(library, "Nunatak", "Nunatak Gongamur", 1, "Intro", 60.0, koner),
             _md(library, "Nunatak", "Nunatak Gongamur", 1, "Intro", 60.0, koner, fmt="MP3")])
    assert _on(cur, 6, "Nunatak")["track_id"] == str(track_uuid("[untitled]", koner))
    assert _on(cur, 9, "Nunatak")["track_id"] == str(track_uuid("[untitled]", koner, 2))
    cur.execute("SELECT count(DISTINCT track_id) AS n FROM media_files WHERE track_number = 1")
    assert cur.fetchone()["n"] == 1


def test_the_release_splits_the_tracks_minted_before_the_rule(cur, library, scratch_dsn):
    """track_occurrences_v1 on a database the old rule minted: both "Pulses"
    files on one track, a listen made from the closing one, the tag slots
    as the materialiser wrote them; once split, a second run moves nothing."""
    import db_migrate
    import sign_audio
    _import(_disc4(library))
    plain, second = str(track_uuid("Pulses", REICH)), str(track_uuid("Pulses", REICH, 2))
    last = _on(cur, 14)
    cur.execute("UPDATE media_files SET track_id = %s, is_analysis_source = FALSE WHERE id = %s",
                (plain, last["id"]))
    cur.execute("DELETE FROM tracks WHERE id = %s", (second,))
    cur.execute("""INSERT INTO listening_history (track_id, media_file_id, started_at, completed)
                   VALUES (%s, %s, now(), TRUE)""", (plain, last["id"]))
    sign_audio._materialize_owned_tracklists(cur)
    # the old track's one binding, stamped on both files, and the copy the
    # positional pass bound
    rec = str(uuid.uuid4())
    cur.execute("UPDATE media_files SET recording_mbid = %s WHERE track_id = %s", (rec, plain))
    cur.execute("""INSERT INTO track_mbids (recording_mbid, track_id, confidence, matcher_version)
                   VALUES (%s, %s, 'positional', 2)""", (rec, plain))
    cur.execute("UPDATE album_variants SET positional_bound = TRUE, release_mbid = %s", (str(uuid.uuid4()),))
    cur.execute("UPDATE artists SET last_mb_sync = now()")
    conn = psycopg2.connect(scratch_dsn)
    try:
        out = db_migrate._split_occurrences(conn)
        assert out["copies"]["moved"] == 1
        assert _on(cur, 14)["track_id"] == second
        # one recording stood for both movements: gone, for the canon to bind
        # each on its own evidence, and its pass woken
        cur.execute("SELECT count(*) AS n FROM track_mbids")
        assert cur.fetchone()["n"] == 0
        cur.execute("SELECT count(*) AS n FROM media_files WHERE recording_mbid IS NOT NULL")
        assert cur.fetchone()["n"] == 0
        cur.execute("SELECT bool_or(positional_bound) AS b FROM album_variants")
        assert cur.fetchone()["b"] is False
        cur.execute("SELECT count(*) AS n FROM artists WHERE last_mb_sync IS NULL")
        assert cur.fetchone()["n"] == 1
        cur.execute("SELECT track_id::text AS t FROM listening_history")
        assert [r["t"] for r in cur.fetchall()] == [second]
        assert db_migrate._split_occurrences(conn)["copies"]["moved"] == 0
    finally:
        conn.close()
    sign_audio._materialize_owned_tracklists(cur)
    cur.execute("""SELECT at.position, at.track_id::text AS t FROM album_tracks at
                    WHERE at.position IN (1, 14) ORDER BY at.position""")
    assert [(r["position"], r["t"]) for r in cur.fetchall()] == [(1, plain), (14, second)]


def test_the_slices_of_a_cue_image_take_their_own_places(cur, library):
    """A CUE image's slices share their image's path: each is minted at its
    own place among the sheet's positions (review round 1, H1)."""
    path = f"{library}/Concerti/Concerti.flac"
    entries = []
    for n, (title, start) in enumerate((("I. Allegro", 0.0), ("II. Largo", 200.0),
                                        ("I. Allegro", 400.0), ("II. Largo", 600.0)), 1):
        d, md = _md(library, "Concerti", "Concerti", n, title, 200.0, "Il Giardino")
        md.update(file_path=path, cue_start_seconds=start, cue_end_seconds=start + 200.0)
        entries.append((d, md))
    _import(entries)
    cur.execute("SELECT track_number AS n, track_id::text AS t FROM media_files ORDER BY track_number")
    assert [r["t"] for r in cur.fetchall()] == [
        str(track_uuid("I. Allegro", "Il Giardino")), str(track_uuid("II. Largo", "Il Giardino")),
        str(track_uuid("I. Allegro", "Il Giardino", 2)), str(track_uuid("II. Largo", "Il Giardino", 2))]


def test_three_repeats_arriving_out_of_order_end_at_their_places_with_their_analysis(cur, library):
    """Positions 6 and 9 read first (the first and second "[untitled]"),
    position 3 later: each ends at its place — 3 the first, 6 the second, 9
    the third — and the analysis made from 6 and 9 goes with each (review
    round 1, M4: from the last position down, so a track a later position
    leaves is free for the earlier one)."""
    koner = "Thomas Köner"
    _import([_md(library, "Nunatak", "Nunatak Gongamur", n, "[untitled]", 200.0 + n, koner) for n in (6, 9)])
    for n in (6, 9):
        cur.execute("""INSERT INTO analysis_sources (track_id, media_file_id, chromaprint, duration_seconds)
                       SELECT track_id, id, %s, %s FROM media_files WHERE track_number = %s""",
                    (f"AQAAA{n}", 200 + n, n))
    _import([_md(library, "Nunatak", "Nunatak Gongamur", 3, "[untitled]", 203.0, koner)])
    want = {3: 1, 6: 2, 9: 3}
    for n, k in want.items():
        assert _on(cur, n, "Nunatak")["track_id"] == str(track_uuid("[untitled]", koner, k))
    cur.execute("""SELECT mf.track_number AS n, s.chromaprint FROM analysis_sources s
                     JOIN media_files mf ON mf.track_id = s.track_id ORDER BY 1""")
    assert [(r["n"], r["chromaprint"]) for r in cur.fetchall()] == [(6, "AQAAA6"), (9, "AQAAA9")]


def _medium(cur, names_at: dict, recordings: dict, credit=7, medium=40):
    """A MusicBrainz medium as a slice or the dump holds it: {position: name},
    {position: recording gid}."""
    cur.execute("INSERT INTO mb_medium (id, release, position) VALUES (%s, 4, 1)", (medium,))
    for position, name in names_at.items():
        rid = 1000 + position
        cur.execute("INSERT INTO mb_recording (id, gid, name, artist_credit) VALUES (%s, %s, %s, %s)",
                    (rid, recordings[position], name, credit))
        cur.execute("""INSERT INTO mb_track (id, gid, recording, medium, position, number, name,
                                             artist_credit) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    (rid, str(uuid.uuid4()), rid, medium, position, str(position), name, credit))


def test_a_copy_held_in_part_takes_its_place_from_the_medium_it_is_bound_to(cur, library):
    """A node without track 1 gives track 14 the id a node with the whole
    copy gives it, once the file is bound to MusicBrainz's medium; an
    unbound partial copy keeps the first place (the residue)."""
    from database import get_db_context
    for table in ("mb_track", "mb_recording", "mb_medium"):
        cur.execute(f"DELETE FROM {table}")
    recs = {n: str(uuid.uuid4()) for n in range(1, 15)}
    _medium(cur, {n: f"Music for 18 Musicians: {t}" for n, (t, _s) in enumerate(DISC4, 1)}, recs)
    _import(_disc4(library, {14}))
    assert _on(cur, 14)["track_id"] == str(track_uuid("Pulses", REICH))
    with get_db_context() as db:
        assert occurrence.settle_copies(db, complete=True)["moved"] == 0      # not bound yet
    cur.execute("UPDATE media_files SET recording_mbid = %s WHERE track_number = 14", (recs[14],))
    cur.execute("""INSERT INTO track_mbids (recording_mbid, track_id, confidence, matcher_version)
                   VALUES (%s, %s, 'overlap_verified', 2)""", (recs[14], str(track_uuid("Pulses", REICH))))
    with get_db_context() as db:
        assert occurrence.settle_copies(db)["moved"] == 0                      # the files alone see no earlier one
        assert occurrence.settle_copies(db, complete=True)["moved"] == 1
        db.commit()
    assert _on(cur, 14)["track_id"] == str(track_uuid("Pulses", REICH, 2))
    # the binding follows its file: the stamp was this file's own recording
    cur.execute("SELECT track_id::text AS t FROM track_mbids WHERE recording_mbid = %s", (recs[14],))
    assert cur.fetchone()["t"] == str(track_uuid("Pulses", REICH, 2))
    with get_db_context() as db:
        assert occurrence.settle_copies(db, complete=True)["moved"] == 0
        assert occurrence.settle_copies(db)["moved"] == 0                      # a copy in part: never shrunk
    _import(_disc4(library, {1}))                                               # track 1 arrives
    assert _on(cur, 1)["track_id"] == str(track_uuid("Pulses", REICH))
    assert _on(cur, 14)["track_id"] == str(track_uuid("Pulses", REICH, 2))


def test_a_track_added_to_a_bound_copy_leaves_its_bindings_where_they_are(cur, library):
    """Track 14 added later to a copy already bound: the batch mints it on
    the first "Pulses" and the settle moves it to the second — a fresh file,
    no collapse: the first's binding and stamp stay (review round 2, M5)."""
    _import(_disc4(library, {1}))
    rec = str(uuid.uuid4())
    plain = str(track_uuid("Pulses", REICH))
    cur.execute("UPDATE media_files SET recording_mbid = %s WHERE track_number = 1", (rec,))
    cur.execute("""INSERT INTO track_mbids (recording_mbid, track_id, confidence, matcher_version)
                   VALUES (%s, %s, 'overlap_verified', 2)""", (rec, plain))
    _import(_disc4(library, {14}))
    assert _on(cur, 14)["track_id"] == str(track_uuid("Pulses", REICH, 2))
    cur.execute("SELECT track_id::text AS t FROM track_mbids")
    assert [r["t"] for r in cur.fetchall()] == [plain]
    cur.execute("SELECT recording_mbid::text AS r FROM media_files WHERE track_number = 1")
    assert cur.fetchone()["r"] == rec


def test_one_disc_of_a_box_kept_as_an_album_reads_its_own_medium_alone(cur, library):
    """A copy of a box's disc 4 kept as an album of its own (DISCNUMBER 4):
    the box's disc 1 is not this tracklist — its "I. Allegro" does not make
    this one the second (review round 2, M4)."""
    from database import get_db_context
    for table in ("mb_track", "mb_recording", "mb_medium"):
        cur.execute(f"DELETE FROM {table}")
    cur.execute("INSERT INTO mb_medium (id, release, position) VALUES (40, 4, 1), (41, 4, 4)")
    rec = str(uuid.uuid4())
    for rid, gid, medium, pos, name in ((1, str(uuid.uuid4()), 40, 1, "I. Allegro"),
                                        (2, str(uuid.uuid4()), 41, 1, "II. Largo"),
                                        (3, rec, 41, 2, "I. Allegro")):
        cur.execute("INSERT INTO mb_recording (id, gid, name, artist_credit) VALUES (%s, %s, %s, 7)",
                    (rid, gid, name))
        cur.execute("""INSERT INTO mb_track (id, gid, recording, medium, position, number, name, artist_credit)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, 7)""", (rid, str(uuid.uuid4()), rid, medium, pos,
                                                                  str(pos), name))
    _import([_md(library, "Mozart Box Vol 4", "Mozart Box Vol. 4", 2, "I. Allegro", 300.0, "Il Giardino",
                 disc=4)])
    cur.execute("UPDATE media_files SET recording_mbid = %s", (rec,))
    with get_db_context() as db:
        assert occurrence.settle_copies(db, complete=True)["moved"] == 0
    cur.execute("SELECT track_id::text AS t FROM media_files")
    assert cur.fetchone()["t"] == str(track_uuid("I. Allegro", "Il Giardino"))


def test_a_rename_and_the_identity_rule_keep_the_place(cur, library, scratch_dsn, monkeypatch):
    """Every rule that recomputes a track's id from its row takes the
    occurrence: an artist's rename re-keys the closing Pulses to the second
    place under the new name, and the identity re-normalization finds
    nothing to change."""
    from canon.identity import recanonicalize_artist
    from canon.migrations import _renormalize_tracks
    from config import settings
    from database import get_db_context
    from urllib.parse import urlparse
    from uuid_utils import artist_uuid
    _import(_disc4(library))
    with get_db_context() as db:
        recanonicalize_artist(db, str(artist_uuid(REICH)), "Steve Reich & Musicians")
        db.commit()
    assert _on(cur, 14)["track_id"] == str(track_uuid("Pulses", "Steve Reich & Musicians", 2))
    assert _on(cur, 1)["track_id"] == str(track_uuid("Pulses", "Steve Reich & Musicians"))
    url = urlparse(scratch_dsn)
    for key, value in dict(postgres_host=url.hostname, postgres_port=url.port,
                           postgres_user=url.username, postgres_password=url.password,
                           postgres_db=url.path.lstrip("/")).items():
        monkeypatch.setattr(settings, key, value)
    with get_db_context() as db:
        assert _renormalize_tracks(db, dry_run=True)["changing"] == 0


def _slot_album(cur, title, slots):
    """An album with a primary artist and its slots {position: (title, track id)}."""
    artist, album = str(uuid.uuid4()), str(uuid.uuid4())
    cur.execute("INSERT INTO artists (id, name) VALUES (%s, 'Il Giardino')", (artist,))
    cur.execute("INSERT INTO albums (id, title) VALUES (%s, %s)", (album, title))
    for position, (t, track) in slots.items():
        cur.execute("INSERT INTO tracks (id, title) VALUES (%s, %s) ON CONFLICT DO NOTHING", (track, t))
        cur.execute("""INSERT INTO track_artists (track_id, artist_id, role) VALUES (%s, %s, 'primary')
                       ON CONFLICT DO NOTHING""", (track, artist))
        cur.execute("""INSERT INTO album_tracks (album_id, track_id, disc, position, source)
                       VALUES (%s, %s, 1, %s, 'mb')""", (album, track, position))
    return album


def test_slots_minted_before_the_rule_split_and_a_second_pass_does_nothing(cur, library, scratch_dsn):
    allegro = str(track_uuid("I. Allegro", "Il Giardino"))
    album = _slot_album(cur, "Concerti", {1: ("I. Allegro", allegro), 2: ("II. Largo", str(uuid.uuid4())),
                                           4: ("I. Allegro", allegro), 7: ("I. Allegro", allegro)})
    conn = psycopg2.connect(scratch_dsn)
    try:
        assert occurrence.settle_slots(conn)["slots"] == 2
        conn.commit()
        assert occurrence.settle_slots(conn)["slots"] == 0
    finally:
        conn.close()
    cur.execute("""SELECT at.position, at.track_id::text AS t, tr.occurrence, a.name
                     FROM album_tracks at JOIN tracks tr ON tr.id = at.track_id
                     JOIN track_artists ta ON ta.track_id = tr.id AND ta.role = 'primary'
                     JOIN artists a ON a.id = ta.artist_id
                    WHERE at.album_id = %s AND at.position <> 2 ORDER BY at.position""", (album,))
    assert [(r["position"], r["t"], r["occurrence"], r["name"]) for r in cur.fetchall()] == [
        (1, allegro, 1, "Il Giardino"),
        (4, str(track_uuid("I. Allegro", "Il Giardino", 2)), 2, "Il Giardino"),
        (7, str(track_uuid("I. Allegro", "Il Giardino", 3)), 3, "Il Giardino")]


def test_the_mint_gives_each_slot_of_a_title_its_place(cur, library, monkeypatch):
    import discography
    import mb_answers
    artist, album, group = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    cur.execute("INSERT INTO artists (id, name) VALUES (%s, 'Il Giardino')", (artist,))
    cur.execute("INSERT INTO albums (id, title, musicbrainz_id) VALUES (%s, 'Concerti', %s)", (album, group))
    cur.execute("INSERT INTO album_artists (album_id, artist_id, role) VALUES (%s, %s, 'primary')",
                (album, artist))
    titles = ["I. Allegro", "II. Largo", "III. Presto", "I. Allegro", "II. Adagio"]
    release = {"release_mbid": str(uuid.uuid4()), "tracks": [
        {"title": t, "disc": 1, "position": n, "recording_mbid": str(uuid.uuid4()),
         "length_ms": 200000 + n, "credit": "Il Giardino",
         "credit_names": [{"name": "Il Giardino", "gid": None}]} for n, t in enumerate(titles, 1)]}
    monkeypatch.setattr(mb_answers, "required", lambda: False)
    monkeypatch.setattr(discography.mb, "fetch_release_tracklists", lambda rg: [release])
    assert discography._persist_phantom_tracklist(album, artist, "Il Giardino", group) == 5
    assert discography._persist_phantom_tracklist(album, artist, "Il Giardino", group) == 5
    cur.execute("SELECT position, track_id::text AS t FROM album_tracks WHERE album_id = %s "
                "AND position IN (1, 4) ORDER BY position", (album,))
    assert [r["t"] for r in cur.fetchall()] == [str(track_uuid("I. Allegro", "Il Giardino")),
                                                str(track_uuid("I. Allegro", "Il Giardino", 2))]
    cur.execute("SELECT occurrence FROM tracks WHERE id = %s",
                (str(track_uuid("I. Allegro", "Il Giardino", 2)),))
    assert cur.fetchone()["occurrence"] == 2


def test_a_seed_made_before_the_rule_lands_split(cur, library, scratch_dsn):
    """insert_structural (the seed's and a share's import) takes the mint's
    rule: an older bundle's two slots of one title on one track are split,
    the moved slot's seal shed (it no longer says what its author signed)."""
    import seed_import
    artist, album = str(uuid.uuid4()), str(uuid.uuid4())
    allegro = str(track_uuid("I. Allegro", "Il Giardino"))
    structural = {
        "artists": [{"id": artist, "name": "Il Giardino"}],
        "albums": [{"id": album, "title": "Concerti"}],
        "tracks": [{"id": allegro, "title": "I. Allegro"}],
        "album_tracks": [{"album_id": album, "track_id": allegro, "disc": 1, "position": p,
                          "author_pubkey": "ab" * 32, "signature": "cd" * 64} for p in (1, 4)],
        "track_artists": [{"track_id": allegro, "artist_id": artist, "role": "primary"}],
    }
    conn = psycopg2.connect(scratch_dsn)
    try:
        seed_import.insert_structural(conn, structural)
    finally:
        conn.close()
    cur.execute("SELECT position, track_id::text AS t, signature FROM album_tracks "
                "WHERE album_id = %s ORDER BY position", (album,))
    assert [(r["position"], r["t"], r["signature"]) for r in cur.fetchall()] == [
        (1, allegro, "cd" * 64), (4, str(track_uuid("I. Allegro", "Il Giardino", 2)), None)]
