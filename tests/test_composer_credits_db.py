"""The composer as a credit, read from the files' own tags (CREDITS-1 step 2b;
CREDITS-1-DESIGN §2–§5, §7 T1–T13, the design session's answers §12).

Real FLAC / MP3 / M4A / WavPack files (ffmpeg, tags by mutagen) in a
temporary music folder, scanned into the module's scratch database
(test_scan_unreachable's fixtures); the HQPlayer library through a
hand-written LibraryGet answer, as test_hqp_library does. Red on the tree
before 2b: nothing read COMPOSER, so "Bach" found no file credited only as
composer, no page named one, and a v2.4 frame of two values failed the
file's import on a NUL."""

import json
import os
import shutil
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
REPO = BACKEND.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

psycopg2 = pytest.importorskip("psycopg2")
mutagen = pytest.importorskip("mutagen")
from mutagen.apev2 import APEv2  # noqa: E402
from mutagen.flac import FLAC  # noqa: E402
from mutagen.id3 import ID3, TALB, TCOM, TIT2, TPE1, TPE2  # noqa: E402
from mutagen.mp4 import MP4  # noqa: E402

import scanner  # noqa: E402
from tests.test_scan_unreachable import cur, library  # noqa: E402,F401
from uuid_utils import artist_uuid  # noqa: E402

pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "ffmpeg"], capture_output=True).returncode, reason="no ffmpeg")

BACH = "Johann Sebastian Bach"
AKAMUS = "Akademie für Alte Musik Berlin"


@pytest.fixture(scope="module")
def templates(tmp_path_factory):
    d = tmp_path_factory.mktemp("tone")
    out = {}
    for ext, args in (("flac", ["-sample_fmt", "s16"]), ("mp3", []), ("m4a", ["-c:a", "aac"]),
                      ("wv", ["-c:a", "wavpack"])):
        path = d / f"tone.{ext}"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=2", "-ar", "44100", *args, str(path)], check=True)
        out[ext] = path
    return out


def _copy(templates, path: Path, ext="flac") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(templates[ext], path)
    return path


def _flac(templates, path, title, artist, album="Album", composers=None, track=None, albumartist=None):
    _copy(templates, path)
    f = FLAC(path)
    f["title"], f["artist"], f["album"] = title, artist, album
    if albumartist:
        f["albumartist"] = albumartist
    if track:
        f["tracknumber"] = str(track)
    if composers is not None:
        f["composer"] = list(composers)
    f.save()
    return path


def _retag(path, **tags):
    f = FLAC(path)
    for k, v in tags.items():
        if v is None:
            f.pop(k, None)
        else:
            f[k] = v if isinstance(v, list) else [v]
    f.save()


def _scan(**kw):
    return scanner.LibraryScanner().scan_and_import(**kw)


def _clean(cur):
    for table in ("listening_history", "album_genres", "album_artists", "track_artists",
                  "track_credits", "hqp_library_files", "album_variants", "hqp_endpoints",
                  "artist_bios", "artist_tags", "similar_artists", "external_metadata",
                  "artist_mbids", "artists", "genres"):
        cur.execute(f"DELETE FROM {table}")


def _credits(cur):
    """{track title: [composer names in position order]}"""
    cur.execute("""SELECT t.title, a.name FROM track_credits tc
                     JOIN tracks t ON t.id = tc.track_id JOIN artists a ON a.id = tc.artist_id
                    WHERE tc.role = 'composer' ORDER BY t.title, tc.position""")
    out = {}
    for title, name in cur.fetchall():
        out.setdefault(title, []).append(name)
    return out


def _artist_id(cur, name):
    cur.execute("SELECT id::text FROM artists WHERE name = %s", (name,))
    row = cur.fetchone()
    return row[0] if row else None


# -- T8: the tag forms --------------------------------------------------------

