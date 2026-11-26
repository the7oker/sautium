"""One performance in two copies is one track (CANON-TRACKS-1 §3, B-134),
and every fold keeps one id the same on every node (F2).

*Music for 18 Musicians* in its own folder titles its sections "Music for
18 Musicians: Section I", the box's Disc 4 "Section I": 27 rows for 14
positions. Two owned tracks fold when a file of each is within 2 s of the
other's length, their own prints are one material, and a word says they are
the same thing — MusicBrainz's (one recording, any albums) or the album's
(one position of two copies, titles alike once folded). The print alone is
no such word: an instrumental edition is one material with the vocal one at
some positions. Red before: nothing folded such tracks, and the credit
fold's keeper was the older row.

On a real PostgreSQL — the module's scratch database (test_tracklist_source's
fixtures).
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
from canon import content  # noqa: E402
from tests.test_tracklist_source import cur, library  # noqa: E402,F401
from uuid_utils import track_uuid  # noqa: E402

REICH = "Steve Reich"
# Prints the material rule reads as one material (an equal string) or as two
# (strings that differ and decode to nothing): the rule itself is
# desktop/p2p/material.py's, tested there.
PRINT, OTHER = "AQAAA" + "B" * 40, "AQAAA" + "C" * 40


@pytest.fixture(autouse=True)
def _sessions(cur, monkeypatch):
    """canon.content holds its own name of the session factory."""
    import database
    monkeypatch.setattr(content, "SessionLocal", database.SessionLocal)


def _md(root, folder, album, number, title, seconds, artist=REICH):
    return (f"{root}/{folder}",
            {"file_path": f"{root}/{folder}/{number:02d}. {title}.flac",
             "title": title, "artist": artist, "album_artist": artist, "album": album,
             "track_number": number, "disc_number": 1, "duration_seconds": seconds,
             "file_format": "FLAC", "is_lossless": True, "sample_rate": 44100, "bit_depth": 16})


def _import(entries):
    scanner.import_metadata(entries, sink=scanner.LOCAL_FILES, stats={})


def _track(cur, title, artist=REICH):
    cur.execute("SELECT id::text AS id FROM tracks WHERE id = %s", (str(track_uuid(title, artist)),))
    row = cur.fetchone()
    return row["id"] if row else None


def _print(cur, title, fp=PRINT, artist=REICH, seconds=None):
    """The track's own first-hand print, of its file's length."""
    cur.execute("""INSERT INTO analysis_sources (track_id, media_file_id, chromaprint, duration_seconds,
                                                 sample_rate, bit_depth, is_lossless)
                   SELECT mf.track_id, mf.id, %s, coalesce(%s, round(mf.duration_seconds)::int), 44100, 16, TRUE
                     FROM media_files mf WHERE mf.track_id = %s LIMIT 1""",
                (fp, seconds, str(track_uuid(title, artist))))


def _bound(cur, title, recording, artist=REICH):
    cur.execute("UPDATE media_files SET recording_mbid = %s WHERE track_id = %s",
                (recording, str(track_uuid(title, artist))))


def _tracks(cur):
    cur.execute("SELECT count(*) AS n FROM tracks t WHERE EXISTS "
                "(SELECT 1 FROM media_files mf WHERE mf.track_id = t.id)")
    return cur.fetchone()["n"]


def _two_copies(library, sections=("Section I", "Section II"), album="Music for 18 Musicians"):
    _import([_md(library, "ECM", album, n, f"Music for 18 Musicians: {t}", 238.5 + n)
             for n, t in enumerate(sections, 1)])
    _import([_md(library, "Works/Disc 4", album, n, t, 238.48 + n) for n, t in enumerate(sections, 1)])


def test_the_albums_word_folds_one_position_of_two_copies_and_the_print_decides(cur, library):
    _two_copies(library)
    for t in ("Section I", "Section II"):
        _print(cur, f"Music for 18 Musicians: {t}")
        _print(cur, t, PRINT if t == "Section I" else OTHER)
    assert _tracks(cur) == 4
    cur.execute("""INSERT INTO listening_history (track_id, media_file_id, started_at, completed)
                   SELECT track_id, id, now(), TRUE FROM media_files WHERE raw_track_name = 'Section I'""")
    st = content.fold_copy_twins()
    assert (st["merged"], st["apart"]) == (1, 1)            # Section II: the prints differ
    # no MusicBrainz name here: the smallest uuid keeps
    keeper = min(str(track_uuid("Section I", REICH)), str(track_uuid("Music for 18 Musicians: Section I", REICH)))
    cur.execute("SELECT DISTINCT track_id::text AS t FROM media_files WHERE track_number = 1")
    assert [r["t"] for r in cur.fetchall()] == [keeper]
    cur.execute("SELECT track_id::text AS t FROM listening_history")
    assert [r["t"] for r in cur.fetchall()] == [keeper]
    cur.execute("SELECT play_count FROM local_play_stats WHERE track_id = %s", (keeper,))
    assert cur.fetchone()["play_count"] == 1
    assert _tracks(cur) == 3
    assert content.fold_copy_twins()["merged"] == 0           # idempotent


def test_a_track_with_no_print_of_its_own_waits_and_a_fold_of_the_tracks_just_analysed_takes_it(
        cur, library):
    _two_copies(library, ("Section I",))
    _print(cur, "Music for 18 Musicians: Section I")
    assert content.fold_copy_twins()["waiting"] == 1
    _print(cur, "Section I")
    assert content.fold_copy_twins(track_ids=[_track(cur, "Section I")])["merged"] == 1


def test_musicbrainzs_word_folds_across_albums_and_names_the_keeper(cur, library):
    """A compilation of the same master beside the album: one recording,
    prints alike; the keeper is the title the recording's MusicBrainz name
    is. A remaster MusicBrainz keeps as one recording whose print differs
    stays apart."""
    artist = "Michael Jackson"
    _import([_md(library, "HIStory", "HIStory", 15, "Heal the World (Album Version)", 384.5, artist),
             _md(library, "HIStory", "HIStory", 16, "Vulture Culture (2024 Remaster)", 300.0, artist)])
    _import([_md(library, "Essential", "The Essential", 10, "Heal the World", 385.7, artist),
             _md(library, "Essential", "The Essential", 11, "Vulture Culture", 300.6, artist)])
    heal, vulture = str(uuid.uuid4()), str(uuid.uuid4())
    cur.execute("DELETE FROM mb_recording")
    cur.execute("INSERT INTO mb_recording (id, gid, name) VALUES (1, %s, 'Heal the World'), (2, %s, 'Vulture Culture')",
                (heal, vulture))
    for t in ("Heal the World (Album Version)", "Heal the World"):
        _bound(cur, t, heal, artist)
        _print(cur, t, artist=artist)
    _bound(cur, "Vulture Culture (2024 Remaster)", vulture, artist)
    _bound(cur, "Vulture Culture", vulture, artist)
    _print(cur, "Vulture Culture (2024 Remaster)", PRINT, artist)
    _print(cur, "Vulture Culture", OTHER, artist)
    st = content.fold_copy_twins()
    assert (st["merged"], st["apart"]) == (1, 1)
    cur.execute("SELECT DISTINCT track_id::text AS t FROM media_files WHERE raw_track_name LIKE 'Heal%%'")
    assert [r["t"] for r in cur.fetchall()] == [str(track_uuid("Heal the World", artist))]


def test_titles_that_differ_beyond_the_fold_and_two_positions_of_one_copy_stay_apart(cur, library):
    """Morcheeba's vocal and instrumental editions: one position, one
    length, prints alike — the titles say two things. Two positions of one
    copy bound to one recording and alike never fold either."""
    m = "Morcheeba"
    _import([_md(library, "Charango", "Charango", 5, "Charango [feat. Pacewon]", 242.5, m)])
    _import([_md(library, "Charango (Instrumental)", "Charango", 5, "Charango [instrumental]", 242.5, m)])
    _print(cur, "Charango [feat. Pacewon]", artist=m)
    _print(cur, "Charango [instrumental]", artist=m)
    v = "Владимир Высоцкий"
    rec = str(uuid.uuid4())
    _import([_md(library, "Box", "Box", 10, "Слухи", 190.4, v), _md(library, "Box", "Box", 21, "Слухи", 190.4, v)])
    cur.execute("UPDATE media_files SET recording_mbid = %s WHERE raw_track_name = 'Слухи'", (rec,))
    cur.execute("""INSERT INTO analysis_sources (track_id, media_file_id, chromaprint, duration_seconds)
                   SELECT track_id, id, %s, 190 FROM media_files WHERE raw_track_name = 'Слухи'""", (PRINT,))
    assert content.fold_copy_twins()["merged"] == 0
    assert _tracks(cur) == 4


def test_a_group_that_would_put_two_positions_of_one_copy_on_one_track_is_left(cur, library):
    """Two positions of one copy, one recording (a box repeating a song),
    and a third copy of it elsewhere: each pair alone would fold, the group
    would hold two positions of one copy — left as it is."""
    v, rec = "Владимир Высоцкий", str(uuid.uuid4())
    _import([_md(library, "Box", "Box", 10, "Слухи", 190.4, v), _md(library, "Box", "Box", 21, "Слухи", 190.4, v)])
    _import([_md(library, "Best", "Best", 3, "Слухи", 190.5, v)])
    cur.execute("UPDATE media_files SET recording_mbid = %s", (rec,))
    cur.execute("""INSERT INTO analysis_sources (track_id, media_file_id, chromaprint, duration_seconds)
                   SELECT DISTINCT ON (track_id) track_id, id, %s, 190 FROM media_files""", (PRINT,))
    st = content.fold_copy_twins()
    assert (st["merged"], st["vetoed"]) == (0, 1)


def test_titles_alike_fold_their_accents(cur, library):
    from canon.occurrence import titled_alike
    assert titled_alike("Gymnopédie No. 1", "Gymnopedie No. 1")
    assert titled_alike("Music for 18 Musicians: Section I", "Section I")
    assert not titled_alike("Charango [instrumental]", "Charango [feat. Pacewon]")


def test_the_credit_folds_keeper_is_the_same_on_every_node(cur, library):
    """F2: two rows of one album differing by a featuring credit fold into
    the smallest uuid, whichever came first — it was the older row."""
    a = "Bonobo"
    big, small = sorted([str(track_uuid("Eyesdown (feat. Andreya Triana)", a)), str(track_uuid("Eyesdown", a))],
                        reverse=True)
    first = "Eyesdown (feat. Andreya Triana)" if big == str(track_uuid("Eyesdown (feat. Andreya Triana)", a)) \
        else "Eyesdown"
    second = "Eyesdown" if first != "Eyesdown" else "Eyesdown (feat. Andreya Triana)"
    _import([_md(library, "Black Sands", "Black Sands", 3, first, 300.0, a)])        # the older row: big
    _import([_md(library, "Black Sands (HQP)", "Black Sands", 3, second, 300.5, a)])
    assert content.fold_credit_duplicates(dry_run=False)["merged"] == 1
    cur.execute("SELECT DISTINCT track_id::text AS t FROM media_files")
    assert [r["t"] for r in cur.fetchall()] == [small]
