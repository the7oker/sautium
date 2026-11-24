"""A file the catalogue holds is read again when its stat moves, in place,
and the folder — not the file — is the unit of album identity
(LIBRARY-WATCH-DESIGN §3.4, §6; the owner's answers §15.1).

Real FLAC files (ffmpeg, tags by mutagen) in a temporary music folder, on
the module's scratch database (test_scan_unreachable's fixtures). Before
this, a re-tag never reached the catalogue (the scan compared paths only), a
canon move of an album's first files sent the later ones to a second album
in the same folder, and a compilation's first file stayed an album of its
own."""

import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

psycopg2 = pytest.importorskip("psycopg2")
FLAC = pytest.importorskip("mutagen.flac").FLAC

import scanner  # noqa: E402
from tests.test_scan_unreachable import cur, library  # noqa: E402,F401
from uuid_utils import album_uuid, track_uuid  # noqa: E402

pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "ffmpeg"], capture_output=True).returncode, reason="no ffmpeg")


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    path = tmp_path_factory.mktemp("flac") / "tone.flac"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                    "-ar", "44100", "-sample_fmt", "s16", str(path)], check=True)
    f = FLAC(path)
    f.save(padding=lambda info: 8192)          # room for an edit in place
    return path


def _flac(template, path: Path, title, artist, album=None, albumartist=None, track=None,
          disc=None, genre=None, totals=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(template, path)
    _tag(path, title=title, artist=artist, album=album, albumartist=albumartist, tracknumber=track,
         discnumber=disc, genre=genre, tracktotal=totals)
    return path


def _tag(path: Path, keep_mtime=False, **tags):
    st = os.stat(path)
    f = FLAC(path)
    for k, v in tags.items():
        if v is None:
            f.pop(k, None)
        else:
            f[k] = str(v)
    f.save(padding=lambda info: info.padding)
    if keep_mtime:
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))


def _scan(**kw):
    return scanner.LibraryScanner().scan_and_import(**kw)


def _rows(cur):
    cur.execute("""SELECT mf.id, mf.file_path, t.title, al.title, av.directory_path, al.id::text
                     FROM media_files mf JOIN tracks t ON t.id = mf.track_id
                     JOIN album_variants av ON av.id = mf.album_variant_id
                     JOIN albums al ON al.id = av.album_id ORDER BY mf.file_path""")
    return cur.fetchall()


def _albums(cur):
    cur.execute("SELECT count(DISTINCT av.album_id), count(*) FROM album_variants av "
                "WHERE EXISTS (SELECT 1 FROM media_files mf WHERE mf.album_variant_id = av.id)")
    return cur.fetchone()


def _clean(cur):
    for table in ("listening_sessions", "listening_history", "album_genres", "album_artists",
                  "track_artists", "artists", "genres"):
        cur.execute(f"DELETE FROM {table}")


def test_a_retag_with_its_mtime_kept_is_read_again_and_its_track_follows(cur, library, template):
    _clean(cur)
    f = _flac(template, library / "A" / "LP" / "01.flac", "Sunrse", "Aurora", "LP")
    assert _scan()["added"] == 1
    (mf, _p, title, *_r), = _rows(cur)
    cur.execute("INSERT INTO listening_history (track_id, media_file_id, started_at, completed) "
                "SELECT track_id, id, now(), true FROM media_files")
    size = os.path.getsize(f)
    _tag(f, keep_mtime=True, title="Sunrise")       # the editor keeps the mtime
    assert os.path.getsize(f) == size               # in the padding: only the ctime moved

    stats = _scan()
    assert (stats["added"], stats["reread"]) == (0, 1)
    (mf2, _p, title2, *_r), = _rows(cur)
    assert (mf2, title2) == (mf, "Sunrise")         # the same row, the track renamed
    cur.execute("SELECT t.title FROM listening_history lh JOIN tracks t ON t.id = lh.track_id")
    assert cur.fetchall() == [("Sunrise",)]
    assert _scan()["reread"] == 0                   # read once