def test_every_container_names_its_composers_in_its_own_form(templates, tmp_path):
    flac = _copy(templates, tmp_path / "a.flac")
    f = FLAC(flac)
    f["composer"] = [BACH, "Carl Philipp Emanuel Bach"]
    f.save()
    v24 = _copy(templates, tmp_path / "v24.mp3", "mp3")
    t = ID3()
    t.add(TCOM(encoding=3, text=["Lennon", "McCartney"]))
    t.save(v24, v2_version=4)
    v23 = _copy(templates, tmp_path / "v23.mp3", "mp3")
    t = ID3()
    t.add(TCOM(encoding=3, text=["Lennon", "McCartney"]))
    t.save(v23, v2_version=3)               # v2.3 has no list: "Lennon/McCartney"
    m4a = _copy(templates, tmp_path / "a.m4a", "m4a")
    m = MP4(m4a)
    m["\xa9wrt"] = ["Arvo Pärt"]
    m.save()
    wv = _copy(templates, tmp_path / "a.wv", "wv")
    ape = APEv2()
    ape["Composer"] = "Steve Reich\x00Philip Glass"
    ape.save(wv)

    names = {p.name: scanner.composer_names(mutagen.File(p)) for p in (flac, v24, v23, m4a, wv)}
    assert names == {"a.flac": [BACH, "Carl Philipp Emanuel Bach"],
                     "v24.mp3": ["Lennon", "McCartney"], "v23.mp3": ["Lennon", "McCartney"],
                     "a.m4a": ["Arvo Pärt"], "a.wv": ["Steve Reich", "Philip Glass"]}


def test_a_value_splits_on_semicolons_and_a_spaced_slash_only(templates, tmp_path):
    flac = _copy(templates, tmp_path / "a.flac")
    f = FLAC(flac)
    f["composer"] = ["Steve Reich; Philip Glass", "AC/DC", "Lennon, McCartney",
                     "Rodgers & Hammerstein", "John Adams / Terry Riley", "AC/DC", "TONO / Manuscript"]
    f.save()
    assert scanner.composer_names(mutagen.File(flac)) == [
        "Steve Reich", "Philip Glass", "AC/DC", "Lennon, McCartney", "Rodgers & Hammerstein",
        "John Adams", "Terry Riley"]        # each once; a publishing credit is nobody
    from credits import NAME_MAX, split_names
    assert split_names(["x" * (NAME_MAX + 1), "Real Name"]) == ["Real Name"]


def test_a_cue_sheet_names_a_slices_composer_as_its_performer():
    import cue_sheet
    parsed = cue_sheet.parse_cue("""REM COMPOSER "Sheet Rem"
SONGWRITER "Arvo Pärt"
FILE "x.flac" WAVE
  TRACK 01 AUDIO
    TITLE "Fratres"
    SONGWRITER "Arvo Pärt; Gidon Kremer"
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "Spiegel"
    INDEX 01 05:00:00
  TRACK 03 AUDIO
    TITLE "Tabula"
    REM COMPOSER "Benjamin Britten"
    INDEX 01 09:00:00
""")
    tracks = parsed["files"][0][1]
    assert parsed["songwriter"] == "Arvo Pärt" and parsed["rem_composer"] == "Sheet Rem"
    assert [(t["songwriter"], t["rem_composer"]) for t in tracks] == [
        ("Arvo Pärt; Gidon Kremer", None), (None, None), (None, "Benjamin Britten")]
    sheet = cue_sheet.CueSheet(cue_path=Path("x.cue"), audio_path=Path("x.flac"), title="T",
                               performer="P", songwriter="Arvo Pärt", date=None, genre=None,
                               catalog=None, tracks=[])
    image = {"composers": ["From The Image"], "artist": "P"}
    slices = [cue_sheet.CueTrack(number=1, title="a", performer=None, songwriter="A; B", isrc=None,
                                 start_seconds=0.0, end_seconds=1.0),
              cue_sheet.CueTrack(number=2, title="b", performer=None, songwriter=None, isrc=None,
                                 start_seconds=1.0, end_seconds=None)]
    assert [cue_sheet.synthesize_metadata(image, sheet, s, 2.0)["composers"] for s in slices] == [
        ["A", "B"], ["Arvo Pärt"]]
    no_sheet = cue_sheet.CueSheet(**{**sheet.__dict__, "songwriter": None})
    assert cue_sheet.synthesize_metadata(image, no_sheet, slices[1], 2.0)["composers"] == ["From The Image"]


