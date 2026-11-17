"""
The features (librosa) and the instruments (the AST + PaSST ensemble) of a
track, from its whole decoded audio.

- librosa amplitude block (energy, brightness, dynamic range, ZCR): the
  WHOLE track — the middle 30s systematically overstates energy and
  understates dynamic range on quiet-intro/loud-climax pieces
- librosa BPM + key/mode: the middle 30s — a scalar can't express a
  multi-act piece, and the middle is the most representative sample of
  the "main body"
- AST + PaSST ensemble instruments: 10s windows tiled across the WHOLE
  track, max per label (ensemble_instruments.tag_full_track)

Two records, each of its own (record_sig): the features (audio_features,
the nine fields of FEATURE_ORDER at ANALYSIS_VERSION) and the instruments
(track_instruments, at ensemble_instruments.INSTRUMENTS_ANALYSIS_VERSION);
save_features and save_instruments are every first-hand writer's saves.
CLAP's zero-shot values — moods, the vocal score, danceability — are no
analysis here since 2026-11-10: the database derives them from the segment
embeddings a node holds (zero_shot_scores).

The two passes over the library are split by what they make (2026-11-11):
the features go with the segments in the sonic pass
(embeddings.AudioEmbeddingGenerator.sonic_pass, which calls
features_from_array on its one decode), the instruments in the instrument
pass here (AudioAnalyzer.instrument_pass), after it and behind the owner's
switch (enrichment.instruments). Each decodes a track once.
"""

import logging
import os
import subprocess
import tempfile
import threading
from typing import Any, Dict, Optional

import librosa
import numpy as np
from scipy.stats import pearsonr
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session
from tqdm import tqdm

import provenance
from config import settings
from database import get_db_context
from file_stat import FileStat, stat_of
from ensemble_instruments import INSTRUMENTS_ANALYSIS_VERSION
from models import AudioFeature, TrackInstruments
from record_sig import FEATURE_ORDER

logger = logging.getLogger(__name__)

# Methodology version stamped on audio_features rows and carried through P2P
# sync (peers re-pull rows whose version is older than the source's):
#   1 — middle-30s everything
#   2 — whole-track amplitude block, tempo and key from the middle 30 s
#       (2026-07-03)
ANALYSIS_VERSION = 2

# The rate every track is decoded at: the instrument tagger resamples from it
# to each model's own, librosa to its 22050 Hz.
DECODE_SR = 48000

# Krumhansl-Schmuckler key profiles
_MAJOR_PROFILE = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09,
                            2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
_MINOR_PROFILE = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53,
                            2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
_KEY_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


# The decode's deadline: five minutes, as subprocess.run's timeout was, and a
# quarter of the track's known length more — a decode slower than four times
# real time has stalled, whatever the track.
DECODE_TIMEOUT_SECONDS = 300


class TrackTooLong(Exception):
    """A decode passed the longest track the pass admitted
    (analysis_pace.longest_track_seconds): its file says less than it holds."""


class ShortDecode(Exception):
    """A decode that ended short of the length the file's own header
    declares: a file caught mid-copy declares its whole length, reads every
    tag and decodes what is there with exit code 0 (LIBRARY-WATCH M6), a
    truncated file the same. An analysis of it would be sealed under the
    material declaration of the whole, so nothing is made of it."""


class ChangedUnderDecode(Exception):
    """The file's stat moved while it was decoded, or differs from the one
    its row recorded at its last read: the bytes decoded may be no version
    of the file the catalogue holds. Its change brings the re-read."""


# The formats whose header declares the length exactly (STREAMINFO, the
# WavPack/APE/TTA headers, DSF/DFF's sample counts, a WAV/AIFF data chunk,
# an Ogg stream's last granule, MP4's media header). An MP3 is left out: its
# length is mutagen's estimate unless a Xing/VBRI/LAME header carries it,
# which the scan does not record.
EXACT_LENGTH_FORMATS = {".flac", ".ape", ".wv", ".tta", ".dsf", ".dff", ".wav", ".aiff",
                        ".ogg", ".m4a"}
