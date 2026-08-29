"""Background CLAP/feature enrichment of previewed audio.

A phantom track has no local file → no audio embedding → it can't be found by
audio-similarity search and adds nothing to the P2P feature pool. When we stream
a preview we already hold the whole FLAC in memory; tee it through the SAME
analysis the scanner runs on owned files (CLAP 512-d embedding + librosa DSP +
AST/PaSST instruments), keyed to the phantom's track_id.

Provenance: the streamed bytes are content-addressed exactly like a local file
(analysis_sources row with origin='deezer'|'youtube', media_file_id NULL,
pcm_hash + chromaprint of the fetched audio) — this is what makes a
Deezer-lossless analysis signable against the STREAM's material without
claiming possession of any local rip (tier 3 in P2P-SYNC-INTEGRITY.md). The
origin rank (local > deezer > youtube) means an owned rip later OVERWRITES a
preview analysis, and a preview never overwrites a real-file one.

GATE: only tracks with a known catalog length (TrackQuery.duration / .lengths,
i.e. local album_tracks.length_ms) are enriched. Without it the provider match
isn't length-verified, so the audio may be the wrong recording and its features
would poison similarity search. The test is base.same_recording — the one the
host's resolve() already applied to the provider's listing, now applied to the
audio that actually arrived.

Full-track methodology, same as owned scans (balanced-grid segments, whole-track
amplitude block), so vectors land in the same embedding space.

GPU work is serialised on a single worker and reuses the process-wide CLAP and
AST/PaSST singletons (no duplicate model load — VRAM)."""
from __future__ import annotations

import io
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import numpy as np

from .base import attested_lengths, same_recording
from .events import preview_events

logger = logging.getLogger(__name__)