# -- T10: a v2.4 frame of two values imports by its first ---------------------

def test_an_mp3_whose_artist_frame_holds_two_values_imports(cur, library, templates):
    _clean(cur)
    mp3 = _copy(templates, library / "X" / "01.mp3", "mp3")
    t = ID3()
    t.add(TIT2(encoding=3, text=["Song"]))
    t.add(TPE1(encoding=3, text=["Artist A", "Artist B"]))
    t.add(TPE2(encoding=3, text=["Artist A", "Artist B"]))
    t.add(TALB(encoding=3, text=["Record"]))
    t.save(mp3, v2_version=4)
    stats = _scan()
    assert (stats["added"], stats["errors"]) == (1, 0)
    cur.execute("SELECT raw_artist, raw_album_artist FROM media_files")
    assert cur.fetchall() == [("Artist A", "Artist A")]


# -- T1, T13: "Bach" finds a file credited only as composer ---------------------

def _names(q, corpus="all"):
    from routers import discovery
    return {r["artist"]: r for r in discovery.run_search(q=q, target="artist", limit=50,
                                                          corpus=corpus)["results"]}


def test_bach_finds_a_file_credited_only_as_composer(cur, library, templates):
    _clean(cur)
    _flac(templates, library / "Akamus" / "Brandenburg" / "01.flac", "Concerto No. 1: I. Allegro",
          AKAMUS, "Brandenburg Concertos", composers=[BACH])
    _scan()
    assert _credits(cur) == {"Concerto No. 1: I. Allegro": [BACH]}
    for q in ("Bach", "bach, johann"):
        for corpus in ("all", "owned"):
            hit = _names(q, corpus).get(BACH)
            assert hit is not None and hit["is_owned"] is True and hit["composer_only"] is True, (q, corpus)
    assert _names("Akademie")[AKAMUS]["composer_only"] is False
    # no library artist (library_stats counts performers) and no phantom (T13)
    cur.execute("SELECT total_artists FROM library_stats")
    assert cur.fetchone()[0] == 1
    from sql_queries import ARTIST_OWNED
    cur.execute(f"SELECT a.name FROM artists a WHERE NOT {ARTIST_OWNED}")
    assert cur.fetchall() == []


# -- T2: a composer engages nothing ---------------------------------------------

def test_a_composer_is_no_engaged_artist_even_after_his_page_fetched_his_bio(
        cur, library, templates, scratch_dsn, monkeypatch):
    _clean(cur)
    path = _flac(templates, library / "Akamus" / "B" / "01.flac", "Air", AKAMUS, "B", composers=[BACH])
    _scan()
    bach, akamus = _artist_id(cur, BACH), _artist_id(cur, AKAMUS)
    # analysis held on the track: the announce tail's own test
    cur.execute("SELECT track_id FROM media_files")
    track = cur.fetchone()[0]
    cur.execute("INSERT INTO audio_features (track_id, bpm, signature) VALUES (%s, 120, %s)",
                (track, "s" * 128))       # a sealed record: HELD_RECORD
    cur.execute("INSERT INTO listening_history (track_id, started_at, completed) VALUES (%s, now(), true)",
                (track,))

    import lastfm
    from routers import artists as artists_router
    asked = []

    def info(self, name, fetch_similar=True):
        asked.append((name, fetch_similar))
        return {"bio": {"content": f"{name} was a composer.", "summary": "A composer."},
                "tags": [{"name": "baroque", "count": 100}], "similar": [], "listeners": 1}
    monkeypatch.setattr(lastfm.LastFmService, "get_artist_info", info)
    done = threading.Event()
    monkeypatch.setattr(artists_router, "_wake_artist_clients", done.set)
    monkeypatch.setattr(artists_router, "_bio_asked", set())
    assert artists_router._composer_bio(bach, BACH) is True
    assert done.wait(30)
    assert asked == [(BACH, False)]                      # a bio, never similars
    cur.execute("SELECT summary FROM artist_bios WHERE artist_id = %s", (bach,))
    assert cur.fetchone() is not None
    assert artists_router._composer_bio(bach, BACH) is False     # once: the bio is there

    from sql_queries import ARTIST_ENGAGED_SET
    cur.execute(f"SELECT artist_id::text FROM ({ARTIST_ENGAGED_SET}) e")
    assert {r[0] for r in cur.fetchall()} == {akamus}
    from desktop.p2p import lb_slice_queries, mb_slice_queries, sync_queries
    conn = psycopg2.connect(scratch_dsn)
    try:
        assert sync_queries.split_engaged(conn, [bach, akamus])[0] == [akamus]
        # each set holds the performer of the same track (it can fail) and not the composer
        tail = sync_queries.get_announce_tail_uuids(conn, 100)
        assert akamus in tail and bach not in tail
        assert bach not in sync_queries.get_rare_search_uuids(conn)
        pending = [str(n).lower() for n in mb_slice_queries.pending_slice_names(conn)]
        assert AKAMUS.lower() in pending and BACH.lower() not in pending
        cur.execute("INSERT INTO artist_mbids (mbid, artist_id, confidence) VALUES (%s, %s, 'name_exact')",
                    (str(uuid.uuid4()), bach))
        assert all(m[0] not in _mbids(cur, bach) for m in lb_slice_queries.pending_slice_mbids(conn))
    finally:
        conn.close()
    fetched = []
    monkeypatch.setattr(lastfm.LastFmService, "fetch_and_store_similar",
                        lambda self, db, aid, name: fetched.append(name) or {"stored": 0, "status": "ok"})
    lastfm.backfill_similar()
    assert BACH not in fetched and AKAMUS in fetched


