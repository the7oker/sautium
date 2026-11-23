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
from tests.test_scan_unreachable import cur, library, main_module  # noqa: E402,F401
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


def test_a_value_splits_on_semicolons_and_a_spaced_slash_and_a_placeholder_is_none(templates, tmp_path):
    flac = _copy(templates, tmp_path / "a.flac")
    f = FLAC(flac)
    f["composer"] = ["Steve Reich; Philip Glass", "AC/DC", "Lennon, McCartney",
                     "Rodgers & Hammerstein", "John Adams / Terry Riley", "AC/DC", "TONO / Manuscript",
                     "Various Composers", "VARIOUS", "[unknown]; Traditional", "Anonymous"]
    f.save()
    # each once; a publishing credit is nobody, a placeholder none (Q16); a
    # comma list stays one value until the derive names it (Q15)
    assert scanner.composer_names(mutagen.File(flac)) == [
        "Steve Reich", "Philip Glass", "AC/DC", "Lennon, McCartney", "Rodgers & Hammerstein",
        "John Adams", "Terry Riley", "Traditional", "Anonymous"]


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


# -- T9: "Last, First" by MusicBrainz's sort name, from the rows the node holds ----

def _dump(cur, on: bool):
    if on:
        cur.execute("INSERT INTO user_settings (key, value) VALUES ('musicbrainz.db_version', '\"x\"')")
    else:
        cur.execute("DELETE FROM user_settings WHERE key = 'musicbrainz.db_version'")


def _rederive():
    import credits
    from database import get_db_context
    with get_db_context() as db:
        n = credits.rederive_commas(db)
        db.commit()
    return n


def test_last_first_takes_mbs_name_from_any_rows_held_and_its_shape_reads_by_the_node(
        cur, library, templates):
    """LIBRARY-FIXES-1 §9 Q1 (B-130): without the dump MB's silence says
    nothing — a two-part value whose first part is one word is one name as
    written; a slice's row turns it as the dump's does; on a dump node the
    silence is evidence and such a value is a list. Red before: a slice's
    row turned nothing, and a dump-less node split both."""
    _clean(cur)
    cur.execute("DELETE FROM mb_artist")
    _flac(templates, library / "G" / "G" / "01.flac", "Aria", "Glenn Gould", "G",
          ["Bach, Johann Sebastian", "Lennon, McCartney"], 1)
    _scan()
    assert _credits(cur) == {"Aria": ["Bach, Johann Sebastian", "Lennon, McCartney"]}
    assert _artist_id(cur, "Bach") is None and _artist_id(cur, "Johann Sebastian") is None
    # a slice brought Bach's row (a co-credited collaborator's bare row)
    cur.execute("INSERT INTO mb_artist (id, gid, name, sort_name) VALUES (1, %s, %s, %s)",
                (str(uuid.uuid4()), BACH, "Bach, Johann Sebastian"))
    assert _rederive() == 1
    assert _credits(cur) == {"Aria": [BACH, "Lennon, McCartney"]}
    assert _artist_id(cur, "Bach, Johann Sebastian") is None      # no orphan of the written form
    assert _rederive() == 0                                        # nothing new arrived: nothing derived
    _dump(cur, True)                                               # the dump arrived (§5.3)
    assert _rederive() == 1
    assert _credits(cur) == {"Aria": [BACH, "Lennon", "McCartney"]}
    # the dump deleted, the slice's row dropped: the written forms again,
    # and the turned name leaves with the credit that alone held it
    cur.execute("DELETE FROM mb_artist")
    _dump(cur, False)
    assert _rederive() == 1
    assert _credits(cur) == {"Aria": ["Bach, Johann Sebastian", "Lennon, McCartney"]}
    assert _artist_id(cur, BACH) is None and _artist_id(cur, "Lennon") is None


