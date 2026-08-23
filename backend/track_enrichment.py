"""
Comprehensive track enrichment pipeline.

Orchestrates all data aggregation steps in the correct order:
1. Audio Embedding (CLAP) - base audio feature
2. Last.fm Artist Info - artist metadata
3. Last.fm Track Stats - track popularity
4. Audio Analysis - DSP features + CLAP classification

Each step is conditional - only runs if data is missing.
Supports filters, limits, time constraints, and graceful error handling.

Two modes:
- TrackEnrichmentPipeline: per-track sequential processing (legacy)
- run_parallel_enrichment(): parallel pipelines (recommended)
"""

import logging
import time
from typing import Callable, Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from sqlalchemy.orm import Session
from sqlalchemy import or_, and_, exists, select, func, cast, String, text
from tqdm import tqdm

try:
    import torch
except ImportError:
    torch = None

from api_cooldown import cooling_down
from config import settings
from database import get_db_context
from models import (
    Track, MediaFile, Album, AlbumVariant, Artist, TrackArtist,
    AudioFeature, Embedding, SimilarArtist, ArtistBio,
    TrackStats, ExternalMetadata,
)
# embeddings / audio_analysis are imported where they are constructed, not
# here: both pull torch + librosa at module scope, which a torch-less node
# does not have. A module-level import made THIS module unimportable there,
# and the enrichment worker died on `from track_enrichment import …` before
# it could clear its running flag — the Enrich button wedged until restart.
# The metadata half (Last.fm, lyrics) needs neither.

logger = logging.getLogger(__name__)


