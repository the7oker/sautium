"""CUE sheet parsing/resolution — the 7 real library sheets as fixtures plus
synthetic defect cases. Pure logic; the scanner integration runs against the
live Docker DB per CLAUDE.md."""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "backend"))

import cue_sheet
from cue_sheet import (
    CueSheet, CueTrack, build_cue_map, parse_cue, read_cue_text, resolve_cue,
    synthesize_metadata,
)

FIXTURES = Path(__file__).parent / "fixtures" / "cue"
AUDIO_EXTS = {'.flac', '.ape', '.wav', '.aiff', '.wv', '.tta', '.dsf', '.dff',
              '.mp3', '.ogg', '.m4a'}


def _fixture_text(name: str) -> str:
    return read_cue_text(FIXTURES / name)


# ---- field parsing on the real sheets ---------------------------------------

def test_theatre_play_music_fields():
    parsed = parse_cue(_fixture_text("theatre_play_music.cue"))
    assert parsed["title"] == "Theatre Play Music"
    assert parsed["performer"] == "Contemporary Noise Quartet"
    assert parsed["genre"] == "Theatre, Jazz Piano, Post-Rock"  # quoted list
    assert parsed["date"] == "2008"
    assert parsed["catalog"] == "5905912554205"
    (filename, tracks), = parsed["files"]
    assert filename == "Contemporary Noise Quartet - Theatre Play Music.wav"
    assert len(tracks) == 9
    assert tracks[0]["title"] == "Main Tune"
    # INDEX 00/01 pair: 02:46:62 and 02:48:60
    assert tracks[1]["indexes"][0] == pytest.approx(166 + 62 / 75.0)
    assert tracks[1]["indexes"][1] == pytest.approx(168.8)


def test_still_waters_isrc_and_noise():
    parsed = parse_cue(_fixture_text("still_waters.cue"))
    assert parsed["genre"] == "Pop"          # unquoted REM GENRE
    assert parsed["catalog"] == "0000000000000"
    (_, tracks), = parsed["files"]
    assert len(tracks) == 12
    assert tracks[0]["isrc"] == "GBAKW9700072"
    # REM REPLAYGAIN_* inside tracks must not leak anywhere
    assert parsed["date"] == "1997"


def test_aztec_mystic_per_track_performer_and_flags():
    parsed = parse_cue(_fixture_text("aztec_mystic.cue"))
    assert parsed["performer"] == "DJ Rolando"
    (_, tracks), = parsed["files"]
    assert len(tracks) == 23
    assert tracks[0]["performer"] == "Underground Resistance"  # FLAGS DCP ignored
    assert tracks[1]["title"] == "Jaguar"


def test_live_at_ur_party_file_type_token_ignored():
    parsed = parse_cue(_fixture_text("live_at_ur_party.cue"))
    (filename, tracks), = parsed["files"]
    assert filename == "DJ Rolando - Live @ UR Party.mp3"      # FILE ... MP3
    assert len(tracks) == 26


def test_multi_file_sheet_parses_but_does_not_resolve():
    parsed = parse_cue(_fixture_text("red.cue"))
    assert len(parsed["files"]) == 10
    assert resolve_cue(FIXTURES / "red.cue", AUDIO_EXTS) is None


# ---- boundary math ----------------------------------------------------------

def _write(tmp_path, name, text=""):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


BASIC_CUE = """\
PERFORMER "Artist"
TITLE "Album"
FILE "image.wav" WAVE
  TRACK 01 AUDIO
    TITLE "One"
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "Two"
    INDEX 00 02:46:62
    INDEX 01 02:48:60
  TRACK 03 AUDIO
    TITLE "Three"
    INDEX 01 05:56:23
"""


def test_spans_index01_to_index01_last_open(tmp_path):
    _write(tmp_path, "image.ape")
    cue = _write(tmp_path, "album.cue", BASIC_CUE)
    sheet = resolve_cue(cue, AUDIO_EXTS)
    starts = [t.start_seconds for t in sheet.tracks]
    assert starts == [0.0, pytest.approx(168.8), pytest.approx(356 + 23 / 75.0)]
    assert sheet.tracks[0].end_seconds == pytest.approx(168.8)   # next INDEX 01, not 00
    assert sheet.tracks[1].end_seconds == pytest.approx(356 + 23 / 75.0)
    assert sheet.tracks[2].end_seconds is None                   # to EOF


def test_index00_fallback_when_01_missing(tmp_path):
    _write(tmp_path, "image.ape")
    cue = _write(tmp_path, "album.cue", BASIC_CUE.replace(
        "    INDEX 01 05:56:23", "    INDEX 00 05:56:23"))
    sheet = resolve_cue(cue, AUDIO_EXTS)
    assert sheet.tracks[2].start_seconds == pytest.approx(356 + 23 / 75.0)


def test_track_without_any_index_rejects_sheet(tmp_path):
    _write(tmp_path, "image.ape")
    cue = _write(tmp_path, "album.cue", BASIC_CUE.replace(
        "    INDEX 01 05:56:23\n", ""))
    assert resolve_cue(cue, AUDIO_EXTS) is None


def test_non_monotonic_starts_reject_sheet(tmp_path):
    _write(tmp_path, "image.ape")
    cue = _write(tmp_path, "album.cue", BASIC_CUE.replace(
        "INDEX 01 05:56:23", "INDEX 01 01:00:00"))
    assert resolve_cue(cue, AUDIO_EXTS) is None


def test_data_track_is_skipped(tmp_path):
    _write(tmp_path, "image.ape")
    cue = _write(tmp_path, "album.cue", BASIC_CUE + """\
  TRACK 04 MODE1/2352
    INDEX 01 07:00:00
""")
    sheet = resolve_cue(cue, AUDIO_EXTS)
    assert [t.number for t in sheet.tracks] == [1, 2, 3]


