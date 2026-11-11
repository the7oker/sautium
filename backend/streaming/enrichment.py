"""Background CLAP/feature enrichment of previewed audio.

A phantom track has no local file → no audio embedding → it can't be found by
audio-similarity search and adds nothing to the P2P feature pool. When we stream
a preview we already hold the whole FLAC in memory; tee it through the SAME
analysis the passes run on owned files, keyed to the phantom's track_id, on
one decode, for each kind the track lacks: the segments (CLAP 512-d) for a
track with no embedding that has them, the features (librosa DSP) for one
with none, and the instruments (AST/PaSST) only while the owner's switch says
so (enrichment.instruments) — so a stream of a track whose sonic records are
held, imported or its own, runs the tagger alone, and one whose features
alone are missing runs librosa alone.

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
        """Queue a previewed track for enrichment. No-ops without an fpcalc that
        passed the fingerprint self-test, a
        track_id, catalog lengths (unverified match — see GATE) or audio, for
        an excerpt, or if already queued. In trickle mode a full backlog drops
        the track instead of queueing. ``lossless`` is the ACTUAL fetch
        quality (lossless provider fetch=True, degraded tiers/YouTube=False);
        ``provider_id`` is the serving provider's manifest id — the row's
        provenance. An ``excerpt`` (a 30 s clip) is refused here,
        before any decode or provenance row: it is not the recording, and
        every first-hand stream analysis is signed and synced."""
        if not track_id or not lengths or not flac:
            return
        # Streamed audio with no fingerprint has no address and is never
        # saved, and one from a fingerprinter that fails the self-test would
        # carry another address than the release's: without a passing fpcalc
        # accept nothing, rather than decode the track (the self-test says
        # why, once per fpcalc found; one installed later is taken then)
        import provenance
        if not provenance.fingerprinter().ok:
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
            # under the lock: a stop between the check and the pool's shutdown
            # would refuse it there
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
        """Drops the analyzer's AST+PaSST reference, then releases the
        singleton weights — as a pass of the accelerator: the analysis run
        tags with the same pair, and a release under its batches would leave
        them without instruments. A busy accelerator puts the release after
        its passes; a job or a newer timer that came meanwhile takes it over."""
        if self._analyzer is None or self._analyzer.ensemble is None:   # no tagger loaded
            return
        from device import call_when_idle, get_device, heavy_pass
        with heavy_pass("the preview's idle release", wait=False) as held:
            if not held:
                call_when_idle(self._idle_release)
                return
            with self._lock:
                if self._inflight or self._idle_timer is not None:
                    return
                self._analyzer.ensemble = None
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

        # Idempotent, per kind: what the track already holds of a kind —
        # an embedding with its segments, the features, the instruments (and
        # those only while the owner's switch says to make them) — is not
        # made again; a track holding all it may gain skips the decode + GPU
        # entirely. A phantom whose segments were imported and whose features
        # are not (the records' cutover deletes imported features) gains
        # them from its stream, which may be its one full listen. A
        # lossy-sourced analysis is as final as a lossless one: every
        # first-hand stream signs (sign_audio._SIGNABLE_SRC), and re-deriving
        # it from a later lossless fetch would only re-spend the GPU and
        # re-seal rows that peers may already hold.
        from sqlalchemy import text as sa_text
        from routers.settings import instruments_on
        with SessionLocal() as db:
            held = db.execute(sa_text("""
                SELECT EXISTS (SELECT 1 FROM embeddings e
                                WHERE e.track_id = CAST(:t AS uuid)
                                  AND EXISTS (SELECT 1 FROM embedding_segments g
                                               WHERE g.embedding_id = e.id)),
                       EXISTS (SELECT 1 FROM audio_features WHERE track_id = CAST(:t AS uuid)),
                       EXISTS (SELECT 1 FROM track_instruments WHERE track_id = CAST(:t AS uuid))
            """), {"t": str(track_id)}).one()
        kinds = tuple(kind for kind, lacking in (
            ("embeddings", not held[0]), ("audio_features", not held[1]),
            ("track_instruments", not held[2] and instruments_on())) if lacking)
        if not kinds:
            return
        # A stream longer than this machine can hold now is not analysed: its
        # own decode bounds it (native rate and channels as float32, then
        # mono, then 48 kHz — analysis_pace.stream_fits, read off the bytes'
        # header before any decode), within what the models it would still
        # load, and those that may load beside it, leave
        import analysis_pace
        passes = ([analysis_pace.SONIC] if "embeddings" in kinds or "audio_features" in kinds
                  else []) + ([analysis_pace.INSTRUMENTS] if "track_instruments" in kinds else [])
        fits, need, room = analysis_pace.stream_fits(flac, lengths, passes)
        if not fits:
            logger.info("preview enrich SKIP %s: its analysis would hold %.0f MiB, this machine "
                        "has room for %.0f now", track_id, need, room)
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
        # fingerprint, or every kind it would make already held here at a
        # higher rank, means nothing to save — and nothing to spend GPU on.
        # Then each kind its material group already holds at a rank no lower
        # is not made (held_in_group, as the passes skip). The row commits on
        # its own; an analysis that then fails leaves a source nothing links
        # to, which the next registration reuses.
        with SessionLocal() as db:
            src_id = provenance.create_stream_source(
                db, track_id, flac, provider_id, lossless, kinds)
            if src_id is None:
                db.rollback()
                return
            make = {k for k in kinds if not provenance.held_in_group(db, src_id, k)}
            db.commit()
        if not make:
            return

        from device import heavy_pass
        with heavy_pass("a stream's analysis") as held:
            if not held:        # the node stops
                return
            embedder, analyzer = self._models(sonic="embeddings" in make,
                                              tag="track_instruments" in make)
            # Full-track methodology, same as owned scans: amplitude block +
            # windowed instruments over the whole streamed audio, bpm/key from
            # its middle (_librosa_features slices that itself).
            feats = (analyzer.features_from_array(audio, sr=48000)
                     if "audio_features" in make else None)
            instruments = self._tag(analyzer, track_id, audio) if "track_instruments" in make else None
            # Balanced-grid segments from the full streamed audio; the track
            # vector is their normalized mean.
            computed = embedder._compute_segments(audio) if "embeddings" in make else None

        from audio_analysis import save_features, save_instruments
        from embeddings import NotUnitVectors
        with SessionLocal() as db:
            # Each save decides the overwrite by material rank BEFORE writing
            # (provenance.outranked), so a preview never touches a real-file
            # analysis of any kind (segments included).
            saved = []
            if computed is not None:
                model = embedder._get_or_create_embedding_model(db)
                try:
                    if embedder._persist_analysis(db, track_id, model, *computed, src_id):
                        saved.append("embedding")
                except NotUnitVectors as e:
                    # This node's own analysis went wrong: none of it is
                    # saved, the features and instruments of the same run
                    # neither (the session ends uncommitted)
                    logger.error("preview enrich: nothing saved — %s", e)
                    return
            if feats and save_features(db, track_id, feats, src_id):
                saved.append("features")
            if instruments is not None and save_instruments(db, track_id, instruments, src_id):
                saved.append("instruments")
            db.commit()

        if saved:
            import notary
            notary.wake("preview analysis")
        preview_events.ping()   # features committed → open album page re-fetches key·bpm
        logger.info("preview enriched %s (%s)", track_id, ", ".join(saved) or "nothing saved")

    @staticmethod
    def _tag(analyzer, track_id: str, audio) -> Optional[dict]:
        """The tagger's scores, or None when it failed on the stream — its
        sonic records are saved all the same."""
        try:
            return analyzer.ensemble.tag_full_track(audio)
        except Exception as e:
            logger.warning("preview instrument tagging failed for %s: %s", track_id, e)
            return None

    def _models(self, sonic: bool, tag: bool):
        # Lazy: load the singletons a preview needs on first use (CLAP for
        # the segments — librosa needs no model — AST/PaSST for the tagger). Reused across previews;
        # the same process-wide models the passes use.
        if self._embedder is None:
            from embeddings import AudioEmbeddingGenerator
            self._embedder = AudioEmbeddingGenerator()
        if sonic:
            self._embedder.load_model()
        if self._analyzer is None:
            from audio_analysis import AudioAnalyzer
            self._analyzer = AudioAnalyzer()
        if tag:
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