# A damaged frame the decoder drops mid-file, or an AAC encoder's priming,
# is a fraction of a second; a copy caught mid-way is the rest of the track.
DECLARED_LENGTH_TOLERANCE_S = 1.0


def check_whole(audio_seconds: float, local_path: str, duration_seconds: Optional[float]) -> None:
    """ShortDecode when a format that declares its length exactly decoded
    short of it."""
    if (duration_seconds
            and os.path.splitext(local_path)[1].lower() in EXACT_LENGTH_FORMATS
            and audio_seconds < float(duration_seconds) - DECLARED_LENGTH_TOLERANCE_S):
        raise ShortDecode(f"{local_path} decodes to {audio_seconds:.1f} s of the "
                          f"{float(duration_seconds):.1f} s its header declares")


def recorded_stat(row) -> Optional[FileStat]:
    """The stat a pass's queue row says the file had at its last read; None
    for a row read before the stat was recorded."""
    if row.stat_mtime_ns is None:
        return None
    return FileStat(row.file_size_bytes, row.stat_mtime_ns, row.stat_ctime_ns)


def _stat_now(local_path: str) -> Optional[FileStat]:
    try:
        return stat_of(local_path)
    except OSError:
        return None     # the decode that follows fails on its own


def decode_as_read(row, local_path: str, decode):
    """`decode()` of a queued file, held to the version its row describes:
    ChangedUnderDecode when the file differs from its last read's stat
    before the decode, or moved while it ran — the decode may then be of no
    version the catalogue holds, and the change itself brings the re-read
    (scanner: a stat that moved is read again). ShortDecode comes from the
    decode (check_whole). Optimistic: nothing waits, the race cannot be
    saved."""
    before = _stat_now(local_path)
    if before is None:
        return None             # unreadable now: a failed decode, no refusal to keep
    recorded = recorded_stat(row)
    if recorded is not None and before != recorded:
        raise ChangedUnderDecode(f"{local_path} changed since its last read")
    audio = decode()
    after = _stat_now(local_path)
    if after is None:
        return None             # a mount's hiccup is no change of the file
    if after != before:
        raise ChangedUnderDecode(f"{local_path} changed while it was decoded")
    return audio


_AS_READ = """id = :id AND file_size_bytes IS NOT DISTINCT FROM :size
              AND stat_mtime_ns IS NOT DISTINCT FROM :mtime
              AND stat_ctime_ns IS NOT DISTINCT FROM :ctime"""


def _as_read_params(row) -> Dict[str, Any]:
    return {"id": row.media_file_id, "size": row.file_size_bytes,
            "mtime": row.stat_mtime_ns, "ctime": row.stat_ctime_ns}


def refuse(db: Session, row, reason: Exception) -> None:
    """A decode refused: the file waits, out of both passes' queues, until
    it is read again — every re-read clears the mark (scanner), nothing else
    does. Marked only on the row as the pass read it: a file re-read since
    is another version, not this refusal's."""
    db.execute(sa_text(f"UPDATE media_files SET decode_refused = true WHERE {_AS_READ}"),
               _as_read_params(row))
    logger.error(f"No analysis made of track {row.track_id}: {reason}")


def still_as_read(db: Session, row) -> bool:
    """Whether the file's row is still the version the pass decoded, asked
    right before the save: a re-read that came first is the catalogue's
    version, pending again, and the save is not made. One that lands between
    this and the batch's commit unlinks the file's sources (scanner.
    reread_metadata), so the record saved under them is pending again too:
    no lock is held through the batch's work."""
    return db.execute(sa_text(f"SELECT 1 FROM media_files WHERE {_AS_READ}"),
                      _as_read_params(row)).first() is not None


