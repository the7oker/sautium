"""Background CLAP/feature enrichment of previewed audio.

A phantom track has no local file → no audio embedding → it can't be found by
audio-similarity search and adds nothing to the P2P feature pool. When we stream
a preview we already hold the whole FLAC in memory; tee it through the SAME
analysis the scanner runs on owned files (CLAP 512-d embedding + librosa DSP +
AST/PaSST instruments), keyed to the phantom's track_id.

Provenance: features carry source_media_file_id=NULL + source_is_lossless=False
(a preview is lossy, from no local file). The embedding quality guard then treats
them as low priority — an owned rip later OVERWRITES them, and a preview never
overwrites a real-file analysis (explicit guards below).

GATE: only tracks with a known duration (TrackQuery.duration, i.e. local
album_tracks.length_ms) are enriched. Without it the YouTube match isn't
length-verified, so the audio may be the wrong recording and its features would
poison similarity search.

Windowing matches the scanner exactly (middle ``audio_sample_duration`` seconds)
so the vector lands in the same embedding space and cosine stays comparable.

GPU work is serialised on a single worker and reuses the process-wide CLAP and
AST/PaSST singletons (no duplicate model load — VRAM)."""
from __future__ import annotations

import io
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import numpy as np

from .events import preview_events

logger = logging.getLogger(__name__)


class PreviewEnricher:
    def __init__(self) -> None:
        # One worker: serialise GPU passes. Previews trickle in per album, so a
        # single lane is plenty and keeps VRAM contention with the bulk
        # analysers off.
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="preview-enrich")
        self._inflight: set[str] = set()
        self._lock = threading.Lock()
        self._embedder = None
        self._analyzer = None

    def submit(self, track_id: Optional[str], flac: Optional[bytes],
               duration: Optional[float], lossless: bool = False) -> None:
        """Queue a previewed track for enrichment. No-ops without a track_id, a
        duration (unverified match — see GATE) or audio, or if already queued.
        ``lossless`` is the provider's source-quality flag (Deezer FLAC=True,
        YouTube=False) — it sets the feature provenance + embedding quality tier."""
        if not track_id or not duration or not flac:
            return
        with self._lock:
            if track_id in self._inflight:
                return
            self._inflight.add(track_id)
        self._pool.submit(self._run, track_id, flac, duration, lossless)

    # ---- worker ----------------------------------------------------------
    def _run(self, track_id: str, flac: bytes, duration: float, lossless: bool) -> None:
        try:
            self._enrich(track_id, flac, duration, lossless)
        except Exception:
            logger.exception("preview enrichment failed for %s", track_id)
        finally:
            with self._lock:
                self._inflight.discard(track_id)

    def _enrich(self, track_id: str, flac: bytes, duration: float, lossless: bool) -> None:
        import librosa
        from config import settings
        from database import SessionLocal
        from models import Embedding

        # Idempotent: a previously-enriched (or owned) track already has an
        # embedding — skip the decode + GPU entirely.
        with SessionLocal() as db:
            if db.query(Embedding.id).filter(Embedding.track_id == track_id).first():
                return

        embedder, analyzer = self._models()

        # Decode the in-memory FLAC to 48k mono and take the SAME middle window
        # the scanner uses, so the embedding is comparable to owned tracks.
        audio, _ = librosa.load(io.BytesIO(flac), sr=48000, mono=True)
        mid = self._middle(audio, 48000, float(settings.audio_sample_duration))

        vec = embedder._generate_batch_embeddings([mid])     # (1, 512) L2-normed | None
        feats = analyzer.analyze_from_array(mid, sr=48000)    # dict | None

        with SessionLocal() as db:
            if vec is not None and len(vec):
                model = embedder._get_or_create_embedding_model(db)
                embedder._save_embedding(
                    db, track_id, vec[0], model,
                    source_media_file_id=None, source_bit_depth=None,
                    source_sample_rate=48000, source_is_lossless=lossless,
                    is_preview=True,
                )
            if feats:
                self._save_features(db, track_id, feats, lossless)
            db.commit()

        self._empty_cache()
        preview_events.ping()   # features committed → open album page re-fetches key·bpm
        logger.info("preview enriched %s (embedding=%s features=%s)",
                    track_id, vec is not None, bool(feats))

    @staticmethod
    def _save_features(db, track_id: str, feats: dict, lossless: bool) -> None:
        from models import AudioFeature

        existing = db.query(AudioFeature).filter(
            AudioFeature.track_id == track_id).first()
        if existing and existing.source_media_file_id is not None:
            return   # never let a preview overwrite a real-file analysis

        cols = ("bpm", "key", "mode", "key_confidence", "energy", "energy_db",
                "brightness", "dynamic_range_db", "zero_crossing_rate",
                "instruments", "moods", "vocal_instrumental", "vocal_score",
                "danceability")
        if existing:
            for k in cols:
                if k in feats:
                    setattr(existing, k, feats[k])
            existing.source_media_file_id = None
            existing.source_bit_depth = None
            existing.source_sample_rate = 48000
            existing.source_is_lossless = lossless
        else:
            db.add(AudioFeature(
                track_id=track_id,
                source_media_file_id=None, source_bit_depth=None,
                source_sample_rate=48000, source_is_lossless=lossless,
                **{k: feats.get(k) for k in cols},
            ))

    @staticmethod
    def _middle(audio: np.ndarray, sr: int, seconds: float) -> np.ndarray:
        n = int(seconds * sr)
        if len(audio) <= n:
            return audio
        start = (len(audio) - n) // 2
        return audio[start:start + n]

    def _models(self):
        # Lazy: load the singletons on first preview (CLAP + AST/PaSST). Reused
        # across previews; the same process-wide models the scanner uses.
        if self._embedder is None:
            from embeddings import AudioEmbeddingGenerator
            emb = AudioEmbeddingGenerator()
            emb.load_model()
            self._embedder = emb
        if self._analyzer is None:
            from audio_analysis import AudioAnalyzer
            an = AudioAnalyzer()
            an.load_model()
            self._analyzer = an
        return self._embedder, self._analyzer

    @staticmethod
    def _empty_cache() -> None:
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