def _mbids(cur, artist):
    cur.execute("SELECT mbid::text FROM artist_mbids WHERE artist_id = %s", (artist,))
    return {r[0] for r in cur.fetchall()}


# -- T3: one row for a composer who performs; the Composed shelf ----------------

def test_a_singer_songwriter_is_one_row_and_composed_holds_only_the_cover(cur, library, templates):
    _clean(cur)
    _flac(templates, library / "Joni" / "Blue" / "01.flac", "River", "Joni Mitchell", "Blue",
          composers=["Joni Mitchell"])
    _flac(templates, library / "Others" / "Covers" / "01.flac", "Both Sides Now", "Judy Collins",
          "Wildflowers", composers=["Joni Mitchell"])
    _scan()
    cur.execute("SELECT count(*) FROM artists WHERE name = 'Joni Mitchell'")
    assert cur.fetchone()[0] == 1
    from routers.artists import get_artist
    import routers.artists as artists_router
    page = get_artist(_artist_id(cur, "Joni Mitchell"))
    assert [a["title"] for a in page["albums"]] == ["Blue"]
    assert [a["title"] for a in page["composed"]] == ["Wildflowers"]
    assert page["bio_pending"] is False          # a performer: the background step's
    assert artists_router._bio_inflight == set()


# -- T4: the album page's lines -------------------------------------------------

def test_the_album_page_names_the_shared_composers_once_and_the_rest_per_track(cur, library, templates):
    from routers.albums import get_album
    _clean(cur)
    d = library / "Hewitt" / "Bach"
    _flac(templates, d / "01.flac", "Partita", "Angela Hewitt", "Bach Arrangements", [BACH], 1)
    _flac(templates, d / "02.flac", "Chaconne", "Angela Hewitt", "Bach Arrangements",
          [BACH, "Ferruccio Busoni"], 2)
    d2 = library / "Kremer" / "Recital"
    for i, who in enumerate(("Arvo Pärt", "Philip Glass", "Steve Reich", "John Adams", "Terry Riley"), 1):
        _flac(templates, d2 / f"0{i}.flac", f"Piece {i}", "Gidon Kremer", "Recital", [who], i)
    _flac(templates, library / "Plain" / "LP" / "01.flac", "Song", "Plain", "LP", None, 1)
    _scan()
    cur.execute("SELECT al.title, al.id::text FROM albums al")
    albums = dict(cur.fetchall())

    page = get_album(albums["Bach Arrangements"], variant_id=None)
    assert [c["name"] for c in page["composers"]] == [BACH]
    assert {t["title"]: [c["name"] for c in t["composers"]] for t in page["tracks"]} == {
        "Partita": [], "Chaconne": ["Ferruccio Busoni"]}
    assert page["composers"][0]["id"] == _artist_id(cur, BACH)

    recital = get_album(albums["Recital"], variant_id=None)
    assert recital["composers"] == []
    assert [[c["name"] for c in t["composers"]] for t in recital["tracks"]] == [
        ["Arvo Pärt"], ["Philip Glass"], ["Steve Reich"], ["John Adams"], ["Terry Riley"]]
    plain = get_album(albums["LP"], variant_id=None)
    assert plain["composers"] == [] and plain["tracks"][0]["composers"] == []