def run_parallel_enrichment(
    limit: Optional[int] = None,
    skip_embeddings: bool = False,
    skip_lastfm: bool = False,
    skip_audio_analysis: bool = False,
    force_embeddings: bool = False,
    force_audio_analysis: bool = False,
    lastfm_delay: float = 0.2,
    cancel_flag: Optional[Callable] = None,
    progress_cb: Optional[Callable] = None,
    track_ids: Optional[List] = None,
    order_by_date: bool = False,
    max_duration_seconds: Optional[int] = None,
    worker_id: Optional[int] = None,
    worker_count: Optional[int] = None,
) -> Dict:
    """Run enrichment with parallel pipelines.

    Phase 1 (parallel via ThreadPoolExecutor):
      A) GPU: audio embeddings (batched) → audio features (CLAP classify)
      B) Network: Last.fm artist/album/track enrichment
      C) Network: Lyrics fetching (lrclib/genius)
    Phase 2 (sequential, GPU):
      D) Text embeddings (sentence-transformers)
      E) Lyrics embeddings
      F) Enrichment embeddings (artist bios, album info, genre desc)

    Returns dict with stats from all pipelines.
    """
    if cancel_flag is None:
        cancel_flag = lambda: False

    # Preflight: audio embeddings + analysis decode via the `ffmpeg` binary
    # (audio_analysis.load_full_track_48k). Missing ffmpeg fails every track
    # silently ("0 / N"); surface it once and skip the audio pipeline instead
    # of logging N identical per-file errors. Metadata enrichment still runs.
    import shutil
    if (not skip_embeddings or not skip_audio_analysis) and not shutil.which("ffmpeg"):
        logger.error(
            "ffmpeg not found on PATH — audio embeddings and analysis disabled "
            "for this run. Install ffmpeg (macOS: brew install ffmpeg) and re-run."
        )
        skip_embeddings = True
        skip_audio_analysis = True

    # Same shape for the node that has no ML runtime at all: state it once and
    # skip, rather than let every model step raise its own ImportError
    # traceback. The metadata pipelines below are the whole run there.
    from hardware_profile import resolve as _hw
    ml_available = _hw().ml_available
    if not ml_available and not (skip_embeddings and skip_audio_analysis):
        logger.info("torch-less node — audio embeddings and analysis skipped; "
                    "they arrive via P2P import")
        skip_embeddings = True
        skip_audio_analysis = True

    _skip_lastfm = skip_lastfm or not settings.lastfm_api_key

    result_parts = {}
    pipeline_progress = {"gpu": "", "lastfm": "", "lyrics": ""}

    def _update_progress():
        parts = [v for v in pipeline_progress.values() if v]
        msg = " | ".join(parts) if parts else "Working..."
        if progress_cb:
            progress_cb(msg)

    # Common kwargs for bulk GPU functions
    gpu_kwargs = dict(
        limit=limit, track_ids=track_ids, order_by_date=order_by_date,
        max_duration_seconds=max_duration_seconds,
        worker_id=worker_id, worker_count=worker_count,
    )

    # --- Step 0: Normalize artists ---
    if progress_cb:
        progress_cb("Normalizing artists...")
    try:
        from canon.migrations import normalize_artists as do_normalize
        with get_db_context() as db:
            norm_stats = do_normalize(db, pass1=True)  # pass2 (Last.fm split) removed — non-deterministic, forks UUIDs
            splits = norm_stats.get('pass1', {}).get('split', 0)
            if splits > 0:
                logger.info(f"Pre-enrich normalization: {splits} artists split")
    except Exception as e:
        logger.error(f"Pre-enrich normalization failed: {e}")

    if cancel_flag():
        return result_parts

    # --- Pipeline A: GPU (audio embeddings + audio features) ---
    def _run_gpu_pipeline():
        logger.info("Pipeline A (GPU): starting audio embeddings + features")
        combined = {}

        if not skip_embeddings:
            from embeddings import AudioEmbeddingGenerator
            pipeline_progress["gpu"] = "GPU: embeddings..."
            _update_progress()
            emb_stats = AudioEmbeddingGenerator().generate_embeddings(
                **gpu_kwargs, cancel_flag=cancel_flag, force=force_embeddings
            )
            combined["embeddings"] = emb_stats
            pipeline_progress["gpu"] = f"GPU: {emb_stats.get('success', 0)} embeddings"
            _update_progress()

        if cancel_flag():
            return combined

        if not skip_audio_analysis:
            from audio_analysis import AudioAnalyzer
            pipeline_progress["gpu"] = "GPU: audio analysis..."
            _update_progress()
            af_stats = AudioAnalyzer().analyze_all(
                force=force_audio_analysis, cancel_flag=cancel_flag,
                **gpu_kwargs
            )
            combined["audio_features"] = af_stats
            pipeline_progress["gpu"] = f"GPU: {af_stats.get('success', 0)} features"
            _update_progress()

        pipeline_progress["gpu"] = "GPU: done"
        _update_progress()
        return combined

    # --- Pipeline B: Last.fm enrichment (by artist + album, not per-track) ---
    def _run_lastfm_pipeline():
        if _skip_lastfm:
            return {}
        logger.info("Pipeline B (Network): starting Last.fm enrichment")

        from lastfm import LastFmService
        lastfm = LastFmService()
        stats = {"artists_processed": 0, "artists_success": 0, "errors": 0}
        start_time = time.time()

        with get_db_context() as db:
            # --- Enrich artists (only library artists with tracks) ---
            artist_sql = """
                SELECT DISTINCT a.id, a.name
                FROM artists a
                JOIN track_artists ta ON ta.artist_id = a.id
                WHERE NOT EXISTS (
                    SELECT 1 FROM artist_bios ab
                    WHERE ab.artist_id = a.id AND ab.source = 'lastfm'
                )
                AND NOT EXISTS (
                    SELECT 1 FROM external_metadata em
                    WHERE em.entity_type = 'artist'
                    AND em.entity_id = a.id::text
                    AND em.source = 'lastfm'
                    AND em.metadata_type = 'bio'
                    AND em.fetch_status = 'not_found'
                )
                ORDER BY a.name
            """
            artists = db.execute(text(artist_sql)).fetchall()
            total_artists = len(artists)
            logger.info(f"Last.fm: {total_artists} artists to enrich")

            for i, (artist_id, artist_name) in enumerate(artists):
                if (cancel_flag and cancel_flag()) or cooling_down('lastfm'):
                    break
                if max_duration_seconds:
                    if time.time() - start_time >= max_duration_seconds:
                        logger.info("Last.fm: time limit reached")
                        break

                pipeline_progress["lastfm"] = f"artist {i+1}/{total_artists}: {artist_name}"
                _update_progress()

                try:
                    result = lastfm.enrich_artist(db, artist_id, artist_name)
                    stats["artists_processed"] += 1
                    if result.get("status") == "rate_limited":
                        logger.info("Last.fm: rate-limited — ending pipeline")
                        break
                    if result.get("status") == "success":
                        stats["artists_success"] += 1
                    time.sleep(lastfm_delay)
                except Exception as e:
                    logger.error(f"Last.fm artist failed {artist_name}: {e}")
                    stats["errors"] += 1
                    db.rollback()


        pipeline_progress["lastfm"] = f"Last.fm: done ({stats['artists_success']} artists)"
        _update_progress()
        return stats

    # --- Pipeline C: Lyrics fetching ---
    def _run_lyrics_pipeline():
        logger.info("Pipeline C (Network): starting lyrics fetch")

        def lyrics_progress(msg):
            pipeline_progress["lyrics"] = f"Lyrics: {msg}"
            _update_progress()

        stats = _fetch_lyrics_batch(
            limit=limit, cancel_flag=cancel_flag, progress_cb=lyrics_progress,
        )
        pipeline_progress["lyrics"] = f"Lyrics: {stats.get('found', 0)} found"
        _update_progress()
        return stats

    def _sign_analysis(progress_cb):
        """Author-sign + Worker-timestamp whatever first-hand analysis is now
        signable, the moment the audio pipeline commits. Idempotent and
        non-fatal — a Worker/network failure leaves records for the next run."""
        try:
            import sign_audio
            if progress_cb:
                progress_cb("Signing analysis records...")
            sign_audio.run()
        except Exception as e:
            logger.warning(f"Signing failed (records left for next run): {e}")

    # === Phase 1: Run A, B, C in parallel ===
    if progress_cb:
        progress_cb("Phase 1: GPU + Last.fm + Lyrics (parallel)...")
    logger.info("=== Phase 1: parallel pipelines ===")

    # GPU runs in the main thread to keep the CUDA context owner stable across
    # phases. Network-bound pipelines (Last.fm, lyrics) run in worker threads
    # in parallel — same wall-clock as the previous all-in-pool layout, but
    # Phase 2 inherits a clean CUDA context instead of one handed off from a
    # worker that already exited.
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="enrich") as pool:
        futures = {
            pool.submit(_run_lastfm_pipeline): "lastfm",
            pool.submit(_run_lyrics_pipeline): "lyrics",
        }

        try:
            result_parts["gpu"] = _run_gpu_pipeline()
            logger.info(f"Pipeline gpu complete: {result_parts['gpu']}")
        except Exception as e:
            logger.error(f"Pipeline gpu failed: {e}", exc_info=True)
            result_parts["gpu"] = {"error": str(e)}
        finally:
            # Seal the first-hand analysis the moment the audio pipeline has
            # committed — BEFORE the slow lyrics tail (minutes) and independent
            # of any later manual cancel of it. _run_gpu_pipeline returns what
            # it committed even on cancel/error, so a mid-run stop still seals
            # its completed tracks. Idempotent — touches only unsigned records.
            if not (skip_embeddings and skip_audio_analysis):
                _sign_analysis(progress_cb)

        for future in as_completed(futures):
            name = futures[future]
            try:
                stats = future.result()
                result_parts[name] = stats
                logger.info(f"Pipeline {name} complete: {stats}")
            except Exception as e:
                logger.error(f"Pipeline {name} failed: {e}", exc_info=True)
                result_parts[name] = {"error": str(e)}

    if cancel_flag():
        result_parts["note"] = "cancelled during phase 1"
        return result_parts

    # Phase 1's CLAP/instrument tensors are finished; hand their cached blocks
    # back to the driver before Phase 2 so the text encoder isn't squeezed into
    # a full allocator arena on the 16 GB laptop GPU. Singleton model weights
    # stay resident — only the allocator's free cache is returned. On the
    # standard/lite profiles the AST+PaSST pair is released entirely: nothing
    # after Phase 1 needs it, and those machines want the memory back more
    # than they want an instant next run.
    if torch is not None:
        if _hw().unload_instruments_after_run:
            from device import get_device
            from instrument_tagger import release_instrument_tagger
            release_instrument_tagger(get_device())
        from device import empty_cache
        empty_cache()

    # === Phase 2: Text embeddings (sentence-transformers, needs GPU) ===
    # Runs on lite too — the tier turns off the audio pipeline, not the text
    # encoders, and lite's semantic search reads exactly these vectors
    # (HARDWARE-TIERS §4). Only a node with no ML runtime skips it.
    if not ml_available:
        logger.info("=== Phase 2 skipped: no ML runtime ===")
        return result_parts

    logger.info("=== Phase 2: text embeddings ===")

    # Text embeddings
    try:
        from text_embeddings import generate_text_embeddings
        if progress_cb:
            progress_cb("Phase 2: text embeddings...")
        text_stats = generate_text_embeddings(limit=None, cancel_flag=cancel_flag)
        result_parts["text_embeddings"] = text_stats
        logger.info(f"Text embeddings: {text_stats}")
    except Exception as e:
        logger.error(f"Text embeddings failed: {e}", exc_info=True)
        result_parts["text_embeddings"] = {"error": str(e)}

    if cancel_flag():
        result_parts["note"] = "cancelled during phase 2"
        return result_parts

    # Lyrics embeddings
    try:
        from lyrics_embeddings import generate_lyrics_embeddings
        if progress_cb:
            progress_cb("Phase 2: lyrics embeddings...")
        lyrics_emb_stats = generate_lyrics_embeddings(limit=None, cancel_flag=cancel_flag)
        result_parts["lyrics_embeddings"] = lyrics_emb_stats
        logger.info(f"Lyrics embeddings: {lyrics_emb_stats}")
    except Exception as e:
        logger.error(f"Lyrics embeddings failed: {e}", exc_info=True)
        result_parts["lyrics_embeddings"] = {"error": str(e)}

    if cancel_flag():
        result_parts["note"] = "cancelled during phase 2"
        return result_parts

    # Enrichment embeddings (artist bios, album info, genre descriptions)
    try:
        from enrichment_embeddings import generate_all_enrichment_embeddings
        if progress_cb:
            progress_cb("Phase 2: enrichment embeddings...")
        enrich_emb_stats = generate_all_enrichment_embeddings(
            limit=None, progress_cb=progress_cb, cancel_flag=cancel_flag,
        )
        result_parts["enrichment_embeddings"] = enrich_emb_stats
        logger.info(f"Enrichment embeddings: {enrich_emb_stats}")
    except Exception as e:
        logger.error(f"Enrichment embeddings failed: {e}", exc_info=True)
        result_parts["enrichment_embeddings"] = {"error": str(e)}

    return result_parts


