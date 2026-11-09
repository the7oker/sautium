"""Write backend/fingerprint_probe/probe.flac, the signal the fingerprint
self-test runs fpcalc on (provenance.fingerprinter).

A deterministic piece of synthetic music — chords with harmonics, a melody,
noise percussion — 12 s of 44.1 kHz stereo 16-bit, so fpcalc's own path
does what it does to a library file: decode, downmix, resample to its
11025 Hz. The file is what ships; this script only says how it was made.
Its reference fingerprint (reference.txt) is fpcalc's on the release image,
never this machine's: run there after a change to the signal,

    docker run --rm -v "$PWD/backend/fingerprint_probe:/p:ro" --entrypoint fpcalc \
        <release image> -plain /p/probe.flac > backend/fingerprint_probe/reference.txt

and a new signal is a new reference for every node — the reference names the
release's decoder, so a move of it is a design question first.

    python scripts/make_fingerprint_probe.py
"""

from pathlib import Path

import numpy as np
import soundfile

RATE = 44100
SECONDS = 12.0
SEED = 20261109
OUT = Path(__file__).resolve().parent.parent / "backend" / "fingerprint_probe" / "probe.flac"


def _voice(t, f, amp):
    return amp * (np.sin(2 * np.pi * f * t) + 0.5 * np.sin(4 * np.pi * f * t)
                  + 0.25 * np.sin(6 * np.pi * f * t))


def _envelope(n, rise, fall):
    env = np.ones(n)
    a, r = min(n, int(rise * RATE)), min(n, int(fall * RATE))
    env[:a] = np.linspace(0.0, 1.0, a)
    if r:
        env[n - r:] *= np.linspace(1.0, 0.0, r)
    return env


def render() -> np.ndarray:
    rng = np.random.Generator(np.random.PCG64(SEED))
    n = int(SECONDS * RATE)
    out = np.zeros((n, 2))
    beat = 0.5
    chords = ((220.0, 277.18, 329.63), (196.0, 246.94, 293.66),
              (174.61, 220.0, 261.63), (196.0, 246.94, 329.63))
    for bar in range(int(SECONDS / (4 * beat))):
        start = int(bar * 4 * beat * RATE)
        length = min(n - start, int(4 * beat * RATE))
        t = np.arange(length) / RATE
        pad = sum(_voice(t, f, 0.08) for f in chords[bar % len(chords)])
        pad *= _envelope(length, 0.05, 0.3)
        out[start:start + length, 0] += pad
        out[start:start + length, 1] += 0.8 * pad
    scale = (440.0, 493.88, 554.37, 587.33, 659.25, 739.99, 830.61, 880.0)
    for step in range(int(SECONDS / beat)):
        start = int(step * beat * RATE)
        length = min(n - start, int(beat * 0.9 * RATE))
        t = np.arange(length) / RATE
        note = _voice(t, scale[rng.integers(len(scale))], 0.12) * _envelope(length, 0.005, 0.2)
        pan = rng.uniform(0.3, 0.7)
        out[start:start + length, 0] += (1 - pan) * note
        out[start:start + length, 1] += pan * note
        hit = int(0.06 * RATE)
        if start + hit <= n:
            burst = rng.normal(0.0, 0.15, hit) * np.exp(-np.arange(hit) / (0.012 * RATE))
            out[start:start + hit] += burst[:, None]
        if step % 2 == 0:
            kick = np.arange(int(0.15 * RATE)) / RATE
            k = 0.4 * np.sin(2 * np.pi * (55 * kick + 1.2 * (1 - np.exp(-kick / 0.03)))) * np.exp(-kick / 0.08)
            end = min(n, start + len(k))
            out[start:end] += k[:end - start, None]
    out *= 0.89 / np.abs(out).max()
    return np.round(out * 32767).astype(np.int16)


if __name__ == "__main__":
    OUT.parent.mkdir(parents=True, exist_ok=True)
    soundfile.write(OUT, render(), RATE, subtype="PCM_16", format="FLAC")
    print(f"{OUT}: {OUT.stat().st_size} bytes")
