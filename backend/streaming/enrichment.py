"""Background CLAP/feature enrichment of previewed audio.

A phantom track has no local file → no audio embedding → it can't be found by
audio-similarity search and adds nothing to the P2P feature pool. When we stream
a preview we already hold the whole FLAC in memory; tee it through the SAME
analysis the scanner runs on owned files (CLAP 512-d embedding + librosa DSP +
AST/PaSST instruments), keyed to the phantom's track_id.

Provenance: the streamed bytes are content-addressed exactly like a local file
(analysis_sources row with provider_id = the serving provider's manifest id,
media_file_id NULL, the chromaprint of the fetched audio) — this is what
makes a stream analysis signable against the STREAM's material without
claiming possession of any local rip (tier 3 in P2P-SYNC-INTEGRITY.md). The
material rank (own file > lossless stream > lossy stream) means an owned rip
later OVERWRITES a preview analysis, and a preview never overwrites a
real-file one. No fingerprint (fpcalc has a floor of a few seconds; under one
grid window nothing is analysed at all) means no address and no analysis.

GATE: only tracks with a known catalog length (TrackQuery.duration / .lengths,
i.e. local album_tracks.length_ms) are enriched. Without it the provider match
isn't length-verified, so the audio may be the wrong recording and its features
would poison similarity search. The test is base.same_recording — the one the
host's resolve() already applied to the provider's listing, now applied to the
audio that actually arrived.

Full-track methodology, same as owned scans (balanced-grid segments, whole-track
amplitude block), so vectors land in the same embedding space.

GPU work is serialised on a single worker, is a heavy pass of the accelerator
(device.heavy_pass: it waits for the analysis run, never shares memory with
it) and reuses the process-wide CLAP and AST/PaSST singletons (no duplicate
model load — VRAM). It waits rather than drops: on a demo-limited provider the
track may never stream in full again."""
from __future__ import annotations