def _fetch_lyrics_batch(
    limit: Optional[int] = None,
    cancel_flag: Optional[Callable] = None,
    progress_cb: Optional[Callable] = None,
) -> Dict:
    """Fetch lyrics for all tracks missing them (lrclib + genius fallback)."""
    use_lrclib = True
    use_genius = bool(settings.genius_access_token)

    lrclib_service = None
    genius_service = None

    if use_lrclib:
        from lrclib import LrclibService
        lrclib_service = LrclibService()
    if use_genius:
        from genius import GeniusService
        genius_service = GeniusService(settings.genius_access_token)

    stats = {"processed": 0, "found": 0, "not_found": 0, "errors": 0}

    with get_db_context() as db:
        # Driven from media_files (is_analysis_source) — the OWNED set (~37k),
        # NOT the tracks table, which now holds millions of trackless phantom
        # rows from missing-album discovery. Walking tracks by title (the old
        # ORDER BY) scanned all 2.7M to find 50 owned ones missing lyrics — the
        # recurring multi-hour runaway query. No ORDER BY: an enrichment batch
        # only needs to make progress; a processed track gains a lyrics row or
        # a 'not_found' marker and drops out of the next batch.
        query_sql = """
            SELECT t.id as track_id, t.title,
                   a.name as artist,
                   al.title as album,
                   mf.duration_seconds
            FROM media_files mf
            JOIN tracks t ON t.id = mf.track_id
            JOIN track_artists ta ON ta.track_id = t.id AND ta.role = 'primary'
            JOIN artists a ON a.id = ta.artist_id
            JOIN album_variants av ON av.id = mf.album_variant_id
            JOIN albums al ON al.id = av.album_id
            WHERE mf.is_analysis_source = true
            AND NOT EXISTS (
                SELECT 1 FROM track_lyrics tl WHERE tl.track_id = t.id
            )
            AND NOT EXISTS (
                SELECT 1 FROM external_metadata em
                WHERE em.entity_type = 'track'
                AND em.entity_id = t.id::text
                AND em.source = 'lrclib'
                AND em.metadata_type = 'lyrics'
                AND em.fetch_status = 'not_found'
            )
        """
        if limit:
            query_sql += f" LIMIT {int(limit)}"

        rows = db.execute(text(query_sql)).fetchall()
        total = len(rows)
        logger.info(f"Lyrics: {total} tracks to process")
        start = time.time()

        for i, row in enumerate(rows):
            if cancel_flag and cancel_flag():
                break

            stats["processed"] += 1

            if progress_cb and i % 5 == 0:
                elapsed = time.time() - start
                eta_str = ""
                if i > 0:
                    eta = elapsed / i * (total - i)
                    eta_str = f", ETA {int(eta)}s"
                progress_cb(f"{i+1}/{total}{eta_str}")

            # Skip a lyrics source while it's rate-limited; stop the whole batch
            # only when BOTH available sources are cooling (nothing left to do).
            lrclib_ok = use_lrclib and lrclib_service and not cooling_down('lrclib')
            genius_ok = use_genius and genius_service and not cooling_down('genius')
            if not lrclib_ok and not genius_ok:
                break

            found = False
            if lrclib_ok:
                try:
                    result = lrclib_service.fetch_and_store(
                        db, row.track_id, row.title, row.artist,
                        album_name=row.album,
                        duration=int(row.duration_seconds) if row.duration_seconds else None,
                    )
                    if result and result.get("status") not in ("not_found", "error"):
                        found = True
                    time.sleep(0.1)
                except Exception as e:
                    logger.debug(f"LRCLIB failed for {row.artist} - {row.title}: {e}")

            if not found and genius_ok:
                try:
                    result = genius_service.fetch_and_store(
                        db, row.track_id, row.title, row.artist,
                    )
                    if result and result.get("status") not in ("not_found", "error"):
                        found = True
                    time.sleep(1.0)
                except Exception as e:
                    logger.debug(f"Genius failed for {row.artist} - {row.title}: {e}")

            if found:
                stats["found"] += 1
            else:
                stats["not_found"] += 1

            db.commit()

    return stats


