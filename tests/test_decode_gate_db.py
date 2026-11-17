"""The analysis passes take only the whole file as it was read (LIBRARY-WATCH
M6, 2026-11-17). A FLAC caught mid-copy — cut short, or full-sized with a
zero tail, as Windows' CopyFileEx gives a copy its final size at once —
declares its whole length in STREAMINFO, reads every tag, and ffmpeg decodes
the part that is there with exit code 0: before this, the sonic pass saved
and the notary sealed an analysis of 40 % of the track under the material
declaration of the whole.

The decode must reach the length the header declares (audio_analysis.
check_whole), and the file's stat must stay the one its row recorded through
the decode and to the save (decode_as_read, still_as_read). A refused file is
marked and left out of both queues until a read clears the mark. The
decoder's tests use the real ffmpeg; the passes' run on the module's scratch
database with test_analysis_passes_db's fakes."""

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BACKEND = REPO / "backend"
for p in (REPO, BACKEND):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

np = pytest.importorskip("numpy")

import audio_analysis  # noqa: E402
import embeddings  # noqa: E402
from file_stat import stat_of  # noqa: E402
from tests.test_analysis_passes_db import (FP, _file, _queued, _records, _track, cur,  # noqa: E402,F401
                                           models, prints)


def _flac(path: Path, seconds: int = 60) -> bytes:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                    "-i", f"sine=frequency=440:duration={seconds}",
                    "-ar", "44100", "-sample_fmt", "s16", str(path)], check=True)
    return path.read_bytes()


needs_ffmpeg = pytest.mark.skipif(subprocess.run(["which", "ffmpeg"], capture_output=True).returncode,
                                  reason="no ffmpeg")


@needs_ffmpeg
@pytest.mark.parametrize("caught", ["cut", "zero tail"])
def test_a_flac_caught_mid_copy_decodes_short_and_is_refused(tmp_path, caught):
    whole = _flac(tmp_path / "whole.flac")
    part = whole[: int(len(whole) * 0.4)]
    if caught == "zero tail":
        part += b"\0" * (len(whole) - len(part))
    mid = tmp_path / "mid.flac"
    mid.write_bytes(part)
    with pytest.raises(audio_analysis.ShortDecode):
        audio_analysis.load_full_track_48k(str(mid), duration_seconds=60.0)
    audio = audio_analysis.load_full_track_48k(str(tmp_path / "whole.flac"), duration_seconds=60.0)
    assert len(audio) / audio_analysis.DECODE_SR == pytest.approx(60.0, abs=0.05)


def test_an_mp3_s_estimated_length_is_no_gate():
    audio_analysis.check_whole(10.0, "/music/a.mp3", 60.0)
    with pytest.raises(audio_analysis.ShortDecode):
        audio_analysis.check_whole(10.0, "/music/a.flac", 60.0)
    audio_analysis.check_whole(59.2, "/music/a.flac", 60.0)      # a dropped frame, AAC priming


def _refused(cur, media_file) -> bool:
    cur.execute("SELECT decode_refused FROM media_files WHERE id = %s", (media_file,))
    return cur.fetchone()[0]


def _short_decode_of(monkeypatch, name):
    real = embeddings.AudioEmbeddingGenerator._load_audio

    def load(self, path, *a):
        if path.endswith(name):
            raise audio_analysis.ShortDecode(f"{path} decodes to 24.0 s of the 60.0 s its header declares")
        return real(self, path, *a)
    monkeypatch.setattr(embeddings.AudioEmbeddingGenerator, "_load_audio", load)