import io
import logging
import shutil
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
        # single lane is plenty; the bulk analysers are kept off by the
        # accelerator's own (device.heavy_pass).
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="preview-enrich")
        self._inflight: set[str] = set()
        self._lock = threading.Lock()
        self._embedder = None
        self._analyzer = None
        # Streamed audio with no fingerprint has no address and is never
        # saved; without fpcalc that is every stream — say so once and accept
        # nothing, rather than decode and drop every track.
        self._fpcalc = shutil.which("fpcalc") is not None
        if not self._fpcalc:
            logger.error("preview enrichment disabled: fpcalc (Chromaprint) is not on PATH")
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
        self._stopped = False

    def submit(self, track_id: Optional[str], flac: Optional[bytes],
               lengths: tuple, lossless: bool, provider_id: str,
               excerpt: bool = False) -> None:
        """Queue a previewed track for enrichment. No-ops without fpcalc, a
        track_id, catalog lengths (unverified match — see GATE) or audio, for
        an excerpt, or if already queued. In trickle mode a full backlog drops
        the track instead of queueing. ``lossless`` is the ACTUAL fetch
        quality (lossless provider fetch=True, degraded tiers/YouTube=False);
        ``provider_id`` is the serving provider's manifest id — the row's
        provenance. An ``excerpt`` (a 30 s clip) is refused here,
        before any decode or provenance row: it is not the recording, and
        every first-hand stream analysis is signed and synced."""
        if not self._fpcalc or not track_id or not lengths or not flac:
            return
        if excerpt:
            logger.debug("preview enrich skip %s: excerpt, not the recording", track_id)
            return
        with self._lock:
            if self._stopped or track_id in self._inflight:
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
             provider_id: str) -> None:
        try:
            self._enrich(track_id, flac, lengths, lossless, provider_id)
        except Exception:
            logger.exception("preview enrichment failed for %s", track_id)
        finally:
            with self._lock:
                self._inflight.discard(track_id)
                if self._idle_unload and not self._inflight and not self._stopped:
                    self._idle_timer = threading.Timer(
                        self._IDLE_UNLOAD_SECONDS, self._on_idle_timer)
                    self._idle_timer.daemon = True
                    self._idle_timer.start()

    def _on_idle_timer(self) -> None:
        """One-shot idle timeout (event-scheduled after the last completion,
        cancelled by new work — not a poll)."""
        with self._lock:
            if self._idle_timer is not threading.current_thread() or self._inflight:
                return
            self._idle_timer = None
        self._idle_release()

    def _idle_release(self) -> None:
        """Drops the analyzer so its AST+PaSST reference dies, then releases
        the singleton weights — as a pass of the accelerator: the analysis run
        tags with the same pair, and a release under its batches would leave
        them without instruments. A busy accelerator puts the release after
        its passes; a job or a newer timer that came meanwhile takes it over."""
        if self._analyzer is None:      # nothing loaded: a torch-less node's every timer
            return
        from device import call_when_idle, get_device, heavy_pass
        with heavy_pass("the preview's idle release", wait=False) as held:
            if not held:
                call_when_idle(self._idle_release)
                return
            with self._lock:
                if self._inflight or self._idle_timer is not None:
                    return
                analyzer, self._analyzer = self._analyzer, None
            if analyzer is not None:
                from instrument_tagger import release_instrument_tagger
                release_instrument_tagger(get_device())
                logger.info("preview enrich idle — instrument models released")

    def stop(self) -> None:
        """The node stops: no job is taken, the queued ones go with the
        process, and the one under way ends at the closed accelerator
        (device.close_heavy_passes) unless its analysis has begun."""
        with self._lock:
            self._stopped = True
            if self._idle_timer is not None:
                self._idle_timer.cancel()
                self._idle_timer = None
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _enrich(self, track_id: str, flac: bytes, lengths: tuple,
                lossless: bool, provider_id: str) -> None:
        import librosa
        from config import settings
        from database import SessionLocal

        # Idempotent: a previously-enriched (or owned) track already has an
        # embedding — skip the decode + GPU entirely. A lossy-sourced analysis
        # is as final as a lossless one: every first-hand stream signs
        # (sign_audio._SIGNABLE_SRC), and re-deriving it from a later
        # lossless fetch would only re-spend the GPU and re-seal rows that
        # peers may already hold.
        from models import Embedding
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

        import provenance

        if actual < provenance.MIN_MATERIAL_SECONDS:
            logger.info("preview enrich SKIP %s: %.0fs of audio is under one grid window",
                        track_id, actual)
            return

        # Content-address the STREAMED bytes before any model work: no
        # fingerprint, or the material already registered here at a higher
        # rank, means nothing to save — and nothing to spend GPU on. The row
        # commits on its own; an analysis that then fails leaves a source
        # nothing links to, which the next registration reuses.
        with SessionLocal() as db:
            src_id = provenance.create_stream_source(
                db, track_id, flac, provider_id, lossless)
            if src_id is None:
                db.rollback()
                return
            db.commit()

        from device import heavy_pass
        with heavy_pass("a stream's analysis") as held:
            if not held:        # the node stops
                return
            embedder, analyzer = self._models()
            # Full-track methodology, same as owned scans: amplitude block +
            # windowed instruments over the whole streamed audio, bpm/key from
            # its middle (analyze_from_array slices that itself).
            feats = analyzer.analyze_from_array(audio, sr=48000)   # dict | None
            # Balanced-grid segments from the full streamed audio; the track
            # vector is their normalized mean.
            computed = embedder._compute_segments(audio)

        with SessionLocal() as db:
            model = embedder._get_or_create_embedding_model(db)
            # _persist_analysis decides the overwrite by material rank BEFORE
            # writing, so a preview never touches a real-file analysis
            # (segments included).
            saved = False
            if computed is not None:
                idxs, vecs, portrait = computed
                saved = embedder._persist_analysis(
                    db, track_id, model, idxs, vecs, portrait, src_id,
                    provider_id=provider_id, is_lossless=lossless)
            if feats:
                self._save_features(db, track_id, feats, src_id,
                                    provider_id, lossless)
            db.commit()

        if saved or feats:
            import notary
            notary.wake("preview analysis")
        preview_events.ping()   # features committed → open album page re-fetches key·bpm
        logger.info("preview enriched %s (embedding=%s features=%s)",
                    track_id, saved, bool(feats))

    @staticmethod
    def _save_features(db, track_id: str, feats: dict, src_id,
                       provider_id: str, lossless: bool) -> None:
        import provenance
        from audio_analysis import ANALYSIS_VERSION
        from models import AudioFeature

        existing = db.query(AudioFeature).filter(
            AudioFeature.track_id == track_id).first()
        if existing and existing.analysis_source_id is not None:
            old_rank = provenance.material_rank_of(db, existing.analysis_source_id)
            if old_rank > provenance.material_rank(provider_id, lossless):
                return   # never let a preview overwrite a better-material analysis

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
        else:
            # The analysis run releases AST+PaSST at its end where the profile
            # wants the memory back: the pair is taken anew, loaded again if so
            from instrument_tagger import get_instrument_tagger
            self._analyzer.ensemble = get_instrument_tagger(self._analyzer.device)
        return self._embedder, self._analyzer


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
        self._stopped = False

    def submit(self, query) -> None:
        """Queue a streamed track's lyrics fetch+embed. No-ops without the identity
        LRCLIB needs (track_id + title + artist), or if already queued."""
        tid = getattr(query, "track_id", None)
        if not tid or not query.title or not query.artist:
            return
        tid = str(tid)
        with self._lock:
            if self._stopped or tid in self._inflight:
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
        # cleans to nothing / exceeds the junk-length cap) — a heavy pass of the
        # accelerator like any text pass.
        from device import heavy_pass
        from lyrics_embeddings import generate_lyrics_embeddings
        with heavy_pass("a stream's lyrics") as held:
            if held:            # else the node stops
                generate_lyrics_embeddings(track_ids=[tid])

    def stop(self) -> None:
        """The node stops: no job is taken, the queued ones go with the process."""
        with self._lock:
            self._stopped = True
        self._pool.shutdown(wait=False, cancel_futures=True)
