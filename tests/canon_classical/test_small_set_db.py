"""The small set of CANON-CLASSICAL (DESIGN §6.3, T6, T7, T9, T10, T17, T18):
albums made by the generator from MusicBrainz's real releases (the fixture
rows, CC0), written as silent FLAC into the test's own music folder, scanned,
and the canon run on a dump node — each against its known answer: the
release group and each position's recording.

Before this slice: Polarity reached no group (an ensemble's album whose
tracks credit its leader), Appassionata's group (no primary type) was
refused, the two Beethoven discs met on the Vol. 2 duplicate, and the two
TITLE values of the Goldberg files minted one track for all 32."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
for p in (REPO, REPO / "backend"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

psycopg2 = pytest.importorskip("psycopg2")
FLAC = pytest.importorskip("mutagen.flac").FLAC

import scanner  # noqa: E402
from tests.canon_classical import fixtures as fx  # noqa: E402
from tests.canon_classical.generator import generate  # noqa: E402
from canon.content import _norm  # noqa: E402
from tests.test_scan_unreachable import cur, library  # noqa: E402,F401

pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "ffmpeg"], capture_output=True).returncode, reason="no ffmpeg")


@pytest.fixture(scope="module")
def silence(tmp_path_factory):
    """A silent FLAC per length (8 kHz mono s16: a few kB a minute)."""
    folder = tmp_path_factory.mktemp("silence")
    made = {}

    def of(seconds: float) -> Path:
        if seconds not in made:
            path = folder / f"{seconds:.3f}.flac"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono",
                            "-t", f"{seconds:.3f}", "-sample_fmt", "s16", str(path)], check=True)
            made[seconds] = path
        return made[seconds]
    return of


def _write(library, silence, case) -> None:
    for f in case.files:
        path = library / f.path
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(silence(f.seconds), path)
        tags = FLAC(path)
        for k, v in f.tags.items():
            tags[k] = v
        tags.save()


@pytest.fixture
def dump_node(cur, monkeypatch):
    import mb_backend
    from desktop.p2p import mb_asks
    for table in ("track_mbids", "album_artists", "track_artists", "artist_mbids", "artists"):
        cur.execute(f"DELETE FROM {table}")
    fx.load(cur)
    cur.execute("INSERT INTO user_settings (key, value) VALUES (%s, '\"20261111-002335\"')", (mb_asks.DUMP_MARKER,))
    monkeypatch.setattr(mb_backend, "LOCAL_DUMP", True)
    import database
    from canon import content
    monkeypatch.setattr(content, "SessionLocal", database.SessionLocal)   # the scan fixture's, bound to the scratch
    return cur


def _canon():
    from canon import content
    return content.canonicalize_pending()


def _album_of(cur, case):
    first = next(iter(case.recordings))
    cur.execute("""SELECT al.musicbrainz_id::text, al.mb_match_confidence::text FROM media_files mf
                     JOIN album_variants av ON av.id = mf.album_variant_id JOIN albums al ON al.id = av.album_id
                    WHERE mf.file_path LIKE %s""", ("%" + first,))
    return cur.fetchone()


def _recordings(cur, case):
    """Each file's recording, read as the answer's where MusicBrainz holds the
    same performance twice: one name, lengths within a second (the Vol. 2
    duplicate release carries its own recording MBIDs of Minnaar's takes)."""
    cur.execute("SELECT file_path, recording_mbid::text FROM media_files")
    got = {p: r for p, r in cur.fetchall()}
    out = {}
    for path, want in case.recordings.items():
        rec = next((r for p, r in got.items() if p.endswith(path)), "missing")
        cur.execute(f"""SELECT {_norm('a.name')} = {_norm('b.name')} AND abs(a.length - b.length) <= 1000
                          FROM mb_recording a, mb_recording b WHERE a.gid = %s AND b.gid = %s""", (rec, want))
        twin = cur.fetchone()
        out[path] = want if twin and twin[0] else rec
    return out


def test_the_generator_is_a_function_of_its_seed():
    rel = fx.release(fx.GOLDBERG)
    a = generate(rel, "2 values", "N names joined by '; '", {"drift": 1.0}, seed=7)
    assert a == generate(rel, "2 values", "N names joined by '; '", {"drift": 1.0}, seed=7)
    assert a != generate(rel, "2 values", "N names joined by '; '", {"drift": 1.0}, seed=8)
    assert len(a.files) == len(rel["tracks"]) == 32
    assert all(len(f.tags["title"]) == 2 for f in a.files)          # [work, movement]


# (release, its title form, the credit's names the files carry — a classical release's credit opens with
# the composer, the files' ARTIST names the performer)
@pytest.mark.parametrize("mbid, title_form, performers", [
    (fx.APPASSIONATA, "Work: Movement", 0),    # T9: a group with no primary type
    (fx.BEETHOVEN_1_2, "2 values", 1),         # T10: each disc onto its own group,
    (fx.BEETHOVEN_4_5, "Work: Movement", 1),   # never the Vol. 2 duplicate
    (fx.GOLDBERG, "2 values", 1),              # T1 + T6: 32 positions, two TITLE values
    (fx.MEYERS, "Work: Movement", 1),          # D2: Meyers' own recordings, not Zukerman's
    # §12.1's release, its files and its credit one: the cascade binds it — "The Elder" too, MusicBrainz's
    # track 405 s against its recording's 396.5 s, by the track's length (§12.7)
    (fx.POLARITY, "Work: Movement", 0),
])
def test_each_release_binds_to_itself_position_by_position(dump_node, library, silence, mbid, title_form,
                                                            performers):
    rel = fx.release(mbid)
    rel = {**rel, "artists": rel["artists"][performers:]}
    case = generate(rel, title_form, "one name", {"drift": 0.3}, seed=20261118)
    _write(library, silence, case)
    scanner.LibraryScanner().scan_and_import()
    _canon()
    rg, _how = _album_of(dump_node, case)
    assert rg == case.rg
    assert _recordings(dump_node, case) == case.recordings


# The master's own two (§12.7): the files' titles as there — Disc 10's name City Life's movements its own way,
# so the cascade covers too few of them to name a group, and Steve Reich bound as the master has him (by his
# other albums; MusicBrainz's a3031680-…)
REICH = "a3031680-c359-458f-a641-70ccbaec6a74"
WORKS_DISC_10 = ["Proverb", "Nagoya Marimbas", "City Life (Part I)", "City Life (Part II)", "City Life (Part III)",
                 "City Life (Part IV)", "City Life (Part V)"]


@pytest.mark.parametrize("box, disc, single, album, titles, bound", [
    (fx.HITSTORY, 1, fx.ELV1S, "HitStory (Disc 1)", None, None),
    (fx.WORKS_1965_1995, 10, fx.CITY_LIFE, "Disc 10", WORKS_DISC_10, ("Steve Reich", REICH)),
])
def test_one_disc_of_a_box_never_binds_the_one_disc_release_of_its_recordings(dump_node, library, silence, box,
                                                                                disc, single, album, titles, bound):
    """§12.7's fixture cases: the positional tier bound each to the one-disc release (the 50-bind gate's two
    wrong binds); a medium of a box is a candidate too, so the copy fits two groups and abstains."""
    case = generate({**fx.release(box, medium=disc), "title": album}, "Work: Movement", "one name",
                    {"drift": 0.3}, seed=20261120)
    if titles:
        case = case._replace(files=[f._replace(tags={**f.tags, "title": [t]}) for f, t in zip(case.files, titles)])
    _write(library, silence, case)
    scanner.LibraryScanner().scan_and_import()
    if bound:
        from uuid_utils import artist_uuid
        dump_node.execute("INSERT INTO artist_mbids (mbid, artist_id, confidence) "
                          "VALUES (%s, %s, 'overlap_verified')", (bound[1], str(artist_uuid(bound[0]))))
    _canon()
    rg, how = _album_of(dump_node, case)
    assert rg != fx.release(single)["rg"]
    assert rg in (None, case.rg)


def test_one_barcode_on_two_releases_binds_only_the_one_whose_positions_fit(dump_node, library, silence):
    """T7 (DESIGN §3.3): SPES and a Jaco Pastorius live set share 7041888520023."""
    case = generate(fx.release(fx.SPES), "Work: Movement", "one name", {}, seed=1)
    _write(library, silence, case)
    scanner.LibraryScanner().scan_and_import()
    _canon()
    assert _album_of(dump_node, case)[0] == case.rg