# -- T5: derived from the files --------------------------------------------------

def test_credits_derive_from_the_files_and_follow_every_change(cur, library, templates):
    _clean(cur)
    a = _flac(templates, library / "Gould" / "Goldberg" / "01.flac", "Aria", "Glenn Gould",
              "Goldberg Variations", ["J. S. Bach"], 1)
    _scan()
    before = _credits(cur)
    assert before == {"Aria": ["J. S. Bach"]}
    assert _scan()["reread"] == 0 and _credits(cur) == before          # idempotent

    # a corrected tag replaces, and the name it no longer gives leaves no row (Q3)
    _retag(a, composer=BACH)
    assert _scan()["reread"] == 1
    assert _credits(cur) == {"Aria": [BACH]}
    assert _artist_id(cur, "J. S. Bach") is None

    # a second copy with another composer unions in, the best copy first
    b = _copy(templates, library / "Gould" / "Goldberg (Hi-Res)" / "01.flac")
    shutil.copy(a, b)
    _retag(b, composer=["Anonymous", BACH])
    _scan()
    cur.execute("UPDATE media_files SET sample_rate = 96000, bit_depth = 24 WHERE file_path LIKE %s",
                ("%Hi-Res%",))
    import credits
    from database import get_db_context
    cur.execute("SELECT DISTINCT track_id FROM media_files")
    (track,), = cur.fetchall()
    with get_db_context() as db:
        credits.derive_composers(db, [track])
        db.commit()
    assert _credits(cur) == {"Aria": ["Anonymous", BACH]}

    # a confirmed prune of the hi-res copy derives from what is left
    b.unlink()
    cur.execute("SELECT id FROM media_files WHERE file_path LIKE %s", ("%Hi-Res%",))
    (gone,), = cur.fetchall()
    stats = scanner._prune_files([gone], scanner._prune_stats(), dry_run=False,
                                 progress_cb=None, cancel_check=None)
    assert stats["orphan_artists"] == 1                                  # Anonymous
    assert _credits(cur) == {"Aria": [BACH]} and _artist_id(cur, "Anonymous") is None

    # a file re-homed to another track takes its composers; its old track
    # keeps what its other files name
    c = _copy(templates, library / "Gould" / "Goldberg (Live)" / "01.flac")
    shutil.copy(a, c)
    _retag(c, composer="Bach Live Composer")
    _scan()
    assert _credits(cur) == {"Aria": [BACH, "Bach Live Composer"]}
    _retag(c, title="Aria (Live)")
    _scan()
    assert _credits(cur) == {"Aria": [BACH], "Aria (Live)": ["Bach Live Composer"]}


def test_a_track_whose_files_are_unread_keeps_its_credits(cur, library, templates):
    _clean(cur)
    _flac(templates, library / "G" / "G" / "01.flac", "Aria", "Glenn Gould", "G", [BACH], 1)
    _scan()
    import credits
    from database import get_db_context
    cur.execute("UPDATE media_files SET raw_composers = NULL")    # read before this reader
    cur.execute("SELECT track_id FROM media_files")
    (track,), = cur.fetchall()
    with get_db_context() as db:
        assert credits.derive_composers(db, [track]) == set()
        db.commit()
    assert _credits(cur) == {"Aria": [BACH]}