def test_a_short_decode_saves_nothing_and_waits_for_the_file_s_next_read(cur, prints, models,
                                                                          monkeypatch):
    track = _track(cur)
    mf = _file(cur, track, "/music/a.flac")
    prints["/music/a.flac"] = FP
    _short_decode_of(monkeypatch, "a.flac")

    stats = models.sonic([uuid.UUID(track)])
    assert (stats["refused"], stats["success"]) == (1, 0)
    assert _records(cur, track) == {}
    assert _refused(cur, mf)
    # out of both queues: no later pass decodes it again for nothing
    assert _queued(track) == {"embeddings": False, "features": False, "instruments": False}
    models.calls.clear()
    assert models.sonic([uuid.UUID(track)])["processed"] == 0
    assert models.instruments([uuid.UUID(track)])["processed"] == 0
    assert not [c for c in models.calls if "decode" in c[0]]

    cur.execute("UPDATE media_files SET decode_refused = false WHERE id = %s", (mf,))   # a read
    assert _queued(track) == {"embeddings": True, "features": True, "instruments": True}


def test_the_instrument_pass_refuses_a_short_decode_alike(cur, prints, models, monkeypatch):
    track = _track(cur)
    mf = _file(cur, track, "/music/a.flac")
    prints["/music/a.flac"] = FP
    models.sonic([uuid.UUID(track)])

    def short(*a, **kw):
        raise audio_analysis.ShortDecode("cut short")
    monkeypatch.setattr(audio_analysis, "load_full_track_48k", short)
    stats = models.instruments([uuid.UUID(track)])
    assert (stats["refused"], stats["success"]) == (1, 0)
    assert "instruments" not in _records(cur, track)
    assert _refused(cur, mf)


def _real_file(cur, tmp_path, name="b.flac"):
    path = tmp_path / name
    path.write_bytes(b"fLaC" + b"\0" * 4096)
    st = stat_of(str(path))
    track = _track(cur)
    mf = _file(cur, track, str(path))
    cur.execute("UPDATE media_files SET file_size_bytes = %s, stat_mtime_ns = %s, stat_ctime_ns = %s "
                "WHERE id = %s", (st.size, st.mtime_ns, st.ctime_ns, mf))
    return path, track, mf


def test_a_file_written_under_its_decode_is_not_saved(cur, prints, models, monkeypatch, tmp_path):
    path, track, mf = _real_file(cur, tmp_path)
    prints["/music/b.flac"] = FP
    real = embeddings.AudioEmbeddingGenerator._load_audio

    def load(self, p, *a):
        with open(p, "ab") as f:          # the copy goes on while it is decoded
            f.write(b"more")
        return real(self, p, *a)
    monkeypatch.setattr(embeddings.AudioEmbeddingGenerator, "_load_audio", load)

    stats = models.sonic([uuid.UUID(track)])
    assert (stats["refused"], stats["success"]) == (1, 0)
    assert _records(cur, track) == {}
    assert _refused(cur, mf)


def test_a_file_changed_since_its_read_is_not_decoded_as_it(cur, prints, models, tmp_path):
    path, track, mf = _real_file(cur, tmp_path)
    prints["/music/b.flac"] = FP
    with open(path, "ab") as f:            # re-tagged; the scan has not read it again yet
        f.write(b"tag")
    os.utime(path, ns=(0, 0))
    stats = models.sonic([uuid.UUID(track)])
    assert (stats["refused"], stats["success"]) == (1, 0)
    assert _refused(cur, mf)


def test_a_read_between_the_decode_and_the_save_wins(cur, prints, models, monkeypatch, tmp_path):
    """The scan read the file again while it was decoded: that version is
    the catalogue's, and pending — the decoded one is not saved, and the
    re-read's version is not marked."""
    path, track, mf = _real_file(cur, tmp_path)
    prints["/music/b.flac"] = FP
    real = embeddings.AudioEmbeddingGenerator._load_audio

    def load(self, p, *a):
        cur.execute("UPDATE media_files SET stat_mtime_ns = stat_mtime_ns + 1 WHERE id = %s", (mf,))
        return real(self, p, *a)
    monkeypatch.setattr(embeddings.AudioEmbeddingGenerator, "_load_audio", load)

    stats = models.sonic([uuid.UUID(track)])
    assert (stats["refused"], stats["success"]) == (1, 0)
    assert _records(cur, track) == {}
    assert not _refused(cur, mf)