# ---- FILE resolution defects ------------------------------------------------

def test_resolution_wav_to_ape_stem(tmp_path):
    _write(tmp_path, "image.ape")
    cue = _write(tmp_path, "album.cue", BASIC_CUE)          # references image.wav
    sheet = resolve_cue(cue, AUDIO_EXTS)
    assert sheet.audio_path.name == "image.ape"


def test_resolution_case_insensitive_exact(tmp_path):
    _write(tmp_path, "IMAGE.WAV")
    cue = _write(tmp_path, "album.cue", BASIC_CUE)
    sheet = resolve_cue(cue, AUDIO_EXTS)
    assert sheet.audio_path.name == "IMAGE.WAV"


def test_resolution_single_audio_file_fallback(tmp_path):
    _write(tmp_path, "completely different name.mp3")
    cue = _write(tmp_path, "album.cue", BASIC_CUE)
    sheet = resolve_cue(cue, AUDIO_EXTS)
    assert sheet.audio_path.name == "completely different name.mp3"


def test_resolution_missing_image_rejects(tmp_path):
    _write(tmp_path, "a.mp3")
    _write(tmp_path, "b.mp3")                               # two candidates, no match
    cue = _write(tmp_path, "album.cue", BASIC_CUE)
    assert resolve_cue(cue, AUDIO_EXTS) is None


def test_two_cues_one_image_first_wins(tmp_path):
    _write(tmp_path, "image.ape")
    a = _write(tmp_path, "a.cue", BASIC_CUE)
    b = _write(tmp_path, "b.cue", BASIC_CUE)
    cue_map = build_cue_map([b, a], AUDIO_EXTS)
    assert cue_map[str(tmp_path / "image.ape")].cue_path == a


# ---- encoding ---------------------------------------------------------------

def test_cp1251_fixture_decodes(tmp_path):
    text = BASIC_CUE.replace('TITLE "Album"', 'TITLE "Альбом"') \
                    .replace('TITLE "One"', 'TITLE "Пісня"')
    p = tmp_path / "cyr.cue"
    p.write_bytes(text.encode("cp1251"))
    parsed = parse_cue(read_cue_text(p))
    assert parsed["title"] == "Альбом"
    assert parsed["files"][0][1][0]["title"] == "Пісня"


def test_utf8_bom_decodes(tmp_path):
    p = tmp_path / "bom.cue"
    p.write_bytes(b"\xef\xbb\xbf" + BASIC_CUE.encode("utf-8"))
    assert parse_cue(read_cue_text(p))["title"] == "Album"


# ---- synthesize_metadata ----------------------------------------------------

IMAGE_MD = {
    "file_path": "E:/Music/x/image.ape", "file_size_bytes": 1000,
    "file_format": "APE", "is_lossless": True, "duration_seconds": 1800.0,
    "sample_rate": 44100, "bit_depth": 16, "channels": 2, "bitrate": 700,
    "title": "image tag title", "artist": "Image Artist", "album": "Image Album",
    "album_artist": None, "genre": "Image Genre", "date": "1990",
    "track_number": None, "disc_number": 1, "label": "Label",
    "catalog_number": "CAT-1", "isrc": "IMAGEISRC", "release_year": 1990,
}


def _sheet(**over):
    base = dict(cue_path=Path("a.cue"), audio_path=Path("image.ape"),
                title="Cue Album", performer="Cue Artist", date="2008",
                genre="Cue Genre", catalog="5905912554205", tracks=[])
    base.update(over)
    return CueSheet(**base)


def test_synthesize_cue_first_with_image_fallback():
    tr = CueTrack(number=2, title="Bitches Tune", performer=None, isrc=None,
                  start_seconds=168.8, end_seconds=356.3)
    md = synthesize_metadata(IMAGE_MD, _sheet(), tr, 1800.0)
    assert md["title"] == "Bitches Tune"
    assert md["artist"] == "Cue Artist"          # track performer absent -> sheet
    assert md["album"] == "Cue Album"
    assert md["album_artist"] == "Cue Artist"
    assert md["genre"] == "Cue Genre"
    assert md["release_year"] == 2008
    assert md["track_number"] == 2 and md["disc_number"] == 1
    assert md["isrc"] is None                    # never the image's
    assert md["catalog_number"] == "5905912554205"
    assert md["duration_seconds"] == pytest.approx(187.5)
    assert md["cue_start_seconds"] == 168.8
    assert md["cue_end_seconds"] == 356.3
    # technical props ride through
    assert md["sample_rate"] == 44100 and md["file_format"] == "APE"


def test_synthesize_fallbacks_and_last_track():
    sheet = _sheet(title=None, performer=None, genre=None, date=None,
                   catalog="0000000000000")
    tr = CueTrack(number=9, title=None, performer=None, isrc=None,
                  start_seconds=1526.88, end_seconds=None)
    md = synthesize_metadata(IMAGE_MD, sheet, tr, 1800.0)
    assert md["title"] == "Track 09"
    assert md["artist"] == "Image Artist"
    assert md["album"] == "Image Album"
    assert md["genre"] == "Image Genre"
    assert md["release_year"] == 1990
    assert md["catalog_number"] == "CAT-1"       # junk catalog never overrides
    assert md["duration_seconds"] == pytest.approx(273.12)
    assert md["cue_end_seconds"] is None


def test_synthesize_unknown_image_length():
    tr = CueTrack(number=1, title="T", performer=None, isrc=None,
                  start_seconds=0.0, end_seconds=None)
    md = synthesize_metadata(IMAGE_MD, _sheet(), tr, None)
    assert md["duration_seconds"] is None
