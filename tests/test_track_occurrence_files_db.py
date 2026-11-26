"""A re-read moves each movement titled alike to its own place (CANON-TRACKS-1
§2, review round 1 H2).

Real FLAC files in a temporary music folder, scanned and re-read by the
scanner itself (test_composer_credits_db's harness). The box's two "Pulses"
are two tracks; a fix of the disc's artist tag re-homes the first to the new
artist's first "Pulses" and the second to its second — never through the
first's track, where a merge would have left what was the closing
movement's (its lyrics, its analysis, its listens with no file) on the
opening one.
"""

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

psycopg2 = pytest.importorskip("psycopg2")
pytest.importorskip("mutagen")

from tests.test_composer_credits_db import (  # noqa: E402,F401
    _clean, _flac, _retag, _scan, pytestmark, templates)
from tests.test_scan_unreachable import cur, library  # noqa: E402,F401
from uuid_utils import track_uuid  # noqa: E402


def _track_of(cur, name):
    cur.execute("SELECT track_id::text FROM media_files WHERE file_path LIKE %s", (f"%/{name}",))
    return cur.fetchone()[0]


def test_a_retag_moves_each_movement_to_its_own_place_with_what_is_its_own(cur, library, templates):
    _clean(cur)
    disc = library / "Reich" / "Disc 4"
    for n, title in ((1, "Pulses"), (2, "Section I"), (3, "Pulses")):
        _flac(templates, disc / f"0{n}.flac", title, "Steve Reich", "Music for 18 Musicians", None, n)
    _scan()
    second = str(track_uuid("Pulses", "Steve Reich", 2))
    assert _track_of(cur, "03.flac") == second
    # what is the closing movement's and no file's: a merge through the
    # opening track would have left it there
    cur.execute("INSERT INTO track_lyrics (track_id, source, instrumental) VALUES (%s, 'lrclib', TRUE)",
                (second,))
    cur.execute("""INSERT INTO listening_history (track_id, media_file_id, started_at, completed)
                   SELECT track_id, id, now(), TRUE FROM media_files WHERE file_path LIKE '%%/03.flac'""")
    for n in (1, 2, 3):
        _retag(disc / f"0{n}.flac", artist="Steve Reich and Musicians")
    _scan()
    closing = str(track_uuid("Pulses", "Steve Reich and Musicians", 2))
    assert _track_of(cur, "01.flac") == str(track_uuid("Pulses", "Steve Reich and Musicians"))
    assert _track_of(cur, "03.flac") == closing
    cur.execute("SELECT track_id::text FROM track_lyrics")
    assert cur.fetchall() == [(closing,)]
    cur.execute("SELECT track_id::text FROM listening_history")
    assert cur.fetchall() == [(closing,)]
    cur.execute("SELECT occurrence FROM tracks WHERE id = %s", (closing,))
    assert cur.fetchone()[0] == 2


def test_a_file_whose_track_number_is_gone_is_read_again_where_it_was(cur, library, templates):
    """A tag editor cleared one file's TRACKNUMBER: the re-read keeps the
    place the file was read at — it never fails the scan (review round 2,
    H1: a None among the positions stopped every later scan)."""
    _clean(cur)
    disc = library / "Reich" / "Disc 4"
    for n, title in ((1, "Pulses"), (2, "Section I"), (3, "Pulses")):
        _flac(templates, disc / f"0{n}.flac", title, "Steve Reich", "Music for 18 Musicians", None, n)
    _scan()
    _retag(disc / "03.flac", tracknumber=None, comment="edited")
    _scan()
    assert _track_of(cur, "03.flac") == str(track_uuid("Pulses", "Steve Reich", 2))