def test_a_file_of_two_copies_retagged_alone_moves_alone(cur, library, template):
    _clean(cur)
    _flac(template, library / "A" / "LP" / "01.flac", "Song", "Aurora", "LP")
    b = _flac(template, library / "A" / "Best Of" / "07.flac", "Song", "Aurora", "Best Of")
    _scan()
    cur.execute("INSERT INTO listening_history (track_id, started_at, completed) "
                "SELECT DISTINCT track_id, now(), true FROM media_files")
    _tag(b, title="Song (Live)")
    _scan()
    titles = sorted(r[2] for r in _rows(cur))
    assert titles == ["Song", "Song (Live)"]
    cur.execute("SELECT t.title FROM listening_history lh JOIN tracks t ON t.id = lh.track_id")
    assert cur.fetchall() == [("Song",)]            # a listen is of the track, which keeps it


def test_an_album_retitled_in_all_its_files_renames_the_album(cur, library, template):
    _clean(cur)
    files = [_flac(template, library / "A" / "LP" / f"{i:02d}.flac", f"T{i}", "Aurora", "Albm",
                   track=i, genre="Rock") for i in (1, 2, 3)]
    _scan()
    old = _rows(cur)[0][5]
    cur.execute("INSERT INTO listening_sessions (origin, origin_album_id) VALUES ('album', %s)", (old,))
    for f in files:
        _tag(f, album="Album", genre="Jazz")
    stats = _scan()
    assert stats["reread"] == 3
    rows = _rows(cur)
    assert {r[3] for r in rows} == {"Album"} and _albums(cur) == (1, 1)
    new = rows[0][5]
    assert new == str(album_uuid("Album", "Aurora"))
    cur.execute("SELECT origin_album_id::text FROM listening_sessions")
    assert cur.fetchall() == [(new,)]
    cur.execute("""SELECT g.name, ag.count FROM album_genres ag JOIN genres g ON g.id = ag.genre_id
                    WHERE ag.album_id = %s AND ag.source = 'filetag'""", (new,))
    assert cur.fetchall() == [("Jazz", 3)]


@pytest.mark.parametrize("order", ["forward", "reverse"])
def test_a_compilation_copied_file_by_file_ends_as_one_album(cur, library, template, order):
    _clean(cur)
    names = [(1, "Aurora"), (2, "Björk"), (3, "Caribou")]
    if order == "reverse":
        names.reverse()
    for i, artist in names:                         # no album artist: the folder decides
        _flac(template, library / "Mixes" / "Summer" / f"{i:02d}.flac", f"T{i}", artist, "Summer",
              track=i)
        _scan()
    rows = _rows(cur)
    assert len(rows) == 3 and _albums(cur) == (1, 1)
    assert rows[0][5] == str(album_uuid("Summer", "Various Artists"))


def _canon_rename(cur, album_id, title, artist="Aurora"):
    """What an edition rename of the canon does: the album re-keyed, its
    copies' scan-time title kept."""
    from canon.identity import _update_album_uuid
    import database
    new = str(album_uuid(title, artist))
    with database.get_db_context() as db:
        _update_album_uuid(db, album_id, new)
        db.execute(scanner.text("UPDATE albums SET title = :t WHERE id = :a"), {"t": title, "a": new})
        db.commit()
    return new


def test_files_after_a_canon_move_join_the_moved_album(cur, library, template):
    _clean(cur)
    d = library / "A" / "X (Remastered)"
    for i in (1, 2, 3):
        _flac(template, d / f"{i:02d}.flac", f"T{i}", "Aurora", "X (Remastered)", track=i)
    _scan()
    moved = _canon_rename(cur, _rows(cur)[0][5], "X")
    for i in (4, 5):
        _flac(template, d / f"{i:02d}.flac", f"T{i}", "Aurora", "X (Remastered)", track=i)
    _scan()
    assert {r[5] for r in _rows(cur)} == {moved} and _albums(cur) == (1, 1)