class TrackEnrichmentPipeline:
    """
    Comprehensive track-by-track enrichment pipeline.

    Runs all data aggregation steps in correct order,
    only processing missing data for each track.
    """

    def __init__(
        self,
        skip_embeddings: bool = False,
        skip_lastfm: bool = False,
        skip_audio_analysis: bool = False,
        force_embeddings: bool = False,
        force_audio_analysis: bool = False,
        lastfm_delay: float = 0.2,
    ):
        self.skip_embeddings = skip_embeddings
        self.skip_lastfm = skip_lastfm
        self.skip_audio_analysis = skip_audio_analysis
        self.force_embeddings = force_embeddings
        self.force_audio_analysis = force_audio_analysis
        self.lastfm_delay = lastfm_delay

        # Lazy-loaded components
        self._audio_embedding_generator = None
        self._audio_analyzer = None
        self._lastfm_service = None

    def _get_audio_embedding_generator(self) -> "AudioEmbeddingGenerator":
        """Lazy-load audio embedding generator."""
        if self._audio_embedding_generator is None:
            from embeddings import AudioEmbeddingGenerator
            self._audio_embedding_generator = AudioEmbeddingGenerator()
            self._audio_embedding_generator.load_model()
        return self._audio_embedding_generator

    def _get_audio_analyzer(self) -> "AudioAnalyzer":
        """Lazy-load audio analyzer."""
        if self._audio_analyzer is None:
            from audio_analysis import AudioAnalyzer
            self._audio_analyzer = AudioAnalyzer()
            if not self.skip_audio_analysis:
                self._audio_analyzer.load_model()
        return self._audio_analyzer

    def _get_lastfm_service(self):
        """Lazy-load Last.fm service."""
        if self._lastfm_service is None:
            from lastfm import LastFmService
            self._lastfm_service = LastFmService()
        return self._lastfm_service

    def _get_analysis_file(self, db: Session, track: Track) -> Optional[MediaFile]:
        """Get the preferred media file for audio analysis (is_analysis_source=True)."""
        mf = db.query(MediaFile).filter(
            MediaFile.track_id == track.id,
            MediaFile.is_analysis_source == True,
        ).first()
        if not mf:
            mf = db.query(MediaFile).filter(MediaFile.track_id == track.id).first()
        return mf

    def _check_track_status(self, db: Session, track: Track) -> Dict[str, bool]:
        """
        Check what data is missing for a track.

        Returns dict with keys: needs_audio_embedding, needs_artist_info,
        needs_audio_features
        """
        status = {}

        # Audio embedding: check via relationship
        status['needs_audio_embedding'] = (
            not self.skip_embeddings and
            (self.force_embeddings or track.embedding is None)
        )

        # Audio features: check via relationship
        status['needs_audio_features'] = (
            not self.skip_audio_analysis and
            (self.force_audio_analysis or track.audio_feature is None)
        )

        # Last.fm data (only check if not skipping)
        if not self.skip_lastfm:
            # Get primary artist
            artist_row = db.query(Artist).join(TrackArtist).filter(
                TrackArtist.track_id == track.id,
                TrackArtist.role == 'primary'
            ).first()

            if artist_row:
                artist_bio = db.query(ArtistBio).filter(
                    ArtistBio.artist_id == artist_row.id,
                    ArtistBio.source == 'lastfm'
                ).first()
                if artist_bio:
                    status['needs_artist_info'] = False
                else:
                    artist_meta = db.query(ExternalMetadata).filter(
                        ExternalMetadata.entity_type == 'artist',
                        ExternalMetadata.entity_id == str(artist_row.id),
                        ExternalMetadata.source == 'lastfm',
                        ExternalMetadata.metadata_type == 'bio',
                    ).first()
                    status['needs_artist_info'] = artist_meta is None
                status['artist_id'] = artist_row.id
                status['artist_name'] = artist_row.name
            else:
                status['needs_artist_info'] = False

        else:
            status['needs_artist_info'] = False

        return status

    def _enrich_track(
        self,
        db: Session,
        track: Track,
        status: Dict[str, bool],
        progress_callback=None
    ) -> Dict[str, str]:
        """
        Enrich a single track with missing data.

        Returns dict with step results (success/failed/skipped).
        """
        track_id = track.id
        results = {}

        # Get analysis source file (needed for embedding and audio analysis)
        needs_audio = status['needs_audio_embedding'] or status['needs_audio_features']
        analysis_file = None
        audio_48k = None

        if needs_audio:
            analysis_file = self._get_analysis_file(db, track)
            if not analysis_file:
                logger.warning(f"No media file found for track {track_id}")
                if status['needs_audio_embedding']:
                    results['audio_embedding'] = 'failed'
                if status['needs_audio_features']:
                    results['audio_features'] = 'failed'
                status['needs_audio_embedding'] = False
                status['needs_audio_features'] = False
            else:
                # Decode the WHOLE track ONCE at 48kHz (shared scanner decode)
                # — reused for the segment embeddings and the feature pass, so
                # this per-track path produces the same full-track v2 analysis
                # as the bulk scanner, not a middle-30s approximation.
                from audio_analysis import load_full_track_48k
                local_path = settings.translate_to_local_path(analysis_file.file_path)
                try:
                    audio_48k = load_full_track_48k(
                        local_path, analysis_file.cue_start_seconds,
                        analysis_file.cue_end_seconds)
                except Exception as e:
                    logger.error(f"Failed to load audio {local_path}: {e}")
                    audio_48k = None

        # Step 1: Audio Embedding (reuse loaded audio)
        if status['needs_audio_embedding']:
            if progress_callback:
                progress_callback("Audio embedding")
            try:
                if audio_48k is not None:
                    generator = self._get_audio_embedding_generator()
                    if generator.embed_track(db, track.id, analysis_file,
                                             audio_full=audio_48k):
                        db.commit()
                        results['audio_embedding'] = 'success'
                    else:
                        results['audio_embedding'] = 'failed'
                else:
                    results['audio_embedding'] = 'failed'
            except Exception as e:
                logger.error(f"Audio embedding failed for track {track_id}: {e}")
                results['audio_embedding'] = 'failed'
                db.rollback()
        else:
            results.setdefault('audio_embedding', 'skipped')

        # Step 2-4: Last.fm enrichment (if not skipping)
        if not self.skip_lastfm:
            lastfm = self._get_lastfm_service()

            # Artist info
            if status.get('needs_artist_info'):
                if progress_callback:
                    progress_callback("Last.fm artist")
                try:
                    result = lastfm.enrich_artist(
                        db, status['artist_id'], status['artist_name']
                    )
                    results['lastfm_artist'] = result['status']
                    time.sleep(self.lastfm_delay)

                except Exception as e:
                    logger.error(f"Last.fm artist enrichment failed: {e}")
                    results['lastfm_artist'] = 'error'
                    db.rollback()
            else:
                results['lastfm_artist'] = 'skipped'

            results['lastfm_track'] = 'skipped'

        # Step 5: Audio Analysis (reuse loaded audio — no second file read)
        if status['needs_audio_features']:
            if progress_callback:
                progress_callback("Audio analysis")
            try:
                analyzer = self._get_audio_analyzer()
                if audio_48k is not None:
                    features = analyzer.analyze_from_array(audio_48k, sr=48000)
                else:
                    features = None
                if features is not None:
                    import provenance
                    from audio_analysis import ANALYSIS_VERSION
                    src_id = provenance.get_or_create_local(
                        db, track.id, analysis_file.id,
                        analysis_file.file_path, analysis_file.sample_rate,
                        analysis_file.bit_depth, analysis_file.is_lossless,
                        analysis_file.duration_seconds,
                        analysis_file.cue_start_seconds,
                        analysis_file.cue_end_seconds)
                    existing_af = db.query(AudioFeature).filter(
                        AudioFeature.track_id == track.id
                    ).first()
                    if existing_af:
                        for k, v in features.items():
                            if hasattr(existing_af, k):
                                setattr(existing_af, k, v)
                        existing_af.analysis_source_id = src_id
                        existing_af.analysis_version = ANALYSIS_VERSION
                    else:
                        af = AudioFeature(
                            track_id=track.id,
                            bpm=features.get("bpm"),
                            key=features.get("key"),
                            mode=features.get("mode"),
                            key_confidence=features.get("key_confidence"),
                            energy=features.get("energy"),
                            energy_db=features.get("energy_db"),
                            brightness=features.get("brightness"),
                            dynamic_range_db=features.get("dynamic_range_db"),
                            zero_crossing_rate=features.get("zero_crossing_rate"),
                            instruments=features.get("instruments"),
                            moods=features.get("moods"),
                            vocal_instrumental=features.get("vocal_instrumental"),
                            vocal_score=features.get("vocal_score"),
                            danceability=features.get("danceability"),
                            analysis_source_id=src_id,
                            analysis_version=ANALYSIS_VERSION,
                        )
                        db.add(af)
                    db.commit()
                    results['audio_features'] = 'success'
                else:
                    results['audio_features'] = 'failed'
            except Exception as e:
                logger.error(f"Audio analysis failed for track {track_id}: {e}")
                results['audio_features'] = 'failed'
                db.rollback()
        else:
            results.setdefault('audio_features', 'skipped')

        return results

    def _build_needs_enrichment_filter(self):
        """
        Build SQLAlchemy filter conditions to select only tracks
        that need at least one enrichment step.

        Returns a list of OR conditions. If empty, no filtering needed
        (shouldn't happen — means all steps are skipped).
        """
        conditions = []

        # Audio embedding: no Embedding row for this track
        if not self.skip_embeddings:
            if self.force_embeddings:
                conditions.append(True)
            else:
                conditions.append(
                    ~exists(
                        select(Embedding.id).where(
                            Embedding.track_id == Track.id
                        )
                    )
                )

        # Audio features: no AudioFeature row for this track
        if not self.skip_audio_analysis:
            if self.force_audio_analysis:
                conditions.append(True)
            else:
                conditions.append(
                    ~exists(
                        select(AudioFeature.id).where(
                            AudioFeature.track_id == Track.id
                        )
                    )
                )

        # Last.fm enrichment: check both normalized tables AND ExternalMetadata
        if not self.skip_lastfm:
            # Artist: needs enrichment if no ArtistBio AND no ExternalMetadata record
            conditions.append(
                and_(
                    ~exists(
                        select(ArtistBio.id).where(
                            and_(
                                ArtistBio.artist_id == TrackArtist.artist_id,
                                ArtistBio.source == 'lastfm',
                                TrackArtist.track_id == Track.id,
                                TrackArtist.role == 'primary',
                            )
                        )
                    ),
                    ~exists(
                        select(ExternalMetadata.id).where(
                            and_(
                                ExternalMetadata.entity_type == 'artist',
                                ExternalMetadata.entity_id == cast(TrackArtist.artist_id, String),
                                ExternalMetadata.source == 'lastfm',
                                ExternalMetadata.metadata_type == 'bio',
                                TrackArtist.track_id == Track.id,
                                TrackArtist.role == 'primary',
                            )
                        )
                    ),
                )
            )

        return conditions

    def enrich_tracks(
        self,
        limit: Optional[int] = None,
        order_by_date: bool = False,
        max_duration_seconds: Optional[int] = None,
        track_ids: Optional[List] = None,
        worker_id: Optional[int] = None,
        worker_count: Optional[int] = None,
        cancel_flag=None,
        progress_cb=None,
    ) -> Dict[str, int]:
        """
        Run comprehensive enrichment pipeline on tracks.

        Args:
            limit: Maximum number of tracks to process
            order_by_date: Process newest tracks first (by media file modification date)
            max_duration_seconds: Maximum duration in seconds
            track_ids: Specific track IDs (UUIDs) to process (from filters)
            worker_id: Worker ID for parallel processing (0-indexed)
            worker_count: Total number of workers for parallel processing

        Returns:
            Statistics dict with counts per step
        """
        stats = {
            'processed': 0,
            'audio_embedding_success': 0,
            'audio_embedding_failed': 0,
            'lastfm_artist_success': 0,
            'lastfm_album_success': 0,
            'lastfm_track_success': 0,
            'audio_features_success': 0,
            'audio_features_failed': 0,
        }

        start_time = time.time()

        with get_db_context() as db:
            query = db.query(Track)

            if track_ids is not None:
                query = query.filter(Track.id.in_(track_ids))

            # Worker filtering: use hash-based partitioning for UUID PKs
            if worker_count is not None:
                query = query.filter(
                    func.abs(func.hashtext(cast(Track.id, String))) % worker_count == worker_id
                )
                logger.info(f"Worker {worker_id}/{worker_count}: hash-partitioned tracks")

            # Pre-filter: only tracks that need at least one enrichment step
            needs_conditions = self._build_needs_enrichment_filter()
            real_conditions = [c for c in needs_conditions if c is not True]
            has_force = len(real_conditions) < len(needs_conditions)

            if not has_force and real_conditions:
                query = query.filter(or_(*real_conditions))
                logger.info("Pre-filtering: only tracks needing enrichment")
            elif not real_conditions and not has_force:
                logger.info("All enrichment steps are skipped — nothing to do")
                return stats

            if order_by_date:
                # Order by newest media file modification date
                newest_file_date = select(
                    func.max(MediaFile.file_modified_at)
                ).where(
                    MediaFile.track_id == Track.id
                ).correlate(Track).scalar_subquery()
                query = query.order_by(newest_file_date.desc().nulls_last())

            if limit:
                query = query.limit(limit)

            tracks = query.all()
            total = len(tracks)

            if total == 0:
                logger.info("No tracks to process")
                return stats

            logger.info(f"Processing {total} tracks")
            if max_duration_seconds:
                logger.info(f"Time limit: {max_duration_seconds}s")

            for i, track in enumerate(tqdm(tracks, desc="Enriching tracks", unit="track")):
                if cancel_flag and cancel_flag():
                    logger.info("Enrichment cancelled by user")
                    break

                if max_duration_seconds:
                    elapsed = time.time() - start_time
                    if elapsed >= max_duration_seconds:
                        logger.info(f"Time limit reached ({elapsed:.1f}s), stopping")
                        break

                stats['processed'] += 1

                if progress_cb:
                    elapsed = time.time() - start_time
                    if i > 0:
                        eta = elapsed / i * (total - i)
                        eta_str = f", ETA {int(eta)}s" if eta < 3600 else f", ETA {eta/3600:.1f}h"
                    else:
                        eta_str = ""
                    progress_cb(f"Enriching {i+1}/{total}{eta_str}")

                status = self._check_track_status(db, track)
                results = self._enrich_track(db, track, status)

                if results.get('audio_embedding') == 'success':
                    stats['audio_embedding_success'] += 1
                elif results.get('audio_embedding') == 'failed':
                    stats['audio_embedding_failed'] += 1

                if results.get('lastfm_artist') == 'success':
                    stats['lastfm_artist_success'] += 1
                if results.get('lastfm_album') == 'success':
                    stats['lastfm_album_success'] += 1
                if results.get('lastfm_track') == 'success':
                    stats['lastfm_track_success'] += 1

                if results.get('audio_features') == 'success':
                    stats['audio_features_success'] += 1
                elif results.get('audio_features') == 'failed':
                    stats['audio_features_failed'] += 1

            logger.info(f"Enrichment complete: {stats['processed']} tracks processed")


        return stats
