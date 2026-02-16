"""
Comprehensive track enrichment pipeline.

Orchestrates all data aggregation steps in the correct order:
1. Audio Embedding (CLAP) - base audio feature
2. Last.fm Artist Info - artist metadata
3. Last.fm Album Info - album metadata
4. Last.fm Track Stats - track popularity
5. Audio Analysis - DSP features + CLAP classification

Each step is conditional - only runs if data is missing.
Supports filters, limits, time constraints, and graceful error handling.
"""

import logging
import time
from typing import Dict, List, Optional

from sqlalchemy.orm import Session
from tqdm import tqdm

from database import get_db_context
from models import Track, Album, Artist, TrackArtist, AudioFeature, SimilarArtist, ArtistBio
from embeddings import AudioEmbeddingGenerator
from audio_analysis import AudioAnalyzer

logger = logging.getLogger(__name__)


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
        """
        Initialize enrichment pipeline.

        Args:
            skip_embeddings: Skip audio embedding generation
            skip_lastfm: Skip Last.fm enrichment
            skip_audio_analysis: Skip audio feature extraction
            force_embeddings: Regenerate audio embeddings even if exist
            force_audio_analysis: Re-analyze audio even if features exist
            lastfm_delay: Delay between Last.fm requests (seconds)
        """
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

    def _get_audio_embedding_generator(self) -> AudioEmbeddingGenerator:
        """Lazy-load audio embedding generator."""
        if self._audio_embedding_generator is None:
            self._audio_embedding_generator = AudioEmbeddingGenerator()
            self._audio_embedding_generator.load_model()
        return self._audio_embedding_generator

    def _get_audio_analyzer(self) -> AudioAnalyzer:
        """Lazy-load audio analyzer."""
        if self._audio_analyzer is None:
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

    def _enrich_new_similar_artists(self, db: Session, artist_id: int, lastfm) -> int:
        """Enrich similar artists that don't have bios yet (bio+tags only, no recursion)."""
        similar_ids = db.query(SimilarArtist.similar_artist_id).filter(
            SimilarArtist.artist_id == artist_id,
            SimilarArtist.source == "lastfm",
        ).all()

        enriched = 0
        for (sim_id,) in similar_ids:
            # Skip if already has bio
            has_bio = db.query(ArtistBio).filter(
                ArtistBio.artist_id == sim_id,
                ArtistBio.source == "lastfm",
            ).first()
            if has_bio:
                continue

            sim_artist = db.query(Artist).get(sim_id)
            if not sim_artist:
                continue

            try:
                lastfm.enrich_artist(db, sim_id, sim_artist.name, skip_similar=True)
                enriched += 1
                time.sleep(self.lastfm_delay)
            except Exception as e:
                logger.debug(f"Failed to enrich similar artist {sim_artist.name}: {e}")
                db.rollback()

        if enriched:
            logger.info(f"Enriched {enriched} similar artists for artist_id={artist_id}")
        return enriched

    def _check_track_status(self, db: Session, track: Track) -> Dict[str, bool]:
        """
        Check what data is missing for a track.

        Returns dict with keys: needs_audio_embedding, needs_artist_info,
        needs_album_info, needs_track_stats, needs_audio_features
        """
        status = {}

        # Audio embedding
        status['needs_audio_embedding'] = (
            not self.skip_embeddings and
            (self.force_embeddings or track.embedding_id is None)
        )

        # Audio features
        audio_feature = db.query(AudioFeature).filter(
            AudioFeature.track_id == track.id
        ).first()
        status['needs_audio_features'] = (
            not self.skip_audio_analysis and
            (self.force_audio_analysis or audio_feature is None)
        )

        # Last.fm data (only check if not skipping)
        if not self.skip_lastfm:
            # Get primary artist
            artist_row = db.query(Artist).join(TrackArtist).filter(
                TrackArtist.track_id == track.id,
                TrackArtist.role == 'primary'
            ).first()

            if artist_row:
                # Check if artist has bio
                from models import ArtistBio
                artist_bio = db.query(ArtistBio).filter(
                    ArtistBio.artist_id == artist_row.id,
                    ArtistBio.source == 'lastfm'
                ).first()
                status['needs_artist_info'] = artist_bio is None
                status['artist_id'] = artist_row.id
                status['artist_name'] = artist_row.name
            else:
                status['needs_artist_info'] = False

            # Check if album has info
            from models import AlbumInfo
            album_info = db.query(AlbumInfo).filter(
                AlbumInfo.album_id == track.album_id,
                AlbumInfo.source == 'lastfm'
            ).first()
            status['needs_album_info'] = album_info is None

            # Check if track has stats
            from models import TrackStats
            track_stats = db.query(TrackStats).filter(
                TrackStats.track_id == track.id,
                TrackStats.source == 'lastfm'
            ).first()
            status['needs_track_stats'] = track_stats is None
        else:
            status['needs_artist_info'] = False
            status['needs_album_info'] = False
            status['needs_track_stats'] = False

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
        # Save track ID early for safe logging after potential rollback
        track_id = track.id

        results = {}

        # Step 1: Audio Embedding
        if status['needs_audio_embedding']:
            if progress_callback:
                progress_callback("Audio embedding")
            try:
                generator = self._get_audio_embedding_generator()
                audio = generator._load_audio(track.file_path)
                if audio is not None:
                    embeddings = generator._generate_batch_embeddings([audio])
                    if embeddings is not None:
                        embedding_model = generator._get_or_create_embedding_model(db)
                        generator._save_embedding(db, track, embeddings[0], embedding_model)
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
            results['audio_embedding'] = 'skipped'

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

                    # Enrich newly created similar artists (bio only, no recursion)
                    self._enrich_new_similar_artists(db, status['artist_id'], lastfm)

                except Exception as e:
                    logger.error(f"Last.fm artist enrichment failed: {e}")
                    results['lastfm_artist'] = 'error'
                    db.rollback()  # Rollback failed transaction
            else:
                results['lastfm_artist'] = 'skipped'

            # Album info
            if status.get('needs_album_info'):
                if progress_callback:
                    progress_callback("Last.fm album")
                try:
                    album = db.query(Album).get(track.album_id)
                    result = lastfm.enrich_album(
                        db, track.album_id, status.get('artist_name', ''), album.title
                    )
                    results['lastfm_album'] = result['status']
                    time.sleep(self.lastfm_delay)
                except Exception as e:
                    logger.error(f"Last.fm album enrichment failed: {e}")
                    results['lastfm_album'] = 'error'
                    db.rollback()  # Rollback failed transaction
            else:
                results['lastfm_album'] = 'skipped'

            # Track stats
            if status.get('needs_track_stats'):
                if progress_callback:
                    progress_callback("Last.fm track")
                try:
                    result = lastfm.enrich_track(
                        db, track.id, status.get('artist_name', ''), track.title
                    )
                    results['lastfm_track'] = result['status']
                    time.sleep(self.lastfm_delay)
                except Exception as e:
                    logger.error(f"Last.fm track enrichment failed: {e}")
                    results['lastfm_track'] = 'error'
                    db.rollback()  # Rollback failed transaction
            else:
                results['lastfm_track'] = 'skipped'

        # Step 5: Audio Analysis
        if status['needs_audio_features']:
            if progress_callback:
                progress_callback("Audio analysis")
            try:
                analyzer = self._get_audio_analyzer()
                features = analyzer.analyze_track(track.file_path)
                if features is not None:
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
            results['audio_features'] = 'skipped'

        return results

    def enrich_tracks(
        self,
        limit: Optional[int] = None,
        order_by_date: bool = False,
        max_duration_seconds: Optional[int] = None,
        track_ids: Optional[List[int]] = None,
        worker_id: Optional[int] = None,
        worker_count: Optional[int] = None,
    ) -> Dict[str, int]:
        """
        Run comprehensive enrichment pipeline on tracks.

        Args:
            limit: Maximum number of tracks to process
            order_by_date: Process newest tracks first
            max_duration_seconds: Maximum duration in seconds
            track_ids: Specific track IDs to process (from filters)
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

        try:
            with get_db_context() as db:
                # Build query
                query = db.query(Track)

                if track_ids is not None:
                    query = query.filter(Track.id.in_(track_ids))

                if order_by_date:
                    query = query.order_by(Track.file_modified_at.desc().nulls_last())

                if limit:
                    query = query.limit(limit)

                tracks = query.all()
                total_before_worker_filter = len(tracks)

                # Worker filtering: assign tracks to workers based on track.id modulo
                if worker_count is not None:
                    tracks = [t for t in tracks if t.id % worker_count == worker_id]
                    logger.info(f"Worker {worker_id}/{worker_count}: filtered to {len(tracks)} tracks (from {total_before_worker_filter} total)")

                total = len(tracks)

                if total == 0:
                    logger.info("No tracks to process")
                    return stats

                logger.info(f"Processing {total} tracks")
                if max_duration_seconds:
                    logger.info(f"Time limit: {max_duration_seconds}s")

                # Process each track
                for track in tqdm(tracks, desc="Enriching tracks", unit="track"):
                    # Check time limit
                    if max_duration_seconds:
                        elapsed = time.time() - start_time
                        if elapsed >= max_duration_seconds:
                            logger.info(f"Time limit reached ({elapsed:.1f}s), stopping")
                            break

                    stats['processed'] += 1

                    # Check what's needed
                    status = self._check_track_status(db, track)

                    # Enrich track
                    results = self._enrich_track(db, track, status)

                    # Update stats
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

        finally:
            # Cleanup
            if self._audio_embedding_generator:
                self._audio_embedding_generator.unload_model()
            if self._audio_analyzer:
                self._audio_analyzer.unload_model()

        return stats