def test_a_name_held_by_another_id_is_that_artist(cur, library, templates):
    # artists.name is UNIQUE: a row the canon renamed keeps its id, and a
    # composer of that name is that row, not a second mint
    _clean(cur)
    other = str(uuid.uuid4())
    cur.execute("INSERT INTO artists (id, name, name_latin) VALUES (%s, %s, %s)",
                (other, BACH, "johann sebastian bach"))
    _flac(templates, library / "G" / "G" / "01.flac", "Aria", "Glenn Gould", "G", [BACH], 1)
    _scan()
    cur.execute("SELECT artist_id::text FROM track_credits")
    assert cur.fetchall() == [(other,)]


# -- T9: "Last, First" by the dump's sort name ----------------------------------

def test_last_first_takes_the_dumps_name_on_a_dump_node_only(cur, library, templates):
    import credits
    from database import get_db_context
    _clean(cur)
    cur.execute("DELETE FROM mb_artist")
    _flac(templates, library / "G" / "G" / "01.flac", "Aria", "Glenn Gould", "G",
          ["Bach, Johann Sebastian", "Lennon, McCartney"], 1)
    _scan()
    assert _credits(cur) == {"Aria": ["Bach, Johann Sebastian", "Lennon, McCartney"]}
    cur.execute("INSERT INTO mb_artist (id, gid, name, sort_name) VALUES (1, %s, %s, %s)",
                (str(uuid.uuid4()), BACH, "Bach, Johann Sebastian"))
    with get_db_context() as db:                 # slice rows alone: no dump, no rename
        credits.rename_inverted(db)
        db.commit()
    assert _credits(cur)["Aria"][0] == "Bach, Johann Sebastian"
    cur.execute("INSERT INTO user_settings (key, value) VALUES ('musicbrainz.db_version', '\"x\"')")
    with get_db_context() as db:                 # the dump arrived after the scan (§5.3)
        assert credits.rename_inverted(db) == 1
        db.commit()
    assert _credits(cur) == {"Aria": [BACH, "Lennon, McCartney"]}
    assert _artist_id(cur, "Bach, Johann Sebastian") is None
    cur.execute("DELETE FROM mb_artist")
    cur.execute("DELETE FROM user_settings WHERE key = 'musicbrainz.db_version'")


# -- T11: the re-read under the new reader --------------------------------------

def test_a_row_read_by_the_older_reader_is_read_again_in_place(cur, library, templates):
    _clean(cur)
    _flac(templates, library / "G" / "G" / "01.flac", "Aria", "Glenn Gould", "G", [BACH], 1)
    _scan()
    cur.execute("DELETE FROM track_credits")
    cur.execute("UPDATE media_files SET tags_read_rev = 1, raw_composers = NULL, decode_refused = true, "
                "content_at = '2020-01-01', cover_processed_at = '2020-01-01' RETURNING id")
    (mf,), = cur.fetchall()
    stats = _scan()
    assert (stats["added"], stats["reread"]) == (0, 1)
    cur.execute("SELECT id, tags_read_rev, raw_composers, content_at::date::text, "
                "cover_processed_at::date::text, decode_refused FROM media_files")
    # the same row, no canon wake, the cover and the decode's verdict kept
    # (the file did not change)
    assert cur.fetchall() == [(mf, scanner.TAGS_READ_REV, [BACH], "2020-01-01", "2020-01-01", True)]
    assert _credits(cur) == {"Aria": [BACH]}
    assert _scan()["reread"] == 0


# -- T6: the HQPlayer library's composer ----------------------------------------

PI = ("192.168.1.53", 4321)
PI_INFO = {"name": "HQPlayerEmbedded", "product": "Signalyst HQPlayer Embedded",
           "version": "6", "engine": "6.2.3", "platform": "Linux"}


def _library(composer_a, composer_b="Antonio Vivaldi"):
    return f"""<?xml version="1.0" encoding="utf-8"?><LibraryGet>\
<LibraryDirectory album="G" artist="Glenn Gould" bits="16" channels="2" hash="d1" path="/smb/G/G" rate="44100">\
<LibraryFile composer="{composer_a}" hash="f1" length="2" name="01.flac" number="1" song="Aria"/>\
<LibraryFile composer="{composer_b}" hash="f2" length="2" name="02.flac" number="2" song="Spring"/>\
</LibraryDirectory></LibraryGet>
"""


