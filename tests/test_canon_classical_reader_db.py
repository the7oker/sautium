"""The reader of CANON-CLASSICAL (DESIGN §1, §4.2): TITLE's values joined as
`Work: Movement`, every container's values as a list (no NUL join), the
barcode, a rights society never an artist, the read revision that reads every
file once more, and the split of a merged work file by file.

Real FLAC and MP3 files (ffmpeg, tags by mutagen) in a temporary music
folder, on the module's scratch database (test_scan_unreachable's fixtures).
Before this slice a file with two TITLE values minted its track from the
first ("Goldberg Variations, BWV 988" for all 32 movements), an ID3 v2.4
multi-value frame was stored NUL-joined (PostgreSQL refused the row), and a
re-read moved a merged work's listens and binding to whichever movement the
walk read last."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

psycopg2 = pytest.importorskip("psycopg2")
FLAC = pytest.importorskip("mutagen.flac").FLAC

import scanner  # noqa: E402
from tests.test_scan_unreachable import cur, library  # noqa: E402,F401
from uuid_utils import track_uuid  # noqa: E402

pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "ffmpeg"], capture_output=True).returncode, reason="no ffmpeg")


@pytest.fixture(scope="module")
def tone(tmp_path_factory):
    out = {}
    for ext in ("flac", "mp3"):
        path = tmp_path_factory.mktemp(ext) / f"tone.{ext}"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                        "-ar", "44100", str(path)], check=True)
        out[ext] = path
    return out


def _flac(tone, path: Path, **tags):
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(tone["flac"], path)
    f = FLAC(path)
    for k, v in tags.items():
        f[k] = v if isinstance(v, list) else [str(v)]
    f.save()
    return path


def _scan():
    return scanner.LibraryScanner().scan_and_import()


def _clean(cur):
    for table in ("listening_sessions", "listening_history", "local_play_stats", "track_mbids",
                  "album_genres", "album_artists", "track_artists", "artists", "genres"):
        cur.execute(f"DELETE FROM {table}")


def test_the_title_values_join_as_musicbrainz_writes_a_classical_title():
    assert scanner.joined_title(["Goldberg Variations, BWV 988", "Aria"]) == "Goldberg Variations, BWV 988: Aria"
    assert scanner.joined_title(["The Heart Of Noise", "Pt. 1"]) == "The Heart Of Noise: Pt. 1"
    assert scanner.joined_title(["Song"]) == "Song"
    assert scanner.joined_title(["Song", "song"]) == "Song"                    # the same value, folded
    assert scanner.joined_title(["Work", "Work: Movement"]) == "Work: Movement"  # the tagger's own join
    assert scanner.joined_title([]) is None


def test_a_rights_society_is_a_publishing_credit_never_an_artist():
    """T13 and §12.9 (Q17): the rule reads ` / ` alone, the form measured."""
    assert scanner.without_rights_societies("TONO / Manuscript") is None
    assert scanner.without_rights_societies("Trio Mediæval / GEMA") == "Trio Mediæval"
    assert scanner.without_rights_societies("Tono") == "Tono"                  # a whole name stays
    assert scanner.without_rights_societies("Simon & Garfunkel") == "Simon & Garfunkel"
    # CASH is Hong Kong's society, and these are artists: no other separator voids or drops
    assert scanner.without_rights_societies("Cash, Johnny") == "Cash, Johnny"
    assert scanner.without_rights_societies("Cash & Carry") == "Cash & Carry"
    assert scanner.without_rights_societies("Johnny Cash & Koda") == "Johnny Cash & Koda"
    assert scanner.without_rights_societies("Trio Mediæval; GEMA") == "Trio Mediæval; GEMA"
    # MusicBrainz types these labels Rights Society and they are not (scripts/make_rights_societies.py)
    assert scanner.without_rights_societies("Limelight / Anyone") == "Limelight / Anyone"


def test_two_title_values_read_as_one_title_with_the_barcode(cur, library, tone):
    _clean(cur)
    _flac(tone, library / "Musopen" / "01.flac", title=["Goldberg Variations, BWV 988", "Aria"],
          artist="Kimiko Ishizaka", album="Goldberg Variations", tracknumber=1, barcode="0 88807 23011 2")
    _scan()
    cur.execute("SELECT t.title, mf.raw_track_name, mf.barcode, mf.tags_read_rev FROM media_files mf "
                "JOIN tracks t ON t.id = mf.track_id")
    assert cur.fetchall() == [("Goldberg Variations, BWV 988: Aria", "Goldberg Variations, BWV 988: Aria",
                               "088807230112", scanner.TAGS_READ_REV)]


def test_an_id3_multi_value_title_imports_without_a_nul(cur, library, tone):
    from mutagen.id3 import ID3, TIT2, TPE1, TRCK
    _clean(cur)
    path = library / "Pod" / "01.mp3"
    path.parent.mkdir(parents=True)
    shutil.copy(tone["mp3"], path)
    tags = ID3()
    tags.add(TIT2(encoding=3, text=["Suite No. 1", "I. Prélude"]))
    tags.add(TPE1(encoding=3, text=["Yo-Yo Ma"]))
    tags.add(TRCK(encoding=3, text=["1"]))
    tags.save(path, v2_version=4)
    assert _scan()["added"] == 1
    cur.execute("SELECT raw_track_name FROM media_files")
    assert cur.fetchall() == [("Suite No. 1: I. Prélude",)]


def test_a_publishing_credit_falls_to_the_album_artist(cur, library, tone):
    _clean(cur)
    _flac(tone, library / "Aquilonis" / "01.flac", title="Ave Maris Stella", artist="TONO / Manuscript",
          albumartist="Trio Mediæval", album="Aquilonis", tracknumber=1)
    _scan()
    cur.execute("SELECT a.name FROM track_artists ta JOIN artists a ON a.id = ta.artist_id "
                "WHERE ta.role = 'primary'")
    assert cur.fetchall() == [("Trio Mediæval",)]


def test_a_merged_work_splits_file_by_file_on_the_next_read(cur, library, tone):
    """Read under the old reader (the first TITLE value, revision 0), the
    three movements are one track with three listens, one signed binding and
    the owned tracklist's three slots; the next scan reads them again — each
    file to its own movement's track with its own listen and its own slot,
    the binding gone, whatever the walk's order. A whole walk records the
    reader it read the library under (the start's re-read, main)."""
    import psycopg2.extras
    import sign_audio
    _clean(cur)
    for n, mvt in enumerate(("I. Allegro", "II. Largo", "III. Allegro"), 1):
        _flac(tone, library / "Spring" / f"0{n}.flac", title=["Concerto No. 1 in E major, RV 269", mvt],
              artist="Anne Akiko Meyers", album="The Four Seasons", tracknumber=n)
    with pytest.MonkeyPatch.context() as old_reader:
        old_reader.setattr(scanner, "joined_title", lambda values: values[0] if values else None)
        old_reader.setattr(scanner, "TAGS_READ_REV", 0)
        _scan()
    cur.execute("SELECT DISTINCT track_id::text FROM media_files")
    (merged,), = cur.fetchall()
    cur.execute("INSERT INTO listening_history (track_id, media_file_id, started_at, completed) "
                "SELECT track_id, id, now() - id * interval '1 hour', true FROM media_files")
    cur.execute("INSERT INTO track_mbids (recording_mbid, track_id, confidence, signature) "
                "VALUES ('2f9f2a0e-0000-4000-8000-000000000001', %s, 'overlap_verified', repeat('a', 128))",
                (merged,))
    cur.execute("UPDATE media_files SET recording_mbid = '2f9f2a0e-0000-4000-8000-000000000001'")
    slots = cur.connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    assert len(sign_audio._materialize_owned_tracklists(slots)) == 3
    cur.execute("SELECT value FROM user_settings WHERE key = %s", (scanner.TAGS_READ_KEY,))
    assert cur.fetchone() == (0,)
    # as if the positional pass had bound the copy: its to bind again after the split (round 3 M-A)
    cur.execute("UPDATE album_variants SET positional_bound = TRUE, release_mbid = gen_random_uuid(), "
                "positional_asked = 'x'")
    stats = _scan()
    assert stats["reread"] == 3 and stats.get("split") == 1
    cur.execute("""SELECT t.title, (SELECT count(*) FROM listening_history h WHERE h.track_id = t.id
                                      AND h.media_file_id = mf.id)
                     FROM media_files mf JOIN tracks t ON t.id = mf.track_id ORDER BY mf.track_number""")
    assert cur.fetchall() == [("Concerto No. 1 in E major, RV 269: I. Allegro", 1),
                              ("Concerto No. 1 in E major, RV 269: II. Largo", 1),
                              ("Concerto No. 1 in E major, RV 269: III. Allegro", 1)]
    cur.execute("SELECT count(*) FROM track_mbids")
    assert cur.fetchone() == (0,)
    cur.execute("SELECT count(*) FROM media_files WHERE recording_mbid IS NOT NULL")
    assert cur.fetchone() == (0,)
    cur.execute("SELECT count(*) FROM listening_history WHERE track_id = %s", (merged,))
    assert cur.fetchone() == (0,)
    cur.execute("SELECT track_id::text FROM media_files WHERE track_number = 2")
    assert cur.fetchone() == (str(track_uuid("Concerto No. 1 in E major, RV 269: II. Largo", "Anne Akiko Meyers")),)
    cur.execute("SELECT positional_bound, release_mbid, positional_asked FROM album_variants")
    assert cur.fetchall() == [(False, None, None)]
    # the merged track's slots went with the split: each movement takes its own
    cur.execute("SELECT count(*) FROM album_tracks WHERE track_id = %s", (merged,))
    assert cur.fetchone() == (0,)
    assert len(sign_audio._materialize_owned_tracklists(slots)) == 3
    cur.execute("SELECT at.position, at.track_id = mf.track_id FROM album_tracks at "
                "JOIN media_files mf ON mf.track_number = at.position ORDER BY at.position")
    assert cur.fetchall() == [(1, True), (2, True), (3, True)]
    cur.execute("SELECT value FROM user_settings WHERE key = %s", (scanner.TAGS_READ_KEY,))
    assert cur.fetchone() == (scanner.TAGS_READ_REV,)
    assert _scan()["reread"] == 0                    # read once