class PreviewEnricher:
    # Trickle idle: release AST+PaSST after this long without a preview.
    # Long enough to span between-track gaps and a paused album, short
    # enough that a lite node doesn't hold ~2GB of tagger weights all day.
    _IDLE_UNLOAD_SECONDS = 600.0

    def __init__(self) -> None:
        # One worker: serialise GPU passes. Previews trickle in per album, so a
        # single lane is plenty and keeps VRAM contention with the bulk
        # analysers off.
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="preview-enrich")
        self._inflight: set[str] = set()
        self._lock = threading.Lock()
        self._embedder = None
        self._analyzer = None
        # Trickle mode (CPU-only / lite profile — §2.7 HARDWARE-TIERS): the
        # per-track pipeline runs near listening pace there, so backlog is
        # bounded (1 running + 1 queued) and excess is DROPPED — enrichment
        # is idempotent and a dropped track gets another chance on its next
        # stream. Idle unloads the instrument pair; CLAP/BGE stay resident
        # (shared with search).
        from hardware_profile import resolve as _hw
        profile = _hw()
        self._trickle = profile.stream_enrich_mode == "trickle"
        self._max_pending = 2 if self._trickle else None
        # Idle unload follows the profile property, not trickle mode:
        # MPS-full is NOT trickle but must still release the tagger —
        # unified memory (see hardware_profile.unload_instruments_after_run).
        self._idle_unload = profile.unload_instruments_after_run
        self._idle_timer: Optional[threading.Timer] = None

    def submit(self, track_id: Optional[str], flac: Optional[bytes],
               lengths: tuple, lossless: bool = False,
               provider_id: Optional[str] = None) -> None:
        """Queue a previewed track for enrichment. No-ops without a track_id,
        catalog lengths (unverified match — see GATE) or audio, or if already
        queued. In trickle mode a full backlog drops the track instead of
        queueing. ``lossless`` is the ACTUAL fetch quality (Deezer FLAC=True,
        degraded tiers/YouTube=False); ``provider_id`` (manifest id,
        'deezer'|'youtube') becomes the provenance origin."""
        if not track_id or not lengths or not flac:
            return
        with self._lock:
            if track_id in self._inflight:
                return
            if self._max_pending is not None and len(self._inflight) >= self._max_pending:
                logger.info(
                    "preview enrich DROP %s: trickle backlog (%d pending) — "
                    "will retry on next stream", track_id, len(self._inflight))
                return
            self._inflight.add(track_id)
            if self._idle_timer is not None:
                self._idle_timer.cancel()
                self._idle_timer = None
        self._pool.submit(self._run, track_id, flac, lengths, lossless,
                          provider_id)

    # ---- worker ----------------------------------------------------------
    def _run(self, track_id: str, flac: bytes, lengths: tuple, lossless: bool,
             provider_id: Optional[str]) -> None:
        try:
            self._enrich(track_id, flac, lengths, lossless, provider_id)
        except Exception:
            logger.exception("preview enrichment failed for %s", track_id)
        finally:
            with self._lock:
                self._inflight.discard(track_id)
                if self._idle_unload and not self._inflight:
                    self._idle_timer = threading.Timer(
                        self._IDLE_UNLOAD_SECONDS, self._do_idle_unload)
                    self._idle_timer.daemon = True
                    self._idle_timer.start()

    def _do_idle_unload(self) -> None:
        """One-shot idle timeout (event-scheduled after the last completion,
        cancelled by new work — not a poll). Drops the analyzer so its
        AST+PaSST reference dies, then releases the singleton weights."""
        with self._lock:
            if self._inflight:
                return
            self._idle_timer = None
            analyzer, self._analyzer = self._analyzer, None
        if analyzer is not None:
            from device import get_device
            from instrument_tagger import release_instrument_tagger
            release_instrument_tagger(get_device())
            self._empty_cache()
            logger.info("preview enrich idle — instrument models released")

    def _enrich(self, track_id: str, flac: bytes, lengths: tuple,
                lossless: bool, provider_id: Optional[str]) -> None:
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

        # Poison guard (before loading models / GPU work): the resolve held the
        # provider's LISTING to the catalog length; this holds the audio that
        # arrived to it, against every length the catalog attests. A listing
        # can lie (a mislabelled upload, a source with no length to check),
        # and features from the wrong audio would poison the shared pool with
        # no real-file analysis shielding this slot.
        actual = len(audio) / 48000.0
        if not any(same_recording(actual, want) for want in lengths):
            logger.warning(
                "preview enrich SKIP %s: fetched audio %.0fs vs catalog %s — "
                "not the recording, not enriching",
                track_id, actual, "/".join(f"{w:.0f}s" for w in lengths))
            return

        embedder, analyzer = self._models()
        # Full-track methodology, same as owned scans: amplitude block +
        # windowed instruments over the whole streamed audio, bpm/key from
        # its middle (analyze_from_array slices that itself).
        feats = analyzer.analyze_from_array(audio, sr=48000)   # dict | None

        import provenance

        with SessionLocal() as db:
            # Content-address the STREAMED bytes before saving anything they
            # produced; a fingerprint failure saves unlinked (src_id NULL) and
            # the owned-upgrade predicate eventually replaces the rows.
            src_id = provenance.create_stream_source(
                db, track_id, flac, provider_id, lossless)
            model = embedder._get_or_create_embedding_model(db)
            # Balanced-grid segments from the full streamed audio; the track
            # vector is their normalized mean. _persist_analysis decides the
            # overwrite by origin rank BEFORE writing, so a preview never
            # touches a real-file analysis (segments included).
            saved = False
            computed = embedder._compute_segments(audio)
            if computed is not None:
                idxs, vecs, portrait = computed
                saved = embedder._persist_analysis(
                    db, track_id, model, idxs, vecs, portrait, src_id,
                    origin=provider_id or "", is_lossless=lossless)
            if feats:
                self._save_features(db, track_id, feats, src_id, provider_id)
            db.commit()

        self._empty_cache()
        preview_events.ping()   # features committed → open album page re-fetches key·bpm
        logger.info("preview enriched %s (embedding=%s features=%s)",
                    track_id, saved, bool(feats))

    @staticmethod
    def _save_features(db, track_id: str, feats: dict, src_id,
                       provider_id: Optional[str]) -> None:
        import provenance
        from audio_analysis import ANALYSIS_VERSION
        from models import AudioFeature

        existing = db.query(AudioFeature).filter(
            AudioFeature.track_id == track_id).first()
        if existing and existing.analysis_source_id is not None:
            old_origin = provenance.origin_of(db, existing.analysis_source_id)
            if (provenance.ORIGIN_RANK.get(old_origin, -1)
                    > provenance.ORIGIN_RANK.get(provider_id, -1)):
                return   # never let a preview overwrite a better-origin analysis

        cols = ("bpm", "key", "mode", "key_confidence", "energy", "energy_db",
                "brightness", "dynamic_range_db", "zero_crossing_rate",
                "instruments", "moods", "vocal_instrumental", "vocal_score",
                "danceability")
        if existing:
            for k in cols:
                if k in feats:
                    setattr(existing, k, feats[k])
            existing.analysis_source_id = src_id
            existing.analysis_version = ANALYSIS_VERSION
        else:
            db.add(AudioFeature(
                track_id=track_id,
                analysis_source_id=src_id,
                analysis_version=ANALYSIS_VERSION,
                **{k: feats.get(k) for k in cols},
            ))

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
            # device.empty_cache handles MPS too — the direct cuda call
            # was a no-op on Mac nodes and left the preview-enrichment
            # pool parked in unified memory.
            from device import empty_cache
            empty_cache()
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
    lyrics-similarity search and the AI assistant's get_lyrics, and joins the P2P pool.
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
