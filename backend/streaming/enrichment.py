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

        # Decode the in-memory FLAC to 48k mono and take the SAME middle window
        # the scanner uses, so the embedding is comparable to owned tracks.
        audio, _ = librosa.load(io.BytesIO(flac), sr=48000, mono=True)

        # Poison guard (before loading models / GPU work): the provider resolve matches by
        # metadata + a loose ±50% duration gate, so a known-MB-duration track can
        # still stream the WRONG recording (cover / remix / live) that merely passed
        # that gate. Features from the wrong audio would poison the shared pool, and
        # unlike an owned track there is no real-file analysis shielding this slot.
        # Verify the fetched audio's actual length against the MB duration and bail
        # on divergence — tight (~5%) vs the resolve's loose gate, so a real master
        # variance still enriches but a different recording does not. Tunable.
        actual = len(audio) / 48000.0
        tol = max(7.0, 0.05 * duration)
        if abs(actual - duration) > tol:
            logger.warning(
                "preview enrich SKIP %s: fetched audio %.0fs vs MB %.0fs (>%.0fs "
                "tolerance) — likely a different recording, not enriching",
                track_id, actual, duration, tol)
            return

        embedder, analyzer = self._models()
        mid = self._middle(audio, 48000, float(settings.audio_sample_duration))

        vec = embedder._generate_batch_embeddings([mid])     # (1, 512) L2-normed | None
        feats = analyzer.analyze_from_array(mid, sr=48000)    # dict | None

        from sqlalchemy import text

        with SessionLocal() as db:
            if vec is not None and len(vec):
                model = embedder._get_or_create_embedding_model(db)
                embedder._save_embedding(
                    db, track_id, vec[0], model,
                    source_media_file_id=None, source_bit_depth=None,
                    source_sample_rate=48000, source_is_lossless=lossless,
                    is_preview=True,
                )
                # New CLAP vector → the track's album(s) audio-similarity cache is
                # stale; drop it so /similar recomputes with the richer vector set
                # (a phantom album becomes similarity-eligible only as it enriches).
                db.execute(text(
                    "DELETE FROM similar_albums WHERE source = 'clap_assignment' "
                    "AND album_id IN (SELECT album_id FROM album_tracks "
                    "WHERE track_id = :tid)"), {"tid": track_id})
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


class PreviewLyricsEnricher:
    """Fetch + embed LRCLIB lyrics for a streamed phantom track, keyed by track_id.

    Unlike the CLAP/feature enricher this is METADATA-derived: it matches on the
    phantom's MusicBrainz artist/title/album (+ duration as a hint), NOT on the
    streamed audio — so a wrong-recording stream cannot poison it, and it is NOT
    gated on a known duration (LRCLIB's search fallback covers MB-length-less
    tracks; an absent duration is just a weaker match hint). Populates track_lyrics
    AND its BGE-M3 lyrics embedding, so a streamed phantom becomes findable by the
    lyrics-similarity search and the AI DJ's get_lyrics, and joins the P2P pool.
    Idempotent: skips a track that already has lyrics (or a recorded miss), and the
    embedding step no-ops on an already-embedded track — so replays don't re-hit
    LRCLIB or the GPU. Its own single worker keeps the LRCLIB call rate gentle and
    serialises the BGE-M3 passes."""

    def __init__(self) -> None:
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="preview-lyrics")
        self._inflight: set[str] = set()
        self._lock = threading.Lock()

    def submit(self, query) -> None:
        """Queue a streamed track's lyrics fetch+embed. No-ops without the identity
        LRCLIB needs (track_id + title + artist), or if already queued."""
        tid = getattr(query, "track_id", None)
        if not tid or not query.title or not query.artist:
            return
        tid = str(tid)
        with self._lock:
            if tid in self._inflight:
                return
            self._inflight.add(tid)
        self._pool.submit(self._run, tid, query.title, query.artist,
                          query.album or None, query.duration)

    def _run(self, track_id: str, title: str, artist: str,
             album: Optional[str], duration: Optional[float]) -> None:
        try:
            self._enrich(track_id, title, artist, album, duration)
        except Exception:
            logger.exception("preview lyrics enrichment failed for %s", track_id)
        finally:
            with self._lock:
                self._inflight.discard(track_id)

    def _enrich(self, track_id: str, title: str, artist: str,
                album: Optional[str], duration: Optional[float]) -> None:
        import uuid as _uuid
        from database import SessionLocal
        from lrclib import LrclibService
        from models import ExternalMetadata, TrackLyrics

        tid = _uuid.UUID(track_id)
        with SessionLocal() as db:
            has_lyrics = db.query(TrackLyrics.id).filter(
                TrackLyrics.track_id == tid).first() is not None

        if not has_lyrics:
            with SessionLocal() as db:
                miss = db.query(ExternalMetadata.fetch_status).filter_by(
                    entity_type="track", entity_id=track_id,
                    source="lrclib", metadata_type="lyrics").first()
            if miss and miss[0] == "not_found":
                return   # known miss — don't re-hit LRCLIB on every replay
            dur = int(duration) if duration else None
            with LrclibService() as svc, SessionLocal() as db:
                result = svc.fetch_and_store(
                    db, track_id=tid, track_name=title,
                    artist_name=artist, album_name=album, duration=dur)
            logger.info("preview lyrics %s -> %s", track_id, result.get("status"))
            if result.get("status") not in ("synced", "plain"):
                return   # instrumental / not_found / error — nothing to embed

        # Embed the stored lyrics (no-ops if already embedded, or if the text
        # cleans to nothing / exceeds the junk-length cap).
        from lyrics_embeddings import generate_lyrics_embeddings
        generate_lyrics_embeddings(track_ids=[tid])