@pytest.mark.parametrize("titles", [("X", "X"), ("X (Disc 1)", "X (Disc 2)")])
def test_a_disc_folder_arriving_later_joins_its_sibling_s_album(cur, library, template, titles):
    _clean(cur)
    for i in (1, 2):
        _flac(template, library / "A" / "X" / "CD1" / f"{i:02d}.flac", f"A{i}", "Aurora", titles[0],
              track=i, disc=1)
    _scan()
    moved = _canon_rename(cur, _rows(cur)[0][5], "X [Deluxe]")
    for i in (1, 2):
        _flac(template, library / "A" / "X" / "CD2" / f"{i:02d}.flac", f"B{i}", "Aurora", titles[1],
              track=i, disc=2)
    _scan()
    rows = _rows(cur)
    assert {r[5] for r in rows} == {moved}
    assert _albums(cur) == (1, 2)                   # one album, a copy per disc folder


def test_two_full_copies_side_by_side_are_not_taken_for_discs(cur, library, template):
    """Sibling folders join only as discs: two copies of a disc 1 are two
    copies, each by its own key."""
    _clean(cur)
    _flac(template, library / "A" / "X (FLAC)" / "01.flac", "A1", "Aurora", "X", track=1, disc=1)
    _scan()
    moved = _canon_rename(cur, _rows(cur)[0][5], "X [Deluxe]")
    _flac(template, library / "A" / "X (MP3)" / "01.flac", "A1", "Aurora", "X", track=1, disc=1)
    _scan()
    assert {r[5] for r in _rows(cur)} == {moved, str(album_uuid("X", "Aurora"))}


def test_arrival_is_the_file_s_birth_here_never_its_mtime(cur, library, template):
    _clean(cur)
    f = _flac(template, library / "A" / "LP" / "01.flac", "T1", "Aurora", "LP")
    os.utime(f, ns=(1_400_000_000 * 10**9, 1_400_000_000 * 10**9))   # a copy keeps its source's mtime
    _scan()
    cur.execute("SELECT arrived_at > now() - interval '1 hour', file_modified_at < '2015-01-01' "
                "FROM media_files")
    assert cur.fetchone() == (True, True)
    cur.execute("SELECT arrived_at > now() - interval '1 hour' FROM album_variants")
    assert cur.fetchone() == (True,)


def test_a_read_again_clears_a_refused_decode(cur, library, template):
    _clean(cur)
    f = _flac(template, library / "A" / "LP" / "01.flac", "T1", "Aurora", "LP")
    _scan()
    cur.execute("UPDATE media_files SET decode_refused = true")
    _scan()
    cur.execute("SELECT decode_refused FROM media_files")
    assert cur.fetchone() == (True,)                # unchanged: not read, still refused
    _tag(f, comment="the copy finished")
    _scan()
    cur.execute("SELECT decode_refused FROM media_files")
    assert cur.fetchone() == (False,)


def test_a_cue_sheet_edited_with_the_image_untouched_is_read_again(cur, library, template):
    _clean(cur)
    d = library / "A" / "Live"
    img = _flac(template, d / "image.flac", "image", "Aurora", "Live")
    sheet = d / "image.cue"

    def write(title):
        sheet.write_text(f'PERFORMER "Aurora"\nTITLE "Live"\nFILE "image.flac" WAVE\n'
                         f'  TRACK 01 AUDIO\n    TITLE "{title}"\n    INDEX 01 00:00:00\n'
                         f'  TRACK 02 AUDIO\n    TITLE "Two"\n    INDEX 01 00:01:00\n')
    write("Opne")
    _scan()
    ids = [r[0] for r in _rows(cur)]
    write("Open")
    stats = _scan()
    assert stats["reread"] == 2
    rows = _rows(cur)
    assert [r[0] for r in rows] == ids and sorted(r[2] for r in rows) == ["Open", "Two"]
    assert img.exists()


