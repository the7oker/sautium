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
