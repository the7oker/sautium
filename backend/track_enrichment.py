"""
Comprehensive track enrichment pipeline.

Orchestrates all data aggregation steps in the correct order:
1. The sonic pass - CLAP segments + librosa features, one decode
2. Last.fm Artist Info - artist metadata
3. The instrument pass - the AST+PaSST tagger, one decode, while the
   owner's switch is on (enrichment.instruments)

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

from api_cooldown import cooling_down
from config import settings
from database import get_db_context
from sql_queries import ARTIST_ENGAGED
from models import (
    Track, MediaFile, Album, Artist, TrackArtist,
    AudioFeature, Embedding, SimilarArtist, ArtistBio,
    ExternalMetadata, TrackInstruments,
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
    skip_sonic: bool = False,
    skip_lastfm: bool = False,
    skip_instruments: bool = False,
    skip_lyrics: bool = False,
    force_sonic: bool = False,
    force_instruments: bool = False,
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
      A) GPU: the sonic pass (segments + features), then the instrument
         pass while the owner's switch is on — every track's sonic pass
         before any track's instruments
      B) Network: Last.fm artist bios (engagement-gated)
      C) Network: Lyrics fetching (lrclib/genius)
    Phase 2 (sequential, GPU):
      D) Text embeddings (BGE-M3)
      E) Lyrics embeddings
      F) Enrichment embeddings (artist bios, genre desc)

    The product's manual run (/enrich/start) is analysis-only — it passes
    skip_lastfm=skip_lyrics=True, because the network steps belong to
    background_enrichment.py. Only the CLI runs everything in one go.

    Returns dict with stats from all pipelines.
    """
    if cancel_flag is None:
        cancel_flag = lambda: False

    # The instrument pass is the owner's switch (enrichment.instruments):
    # off, the run makes none and removes none
    if not skip_instruments:
        from routers.settings import instruments_on
        skip_instruments = not instruments_on()

    # Preflight: audio embeddings + analysis decode via the `ffmpeg` binary
    # (audio_analysis.load_full_track_48k). Missing ffmpeg fails every track
    # silently ("0 / N"); surface it once and skip the audio pipeline instead
    # of logging N identical per-file errors. Metadata enrichment still runs.
    import shutil
    if (not skip_sonic or not skip_instruments) and not shutil.which("ffmpeg"):
        logger.error(
            "ffmpeg not found on PATH — audio embeddings and analysis disabled "
            "for this run. Install ffmpeg (macOS: brew install ffmpeg) and re-run."
        )
        skip_sonic = True
        skip_instruments = True

    # Same shape for the node that has no ML runtime at all: state it once and
    # skip, rather than let every model step raise its own ImportError
    # traceback. The metadata pipelines below are the whole run there.
    from hardware_profile import resolve as _hw
    ml_available = _hw().ml_available
    if not ml_available and not (skip_sonic and skip_instruments):
        logger.info("torch-less node — audio embeddings and analysis skipped; "
                    "they arrive via P2P import")
        skip_sonic = True
        skip_instruments = True

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

    # The run's halves hold the accelerator for hours on a fresh library: at
    # each batch seam - its generators ask the cancel flag before every batch -
    # a stream's analysis or lyrics waiting for it go first (device.give_way).
    # A node that stops ends the run there, the accelerator no longer its own:
    # no stage after it begins, and what the run would release is not its own
    left_the_lane = []

    def _seam():
        from device import give_way
        if cancel_flag():           # a cancelled run ends here, giving nothing way
            return True
        if not left_the_lane and not give_way():
            left_the_lane.append(True)
        return _halted()

    def _halted():
        return bool(left_the_lane) or cancel_flag()

    # --- Pipeline A: GPU (the sonic pass, then the instrument pass) ---
    def _run_gpu_pipeline():
        logger.info("Pipeline A (GPU): starting the sonic and instrument passes")
        combined = {}
        if not (skip_sonic and skip_instruments):
            from device import get_device, heavy_pass
            with heavy_pass("the analysis run's audio half") as held:
                if not held:        # the node stops
                    return combined
                try:
                    _audio_half(combined)
                finally:
                    # Nothing after this half needs AST+PaSST: on the profiles that
                    # want the memory back more than an instant next run, the pair
                    # goes before the accelerator is free, a cancelled run's too
                    if _hw().unload_instruments_after_run and not left_the_lane:
                        from instrument_tagger import release_instrument_tagger
                        release_instrument_tagger(get_device())
            if _halted():
                return combined
        pipeline_progress["gpu"] = "GPU: done"
        _update_progress()
        return combined

    def _counted(combined, part, stats):
        for key, value in stats.items():
            combined.setdefault(part, {})[key] = combined.get(part, {}).get(key, 0) + value

    def _progress(what):
        def on_batch(done, total):
            pipeline_progress["gpu"] = f"{what}: {done} of {total} tracks"
            _update_progress()
        return on_batch

    # The order rule: the sonic pass of any track goes before the
    # instruments of any track. The instrument pass asks at each of its
    # batch seams whether a file newer than the sonic pass's queue waits
    # for it (a scan added files) and gives way, as the run gives way to a
    # stream; then the sonic pass takes the new files and the instruments
    # go on. A forced or limited run reads each queue once.
    can_give_way = not (skip_sonic or force_sonic or force_instruments or limit)

    def _audio_half(combined):
        while True:
            seen = None
            if not skip_sonic:
                from embeddings import AudioEmbeddingGenerator, newest_file_id
                with get_db_context() as db:
                    seen = newest_file_id(db)
                pipeline_progress["gpu"] = "Sonic analysis..."
                _update_progress()
                _counted(combined, "sonic", AudioEmbeddingGenerator().sonic_pass(
                    **gpu_kwargs, cancel_flag=_seam, force=force_sonic,
                    on_batch=_progress("Sonic analysis")))
                pipeline_progress["gpu"] = f"GPU: {combined['sonic'].get('success', 0)} sonic"
                _update_progress()

            if _halted() or skip_instruments:
                return

            gave_way = []

            def _instrument_seam():
                if _seam():
                    return True
                if can_give_way:
                    from embeddings import sonic_work_since
                    with get_db_context() as db:
                        if sonic_work_since(db, seen):
                            gave_way.append(True)
                            return True
                return False

            from audio_analysis import AudioAnalyzer
            pipeline_progress["gpu"] = "Recognising instruments..."
            _update_progress()
            _counted(combined, "instruments", AudioAnalyzer().instrument_pass(
                force=force_instruments, cancel_flag=_instrument_seam,
                on_batch=_progress("Instruments"), **gpu_kwargs))
            pipeline_progress["gpu"] = (f"GPU: {combined.get('sonic', {}).get('success', 0)} sonic, "
                                        f"{combined['instruments'].get('success', 0)} instruments")
            _update_progress()
            if not gave_way or _halted():
                return
            logger.info("The instrument pass gives way: new files wait for the sonic pass")

    # --- Pipeline B: Last.fm enrichment (by artist + album, not per-track) ---
    def _run_lastfm_pipeline():
        logger.info("Pipeline B (Network): starting Last.fm enrichment")

        from lastfm import LastFmService
        lastfm = LastFmService()
        stats = {"artists_processed": 0, "artists_success": 0, "errors": 0}
        start_time = time.time()

        with get_db_context() as db:
            # --- Enrich artists the human actually touched ---
            # One Last.fm call per artist, so the candidate set carries the
            # engagement gate. "Has a track_artists row" would queue every
            # phantom tracklist credit — 224k names on a dump node, ~12 days
            # of API calls for artists nobody asked about.
            artist_sql = f"""
                SELECT a.id, a.name
                FROM artists a
                WHERE {ARTIST_ENGAGED}
                AND NOT EXISTS (
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
                    if result.get("status") == "unavailable":
                        logger.info("Last.fm: unavailable — ending pipeline")
                        break
                    if result.get("status") == "success":
                        stats["artists_success"] += 1
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

    # === Phase 1: Run A, B, C in parallel ===
    network_pipelines = {}
    if not _skip_lastfm:
        network_pipelines["lastfm"] = _run_lastfm_pipeline
    if not skip_lyrics:
        network_pipelines["lyrics"] = _run_lyrics_pipeline
    phase1 = [name for name, on in (
        ("GPU", not (skip_sonic and skip_instruments)),
        ("Last.fm", "lastfm" in network_pipelines),
        ("Lyrics", "lyrics" in network_pipelines),
    ) if on]
    if progress_cb:
        progress_cb(f"Phase 1: {' + '.join(phase1) if phase1 else 'skipped'}...")
    logger.info(f"=== Phase 1: {', '.join(phase1) if phase1 else 'no pipelines'} ===")

    # GPU runs in the main thread to keep the CUDA context owner stable across
    # phases. Network-bound pipelines (Last.fm, lyrics) run in worker threads
    # in parallel — same wall-clock as the previous all-in-pool layout, but
    # Phase 2 inherits a clean CUDA context instead of one handed off from a
    # worker that already exited.
    with ThreadPoolExecutor(max_workers=max(1, len(network_pipelines)),
                            thread_name_prefix="enrich") as pool:
        futures = {pool.submit(fn): name for name, fn in network_pipelines.items()}

        try:
            result_parts["gpu"] = _run_gpu_pipeline()
            logger.info(f"Pipeline gpu complete: {result_parts['gpu']}")
        except Exception as e:
            logger.error(f"Pipeline gpu failed: {e}", exc_info=True)
            result_parts["gpu"] = {"error": str(e)}
        # Each pass wakes the sealing owner after every batch it commits,
        # so a mid-run stop has sealed what it saved.

        for future in as_completed(futures):
            name = futures[future]
            try:
                stats = future.result()
                result_parts[name] = stats
                logger.info(f"Pipeline {name} complete: {stats}")
            except Exception as e:
                logger.error(f"Pipeline {name} failed: {e}", exc_info=True)
                result_parts[name] = {"error": str(e)}

    if _halted():
        result_parts["note"] = "cancelled during phase 1"
        return result_parts

    # === Phase 2: Text embeddings (sentence-transformers, needs GPU) ===
    # Runs on lite too — the tier turns off the audio pipeline, not the text
    # encoders, and lite's semantic search reads exactly these vectors
    # (HARDWARE-TIERS §4). Only a node with no ML runtime skips it.
    if not ml_available:
        logger.info("=== Phase 2 skipped: no ML runtime ===")
        return result_parts

    logger.info("=== Phase 2: text embeddings ===")
    from device import heavy_pass
    with heavy_pass("the analysis run's text half") as held:
        if not held:                # the node stops
            return result_parts
        # Text embeddings
        try:
            from text_embeddings import generate_text_embeddings
            if progress_cb:
                progress_cb("Phase 2: text embeddings...")
            text_stats = generate_text_embeddings(limit=None, cancel_flag=_seam)
            result_parts["text_embeddings"] = text_stats
            logger.info(f"Text embeddings: {text_stats}")
        except Exception as e:
            logger.error(f"Text embeddings failed: {e}", exc_info=True)
            result_parts["text_embeddings"] = {"error": str(e)}

        if _halted():
            result_parts["note"] = "cancelled during phase 2"
            return result_parts

        # Lyrics embeddings
        try:
            from lyrics_embeddings import generate_lyrics_embeddings
            if progress_cb:
                progress_cb("Phase 2: lyrics embeddings...")
            lyrics_emb_stats = generate_lyrics_embeddings(limit=None, cancel_flag=_seam)
            result_parts["lyrics_embeddings"] = lyrics_emb_stats
            logger.info(f"Lyrics embeddings: {lyrics_emb_stats}")
        except Exception as e:
            logger.error(f"Lyrics embeddings failed: {e}", exc_info=True)
            result_parts["lyrics_embeddings"] = {"error": str(e)}

        if _halted():
            result_parts["note"] = "cancelled during phase 2"
            return result_parts

        # Enrichment embeddings (artist bios, album info, genre descriptions)
        try:
            from enrichment_embeddings import generate_all_enrichment_embeddings
            if progress_cb:
                progress_cb("Phase 2: enrichment embeddings...")
            enrich_emb_stats = generate_all_enrichment_embeddings(
                limit=None, progress_cb=progress_cb, cancel_flag=_seam,
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
        # Driven from owned_files — the OWNED set (~37k; a track held at an
        # HQPlayer included, the lookup needs no bytes), NOT the tracks table,
        # which now holds millions of trackless phantom rows from missing-album
        # discovery. Walking tracks by title (the old ORDER BY) scanned all
        # 2.7M to find 50 owned ones missing lyrics — the recurring multi-hour
        # runaway query. DISTINCT ON folds a track's several files (CUE
        # slices, duplicate rips, the copy at the HQPlayer) to one row, a
        # file here first; a processed track gains a lyrics row or a
        # 'not_found' marker and drops out of the next batch.
        query_sql = """
            SELECT DISTINCT ON (t.id)
                   t.id as track_id, t.title,
                   a.name as artist,
                   al.title as album,
                   mf.duration_seconds
            FROM owned_files mf
            JOIN tracks t ON t.id = mf.track_id
            JOIN track_artists ta ON ta.track_id = t.id AND ta.role = 'primary'
            JOIN artists a ON a.id = ta.artist_id
            JOIN album_variants av ON av.id = mf.album_variant_id
            JOIN albums al ON al.id = av.album_id
            WHERE NOT EXISTS (
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
            ORDER BY t.id, (mf.location = 'local') DESC
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

            # An 'error' verdict does not take the track out of the candidate
            # set (only a not_found marker does), so it must count as an
            # error, not a not_found — the background drain reads a full
            # error-free batch as progress and would never back off from a
            # failing source otherwise.
            found = False
            failed = False
            if lrclib_ok:
                try:
                    result = lrclib_service.fetch_and_store(
                        db, row.track_id, row.title, row.artist,
                        album_name=row.album,
                        duration=int(row.duration_seconds) if row.duration_seconds else None,
                    )
                    status = result.get("status") if result else "not_found"
                    if status == "error":
                        failed = True
                    elif status != "not_found":
                        found = True
                    time.sleep(0.1)
                except Exception as e:
                    logger.warning("LRCLIB failed: %s", type(e).__name__)
                    logger.debug(f"LRCLIB failed for {row.artist} - {row.title}: {e}")
                    failed = True

            if not found and genius_ok:
                try:
                    result = genius_service.fetch_and_store(
                        db, row.track_id, row.title, row.artist,
                    )
                    status = result.get("status") if result else "not_found"
                    if status == "error":
                        failed = True
                    elif status != "not_found":
                        found = True
                    time.sleep(1.0)
                except Exception as e:
                    logger.warning("Genius failed: %s", type(e).__name__)
                    logger.debug(f"Genius failed for {row.artist} - {row.title}: {e}")
                    failed = True

            if found:
                stats["found"] += 1
            elif failed:
                stats["errors"] += 1
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
        skip_sonic: bool = False,
        skip_lastfm: bool = False,
        skip_instruments: bool = False,
        force_sonic: bool = False,
        force_instruments: bool = False,
    ):
        self.skip_sonic = skip_sonic
        self.skip_lastfm = skip_lastfm
        # the instrument pass is the owner's switch (enrichment.instruments)
        if not skip_instruments:
            from routers.settings import instruments_on
            skip_instruments = not instruments_on()
        self.skip_instruments = skip_instruments
        self.force_sonic = force_sonic
        self.force_instruments = force_instruments

        # Lazy-loaded components: the run's own two passes (each loads its
        # models on first use) and Last.fm
        self._audio_embedding_generator = None
        self._audio_analyzer = None
        self._lastfm_service = None

    def _get_audio_embedding_generator(self) -> "AudioEmbeddingGenerator":
        if self._audio_embedding_generator is None:
            from embeddings import AudioEmbeddingGenerator
            self._audio_embedding_generator = AudioEmbeddingGenerator()
        return self._audio_embedding_generator

    def _get_audio_analyzer(self) -> "AudioAnalyzer":
        if self._audio_analyzer is None:
            from audio_analysis import AudioAnalyzer
            self._audio_analyzer = AudioAnalyzer()
        return self._audio_analyzer

    @staticmethod
    def _stage_result(stats: Dict[str, int]) -> str:
        """A pass's statistics for one track as the pipeline's step result:
        nothing pending (no file to analyse, a better copy's records stand)
        is a skip."""
        if stats.get("success"):
            return 'success'
        return 'failed' if stats.get("failed") else 'skipped'

    def _get_lastfm_service(self):
        """Lazy-load Last.fm service."""
        if self._lastfm_service is None:
            from lastfm import LastFmService
            self._lastfm_service = LastFmService()
        return self._lastfm_service

    def _check_track_status(self, db: Session, track: Track) -> Dict[str, bool]:
        """
        Check what data is missing for a track.

        Returns dict with keys: needs_sonic, needs_artist_info,
        needs_instruments. Each pass's own queue decides what of it the
        track lacks.
        """
        status = {
            'needs_sonic': not self.skip_sonic,
            'needs_instruments': not self.skip_instruments,
        }

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

        The audio steps are the run's own passes scoped to this track
        (embeddings.AudioEmbeddingGenerator.sonic_pass, then
        audio_analysis.AudioAnalyzer.instrument_pass — the sonic pass
        before the instruments, as in the run): the elected analysis-source
        file, its decode, its registration, the rank every save replaces by,
        the records' saves and the notary's wake are theirs, so this
        per-track path writes exactly what the bulk run writes.

        Returns dict with step results (success/failed/skipped).
        """
        results = {}

        # Step 1: the sonic pass
        if status['needs_sonic']:
            if progress_callback:
                progress_callback("Sonic analysis")
            try:
                results['sonic'] = self._stage_result(
                    self._get_audio_embedding_generator().sonic_pass(
                        track_ids=[track.id], force=self.force_sonic))
            except Exception as e:
                logger.error(f"Sonic pass failed for track {track.id}: {e}")
                results['sonic'] = 'failed'
        else:
            results['sonic'] = 'skipped'

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

                except Exception as e:
                    logger.error(f"Last.fm artist enrichment failed: {e}")
                    results['lastfm_artist'] = 'error'
                    db.rollback()
            else:
                results['lastfm_artist'] = 'skipped'

        # Step 5: the instrument pass
        if status['needs_instruments']:
            if progress_callback:
                progress_callback("Instruments")
            try:
                results['instruments'] = self._stage_result(
                    self._get_audio_analyzer().instrument_pass(
                        track_ids=[track.id], force=self.force_instruments))
            except Exception as e:
                logger.error(f"Instrument pass failed for track {track.id}: {e}")
                results['instruments'] = 'failed'
        else:
            results['instruments'] = 'skipped'

        return results

    def _build_needs_enrichment_filter(self):
        """
        Build SQLAlchemy filter conditions to select only tracks
        that need at least one enrichment step.

        Returns a list of OR conditions. If empty, no filtering needed
        (shouldn't happen — means all steps are skipped).
        """
        conditions = []

        # The passes: an owned track with an analysis-source file (the
        # passes' own queues read nothing else) lacking a record one of them
        # makes — driven from the file, never from every track: the phantom
        # layer is millions of them
        analysed = exists(select(MediaFile.id).where(
            MediaFile.track_id == Track.id, MediaFile.is_analysis_source.is_(True)))
        lacks = []
        if not self.skip_sonic:
            if self.force_sonic:
                lacks.append(True)
            else:
                lacks.append(~exists(select(Embedding.id).where(Embedding.track_id == Track.id)))
                lacks.append(~exists(select(AudioFeature.id).where(AudioFeature.track_id == Track.id)))
        if not self.skip_instruments:
            if self.force_instruments:
                lacks.append(True)
            else:
                lacks.append(~exists(select(TrackInstruments.id).where(
                    TrackInstruments.track_id == Track.id)))
        if lacks:
            conditions.append(analysed if any(c is True for c in lacks)
                              else and_(analysed, or_(*lacks)))

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
        if not (self.skip_sonic and self.skip_instruments):
            import provenance
            provenance.require_fingerprinter()
        stats = {
            'processed': 0,
            'sonic_success': 0,
            'sonic_failed': 0,
            'lastfm_artist_success': 0,
            'lastfm_album_success': 0,
            'instruments_success': 0,
            'instruments_failed': 0,
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

                if results.get('sonic') == 'success':
                    stats['sonic_success'] += 1
                elif results.get('sonic') == 'failed':
                    stats['sonic_failed'] += 1

                if results.get('lastfm_artist') == 'success':
                    stats['lastfm_artist_success'] += 1
                if results.get('lastfm_album') == 'success':
                    stats['lastfm_album_success'] += 1

                if results.get('instruments') == 'success':
                    stats['instruments_success'] += 1
                elif results.get('instruments') == 'failed':
                    stats['instruments_failed'] += 1

            logger.info(f"Enrichment complete: {stats['processed']} tracks processed")


        return stats