def test_the_hqplayer_librarys_composer_lands_on_the_same_artist(cur, library, templates, monkeypatch):
    hqp_library = pytest.importorskip("hqp_library")
    _clean(cur)
    _flac(templates, library / "G" / "G" / "01.flac", "Aria", "Glenn Gould", "G", [BACH], 1)
    _scan()
    answer = {"xml": _library(BACH)}
    monkeypatch.setattr(hqp_library, "library_hash", lambda h, p: "h1")
    monkeypatch.setattr(hqp_library, "fetch_library", lambda h, p: answer["xml"])
    monkeypatch.setattr(hqp_library, "get_info", lambda h, p: PI_INFO)
    stats = hqp_library.sync(*PI)
    assert (stats["known"], stats["added"], stats["errors"]) == (0, 2, 0)
    cur.execute("SELECT count(*) FROM artists WHERE name = %s", (BACH,))
    assert cur.fetchone()[0] == 1
    assert _credits(cur) == {"Aria": [BACH], "Spring": ["Antonio Vivaldi"]}   # an HQP-only track too

    # the same hash, the library read under this reader: nothing to read
    assert hqp_library.sync(*PI)["unchanged"] is True
    # a library read under an older reader is read whole, its known files too
    cur.execute("UPDATE hqp_endpoints SET library_read_rev = 0")
    cur.execute("UPDATE hqp_library_files SET raw_composers = NULL")
    answer["xml"] = _library(BACH, "Antonio Vivaldi; Johann Georg Pisendel")
    again = hqp_library.sync(*PI)
    assert (again["unchanged"], again["known"], again["added"]) == (False, 2, 0)
    assert _credits(cur) == {"Aria": [BACH], "Spring": ["Antonio Vivaldi", "Johann Georg Pisendel"]}
    cur.execute("SELECT library_read_rev FROM hqp_endpoints")
    assert cur.fetchone()[0] == hqp_library.LIBRARY_READ_REV
    # a forget of the HQPlayer's copies: the track held here keeps its local
    # file's composer; the HQP-only track goes as an orphan, its credits with it
    gone = hqp_library.forget_endpoint(*PI)
    assert (gone["forgotten"], gone["orphan_tracks"]) == (2, 1)
    assert _credits(cur) == {"Aria": [BACH]}


# -- T12: the assistant ----------------------------------------------------------

def test_the_assistant_finds_music_by_its_composer_on_both_surfaces(cur, library, templates, monkeypatch,
                                                                       scratch_dsn):
    import importlib.util
    import assistant_queries as aq
    from tools.executor import execute_tool
    _clean(cur)
    _flac(templates, library / "Akamus" / "B" / "01.flac", "Air", AKAMUS, "Suites", [BACH], 1)
    _flac(templates, library / "Akamus" / "B" / "02.flac", "Spring", AKAMUS, "Suites",
          ["Antonio Vivaldi"], 2)
    _scan()

    api = execute_tool("search_tracks", {"composer": "Bach"})
    assert "Air" in api and "Spring" not in api and f"Composer: {BACH}" in api
    assert "Air" not in execute_tool("search_tracks", {"artist": "Bach"})   # artist= is who performs
    assert "Air" in execute_tool("search_tracks", {"artist": "Akademie", "composer": "bach"})

    spec = importlib.util.spec_from_file_location("assistant_server_composer",
                                                  REPO / "mcp" / "assistant_server.py")
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    conn = psycopg2.connect(scratch_dsn)
    conn.autocommit = True

    def q(sql, params=None):
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c:
            c.execute(sql, params)
            return [dict(r) for r in c.fetchall()]
    monkeypatch.setattr(server, "_db_query", q)
    try:
        assert server.search_tracks(composer="Bach") == api
        cur.execute("SELECT id::text FROM tracks WHERE title = 'Air'")
        (air,), = cur.fetchall()
        info = server.get_track_info(air)
        assert f"Composer: {BACH}" in info and info == execute_tool("get_track_info", {"track_id": air})
        # the performer's matcher has the whole-word arm too (2a round 1 L7)
        assert [r["title"] for r in aq.search_tracks(q, composer="Mozart")] == []
        assert {r["title"] for r in aq.search_tracks(q, artist="Musik")} == {"Air", "Spring"}
    finally:
        conn.close()