def test_a_copy_from_before_its_group_was_kept_is_filled_not_moved(cur, library, template):
    """A copy scanned before raw_title existed (2026-09-28), its album since
    re-keyed by the canon: its group is backfilled, and its first re-read by
    this release moves nothing back to the raw key (review round 1, H1)."""
    _clean(cur)
    for i in (1, 2):
        _flac(template, library / "A" / "X (Remastered)" / f"{i:02d}.flac", f"T{i}", "Aurora",
              "X (Remastered)", track=i)
    _scan()
    moved = _canon_rename(cur, _rows(cur)[0][5], "X")
    cur.execute("UPDATE album_variants SET raw_title = NULL, raw_artist = NULL")
    cur.execute("UPDATE media_files SET stat_mtime_ns = NULL")     # read before this release
    assert scanner.backfill_variant_groups(cur) == 1
    cur.execute("SELECT raw_title, raw_artist FROM album_variants")
    assert cur.fetchone() == ("X (Remastered)", "Aurora")
    cur.execute("UPDATE album_variants SET raw_title = NULL")       # and were it left unfilled
    _scan()
    assert {r[5] for r in _rows(cur)} == {moved} and _albums(cur) == (1, 1)


def test_a_deluxe_edition_beside_the_album_is_not_taken_for_its_disc(cur, library, template):
    _clean(cur)
    _flac(template, library / "A" / "Album" / "01.flac", "A1", "Aurora", "Album", track=1, disc=1)
    _scan()
    moved = _canon_rename(cur, _rows(cur)[0][5], "Album [2009]")
    for d in (1, 2):
        _flac(template, library / "A" / "Album (Deluxe Edition)" / f"{d}-01.flac", f"D{d}", "Aurora",
              "Album (Deluxe Edition)", track=1, disc=d)
    _scan()
    albums = {r[4].rsplit("/", 1)[-1]: r[5] for r in _rows(cur)}
    assert albums["Album"] == moved and albums["Album (Deluxe Edition)"] != moved


def test_a_compilation_copied_file_by_file_keeps_no_first_artist_credit(cur, library, template):
    _clean(cur)
    for i, artist in ((1, "Aurora"), (2, "Björk"), (3, "Caribou")):
        _flac(template, library / "Mixes" / "Summer" / f"{i:02d}.flac", f"T{i}", artist, "Summer",
              track=i)
        _scan()
    cur.execute("""SELECT ar.name FROM album_artists aa JOIN artists ar ON ar.id = aa.artist_id
                    WHERE aa.album_id = %s AND aa.role = 'primary'""",
                (str(album_uuid("Summer", "Various Artists")),))
    assert cur.fetchall() == [("Various Artists",)]


def test_a_read_again_drops_what_was_registered_of_the_file_mid_copy(cur, library, template):
    """A source the passes registered while the file was being copied (its
    print of the part there) holds no record once the decode was refused:
    the file's re-read, its stat moved, drops it (review round 1, M6)."""
    _clean(cur)
    f = _flac(template, library / "A" / "LP" / "01.flac", "T1", "Aurora", "LP")
    _scan()
    cur.execute("""INSERT INTO analysis_sources (track_id, chromaprint, duration_seconds, imported,
                                                 media_file_id, is_lossless, bit_depth)
                   SELECT track_id, 'AQAAA', 3, false, id, true, 16 FROM media_files""")
    _tag(f, comment="the copy finished")
    _scan()
    cur.execute("SELECT count(*) FROM analysis_sources")
    assert cur.fetchone() == (0,)


def test_a_retitled_copy_takes_the_analysis_of_its_material(cur, library, template):
    """The file moves to its new track first, with the analysis made of it;
    only then is the source unlinked for the changed bytes, on the track it
    is now on (review round 2, H-A)."""
    _clean(cur)
    _flac(template, library / "A" / "LP" / "01.flac", "Song", "Aurora", "LP")
    b = _flac(template, library / "A" / "Best Of" / "07.flac", "Song", "Aurora", "Best Of")
    _scan()
    cur.execute("""WITH s AS (
                     INSERT INTO analysis_sources (track_id, chromaprint, duration_seconds, imported,
                                                   media_file_id, is_lossless, bit_depth)
                     SELECT track_id, 'AQAAA', 3, false, id, true, 16 FROM media_files
                      WHERE file_path LIKE %s RETURNING id, track_id)
                   INSERT INTO audio_features (track_id, analysis_source_id) SELECT track_id, id FROM s""",
                ("%Best Of%",))
    _tag(b, title="Song (Live)")
    _scan()
    cur.execute("""SELECT t.title, s.media_file_id IS NULL FROM analysis_sources s
                     JOIN tracks t ON t.id = s.track_id""")
    assert cur.fetchall() == [("Song (Live)", True)]
    cur.execute("SELECT t.title FROM audio_features af JOIN tracks t ON t.id = af.track_id")
    assert cur.fetchall() == [("Song (Live)",)]