def test_the_shape_rule_and_the_generational_suffix(cur, library, templates):
    """A two-part value with a one-word first part: one name on a node
    without the dump, a list on a dump node that knows nothing of it; a
    first part of two words or more is a list on both; a generational
    suffix is part of the name before it on both (§9)."""
    _clean(cur)
    cur.execute("DELETE FROM mb_artist")
    for i, (title, value) in enumerate((("Clair de lune", "Debussy, Claude"),
                                        ("My Fair Lady", "Loewe, Lerner"),
                                        ("Block Rockin' Beats", "Ed Simons, Tom Rowlands"),
                                        ("Ghostbusters", "Ray Parker, Jr., Jeffrey Cohen"),
                                        ("Jack and Jill", "Ray Parker, Jr."),
                                        ("Blue Danube", "Strauss, Johann, II")), 1):
        _flac(templates, library / "V" / "Mix" / f"0{i}.flac", title, "Someone", "Mix", [value], i)
    _scan()
    without = {"Clair de lune": ["Debussy, Claude"], "My Fair Lady": ["Loewe, Lerner"],
               "Block Rockin' Beats": ["Ed Simons", "Tom Rowlands"],
               "Ghostbusters": ["Ray Parker, Jr.", "Jeffrey Cohen"],
               "Jack and Jill": ["Ray Parker, Jr."],
               "Blue Danube": ["Strauss, Johann, II"]}       # "Strauss" and "Johann, II": the shape
    assert _credits(cur) == without
    assert _artist_id(cur, "Jr.") is None
    _dump(cur, True)
    assert _rederive() == 3
    assert _credits(cur) == dict(without, **{"Clair de lune": ["Debussy", "Claude"],
                                             "My Fair Lady": ["Loewe", "Lerner"],
                                             "Blue Danube": ["Strauss", "Johann, II"]})
    _dump(cur, False)
    assert _rederive() == 3
    assert _credits(cur) == without


def test_a_canon_run_after_the_last_slice_row_dropped_derives_the_written_form(
        cur, library, templates, main_module):
    """The whole canon pass the slice cycle, the launcher's endpoint and a
    rebuild wake takes the derive — before its check for MB data, so a drop
    of the last slice row is followed too."""
    _clean(cur)
    cur.execute("DELETE FROM mb_artist")
    cur.execute("INSERT INTO mb_artist (id, gid, name, sort_name) VALUES (1, %s, %s, %s)",
                (str(uuid.uuid4()), BACH, "Bach, Johann Sebastian"))
    _flac(templates, library / "G" / "G" / "01.flac", "Aria", "Glenn Gould", "G",
          ["Bach, Johann Sebastian"], 1)
    _scan()
    assert _credits(cur) == {"Aria": [BACH]}
    cur.execute("DELETE FROM mb_artist")
    main_module._canon_run(True, [], [])
    assert _credits(cur) == {"Aria": ["Bach, Johann Sebastian"]}