def test_a_composer_who_heads_the_album_has_it_on_his_composed_shelf(cur, library, templates,
                                                                      monkeypatch):
    # the classical convention: ALBUMARTIST is the composer, ARTIST the performer
    from routers import artists as artists_router
    from routers.artists import get_artist
    monkeypatch.setattr(artists_router, "_composer_bio", lambda artist_id, name: False)   # no Last.fm
    _clean(cur)
    _flac(templates, library / "Gould" / "Goldberg" / "01.flac", "Aria", "Glenn Gould",
          "Goldberg Variations", [BACH], 1, albumartist=BACH)
    _scan()
    page = get_artist(_artist_id(cur, BACH), sort="release_year", mbid=None)
    assert page["albums"] == [] and [a["title"] for a in page["composed"]] == ["Goldberg Variations"]


def test_an_opened_composer_page_asks_once_whatever_came_of_it(cur, library, templates, monkeypatch):
    import lastfm
    from routers import artists as artists_router
    _clean(cur)
    _flac(templates, library / "A" / "B" / "01.flac", "Air", AKAMUS, "B", [BACH])
    _scan()
    bach = _artist_id(cur, BACH)
    monkeypatch.setattr(artists_router, "_bio_asked", set())
    calls = []

    def broken(self, name, fetch_similar=True):
        calls.append(name)
        raise RuntimeError("our own bug")
    monkeypatch.setattr(lastfm.LastFmService, "get_artist_info", broken)
    done = threading.Event()
    monkeypatch.setattr(artists_router, "_wake_artist_clients", done.set)
    assert artists_router._composer_bio(bach, BACH) is True
    assert done.wait(30)
    # no marker was written, and the page's re-read of the fetch's end starts nothing
    for _ in range(3):
        assert artists_router._composer_bio(bach, BACH) is False
    assert calls == [BACH]


def test_a_failed_derive_keeps_the_batch_and_queues_its_files(cur, library, templates, monkeypatch):
    import credits
    _clean(cur)
    _flac(templates, library / "G" / "G" / "01.flac", "Aria", "Glenn Gould", "G", [BACH], 1)
    _flac(templates, library / "G" / "G" / "02.flac", "Var. 1", "Glenn Gould", "G", [BACH], 2)
    real = credits._derive

    def failing(db, tids, sweep):
        raise RuntimeError("a race no lock covers")
    monkeypatch.setattr(credits, "_derive", failing)
    stats = _scan()
    assert (stats["added"], stats["errors"]) == (2, 0)              # the files stay
    cur.execute("SELECT DISTINCT tags_read_rev, raw_composers FROM media_files")
    assert cur.fetchall() == [(0, None)]                            # queued to be read again
    assert _credits(cur) == {}
    monkeypatch.setattr(credits, "_derive", real)
    assert _scan()["reread"] == 2
    assert _credits(cur) == {"Aria": [BACH], "Var. 1": [BACH]}


def test_a_rescan_of_the_hqplayer_library_leaves_no_composer_behind(cur, library, templates, monkeypatch):
    hqp_library = pytest.importorskip("hqp_library")
    _clean(cur)
    _flac(templates, library / "G" / "G" / "01.flac", "Aria", "Glenn Gould", "G", [BACH], 1)
    _scan()
    monkeypatch.setattr(hqp_library, "library_hash", lambda h, p: "h1")
    monkeypatch.setattr(hqp_library, "get_info", lambda h, p: PI_INFO)
    monkeypatch.setattr(hqp_library, "fetch_library", lambda h, p: _library("Only At The Box"))
    hqp_library.sync(*PI)
    assert _credits(cur)["Aria"] == [BACH, "Only At The Box"]
    # the box no longer lists its copy of the Aria: the track keeps its own file's
    one = _library("x").replace('<LibraryFile composer="x" hash="f1" length="2" name="01.flac" number="1" song="Aria"/>', "")
    monkeypatch.setattr(hqp_library, "fetch_library", lambda h, p: one)
    hqp_library.forget_missing(*PI)
    assert _credits(cur)["Aria"] == [BACH]
    assert _artist_id(cur, "Only At The Box") is None