def load_full_track_48k(local_path: str, cue_start: Optional[float] = None,
                        cue_end: Optional[float] = None,
                        duration_seconds: Optional[float] = None,
                        max_seconds: Optional[float] = None) -> np.ndarray:
    """Decode the whole file to 48kHz mono float32 via ffmpeg — one decode
    path shared by the scanner and backfills so scores stay comparable.
    ffmpeg outruns audioread severalfold on mp3 (which has no seek table and
    must be decoded sequentially anyway). cue_start/cue_end bound a CUE image
    slice: -ss before -i input-seeks (accurate_seek drops samples to the exact
    position), so the returned array IS the track — every downstream window
    grid stays array-relative and slice-agnostic.

    ffmpeg's output is read straight into one array sized from the known
    duration (a second over it; doubled should the file run longer), never
    gathered as bytes and copied — the track is held once, not twice. Past
    `max_seconds` of audio (the pass's bound) the decode stops, ffmpeg is
    killed and TrackTooLong raised: a duration a tag under-states never
    makes the decode hold more than the bound admits. Its errors go to a
    file, not a pipe: a damaged file can say more than a pipe holds while
    its samples are read."""
    cmd = ["ffmpeg", "-v", "error"]
    if cue_start is not None:
        cmd += ["-ss", f"{cue_start:.6f}"]
    cmd += ["-i", local_path]
    if cue_end is not None:
        cmd += ["-t", f"{cue_end - (cue_start or 0.0):.6f}"]
    cmd += ["-ac", "1", "-ar", "48000", "-f", "f32le", "pipe:1"]
    expected = int((float(duration_seconds) if duration_seconds else 600.0) * DECODE_SR) + DECODE_SR
    limit = None if max_seconds is None else int(max_seconds * DECODE_SR) * 4
    if limit is not None:
        expected = min(expected, limit // 4 + DECODE_SR)
    buf = np.empty(expected, dtype=np.float32)
    got = 0
    seconds_allowed = DECODE_TIMEOUT_SECONDS + (float(duration_seconds) / 4 if duration_seconds else 0)
    killed_at_deadline = threading.Event()

    with tempfile.TemporaryFile() as errors:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errors)

        def _deadline():
            killed_at_deadline.set()
            proc.kill()
        deadline = threading.Timer(seconds_allowed, _deadline)
        deadline.start()
        try:
            while True:
                if got == buf.nbytes:
                    size = len(buf) * 2 if limit is None else min(len(buf) * 2, limit // 4 + DECODE_SR)
                    grown = np.empty(size, dtype=np.float32)
                    grown[:len(buf)] = buf
                    buf = grown
                n = proc.stdout.readinto(memoryview(buf).cast("B")[got:])
                if not n:
                    break
                got += n
                if limit is not None and got > limit:
                    raise TrackTooLong(f"{local_path} decodes past {max_seconds / 60:.0f} min")
            proc.stdout.close()
            returncode = proc.wait()
        finally:
            deadline.cancel()
            if proc.poll() is None:         # the loop raised: ffmpeg goes with it
                proc.kill()
                proc.wait()
            proc.stdout.close()
        if killed_at_deadline.is_set():
            logger.warning(f"Decode of {local_path} passed its deadline of {seconds_allowed:.0f} s: killed")
            raise subprocess.TimeoutExpired(cmd, seconds_allowed)
        if returncode != 0:
            errors.seek(0)
            raise subprocess.CalledProcessError(returncode, cmd,
                                                stderr=errors.read().decode("utf-8", "replace"))
    audio = buf[:got // 4]
    if not len(audio):
        raise ValueError(f"ffmpeg produced no samples for {local_path}")
    check_whole(len(audio) / DECODE_SR, local_path, duration_seconds)
    return audio


def middle_segment(audio: np.ndarray, sr: int, seconds: float) -> np.ndarray:
    n = int(seconds * sr)
    if len(audio) <= n:
        return audio
    start = (len(audio) - n) // 2
    return audio[start:start + n]


# librosa's frame grid for the amplitude block: its defaults for rms,
# spectral_centroid and zero_crossing_rate (frame and FFT 2048, hop 512,
# centred: the signal padded by half a frame at either end — zeros for the
# RMS and the STFT, the edge value for the zero crossings).
FRAME = 2048
HOP = 512
# Frames computed at a time: about 47 s of 22 050 Hz audio, a few MB of frame
# arrays whatever the track's length.
CHUNK_FRAMES = 2048


def _padded_slice(y: np.ndarray, start: int, stop: int, mode: str) -> np.ndarray:
    """Samples [start, stop) of `y` padded by FRAME // 2 at either end — the
    centred signal librosa frames — without copying the whole of it."""
    pad = FRAME // 2
    lo, hi = max(0, start - pad), min(len(y), stop - pad)
    part = y[lo:hi] if hi > lo else y[:0]
    before, after = max(0, pad - start), max(0, stop - pad - len(y))
    if not (before or after):
        return part
    if mode == "edge":
        return np.concatenate([np.full(before, y[0], dtype=y.dtype), part,
                               np.full(after, y[-1], dtype=y.dtype)])
    return np.concatenate([np.zeros(before, dtype=y.dtype), part, np.zeros(after, dtype=y.dtype)])


def amplitude_frames(y: np.ndarray, sr: int):
    """librosa's per-frame RMS, spectral centroid and zero-crossing rate of
    the whole signal, computed CHUNK_FRAMES frames at a time over the
    centred signal: each frame sees the samples it sees in librosa's
    whole-signal call, so the values are those, while the frame arrays a
    whole-signal call builds (two STFT-sized ones and more, ~67 MB per
    minute of audio) never exist."""
    n_frames = 1 + len(y) // HOP
    rms, centroid, zcr = [], [], []
    for first in range(0, n_frames, CHUNK_FRAMES):
        last = min(first + CHUNK_FRAMES, n_frames)
        start, stop = first * HOP, (last - 1) * HOP + FRAME
        zeros = _padded_slice(y, start, stop, "constant")
        rms.append(librosa.feature.rms(y=zeros, frame_length=FRAME, hop_length=HOP,
                                       center=False)[0])
        centroid.append(librosa.feature.spectral_centroid(y=zeros, sr=sr, n_fft=FRAME,
                                                          hop_length=HOP, center=False)[0])
        zcr.append(librosa.feature.zero_crossing_rate(_padded_slice(y, start, stop, "edge"),
                                                      frame_length=FRAME, hop_length=HOP,
                                                      center=False)[0])
    return np.concatenate(rms), np.concatenate(centroid), np.concatenate(zcr)


def amplitude_features(y: np.ndarray, sr: int) -> Dict[str, Any]:
    """RMS/dynamic-range/brightness/zcr block, shared by the scanner and the
    full-track backfill so both produce methodologically identical values.
    brightness (centroid/nyquist) and zcr are SR-relative — callers must pass
    the same 22050Hz signal the scanner has always used, or historic values
    stop being comparable. Frame-wise stats over the WHOLE given signal: mean
    for level/spectral features, p95-p5 for dynamic range (a mean would
    erase exactly the quiet-intro-vs-loud-climax spread it measures). The
    frames come chunk by chunk (amplitude_frames), the statistics over all
    of them."""
    features: Dict[str, Any] = {}

    rms, spectral_centroid, zcr = amplitude_frames(y, sr)
    features["energy"] = round(float(np.mean(rms)), 6)
    rms_db = librosa.amplitude_to_db(rms)
    features["energy_db"] = round(float(np.mean(rms_db)), 2)

    if len(rms_db) > 1:
        features["dynamic_range_db"] = round(
            float(np.percentile(rms_db, 95) - np.percentile(rms_db, 5)), 2
        )
    else:
        features["dynamic_range_db"] = 0.0

    nyquist = sr / 2.0
    features["brightness"] = round(float(np.mean(spectral_centroid) / nyquist), 4)

    features["zero_crossing_rate"] = round(float(np.mean(zcr)), 6)

    return features



def save_features(db: Session, track_id, features: Dict[str, Any], src_id: int) -> bool:
    """The track's features record from the copy `src_id` describes — the
    fields of record_sig.FEATURE_ORDER at ANALYSIS_VERSION — unless a better
    copy's stand (provenance.outranked): each pass replaces its own kind at
    its save by the rank. A changed value sheds the row's seal (its guard),
    and sign_audio signs the new one. Returns whether it wrote."""
    existing = db.query(AudioFeature).filter(AudioFeature.track_id == track_id).first()
    if existing and provenance.outranked(db, existing.analysis_source_id, src_id):
        return False
    values = {k: features.get(k) for k in FEATURE_ORDER}
    if existing:
        for k, v in values.items():
            setattr(existing, k, v)
        existing.analysis_source_id = src_id
        existing.analysis_version = ANALYSIS_VERSION
    else:
        db.add(AudioFeature(track_id=track_id, analysis_source_id=src_id,
                            analysis_version=ANALYSIS_VERSION, **values))
    return True


def save_instruments(db: Session, track_id, instruments: Dict[str, float], src_id: int) -> bool:
    """The track's instruments record from the copy `src_id` describes — the
    tagger's raw score per label at INSTRUMENTS_ANALYSIS_VERSION — unless a
    better copy's stand, by the rank as save_features. Returns whether it
    wrote."""
    existing = db.query(TrackInstruments).filter(TrackInstruments.track_id == track_id).first()
    if existing and provenance.outranked(db, existing.analysis_source_id, src_id):
        return False
    if existing:
        existing.instruments = instruments
        existing.analysis_source_id = src_id
        existing.analysis_version = INSTRUMENTS_ANALYSIS_VERSION
    else:
        db.add(TrackInstruments(track_id=track_id, instruments=instruments,
                                analysis_source_id=src_id,
                                analysis_version=INSTRUMENTS_ANALYSIS_VERSION))
    return True


class AudioAnalyzer:
    """Extract a track's features (librosa) and instruments (the AST+PaSST
    ensemble) from its whole decoded audio."""

    def __init__(
        self,
        sample_rate: Optional[int] = None,
        duration: Optional[int] = None,
        device: Optional[str] = None,
    ):
        self.librosa_sr = sample_rate or settings.audio_analysis_sample_rate
        self.duration = duration or settings.audio_analysis_duration

        from device import get_device
        self.device = device or get_device()
        self.ensemble = None

    # --- Model management ---

    def load_model(self):
        """Acquire the process-wide AST+PaSST ensemble singleton."""
        if self.ensemble is not None:
            return
        from instrument_tagger import get_instrument_tagger
        self.ensemble = get_instrument_tagger(self.device)

    # --- librosa DSP features ---

    def _detect_key(self, y: np.ndarray, sr: int) -> Dict[str, Any]:
        """Detect musical key using Krumhansl-Schmuckler algorithm."""
        chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
        chroma_mean = np.mean(chroma, axis=1)

        best_corr = -2.0
        best_key = 0
        best_mode = "major"

        for shift in range(12):
            rolled = np.roll(chroma_mean, -shift)

            corr_major, _ = pearsonr(rolled, _MAJOR_PROFILE)
            if corr_major > best_corr:
                best_corr = corr_major
                best_key = shift
                best_mode = "major"

            corr_minor, _ = pearsonr(rolled, _MINOR_PROFILE)
            if corr_minor > best_corr:
                best_corr = corr_minor
                best_key = shift
                best_mode = "minor"

        # Normalize confidence to 0-1 range (pearson is -1 to 1, typical range 0.3-0.9)
        confidence = max(0.0, min(1.0, (best_corr + 1.0) / 2.0))

        return {
            "key": _KEY_NAMES[best_key],
            "mode": best_mode,
            "key_confidence": round(float(confidence), 3),
        }

    def _extract_tempo_key(self, y: np.ndarray, sr: int) -> Dict[str, Any]:
        """BPM + key/mode — callers pass the middle segment, not the whole
        track (see the module docstring)."""
        features = {}

        tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
        # librosa may return an array; extract scalar
        if hasattr(tempo, '__len__'):
            tempo = float(tempo[0]) if len(tempo) > 0 else 0.0
        features["bpm"] = round(float(tempo), 2) if tempo > 0 else None

        features.update(self._detect_key(y, sr))
        return features

    def _librosa_features(self, y22_full: np.ndarray) -> Dict[str, Any]:
        """Tempo/key from the middle segment + amplitude block from the whole
        given 22050Hz signal."""
        y_mid = middle_segment(y22_full, self.librosa_sr, self.duration)
        features = self._extract_tempo_key(y_mid, self.librosa_sr)
        features.update(amplitude_features(y22_full, self.librosa_sr))
        return features

    # --- The two passes' parts ---

    def features_from_array(self, audio: np.ndarray, sr: int = DECODE_SR) -> Dict[str, Any]:
        """A track's features from its whole decoded audio — the sonic
        pass's librosa part, and the stream enricher's."""
        y_librosa = librosa.resample(audio, orig_sr=sr, target_sr=self.librosa_sr)
        return self._librosa_features(y_librosa)

    def instrument_pass(
        self,
        limit: Optional[int] = None,
        force: bool = False,
        order_by_date: bool = False,
        max_duration_seconds: Optional[int] = None,
        track_ids: Optional[list] = None,
        worker_id: Optional[int] = None,
        worker_count: Optional[int] = None,
        cancel_flag=None,
        on_batch=None,
        attempted: Optional[set] = None,
    ) -> Dict[str, int]:
        """The instrument pass over the tracks whose instruments record is
        pending (analysis_pace.INSTRUMENTS_PENDING_SQL): each track's
        analysis-source file registered first (the sonic pass before it
        normally has), skipped when the file's material group already holds
        instruments at a rank no lower (provenance.held_in_group), then one
        decode and the tagger, saved by the rank (save_instruments). After
        each batch: the pace (analysis_pace) and the notary's wake — sign()
        leaves a record re-saved while it signed unsigned until the next
        wake.

        Architecture: a batch is registered in the main thread, decoded in
        the profile's prefetch pool, tagged one whole track at a time on the
        accelerator (its windows are batched inside), saved with a savepoint
        per track and committed per batch.

        Args:
            limit: Max tracks to process.
            force: Re-tag every analysis-source track regardless of state.
            order_by_date: Process newest tracks first.
            max_duration_seconds: Maximum duration in seconds.
            track_ids: If provided, only process these track IDs.
            worker_id: Worker index (0-based) for parallel processing.
            worker_count: Total number of workers for parallel processing.
            cancel_flag: Asked before every batch; True ends the pass there.
            on_batch: Called with (tracks done, tracks queued) after each batch.
            attempted: media_files ids this run's earlier instrument pass
                took already — left out, and every file this one takes, or
                leaves out as too long, is added: a pass resumed after giving
                way does not try again a file it could not address or decode,
                nor count again one past reach.

        Returns:
            processed, success (saved), failed, skipped (the group holds
            them, or a better copy's record stands).
        """
        import time
        from concurrent.futures import ThreadPoolExecutor

        import analysis_pace
        import notary

        provenance.require_fingerprinter()
        if not force:
            # A failed look costs only the work it would have saved: the
            # pass analyses what stays pending, as before.
            try:
                provenance.relink_returned_files(track_ids, cancel_check=cancel_flag)
            except Exception as e:
                logger.error(f"Re-link before the pass failed: {e}", exc_info=True)
        stats = {"processed": 0, "success": 0, "failed": 0, "skipped": 0, "too_long": 0,
                 "refused": 0}
        start_time = time.time()
        batch_size = 16
        from hardware_profile import resolve as _hw
        prefetch_workers = _hw().prefetch_workers

        with get_db_context() as db:
            from embeddings import _reanalyze_imported
            query_sql = (analysis_pace.INSTRUMENTS_ROWS_SQL if force
                         else analysis_pace.INSTRUMENTS_PENDING_SQL)
            params = {"min_seconds": provenance.MIN_MATERIAL_SECONDS,
                      "reanalyze": _reanalyze_imported()}
            if track_ids is not None:
                query_sql += " AND mf.track_id = ANY(:track_ids)"
                params["track_ids"] = track_ids
            if attempted is not None:
                query_sql += " AND mf.id <> ALL(CAST(:attempted AS integer[]))"
                params["attempted"] = sorted(attempted)
            # A track longer than this machine can hold now is left out by
            # its known duration (analysis_pace.longest_track_seconds), and
            # counted: the decode and the tagger's resamples grow with it. A
            # run that resumes the pass after a give-way counts it once — it
            # is among what the run tried
            params["longest"] = analysis_pace.longest_track_seconds([analysis_pace.INSTRUMENTS])
            beyond = db.execute(sa_text(
                f"SELECT b.media_file_id FROM ({query_sql}{analysis_pace.beyond_reach_sql()}) b"),
                params).scalars().all()
            stats["too_long"] = len(beyond)
            if attempted is not None:
                attempted.update(beyond)
            if stats["too_long"]:
                logger.info(f"Instrument pass: {stats['too_long']} track(s) longer than this machine "
                            f"can hold now ({params['longest'] / 60:.0f} min) are left to the network")
            query_sql += analysis_pace.within_reach_sql()

            # Gaps first — coverage before recomputation (as the sonic pass)
            if order_by_date:
                query_sql += " ORDER BY gap DESC, mf.file_modified_at DESC NULLS LAST"
            else:
                query_sql += " ORDER BY gap DESC, mf.track_id"
            if limit:
                query_sql += f" LIMIT {limit}"

            rows = db.execute(sa_text(query_sql), params).fetchall()
            total = len(rows)
            if total == 0:
                (logger.debug if track_ids is not None else logger.info)(
                    "No tracks pending the instrument pass")
                return stats

            logger.info(f"Instrument pass: {total} tracks "
                        f"(prefetch={prefetch_workers}, batch={batch_size})")

            # Batches are cut by BOTH row count and total audio duration —
            # whole tracks are held decoded in RAM for the batch, and 16
            # Schulze-length pieces at once would be ~14GB.
            # The tagger loads at the first batch that has something to tag;
            # the batches are cut before that, so the budget leaves room for it
            from device import audio_batch_budget_seconds, empty_cache
            from ensemble_instruments import RESIDENT_GB
            batch_budget_seconds = audio_batch_budget_seconds(
                reserve_gb=0.0 if analysis_pace.resident("tagger") else RESIDENT_GB)
            batches = []
            cur, cur_seconds = [], 0.0
            for row in rows:
                dur = float(row.duration_seconds) if row.duration_seconds else analysis_pace.UNKNOWN_SECONDS
                if cur and (len(cur) >= batch_size
                            or cur_seconds + dur > batch_budget_seconds):
                    batches.append(cur)
                    cur, cur_seconds = [], 0.0
                cur.append(row)
                cur_seconds += dur
            if cur:
                batches.append(cur)

            pbar = tqdm(total=total, desc="Instrument pass", unit="track")
            io_pool = ThreadPoolExecutor(max_workers=prefetch_workers)
            try:
                for batch_rows in batches:
                    if cancel_flag and cancel_flag():
                        logger.info("Instrument pass cancelled")
                        break
                    if max_duration_seconds:
                        elapsed = time.time() - start_time
                        if elapsed >= max_duration_seconds:
                            logger.info(f"Time limit reached ({elapsed:.1f}s), stopping")
                            break

                    batch_began = time.monotonic()
                    stats["processed"] += len(batch_rows)
                    if attempted is not None:
                        attempted.update(r.media_file_id for r in batch_rows)
                    # What this machine holds is read again as each batch is
                    # planned: a model loaded beside the pass since the queue
                    # was read (a search's text encoder) leaves less room
                    longest = analysis_pace.longest_track_seconds([analysis_pace.INSTRUMENTS])
                    within = [r for r in batch_rows
                              if r.duration_seconds is None or r.duration_seconds <= longest]
                    stats["too_long"] += len(batch_rows) - len(within)
                    todo = self._register_batch(db, within, force, stats)
                    # The registrations commit before the long part, so no
                    # other pass waits on them while the batch is tagged
                    db.commit()
                    if todo and self.ensemble is None:
                        # loaded once there is something to tag: a queue the
                        # groups hold whole never takes the pair's memory
                        self.load_model()
                        batch_began = time.monotonic()

                    decoded = list(io_pool.map(lambda row: self._decode(row, longest),
                                               [row for row, _ in todo]))
                    analysed = 0.0
                    wrote = False
                    # a decode that failed spent time on no audio: the batch
                    # says nothing true about the pace (nor one a tagging failed)
                    whole = all(isinstance(audio, np.ndarray) for audio in decoded)
                    for (row, src_id), audio in zip(todo, decoded):
                        if audio is TrackTooLong:
                            stats["too_long"] += 1
                            continue
                        if isinstance(audio, Exception):
                            refuse(db, row, audio)
                            stats["refused"] += 1
                            continue
                        if audio is None:
                            stats["failed"] += 1
                            continue
                        outcome = self._tag_track(db, row, src_id, audio)
                        stats[outcome] += 1
                        wrote = wrote or outcome == "success"
                        # a tagging that failed spent time on no record: the
                        # batch is no pace either
                        whole = whole and outcome != "failed"
                        analysed += len(audio) / DECODE_SR
                    del decoded
                    # Batch GPU work (AST/PaSST) is done — return the
                    # allocator's free blocks so the footprint tracks the live
                    # per-batch working set, not the run's cumulative peak (MPS
                    # unified memory otherwise holds ~28 GB for the whole run).
                    empty_cache(self.device)

                    if whole:
                        analysis_pace.record(db, analysis_pace.INSTRUMENTS,
                                             time.monotonic() - batch_began, analysed)
                    db.commit()
                    if wrote:
                        notary.wake("instrument pass")
                    if on_batch:
                        on_batch(stats["processed"], total)
                    pbar.update(len(batch_rows))
            finally:
                io_pool.shutdown(wait=True, cancel_futures=True)
                pbar.close()
            logger.info(f"Instrument pass complete: {stats['success']} success, "
                        f"{stats['skipped']} skipped, {stats['failed']} failed")

        return stats

    def _register_batch(self, db: Session, batch_rows, force: bool, stats: Dict[str, int]) -> list:
        """The batch's tracks registered before anything is decoded: (row,
        source id) for each that has instruments to make. No fingerprint =
        no address, and nothing is saved (the queue asks again next run, as
        for a file that fails to decode). Each under a savepoint of its own,
        as the sonic pass registers: a track renamed or merged by the canon
        since the queue was read fails alone."""
        todo = []
        for row in batch_rows:
            try:
                with db.begin_nested():
                    src_id = provenance.get_or_create_local(
                        db, row.track_id, row.media_file_id, row.file_path,
                        row.sample_rate, row.bit_depth, row.is_lossless,
                        row.duration_seconds, row.cue_start_seconds, row.cue_end_seconds)
                    held = src_id is not None and not force and provenance.held_in_group(
                        db, src_id, "track_instruments")
            except Exception as e:
                stats["failed"] += 1
                logger.error(f"Registration failed for {row.track_id}: {e}", exc_info=True)
                continue
            if src_id is None:
                stats["failed"] += 1
                logger.warning(f"Skipping track {row.track_id}: no content address (fingerprint)")
            elif held:
                stats["skipped"] += 1       # the same material, tagged at a rank no lower
            else:
                todo.append((row, src_id))
        return todo

    def _decode(self, row, max_seconds: float):
        """The track's audio; TrackTooLong (the class) for one that decodes
        past `max_seconds`, the refusal (ShortDecode, ChangedUnderDecode) for
        one that is not the whole file as read, None for one that fails."""
        local_path = settings.translate_to_local_path(row.file_path)
        try:
            return decode_as_read(row, local_path, lambda: load_full_track_48k(
                local_path, row.cue_start_seconds, row.cue_end_seconds,
                row.duration_seconds, max_seconds))
        except TrackTooLong as e:
            logger.info(f"Instrument pass: {e} — left to the network")
            return TrackTooLong
        except (ShortDecode, ChangedUnderDecode) as e:
            return e
        except Exception as e:
            logger.error(f"Decode failed for {row.file_path}: {e}")
            return None

    def _tag_track(self, db: Session, row, src_id: int, audio: np.ndarray) -> str:
        """One decoded track tagged and saved under a savepoint of its own:
        success, skipped (a better copy's record stands), refused (the file
        was read again since its decode: that version is pending) or
        failed."""
        try:
            instruments = self.ensemble.tag_full_track(audio)
        except Exception as e:
            logger.warning(f"Instrument tagging failed for {row.track_id}: {e}")
            return "failed"
        try:
            with db.begin_nested():
                if not still_as_read(db, row):
                    return "refused"
                saved = save_instruments(db, row.track_id, instruments, src_id)
        except Exception as e:
            logger.error(f"Failed to save instruments for {row.track_id}: {e}")
            return "failed"
        return "success" if saved else "skipped"