def test_the_release_derives_the_comma_tracks_once(cur, library, templates, scratch_dsn, monkeypatch):
    """db_migrate's composer_commas_v1 on a database §12.6's rule credited:
    the split inverted name is one name after the start, and a second start
    does nothing."""
    import db_migrate
    import seed_import
    from config import settings
    from urllib.parse import urlparse
    _clean(cur)
    cur.execute("DELETE FROM mb_artist")
    _flac(templates, library / "I" / "I" / "01.flac", "Song", "Someone", "I", ["Ingrams, Robert Q."], 1)
    _scan()
    # the credits as §12.6 wrote them on this node
    cur.execute("DELETE FROM track_credits")
    for pos, name in enumerate(("Ingrams", "Robert Q.")):
        cur.execute("INSERT INTO artists (id, name, name_latin) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                    (str(artist_uuid(name)), name, name.lower()))
        cur.execute("""INSERT INTO track_credits (track_id, role, artist_id, source, position)
                       SELECT id, 'composer', %s, 'tag', %s FROM tracks WHERE title = 'Song'""",
                    (str(artist_uuid(name)), pos))
    url = urlparse(scratch_dsn)
    for key, value in dict(postgres_host=url.hostname, postgres_port=url.port,
                           postgres_user=url.username, postgres_password=url.password,
                           postgres_db=url.path.lstrip("/")).items():
        monkeypatch.setattr(settings, key, value)
    cur.execute("DELETE FROM _schema_migrations WHERE filename = 'composer_commas_v1'")
    marked = db_migrate._marked
    monkeypatch.setattr(db_migrate, "_marked", lambda c, name: (
        marked(c, name) if name == "composer_commas_v1" else True))
    monkeypatch.setattr(seed_import, "bundle_info", lambda: None)
    assert db_migrate.apply_pending()["composer_commas"] == 1
    assert _credits(cur) == {"Song": ["Ingrams, Robert Q."]}
    assert _artist_id(cur, "Ingrams") is None and _artist_id(cur, "Robert Q.") is None
    assert "composer_commas" not in db_migrate.apply_pending()


# -- §12.6 Q15: a comma is a list unless the node knows the whole ----------------

def _known_by_an_edge(cur, name):
    """An artist row a Last.fm similar edge holds (the phantom layer's stub)."""
    aid, seed = str(artist_uuid(name)), str(artist_uuid(name + " seed"))
    cur.execute("INSERT INTO artists (id, name, name_latin) VALUES (%s, %s, %s), (%s, %s, %s)",
                (aid, name, name.lower(), seed, name + " seed", name.lower() + " seed"))
    cur.execute("INSERT INTO similar_artists (artist_id, similar_artist_id, match_score, source) "
                "VALUES (%s, %s, 0.5, 'lastfm')", (seed, aid))
    return aid


def test_a_comma_list_splits_and_a_name_the_node_knows_stays_whole(cur, library, templates):
    _clean(cur)
    cur.execute("DELETE FROM mb_artist")
    # a performer here, by its own file; the phantom layer's stub (an edge),
    # spelt otherwise in the tag (the UUID v5 rule folds typography)
    _flac(templates, library / "EWF" / "Gratitude" / "01.flac", "Shining Star", "Earth, Wind & Fire",
          "Gratitude", ["Maurice White; Philip Bailey; Larry Dunn"], 1)
    stub = _known_by_an_edge(cur, "Crosby, Stills, Nash & Young")
    # a composite row an earlier derive minted, held by its credit alone:
    # no evidence of itself
    composite = str(artist_uuid("Leonard Cohen, Sharon Robinson"))
    cur.execute("INSERT INTO artists (id, name, name_latin) VALUES (%s, %s, %s)",
                (composite, "Leonard Cohen, Sharon Robinson", "leonard cohen, sharon robinson"))
    _flac(templates, library / "V" / "Mix" / "01.flac", "Block Rockin' Beats", "The Chemical Brothers",
          "Mix", ["Ed Simons, Tom Rowlands"], 1)
    _flac(templates, library / "V" / "Mix" / "02.flac", "Boogie Wonderland", "The Emotions",
          "Mix", ["Earth, Wind & Fire"], 2)
    _flac(templates, library / "V" / "Mix" / "03.flac", "Ohio", "Neil Young",
          "Mix", ["crosby, stills, nash & young"], 3)
    _flac(templates, library / "V" / "Mix" / "04.flac", "Everybody Knows", "Leonard Cohen",
          "Mix", ["Leonard Cohen, Sharon Robinson"], 4)
    _flac(templates, library / "V" / "Mix" / "05.flac", "Danny Boy", "Celtic Woman",
          "Mix", ["Traditional, Unknown", "Various Composers"], 5)
    _scan()
    credits = _credits(cur)
    assert credits["Block Rockin' Beats"] == ["Ed Simons", "Tom Rowlands"]
    assert credits["Boogie Wonderland"] == ["Earth, Wind & Fire"]       # the performer's row
    assert credits["Everybody Knows"] == ["Leonard Cohen", "Sharon Robinson"]
    assert credits["Danny Boy"] == ["Traditional"]                       # a placeholder is none (Q16)
    assert credits["Ohio"] == ["Crosby, Stills, Nash & Young"]          # the stub's row, folded
    cur.execute("SELECT artist_id::text FROM track_credits tc JOIN tracks t ON t.id = tc.track_id "
                "WHERE t.title = 'Boogie Wonderland'")
    assert cur.fetchall() == [(str(artist_uuid("Earth, Wind & Fire")),)]
    cur.execute("SELECT count(*) FROM track_credits WHERE artist_id = %s", (stub,))
    assert cur.fetchone()[0] == 1
    assert _artist_id(cur, "Ed Simons, Tom Rowlands") is None             # never minted
    assert _scan()["reread"] == 0 and _credits(cur) == credits            # idempotent


def test_a_comma_value_the_mb_rows_name_is_one_name_whether_a_dump_or_a_slice_brought_them(
        cur, library, templates):
    _clean(cur)
    cur.execute("DELETE FROM mb_artist")
    # a slice's rows: no dump on this node
    cur.execute("INSERT INTO mb_artist (id, gid, name, sort_name) VALUES (1, %s, %s, %s), (2, %s, %s, %s)",
                (str(uuid.uuid4()), "Crosby, Stills, Nash & Young", "Crosby, Stills, Nash & Young",
                 str(uuid.uuid4()), "Johann Strauss II", "Strauss, Johann, II"))
    _flac(templates, library / "V" / "Mix" / "01.flac", "Ohio", "Neil Young", "Mix",
          ["crosby, stills, nash & young"], 1)
    _flac(templates, library / "V" / "Mix" / "02.flac", "Blue Danube", "Wiener Philharmoniker", "Mix",
          ["strauss, johann, ii", "Bryan Adams, Jim Vallance"], 2)
    _flac(templates, library / "V" / "Mix" / "03.flac", "Teach Your Children", "Graham Nash", "Mix",
          ["Crosby, Stills, Nash, Young"], 3)
    _scan()
    # an MB name, and a sort name compared folded, keep the value whole as
    # written; a list MB knows no whole of splits — its first part one word
    # and three parts, it is no inverted name by its shape either
    held = {"Ohio": ["crosby, stills, nash & young"],
            "Blue Danube": ["strauss, johann, ii", "Bryan Adams", "Jim Vallance"],
            "Teach Your Children": ["Crosby", "Stills", "Nash", "Young"]}
    assert _credits(cur) == held
    _dump(cur, True)
    assert _rederive() == 0                                   # the dump knows no more of them
    assert _credits(cur) == held
    assert _artist_id(cur, "Nash & Young") is None and _artist_id(cur, "ii") is None
    cur.execute("DELETE FROM mb_artist")
    _dump(cur, False)


def test_an_unspaced_script_or_traditional_is_no_inversion(cur, library, templates):
    """Review round 1, M2: the shape's one-word first part is no inversion
    when the script writes names without spaces or a part is no given name
    ("Traditional"). A list led by a performer the node knows ("Björk,
    Nellee Hooper") is §9 Q1's accepted residue on a node without the dump:
    one name as written (review round 2, M2: a known part says nothing —
    "Mozart, Wolfgang Amadeus" beside a performer "Mozart"). And on a dump
    node, a performer's row keeps a comma name whole with no MB row (the
    artists arm, which the dump-less shape would hide)."""
    _clean(cur)
    cur.execute("DELETE FROM mb_artist")
    _flac(templates, library / "B" / "Post" / "01.flac", "Army of Me", "Björk", "Post", ["Björk"], 1)
    _flac(templates, library / "EWF" / "Gratitude" / "01.flac", "Shining Star", "Earth, Wind & Fire",
          "Gratitude", ["Maurice White"], 1)
    for i, (title, value) in enumerate((("Isobel", "Björk, Nellee Hooper"),
                                        ("Rydeen", "坂本龍一, 細野晴臣"),
                                        ("Danny Boy", "Traditional, Anonymous"),
                                        ("Boogie Wonderland", "Earth, Wind & Fire")), 1):
        _flac(templates, library / "V" / "Mix" / f"0{i}.flac", title, "Someone", "Mix", [value], i)
    _scan()
    lists = {"Isobel": ["Björk, Nellee Hooper"], "Rydeen": ["坂本龍一", "細野晴臣"],
             "Danny Boy": ["Traditional", "Anonymous"], "Boogie Wonderland": ["Earth, Wind & Fire"]}
    got = _credits(cur)
    assert {t: got[t] for t in lists} == lists
    _dump(cur, True)
    assert _rederive() == 1                                      # Isobel: MB's silence is a list here
    assert {t: _credits(cur)[t] for t in lists} == dict(lists, Isobel=["Björk", "Nellee Hooper"])
    _dump(cur, False)


def test_a_composer_the_canon_merged_into_a_performers_spelling_stays_merged(cur, library, templates):
    """Review round 1, M1: the canon moved the tag's "Bjork" onto the
    performer "Björk" (recanonicalize_artist repoints the credits and drops
    the source). A whole canon pass's derive compares names folded as the
    canon folds (f_unaccent, so "Soren" and "Søren" too — round 2, L1), so it
    leaves the merge as it is; comparing ids it derived the track and minted
    "Bjork" again."""
    from canon.identity import repoint_track_credits
    _clean(cur)
    cur.execute("DELETE FROM mb_artist")
    _flac(templates, library / "B" / "Post" / "01.flac", "Army of Me", "Björk", "Post", ["Björk"], 1)
    _flac(templates, library / "S" / "S" / "01.flac", "Vinter", "Søren Bebe", "S", ["Søren Bebe"], 1)
    _flac(templates, library / "D" / "D" / "01.flac", "I've Seen It All", "Someone", "D",
          ["Bjork", "Soren Bebe", "Sjón Sigurdsson, Lars von Trier"], 1)
    _scan()
    from database import get_db_context
    for spelt, canon in (("Bjork", "Björk"), ("Soren Bebe", "Søren Bebe")):
        tag, performer = _artist_id(cur, spelt), _artist_id(cur, canon)
        assert tag and performer and tag != performer
        with get_db_context() as db:
            repoint_track_credits(db, tag, performer)
            db.commit()
        cur.execute("DELETE FROM artists WHERE id = %s", (tag,))
    merged = ["Björk", "Søren Bebe", "Sjón Sigurdsson", "Lars von Trier"]
    assert _credits(cur)["I've Seen It All"] == merged
    assert _rederive() == 0
    assert _credits(cur)["I've Seen It All"] == merged
    assert _artist_id(cur, "Bjork") is None and _artist_id(cur, "Soren Bebe") is None



def test_a_name_too_long_for_an_artist_names_nobody_and_a_long_list_its_writers(cur, library, templates):
    from credits import NAME_MAX
    _clean(cur)
    a, b = "A" * (NAME_MAX - 10), "B" * (NAME_MAX - 10)
    _flac(templates, library / "V" / "Mix" / "01.flac", "Notes", "Someone", "Mix",
          ["x" * (NAME_MAX + 1), f"{a}, {b}", "Real Name"], 1)
    stats = _scan()
    assert stats["errors"] == 0
    assert _credits(cur) == {"Notes": [a, b, "Real Name"]}


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


def test_a_file_read_by_the_older_reader_derives_again_though_its_composers_read_the_same(
        cur, library, templates):
    # the rev-2 reader kept a comma list whole and its derive minted it as
    # one composer; the same raw value under rev 3 is its writers
    _clean(cur)
    _flac(templates, library / "V" / "Mix" / "01.flac", "Everybody Knows", "Leonard Cohen", "Mix",
          ["Leonard Cohen, Sharon Robinson"], 1)
    _scan()
    composite = str(artist_uuid("Leonard Cohen, Sharon Robinson"))
    cur.execute("DELETE FROM track_credits")
    cur.execute("INSERT INTO artists (id, name, name_latin) VALUES (%s, %s, %s)",
                (composite, "Leonard Cohen, Sharon Robinson", "leonard cohen, sharon robinson"))
    cur.execute("INSERT INTO track_credits (track_id, role, artist_id, source, position) "
                "SELECT track_id, 'composer', %s, 'tag', 0 FROM media_files", (composite,))
    cur.execute("UPDATE media_files SET tags_read_rev = 2")
    assert _scan()["reread"] == 1
    assert _credits(cur) == {"Everybody Knows": ["Leonard Cohen", "Sharon Robinson"]}
    assert _artist_id(cur, "Leonard Cohen, Sharon Robinson") is None     # swept with its credit
    cur.execute("SELECT DISTINCT tags_read_rev FROM media_files")
    assert cur.fetchall() == [(scanner.TAGS_READ_REV,)]


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
    # read under the older reader with its composers as they read now: the
    # comma list is its writers all the same, and a placeholder is none
    answer["xml"] = _library("Various Composers", "Antonio Vivaldi, Johann Georg Pisendel")
    assert hqp_library.sync(*PI)["unchanged"] is True                   # the same hash
    cur.execute("UPDATE hqp_endpoints SET library_read_rev = 1")
    cur.execute("UPDATE hqp_library_files SET raw_composers = ARRAY['Antonio Vivaldi, Johann Georg Pisendel'] "
                "WHERE hqp_path LIKE %s", ("%02.flac",))
    cur.execute("DELETE FROM track_credits tc USING tracks t WHERE t.id = tc.track_id AND t.title = 'Spring'")
    hqp_library.sync(*PI)
    assert _credits(cur) == {"Aria": [BACH], "Spring": ["Antonio Vivaldi", "Johann Georg Pisendel"]}
    cur.execute("SELECT raw_composers FROM hqp_library_files ORDER BY hqp_path")
    assert cur.fetchall() == [([],), (["Antonio Vivaldi, Johann Georg Pisendel"],)]
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
    assert page["discography_pending"] is False       # no slice is ever asked for a composer


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


def test_a_failed_derive_keeps_the_batch_and_its_tracks_wait_for_the_next(cur, library, templates,
                                                                           monkeypatch):
    import credits
    from database import get_db_context
    hqp_library = pytest.importorskip("hqp_library")
    _clean(cur)
    cur.execute("DELETE FROM credits_due")
    _flac(templates, library / "G" / "G" / "01.flac", "Aria", "Glenn Gould", "G", [BACH], 1)
    real = credits._derive

    def failing(db, tids, sweep):
        db.execute(credits.text("SELECT 1/0"))          # a real SQL error, in the savepoint
    monkeypatch.setattr(credits, "_derive", failing)
    stats = _scan()
    assert (stats["added"], stats["errors"]) == (1, 0)              # the file stays
    cur.execute("SELECT tags_read_rev, raw_composers FROM media_files")
    assert cur.fetchall() == [(scanner.TAGS_READ_REV, [BACH])]      # as it was read
    # an HQPlayer sync's failure waits the same way, whatever its tail writes
    monkeypatch.setattr(hqp_library, "library_hash", lambda h, p: "h1")
    monkeypatch.setattr(hqp_library, "get_info", lambda h, p: PI_INFO)
    monkeypatch.setattr(hqp_library, "fetch_library", lambda h, p: _library(BACH))
    assert hqp_library.sync(*PI)["added"] == 2
    cur.execute("SELECT count(*) FROM credits_due")
    assert cur.fetchone()[0] == 2 and _credits(cur) == {}
    monkeypatch.setattr(credits, "_derive", real)
    with get_db_context() as db:                    # not before their retry is due
        credits.derive_composers(db, [])
        db.commit()
    assert _credits(cur) == {}
    cur.execute("UPDATE credits_due SET failed_at = now() - interval '1 hour'")
    with get_db_context() as db:                    # then the next derive, of anything, takes them
        credits.derive_composers(db, [])
        db.commit()
    assert _credits(cur) == {"Aria": [BACH], "Spring": ["Antonio Vivaldi"]}
    cur.execute("SELECT count(*) FROM credits_due")
    assert cur.fetchone()[0] == 0


def test_a_track_whose_derive_keeps_failing_stays_alone(cur, library, templates, monkeypatch):
    import credits
    _clean(cur)
    cur.execute("DELETE FROM credits_due")
    _flac(templates, library / "P" / "P" / "01.flac", "Poison", "Glenn Gould", "P", ["Old Composer"], 1)
    _scan()
    cur.execute("SELECT track_id::text FROM media_files")
    (poison,), = cur.fetchall()
    real = credits._derive

    def failing_for_poison(db, tids, sweep):
        if poison in tids:
            db.execute(credits.text("SELECT 1/0"))
        return real(db, tids, sweep)
    monkeypatch.setattr(credits, "_derive", failing_for_poison)
    _retag(library / "P" / "P" / "01.flac", composer="New Composer")
    _scan()
    cur.execute("UPDATE credits_due SET failed_at = now() - interval '1 hour'")
    for i in range(3):                              # three unrelated scans, the retry due each time
        _flac(templates, library / "Q" / f"Q{i}" / "01.flac", f"Song {i}", "Glenn Gould", f"Q{i}",
              [f"Composer {i}"], 1)
        _scan()
        cur.execute("UPDATE credits_due SET failed_at = now() - interval '1 hour'")
    credited = _credits(cur)
    assert all(credited[f"Song {i}"] == [f"Composer {i}"] for i in range(3))
    cur.execute("SELECT track_id::text FROM credits_due")
    assert cur.fetchall() == [(poison,)]
    # once it derives, it is swept as any derive sweeps: no orphan of the old tag
    monkeypatch.setattr(credits, "_derive", real)
    from database import get_db_context
    with get_db_context() as db:
        credits.derive_composers(db, [], sweep=False)        # a caller that sweeps its own only
        db.commit()
    assert _credits(cur)["Poison"] == ["New Composer"]
    assert _artist_id(cur, "Old Composer") is None


def test_a_track_merge_carries_a_waiting_derive(cur, library, templates):
    from canon.identity import _update_track_uuid
    from database import get_db_context
    _clean(cur)
    cur.execute("DELETE FROM credits_due")
    _flac(templates, library / "M" / "A" / "01.flac", "Aria", "Glenn Gould", "A", [BACH], 1)
    _flac(templates, library / "M" / "B" / "01.flac", "Aria (2)", "Glenn Gould", "B", [BACH], 1)
    _scan()
    cur.execute("SELECT t.title, t.id::text FROM tracks t")
    ids = dict(cur.fetchall())
    cur.execute("INSERT INTO credits_due (track_id) VALUES (%s)", (ids["Aria (2)"],))
    with get_db_context() as db:
        _update_track_uuid(db, ids["Aria (2)"], ids["Aria"])
        db.commit()
    cur.execute("SELECT track_id::text FROM credits_due")
    assert cur.fetchall() == [(ids["Aria"],)]


def test_a_reread_that_changes_a_copys_rank_derives(cur, library, templates, monkeypatch):
    import credits
    _clean(cur)
    _flac(templates, library / "R" / "LP" / "01.flac", "Aria", "Glenn Gould", "LP", [BACH], 1)
    _scan()
    cur.execute("UPDATE media_files SET sample_rate = 22050, tags_read_rev = 1")   # read as another rip
    derived = []
    real = credits.derive_composers
    monkeypatch.setattr(scanner, "derive_composers",
                        lambda db, tids, **kw: derived.append(sorted(tids)) or real(db, tids, **kw))
    _scan()
    cur.execute("SELECT track_id::text FROM media_files")
    (track,), = cur.fetchall()
    assert [track] in derived


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