def test_a_change_of_ctime_alone_keeps_the_analysis_linked(cur, library, template):
    """A chmod, a hard-link backup, an archive bit: the file is read again,
    its bytes are the same — its sources stay, and the canon has nothing new
    to read (review round 2, M-B)."""
    _clean(cur)
    f = _flac(template, library / "A" / "LP" / "01.flac", "T1", "Aurora", "LP")
    _scan()
    cur.execute("""INSERT INTO analysis_sources (track_id, chromaprint, duration_seconds, imported,
                                                 media_file_id, is_lossless, bit_depth)
                   SELECT track_id, 'AQAAA', 3, false, id, true, 16 FROM media_files""")
    cur.execute("UPDATE media_files SET content_at = '2026-01-01'")
    os.chmod(f, 0o600)
    assert _scan()["reread"] == 1
    cur.execute("SELECT count(*) FROM analysis_sources WHERE media_file_id IS NOT NULL")
    assert cur.fetchone() == (1,)
    cur.execute("SELECT content_at::date::text FROM media_files")
    assert cur.fetchone() == ("2026-01-01",)
    _tag(f, tracknumber=5)
    _scan()
    cur.execute("SELECT content_at > '2026-01-02' FROM media_files")
    assert cur.fetchone() == (True,)


def test_a_copy_moved_onto_a_track_with_its_own_analysis_leaves_its_old_link(cur, library, template):
    """The track the tags now name holds its own analysis, so the file's
    stays with the old track — unlinked from a file no longer its own, and
    relinked by its print (review round 3, outside the diff)."""
    _clean(cur)
    _flac(template, library / "A" / "LP" / "01.flac", "Song", "Aurora", "LP")
    b = _flac(template, library / "A" / "Best Of" / "07.flac", "Song", "Aurora", "Best Of")
    _flac(template, library / "A" / "Live" / "01.flac", "Song (Live)", "Aurora", "Live")
    _scan()
    cur.execute("""INSERT INTO analysis_sources (track_id, chromaprint, duration_seconds, imported,
                                                 media_file_id, is_lossless, bit_depth)
                   SELECT track_id, 'AQAAA', 3, false, id, true, 16 FROM media_files
                    WHERE file_path LIKE '%%/Best Of/%%' OR file_path LIKE '%%/Live/%%'""")
    _tag(b, keep_mtime=True, title="Song (Live)")
    _scan()
    cur.execute("""SELECT t.title, s.media_file_id IS NULL FROM analysis_sources s
                     JOIN tracks t ON t.id = s.track_id ORDER BY 1""")
    assert cur.fetchall() == [("Song", True), ("Song (Live)", False)]


def test_a_rip_replaced_in_place_with_the_same_tags_is_read_again_by_the_canon(cur, library, template):
    """Other bytes under the same tags (another rip, another length): the
    canon matches recordings by length, so the watermark moves (review
    round 3)."""
    _clean(cur)
    f = _flac(template, library / "A" / "LP" / "01.flac", "T1", "Aurora", "LP")
    _scan()
    cur.execute("UPDATE media_files SET content_at = '2026-01-01'")
    st = os.stat(f)
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert _scan()["reread"] == 1
    cur.execute("SELECT content_at > '2026-01-02' FROM media_files")
    assert cur.fetchone() == (True,)


