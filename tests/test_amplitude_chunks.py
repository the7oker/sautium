"""The amplitude block computed chunk by chunk (audio_analysis.
amplitude_frames) against librosa's whole-signal calls, and the decode read
into one array (load_full_track_48k) against ffmpeg's bytes: the same
per-frame values and the same features, on signals that span many chunks,
start and end off zero, hold silence, sit on a chunk's edge or are shorter
than a frame. The chunking only bounds the memory: a whole-signal call
built frame arrays of ~67 MB per minute of audio."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

np = pytest.importorskip("numpy")
librosa = pytest.importorskip("librosa")

import audio_analysis  # noqa: E402

SR = 22050
EDGE = audio_analysis.CHUNK_FRAMES * audio_analysis.HOP


def _whole(y):
    """The amplitude block as it was computed: librosa over the whole signal."""
    rms = librosa.feature.rms(y=y)[0]
    centroid = librosa.feature.spectral_centroid(y=y, sr=SR)[0]
    zcr = librosa.feature.zero_crossing_rate(y)[0]
    db = librosa.amplitude_to_db(rms)
    return rms, centroid, zcr, {
        "energy": round(float(np.mean(rms)), 6),
        "energy_db": round(float(np.mean(db)), 2),
        "dynamic_range_db": (round(float(np.percentile(db, 95) - np.percentile(db, 5)), 2)
                             if len(db) > 1 else 0.0),
        "brightness": round(float(np.mean(centroid) / (SR / 2.0)), 4),
        "zero_crossing_rate": round(float(np.mean(zcr)), 6),
    }


def _signals():
    rng = np.random.default_rng(20261111)
    noise = np.cumsum(rng.standard_normal(SR * 420)).astype(np.float32)
    noise = (noise - noise.mean()) / (np.abs(noise).max() + 1e-9) * 0.5        # 7 min
    t = np.arange(SR * 200) / SR
    bursts = (0.4 * np.sin(2 * np.pi * 440 * t) * (np.sin(2 * np.pi * 0.05 * t) > 0.3)
              + 0.2).astype(np.float32)                                         # off zero at both ends
    bursts[SR * 60:SR * 90] = 0.0                                               # silence inside
    yield "noise, 7 min", noise
    yield "bursts over silence, offset", bursts
    yield "3 s", noise[:SR * 3].copy()
    for n in (EDGE - 1, EDGE, EDGE + 1, 3 * EDGE - audio_analysis.FRAME // 2):
        yield f"{n} samples", noise[:n].copy()
    yield "shorter than a frame", noise[:1000].copy()


@pytest.mark.parametrize("name,y", list(_signals()), ids=[n for n, _ in _signals()])
def test_the_chunked_amplitude_block_is_the_whole_signal_one(name, y):
    rms, centroid, zcr, features = _whole(y)
    c_rms, c_centroid, c_zcr = audio_analysis.amplitude_frames(y, SR)
    assert np.array_equal(c_rms, rms)
    assert np.array_equal(c_centroid, centroid)
    assert np.array_equal(c_zcr, zcr)
    assert audio_analysis.amplitude_features(y, SR) == features


@pytest.fixture
def flac(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("no ffmpeg")
    path = tmp_path / "noise.flac"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "anoisesrc=d=95:c=pink:r=44100:a=0.3", "-ac", "2", "-c:a", "flac", str(path)],
                   check=True, timeout=120)
    return str(path)


@pytest.mark.parametrize("duration", [95, None, 10, 400])
def test_the_decode_read_into_one_array_is_ffmpegs_output(flac, duration):
    """Known, unknown, too short (the array grows) or too long (it is cut)."""
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", flac, "-ac", "1", "-ar", "48000",
                          "-f", "f32le", "pipe:1"], capture_output=True, check=True).stdout
    expected = np.frombuffer(out, dtype=np.float32)
    assert np.array_equal(audio_analysis.load_full_track_48k(flac, duration_seconds=duration),
                          expected)


def test_a_file_ffmpeg_cannot_read_raises_with_its_words(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("no ffmpeg")
    bad = tmp_path / "not-audio.flac"
    bad.write_bytes(b"not audio at all" * 100)
    with pytest.raises(subprocess.CalledProcessError) as failed:
        audio_analysis.load_full_track_48k(str(bad), duration_seconds=60)
    assert failed.value.stderr