def test_a_sheet_that_goes_takes_its_slices_tracklist_with_it(cur, library, template):
    """The CUE supersede removes the slices' rows, and with them the slots
    their tags wrote (scanner.drop_tag_slots): they held the slices' tracks
    as an album's with no file — and the first position against the whole
    image that takes it (PI-NODE-2D item 5)."""
    import psycopg2.extras
    import sign_audio
    _clean(cur)
    d = library / "A" / "Live"
    _flac(template, d / "image.flac", "Live (whole)", "Aurora", "Live", track=1)
    sheet = d / "image.cue"
    sheet.write_text('PERFORMER "Aurora"\nTITLE "Live"\nFILE "image.flac" WAVE\n'
                     '  TRACK 01 AUDIO\n    TITLE "Open"\n    INDEX 01 00:00:00\n'
                     '  TRACK 02 AUDIO\n    TITLE "Two"\n    INDEX 01 00:01:00\n')
    _scan()

    def materialise():
        with cur.connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c:
            sign_audio._materialize_owned_tracklists(c)
        cur.execute("""SELECT t.title, at.position FROM album_tracks at
                         JOIN tracks t ON t.id = at.track_id ORDER BY at.position""")
        return cur.fetchall()
    assert materialise() == [("Open", 1), ("Two", 2)]

    sheet.unlink()
    assert _scan()["superseded"] == 2
    cur.execute("SELECT title FROM tracks ORDER BY 1")
    assert cur.fetchall() == [("Live (whole)",)]
    assert materialise() == [("Live (whole)", 1)]
    cur.execute("SELECT DISTINCT source::text FROM album_tracks")
    assert cur.fetchall() == [("tag",)]


def _album_rows(cur):
    cur.execute("""SELECT al.title, (SELECT count(*) FROM album_variants av WHERE av.album_id = al.id),
                          ARRAY(SELECT at.source::text FROM album_tracks at WHERE at.album_id = al.id
                                 ORDER BY at.position)
                     FROM albums al ORDER BY 1""")
    return cur.fetchall()


def _materialise(cur):
    import psycopg2.extras
    import sign_audio
    with cur.connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c:
        sign_audio._materialize_owned_tracklists(c)


def test_an_album_retagged_whole_follows_its_copy_once_its_tracklist_was_written(cur, library, template):
    """A title fixed in all an album's files renames the album. The slots a
    signing pass had written from those files' tags counted as a tracklist
    the album held apart from its copy, so the old album stayed behind with
    no file (PI-NODE-2D item 5: the shape a removal left, by a re-tag)."""
    _clean(cur)
    files = [_flac(template, library / "A" / "LP" / f"{i:02d}.flac", f"T{i}", "Aurora", "LP", track=i)
             for i in (1, 2)]
    _scan()
    _materialise(cur)
    assert _album_rows(cur) == [("LP", 1, ["tag", "tag"])]
    for f in files:
        _tag(f, album="LP (fixed)")
    _scan()
    assert _album_rows(cur) == [("LP (fixed)", 1, ["tag", "tag"])]


def test_a_copy_retagged_out_of_an_album_musicbrainz_wrote_takes_its_own_slots_only(cur, library, template):
    """The album MusicBrainz wrote a tracklist for stays as the phantom it
    was; the slot the copy's tags added to it goes with the copy."""
    _clean(cur)
    files = [_flac(template, library / "A" / "LP" / f"{i:02d}.flac", f"T{i}", "Aurora", "LP", track=i)
             for i in (1, 2, 3)]
    _scan()
    cur.execute("""INSERT INTO album_tracks (album_id, track_id, disc, position)
                   SELECT av.album_id, mf.track_id, 1, mf.track_number
                     FROM media_files mf JOIN album_variants av ON av.id = mf.album_variant_id
                    WHERE mf.track_number < 3""")
    _materialise(cur)
    assert _album_rows(cur) == [("LP", 1, ["mb", "mb", "tag"])]
    for f in files:
        _tag(f, album="LP (fixed)")
    _scan()
    assert _album_rows(cur) == [("LP", 0, ["mb", "mb"]), ("LP (fixed)", 1, [])]
