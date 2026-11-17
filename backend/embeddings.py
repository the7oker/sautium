"""
Audio embeddings using the CLAP model (laion/clap-htsat-unfused, 512-d), and
the sonic pass that makes them.

Uses the analysis source media file (is_analysis_source=TRUE) for each track.
Per track: the track-level embedding (search vector; becomes the materialized
mean of segments after the Stage-2b flip) + windowed segment embeddings on
the canonical 10s grid (embedding_segments).

The sonic pass (AudioEmbeddingGenerator.sonic_pass, 2026-11-11) is the first
of a node's two analysis passes: one decode a track, the fingerprint, the
segments and the features (librosa, audio_analysis.AudioAnalyzer.
features_from_array) — every profile with the ML stack runs it. The
instrument pass (audio_analysis) follows it behind the owner's switch.
"""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

import librosa
import numpy as np
import torch
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session
from tqdm import tqdm

import provenance
from audio_analysis import (ChangedUnderDecode, ShortDecode, TrackTooLong, decode_as_read,
                            load_full_track_48k, refuse, still_as_read)
from config import settings
from database import get_db_context
from desktop.sync_client import UNIT_NORM, is_unit
from models import Embedding, EmbeddingModel, Track, MediaFile
from uuid_utils import embedding_model_uuid


def _reanalyze_imported() -> bool:
    """Whether owned tracks whose analysis arrived over P2P re-enter the GPU
    queue (Settings toggle; audio_analysis.py reads the same flag)."""
    from routers.settings import _read
    return bool(_read("enrichment.reanalyze_imported"))

logger = logging.getLogger(__name__)


# ── Canonical segment grid ──────────────────────────────────────────────────
# Window i covers [i*10s, i*10s+10s) from the track start. Every sampling
# density (balanced default, full research grid, "deepen this artist") writes
# a compatible, top-uppable SUBSET of this one grid — an absent index always
# means "not analyzed", which the done-predicates rely on.

WINDOW_SECONDS = 10

# Methodology version stamped on embeddings rows and carried through P2P sync
# (peers re-pull rows whose version is older than the source's):
#   1 — random 10s crop of the middle 30s (CLAP rand_trunc)
#   2 — normalized mean of canonical-grid segment embeddings (2026-07-05)
EMBEDDING_ANALYSIS_VERSION = 2


def balanced_k(duration_seconds: float) -> int:
    """Windows needed for a portrait within 0.99 cosine of the full-grid
    centroid for >=99% of tracks — measured on a 303-track stratified
    experiment 2026-07-04 (constant-concentration and variance-adaptive
    strategies both measured worse at equal cost)."""
    if duration_seconds <= 480:
        return 12
    if duration_seconds <= 1200:
        return 16
    return 24


def segment_grid_indices(duration_seconds: float, full: bool = False) -> list:
    """Evenly spaced canonical-grid indices (first window at the track start,
    last at the end); the whole grid when full=True."""
    n = max(1, int(duration_seconds) // WINDOW_SECONDS)
    if full:
        return list(range(n))
    k = balanced_k(duration_seconds)
    return np.unique(np.linspace(0, n - 1, min(k, n)).round().astype(int)).tolist()


# On MPS a query's tokens are padded up to the first of these: the backend keeps
# the graphs of every shape a forward has had on the host heap until the process
# ends, ~5 MB a length for this text tower (a query of each length from 1 to 77
# words was 74 shapes: 371 MB), so a query meets a closed set of shapes — the
# encoder of the text embeddings has the same reason (text_embedder._rungs). The
# attention mask leaves the vector as it was: 0.9999 and over from the
# unpadded one. The last rung is the tower's own limit (512 tokens; its position
# table ends at 514): a longer query is not padded, and the tower cannot take it.
_QUERY_RUNGS = (16, 32, 64, 128, 256, 512)


def _padded_to_rung(inputs, pad_token_id: int) -> dict:
    """The tokenizer's output for one text, padded AFTER it to the first of
    _QUERY_RUNGS at or above its length: the tokenizer keeps its settings for
    every caller (the Discovery blocks of one search meet on one processor)."""
    length = inputs["input_ids"].shape[1]
    pad = next((r for r in _QUERY_RUNGS if r >= length), length) - length
    return {k: torch.nn.functional.pad(v, (0, pad), value=pad_token_id if k == "input_ids" else 0)
            for k, v in inputs.items()}



# The sonic pass's queue, driven from media_files (is_analysis_source) — the
# OWNED set. tracks holds millions of trackless phantom rows; driving from
# tracks would scan them all to find owned ones needing analysis. Material
# under one grid window is never queued: no fingerprint, no address, no
# analysis (provenance.MIN_MATERIAL_SECONDS), nor is a file of a copy that is
# away (album_variants.absent_since): nothing can read it until it is back.
# Each row says which of the pass's two records it lacks, so one decode makes
# exactly those.
#
# A record is pending when there is none, when it is linked to no source
# (legacy rows — re-analysis links them), or when it was made from material
# other than the current analysis source holds at its rank: no row of this
# file in the material group of the record's row ranks no higher than it
# (provenance.file_in_group_sql — the source moved to a better rip, or a
# stream preview awaits its owned upgrade; a file that is the same material
# at a rank no higher, which the pass registered and skipped, is not asked
# again). An embedding is pending, too, while it is a portrait without its
# segments — the legacy mean of before 2026-07-05, from which no zero-shot
# score is derived — unless it was imported: the sync fills that gap.
# Analysis that arrived over P2P (asrc.imported) also matches the third arm
# — it ranks below every file — but re-deriving it from the owned file is a
# POLICY, not a repair: first-hand matches this node's actual rip and is the
# network's poison detector, yet it re-spends GPU on tracks that already
# work. enrichment.reanalyze_imported picks the policy; default off. A file
# that returned after a prune is linked back to its analysis
# (provenance.relink_returned_files) after the import's canon and again
# before each pass reads this queue, so it is not pending here.
_SONIC_ROWS_SQL = f"""
    SELECT mf.track_id, mf.id AS media_file_id,
           mf.file_path, mf.bit_depth,
           mf.sample_rate, mf.is_lossless,
           mf.duration_seconds,
           mf.cue_start_seconds, mf.cue_end_seconds, mf.file_modified_at,
           mf.file_size_bytes, mf.stat_mtime_ns, mf.stat_ctime_ns,
           (e.id IS NULL OR af.id IS NULL) AS gap,
           (e.id IS NULL
            OR e.analysis_source_id IS NULL
            OR (NOT {provenance.file_in_group_sql("easrc")}
                AND (:reanalyze OR NOT easrc.imported))
            OR (NOT easrc.imported
                AND NOT EXISTS (SELECT 1 FROM embedding_segments g
                                 WHERE g.embedding_id = e.id))) AS embedding_pending,
           (af.id IS NULL
            OR af.analysis_source_id IS NULL
            OR (NOT {provenance.file_in_group_sql("fasrc")}
                AND (:reanalyze OR NOT fasrc.imported))) AS features_pending
    FROM media_files mf
    JOIN album_variants av ON av.id = mf.album_variant_id
    LEFT JOIN embeddings e ON e.track_id = mf.track_id
    LEFT JOIN analysis_sources easrc ON easrc.id = e.analysis_source_id
    LEFT JOIN audio_features af ON af.track_id = mf.track_id
    LEFT JOIN analysis_sources fasrc ON fasrc.id = af.analysis_source_id
    WHERE mf.is_analysis_source = true
      AND av.absent_since IS NULL
      AND NOT mf.decode_refused
      AND (mf.duration_seconds IS NULL
           OR mf.duration_seconds >= :min_seconds)
"""

SOURCE_FILES_SQL = f"SELECT * FROM ({_SONIC_ROWS_SQL}) q WHERE true"

PENDING_SQL = (f"SELECT * FROM ({_SONIC_ROWS_SQL}) q"
               " WHERE (q.embedding_pending OR q.features_pending)")


def newest_file_id(db: Session) -> int:
    """The newest media_files row: what a sonic pass that reads its queue
    now has seen."""
    return int(db.execute(sa_text("SELECT COALESCE(max(id), 0) FROM media_files")).scalar())


def sonic_work_since(db: Session, file_id: int, track_ids: Optional[list] = None,
                     longest: float = float("inf")) -> bool:
    """Whether a file newer than `file_id` waits for the sonic pass — a scan
    added it after the pass read its queue — among `track_ids` when the run
    is scoped to them. The instrument pass asks at each of its batch seams
    and gives way: the sonic pass of any track goes before the instruments of
    any track. A file the pass has seen and could not analyse stays pending
    without calling it back, nor does one longer than the pass may take
    (`longest`)."""
    import analysis_pace
    scope = "" if track_ids is None else " AND p.track_id = ANY(:track_ids)"
    return bool(db.execute(sa_text(
        f"SELECT EXISTS (SELECT 1 FROM ({PENDING_SQL}) p WHERE p.media_file_id > :since{scope}"
        f"{analysis_pace.within_reach_sql('p')})"),
        {"since": file_id, "min_seconds": provenance.MIN_MATERIAL_SECONDS,
         "reanalyze": _reanalyze_imported(), "track_ids": track_ids,
         "longest": longest}).scalar())


class NotUnitVectors(ValueError):
    """An analysis this node made whose segments or portrait are not unit
    vectors — what the import gate of every peer refuses, and the derived
    scores assume. The embedding is not saved; the track's other kinds
    (features, instruments) are not CLAP's, and are saved as they are when
    CLAP fails in any other way."""


class AudioEmbeddingGenerator:
    """Generate audio embeddings using CLAP model on GPU."""

    def __init__(
        self,
        model_name: Optional[str] = None,
        batch_size: Optional[int] = None,
        device: Optional[str] = None,
    ):
        self.model_name = model_name or settings.embedding_model
        self.batch_size = batch_size or settings.embedding_batch_size
        self.sample_rate = 48000  # CLAP expects 48kHz

        from device import get_device
        self.device = device or get_device()
        if self.device == "cpu":
            logger.warning("No GPU accelerator detected, using CPU (will be slow)")

        self.model = None
        self.processor = None

    def load_model(self):
        """Acquire process-wide singleton CLAP processor + model."""
        if self.model is not None:
            return
        from clap_model import get_clap_model
        self.processor, self.model = get_clap_model(self.model_name, self.device)

    def text_to_embedding(self, text: str) -> np.ndarray:
        """
        Encode text to a 512d embedding using CLAP's text encoder.

        The resulting vector lives in the same space as audio embeddings,
        enabling text-to-audio similarity search.

        Args:
            text: Natural language description (e.g. "slow emotional blues").

        Returns:
            L2-normalized numpy array of shape (512,).
        """
        from device import autocast, cast_inputs
        self.load_model()

        inputs = self.processor(text=[text], return_tensors="pt", padding=True)
        if self.device == "mps":
            inputs = _padded_to_rung(inputs, self.processor.tokenizer.pad_token_id)
        inputs = cast_inputs(inputs, self.model.dtype)
        inputs = {k: v.to(self.device) for k, v in inputs.items()}

        with torch.no_grad(), autocast(self.device):
            text_features = self.model.get_text_features(**inputs)

        # L2 normalize in fp32 — normalization on fp16 saturates near zero
        text_features = torch.nn.functional.normalize(text_features.float(), p=2, dim=1)

        return text_features[0].cpu().numpy()

    def _load_audio(self, file_path: str, cue_start: Optional[float] = None,
                    cue_end: Optional[float] = None,
                    duration_seconds: Optional[float] = None,
                    max_seconds: Optional[float] = None) -> Optional[np.ndarray]:
        """Load the WHOLE track at 48kHz mono (shared scanner decode path) —
        segments cover the full canonical grid; the track-level vector's
        middle window is sliced from the same array. For a CUE slice the
        bounded decode IS the track, so the grid stays array-relative. A
        decode past `max_seconds` raises TrackTooLong, one short of the
        header's length ShortDecode; any other failure is None."""
        try:
            return load_full_track_48k(file_path, cue_start, cue_end, duration_seconds, max_seconds)
        except (TrackTooLong, ShortDecode):
            raise
        except Exception as e:
            logger.error(f"Failed to load audio {file_path}: {e}")
            return None

    def _generate_batch_embeddings(
        self, audio_arrays: List[np.ndarray]
    ) -> Optional[np.ndarray]:
        """
        Generate embeddings for a batch of audio arrays.

        Returns L2-normalized embeddings as numpy array, or None on failure.
        """
        try:
            from device import autocast, cast_inputs
            inputs = self.processor(
                audio=audio_arrays,
                sampling_rate=self.sample_rate,
                return_tensors="pt",
                padding=True,
            )
            inputs = cast_inputs(inputs, self.model.dtype)
            inputs = {k: v.to(self.device) for k, v in inputs.items()}

            with torch.no_grad(), autocast(self.device):
                embeddings = self.model.get_audio_features(**inputs)

            # Diagnostic: report dtype + pre-normalize stats on first NaN to
            # distinguish fp16 attention overflow (post-encoder inf/NaN) from
            # actual model output failure.
            pre_nan = torch.isnan(embeddings).any().item()
            pre_inf = torch.isinf(embeddings).any().item()

            # L2 normalize in fp32 — fp16 norm saturates and loses precision
            embeddings = torch.nn.functional.normalize(embeddings.float(), p=2, dim=1)

            result = embeddings.cpu().numpy()

            if np.isnan(result).any():
                logger.error(
                    "NaN detected in embeddings (model_dtype=%s, pre_norm_nan=%s, pre_norm_inf=%s, batch=%d)",
                    self.model.dtype, pre_nan, pre_inf, len(audio_arrays),
                )
                return None

            return result

        except torch.cuda.OutOfMemoryError:
            logger.error("GPU OOM during batch embedding generation")
            from device import empty_cache
            empty_cache(self.device)
            return None
        except Exception as e:
            logger.error(f"Embedding generation failed: {e}")
            return None

    def _get_or_create_embedding_model(self, db: Session) -> EmbeddingModel:
        """Get existing embedding model record or create one (deterministic UUID PK)."""
        mid = embedding_model_uuid(self.model_name)
        em = db.query(EmbeddingModel).filter(EmbeddingModel.id == mid).first()
        if not em:
            em = EmbeddingModel(
                id=mid,
                name=self.model_name,
                description=f"CLAP audio embedding model ({settings.embedding_dimension}d)",
                dimension=settings.embedding_dimension,
            )
            db.add(em)
            # Committed at once: the run gives way at its seams
            # (device.give_way), and another pass inserting the same id would
            # wait for this transaction while this one waits for that pass
            db.commit()
            logger.info(f"Created embedding model record: {self.model_name}")
        return em

    def _compute_segments(self, audio_full: np.ndarray):
        """Balanced-grid segment embeddings for one track from its already
        decoded audio; windows of one track are encoded as one batch. Pure
        compute, no DB. Returns (indices, vectors, normalized mean) — the
        mean is the track-level portrait — or None on failure."""
        duration = len(audio_full) / self.sample_rate
        idxs = segment_grid_indices(duration)
        step = self.sample_rate * WINDOW_SECONDS
        windows = [audio_full[i * step:(i + 1) * step] for i in idxs]
        vecs = self._generate_batch_embeddings(windows)
        if vecs is None:
            return None
        mean = vecs.mean(axis=0)
        return idxs, vecs, mean / np.linalg.norm(mean)

    def _persist_analysis(
        self, db: Session, track_id, model: EmbeddingModel,
        idxs, vecs, portrait: np.ndarray,
        analysis_source_id: Optional[int],
    ) -> bool:
        """Upsert the track's embedding row and REPLACE its segments, in one
        transaction unit. The overwrite decision happens before anything is
        written, by provenance.outranked — material rank first (own file >
        lossless stream > lossy stream; imported and unlinked legacy rows
        rank below everything), source quality between copies of one
        material — so a lower-ranked pass (e.g. a stream preview racing an
        owned scan) can no longer clobber segments while the mean survives,
        and the track's elected file replaces the analysis of another
        material, of whatever quality, rather than being analysed and
        refused on every run. The source row describes the copy
        (provider_id, bit_depth and is_lossless are its claims).
        Replaced segments are deleted, not updated: fresh rows are unsigned by
        construction, which is the seal-invalidation model for segments.
        Returns False when the existing row outranks the incoming analysis.
        A portrait without its segments (the legacy mean) outranks nothing,
        as it holds nothing (provenance.held_in_group): whatever copy made
        it, the pass that makes segments replaces it.

        This is where every vector this node computed is written, the sonic
        pass's and a stream's alike, so here they are held to the band the
        import gate holds a peer's to (sync_client.is_unit — the gate's own
        function, so the two cannot differ): NotUnitVectors, before anything
        is written, when a segment or the portrait of an analysis that would
        be saved is outside it — one the rank declines is not saved, and
        not checked. The encoder normalizes, so that is a fault of this
        node's model or code — and without the check its bundles would be
        sealed, served, and refused by every peer."""
        existing = db.execute(sa_text("""
            SELECT e.analysis_source_id,
                   EXISTS (SELECT 1 FROM embedding_segments g WHERE g.embedding_id = e.id) AS whole
              FROM embeddings e
             WHERE e.track_id = :tid AND e.model_id = :mid
        """), {"tid": str(track_id), "mid": str(model.id)}).first()
        if existing and existing.whole and provenance.outranked(
                db, existing.analysis_source_id, analysis_source_id):
            return False

        segments = [v.tolist() for v in vecs]
        mean = portrait.tolist()
        if not (is_unit(mean) and all(is_unit(s) for s in segments)):
            norms = [float(np.linalg.norm(np.asarray(s, dtype=np.float64))) for s in segments]
            raise NotUnitVectors(
                f"the analysis of track {track_id} is not unit vectors: segment norms "
                f"{min(norms):.6g}..{max(norms):.6g}, portrait "
                f"{float(np.linalg.norm(np.asarray(mean, dtype=np.float64))):.6g} "
                f"(a unit vector is {UNIT_NORM[0]}..{UNIT_NORM[1]}); model {model.name}")

        eid = db.execute(sa_text("""
            INSERT INTO embeddings (track_id, model_id, vector,
                                    analysis_source_id, analysis_version)
            VALUES (:tid, :mid, CAST(:vec AS vector), :sid, :ver)
            ON CONFLICT (track_id, model_id) DO UPDATE
               SET vector = EXCLUDED.vector,
                   analysis_source_id = EXCLUDED.analysis_source_id,
                   analysis_version = EXCLUDED.analysis_version
            RETURNING id
        """), {"tid": str(track_id), "mid": str(model.id),
               "vec": str(mean), "sid": analysis_source_id,
               "ver": EMBEDDING_ANALYSIS_VERSION}).scalar()
        db.execute(sa_text(
            "DELETE FROM embedding_segments WHERE embedding_id = :eid"),
            {"eid": eid})
        # One statement: the trigger that keeps the bundle's zero-shot scores
        # (zero_shot_scores_refresh) recomputes it once per INSERT
        db.execute(sa_text("""
            INSERT INTO embedding_segments (embedding_id, segment_index, vector)
            SELECT :eid, s.idx, CAST(s.vec AS vector)
              FROM unnest(CAST(:idxs AS smallint[]), CAST(:vecs AS text[])) AS s(idx, vec)
        """), {"eid": eid, "idxs": [int(i) for i in idxs],
               "vecs": [str(s) for s in segments]})
        return True

    def sonic_pass(self, limit: Optional[int] = None, order_by_date: bool = False,
                   max_duration_seconds: Optional[int] = None, track_ids: Optional[list] = None,
                   worker_id: Optional[int] = None, worker_count: Optional[int] = None,
                   cancel_flag=None, force: bool = False, on_batch=None,
                   since_file_id: Optional[int] = None) -> Dict[str, int]:
        """The sonic pass over the tracks whose embedding or features are
        pending (PENDING_SQL): the registration of the track's analysis-
        source file (the fingerprint, once per file), one decode, then what
        the track lacks of its two records — the balanced-grid segments with
        their mean portrait (CLAP; the database derives the zero-shot scores
        from the segments) and the features (librosa) — each skipped when
        the file's material group already holds it at a rank no lower
        (provenance.held_in_group), each saved by the rank. The instrument
        pass (audio_analysis) is the other pass, after this one.

        Args:
            limit: Maximum number of tracks to process.
            order_by_date: If True, process newest tracks first.
            max_duration_seconds: Maximum duration in seconds.
            track_ids: If provided, only process these track IDs.
            worker_id: Worker index (0-based) for parallel processing.
            worker_count: Total number of workers for parallel processing.
            cancel_flag: Asked before every batch; True ends the pass there.
            force: Re-analyse every analysis-source track regardless of state.
            on_batch: Called with (tracks done, tracks queued) after each batch.
            since_file_id: only files newer than this media_files id — the
                files a scan added since the run's sonic pass read its queue,
                when the instrument pass gave way to them.

        Returns:
            processed, success (a record saved), failed, skipped (nothing to
            make: the group holds it, or a better copy's records stand),
            embeddings and features (the records saved), and not_unit — the
            tracks whose embedding was not saved because the vectors computed
            for it were not unit vectors (NotUnitVectors). Such a track is
            counted as any track whose CLAP forward failed: a success when
            its features were saved, failed when nothing was.
        """
        import time
        import notary
        import analysis_pace
        from audio_analysis import AudioAnalyzer, save_features

        provenance.require_fingerprinter()
        stats = {"processed": 0, "success": 0, "failed": 0, "skipped": 0,
                 "embeddings": 0, "features": 0, "too_long": 0, "not_unit": 0, "refused": 0}
        start_time = time.time()
        if not force:
            # A failed look costs only the work it would have saved: the
            # pass analyses what stays pending, as before.
            try:
                provenance.relink_returned_files(track_ids, cancel_check=cancel_flag)
            except Exception as e:
                logger.error(f"Re-link before the pass failed: {e}", exc_info=True)

        with get_db_context() as db:
            embedding_model = self._get_or_create_embedding_model(db)

            query_sql = SOURCE_FILES_SQL if force else PENDING_SQL
            params = {"min_seconds": provenance.MIN_MATERIAL_SECONDS,
                      "reanalyze": _reanalyze_imported()}

            if track_ids is not None:
                query_sql += " AND q.track_id = ANY(:track_ids)"
                params["track_ids"] = track_ids
            if since_file_id is not None:
                query_sql += " AND q.media_file_id > :since"
                params["since"] = since_file_id

            # A track longer than this machine can hold now is left out by
            # its known duration (analysis_pace.longest_track_seconds), and
            # counted: the decode and its resample grow with its length
            params["longest"] = analysis_pace.longest_track_seconds([analysis_pace.SONIC])
            stats["too_long"] = db.execute(sa_text(
                f"SELECT count(*) FROM ({query_sql}{analysis_pace.beyond_reach_sql('q')}) b"),
                params).scalar()
            if stats["too_long"]:
                logger.info(f"Sonic pass: {stats['too_long']} track(s) longer than this machine "
                            f"can hold now ({params['longest'] / 60:.0f} min) are left to the network")
            query_sql += analysis_pace.within_reach_sql("q")

            # Gaps first: a track missing a record beats an upgrade
            # (stream→owned, better rip, imported→first-hand) — coverage
            # before recomputation.
            if order_by_date:
                query_sql += " ORDER BY q.gap DESC, q.file_modified_at DESC NULLS LAST"
            else:
                query_sql += " ORDER BY q.gap DESC, q.track_id"

            if limit:
                query_sql += f" LIMIT {limit}"

            rows = db.execute(sa_text(query_sql), params).fetchall()
            total = len(rows)

            if total == 0:
                (logger.debug if track_ids is not None else logger.info)(
                    "No tracks pending the sonic pass")
                return stats

            # CLAP only for a queue that holds an embedding to make: after the
            # features alone went (an imported record deleted), librosa is all
            if force or any(r.embedding_pending for r in rows):
                self.load_model()
            analyzer = AudioAnalyzer(device=self.device)

            logger.info(f"Sonic pass: {total} tracks (batch_size={self.batch_size})")
            if max_duration_seconds:
                logger.info(f"Time limit: {max_duration_seconds} seconds ({max_duration_seconds/60:.1f} minutes)")

            # --- Pipelined batch processing ---
            # A batch is registered first (the fingerprint, once per file) and
            # what its group already holds is left out; then CPU threads
            # decode it, and run librosa where its features are to be made,
            # while the accelerator encodes the batch before it.
            def _plan(batch_rows):
                """(what to decode, the outcomes of the rest, the longest
                track it may decode) — the outcomes are counted with the
                batch, when it is taken. What this machine holds is read
                again here: a model loaded beside the pass since the queue was
                read (a search's text encoder, the translator) leaves less
                room, and CLAP, once loaded, is resident and no more to
                reserve."""
                longest = analysis_pace.longest_track_seconds([analysis_pace.SONIC])
                todo, outcomes = [], []
                for row in batch_rows:
                    if row.duration_seconds is not None and row.duration_seconds > longest:
                        outcomes.append("too_long")
                        continue
                    planned = self._plan_track(db, row, embedding_model, force)
                    if isinstance(planned, str):
                        outcomes.append(planned)
                    else:
                        todo.append(planned)
                return todo, outcomes, longest

            # librosa runs beside the decodes and the accelerator: at most the
            # profile's prefetch workers at once, as the features stage ran it
            # (2 on lite — a Pi's four cores also carry the torch threads)
            from hardware_profile import resolve as _hw
            librosa_slots = threading.BoundedSemaphore(_hw().prefetch_workers)

            def _load_one(item, longest):
                row, _src, _embed, describe = item
                local_path = settings.translate_to_local_path(row.file_path)
                try:
                    audio = decode_as_read(row, local_path, lambda: self._load_audio(
                        local_path, row.cue_start_seconds, row.cue_end_seconds,
                        row.duration_seconds, longest))
                except TrackTooLong as e:
                    logger.info(f"Sonic pass: {e} — left to the network")
                    return item, TrackTooLong, None
                except (ShortDecode, ChangedUnderDecode) as e:
                    return item, e, None
                features = None
                if audio is not None and describe:
                    try:
                        with librosa_slots:
                            features = analyzer.features_from_array(audio, self.sample_rate)
                    except Exception as e:
                        logger.error(f"librosa failed for {row.track_id}: {e}")
                return item, audio, features

            def _load_batch(todo, longest):
                """Decode a batch in the I/O pool: (loaded, failed count, too
                long count, the refused (row, why), when the decode began and
                ended)."""
                began = time.monotonic()
                loaded, refused = [], []
                failed = too_long = 0
                futures = [io_pool.submit(_load_one, item, longest) for item in todo]
                for future in as_completed(futures):
                    item, audio, features = future.result()
                    if audio is TrackTooLong:
                        too_long += 1
                    elif isinstance(audio, Exception):
                        refused.append((item[0], audio))
                    elif audio is not None:
                        loaded.append((item, audio, features))
                    else:
                        failed += 1
                        logger.warning(f"Skipping track {item[0].track_id}: audio load failed")
                return loaded, failed, too_long, refused, began, time.monotonic()

            # Split into batch slices — bounded by BOTH row count and total
            # audio duration: whole tracks are held decoded in RAM per batch,
            # and a batch of Schulze-length pieces would be gigabytes.
            from device import audio_batch_budget_seconds
            batch_budget_seconds = audio_batch_budget_seconds()
            batch_slices, cur, cur_seconds = [], [], 0.0
            for row in rows:
                dur = float(row.duration_seconds) if row.duration_seconds else analysis_pace.UNKNOWN_SECONDS
                if cur and (len(cur) >= self.batch_size
                            or cur_seconds + dur > batch_budget_seconds):
                    batch_slices.append(cur)
                    cur, cur_seconds = [], 0.0
                cur.append(row)
                cur_seconds += dur
            if cur:
                batch_slices.append(cur)
            num_batches = len(batch_slices)

            # Persistent I/O thread pool. Each worker is a whole ffmpeg
            # subprocess and librosa — 16 of them saturate a 24-core dev box
            # but thrash a 4-core laptop, so the size comes from the profile.
            io_pool = ThreadPoolExecutor(max_workers=_hw().io_workers)
            # Prefetch pool runs _load_batch in background
            prefetch_pool = ThreadPoolExecutor(max_workers=1)

            try:
                # Pre-submit first batch loading; its registrations commit
                # first, so the seam below holds no open write
                batch_began = time.monotonic()
                planned, outcomes, longest = _plan(batch_slices[0])
                db.commit()
                prefetch_future = prefetch_pool.submit(_load_batch, planned, longest)

                for batch_idx in tqdm(range(num_batches), desc="Sonic pass", unit="batch"):
                    # What the seam gave to another pass is no time of this one —
                    # but the batch's decode, under way beside it, is: the wait
                    # is left out less the part of it the decode overlapped
                    seam_began = time.monotonic()
                    if cancel_flag and cancel_flag():
                        logger.info("Sonic pass cancelled")
                        break
                    seam_ended = time.monotonic()

                    if max_duration_seconds:
                        elapsed = time.time() - start_time
                        if elapsed >= max_duration_seconds:
                            logger.info(f"Time limit reached ({elapsed:.1f}s), stopping gracefully")
                            break

                    (loaded, batch_failed, batch_too_long, batch_refused,
                     decode_began, decode_ended) = prefetch_future.result()
                    overlap = max(0.0, min(seam_ended, decode_ended) - max(seam_began, decode_began))
                    batch_began += (seam_ended - seam_began) - overlap
                    stats["processed"] += len(batch_slices[batch_idx])
                    stats["failed"] += batch_failed
                    stats["too_long"] += batch_too_long
                    for refused_row, why in batch_refused:
                        refuse(db, refused_row, why)
                    stats["refused"] += len(batch_refused)
                    for outcome in outcomes:
                        stats[outcome] += 1
                    # The batch's pace, when every track it worked on runs
                    # the whole pass: a batch of features alone, or of
                    # segments alone, is not the pass's pace, nor one whose
                    # decode failed (time spent on no audio)
                    whole = not (batch_failed or batch_too_long or batch_refused) and all(
                        embed and describe for _r, _s, embed, describe in planned)

                    # Register the NEXT batch and start loading it (runs while
                    # the accelerator works)
                    if batch_idx + 1 < num_batches:
                        planned, outcomes, longest = _plan(batch_slices[batch_idx + 1])
                        prefetch_future = prefetch_pool.submit(_load_batch, planned, longest)

                    analysed, wrote_any = 0.0, False
                    for item, audio, features in loaded:
                        outcome, made, not_unit = self._sonic_track(
                            db, item, audio, features, embedding_model, save_features)
                        stats["not_unit"] += not_unit
                        stats[outcome] += 1
                        stats["embeddings"] += made.count("embeddings")
                        stats["features"] += made.count("features")
                        wrote_any = wrote_any or bool(made)
                        whole = whole and outcome == "success" and len(made) == 2
                        analysed += len(audio) / self.sample_rate

                    # A batch's wall time runs from the end of the one before:
                    # the batches overlap, and their walls add up to the pass's
                    if whole:
                        analysis_pace.record(db, analysis_pace.SONIC,
                                             time.monotonic() - batch_began, analysed)
                    db.commit()
                    batch_began = time.monotonic()
                    if wrote_any:
                        notary.wake("sonic pass")
                    if on_batch:
                        on_batch(stats["processed"], total)
                    # Hand the allocator's free blocks back each batch — else
                    # the caching allocator holds its peak for the whole run
                    # (~28 GB on MPS unified memory; heavy swap on a 16 GB Mac).
                    from device import empty_cache
                    empty_cache(self.device)
            finally:
                io_pool.shutdown(wait=True, cancel_futures=True)
                prefetch_pool.shutdown(wait=True, cancel_futures=True)

            logger.info(
                f"Sonic pass complete: {stats['success']} success, {stats['failed']} failed, "
                f"{stats['embeddings']} embeddings, {stats['features']} features"
            )
            if stats["not_unit"]:
                logger.error(
                    f"Sonic pass: {stats['not_unit']} track(s) had their new embedding refused "
                    "because this node's own analysis made vectors that are not unit vectors — a "
                    "fault of its model or code, not of the files (each is named above)")

        return stats

    def _plan_track(self, db: Session, row, model, force: bool):
        """A queued track registered before anything is decoded or computed:
        (row, source id, make the segments, make the features), or the
        outcome when there is nothing to make — "failed" with no fingerprint
        (no address: nothing is saved, and the queue asks again next run, as
        for a file that fails to decode), "skipped" when the file's material
        group already holds both records at a rank no lower."""
        try:
            with db.begin_nested():
                src_id = provenance.get_or_create_local(
                    db, row.track_id, row.media_file_id, row.file_path,
                    row.sample_rate, row.bit_depth, row.is_lossless,
                    row.duration_seconds, row.cue_start_seconds, row.cue_end_seconds)
                if src_id is None:
                    logger.warning(f"Skipping track {row.track_id}: no content address (fingerprint)")
                    return "failed"
                embed = force or (row.embedding_pending and not provenance.held_in_group(
                    db, src_id, "embeddings", model_id=model.id))
                describe = force or (row.features_pending and not provenance.held_in_group(
                    db, src_id, "audio_features"))
        except Exception as e:
            logger.error(f"Registration failed for {row.track_id}: {e}", exc_info=True)
            return "failed"
        if not (embed or describe):
            return "skipped"
        return row, src_id, embed, describe

    def _sonic_track(self, db: Session, item, audio, features, model, save_features):
        """One decoded track of the sonic pass, its records made and saved
        under a savepoint of its own. Returns the outcome (success: a record
        saved; failed; skipped: a better copy's records stand; refused: the
        file was read again since its decode, and that version is pending), the kinds
        saved, and whether its embedding was refused as not unit vectors
        (NotUnitVectors) — the embedding alone: the features are saved all
        the same, as when the forward fails."""
        row, src_id, embed, describe = item
        made, failed, not_unit = [], False, False
        try:
            with db.begin_nested():
                computed = self._compute_segments(audio) if embed else None
                if not still_as_read(db, row):
                    return "refused", [], False
                if embed:
                    if computed is None:
                        failed = True
                        logger.error(f"Failed embedding for track {row.track_id}")
                    else:
                        try:
                            if self._persist_analysis(db, row.track_id, model, *computed, src_id):
                                made.append("embeddings")
                        except NotUnitVectors as e:
                            failed = not_unit = True
                            logger.error(f"Sonic pass: no embedding saved — {e}")
                if describe:
                    if features is None:
                        failed = True
                    elif save_features(db, row.track_id, features, src_id):
                        made.append("features")
        except Exception as e:
            logger.error(f"Sonic pass failed for {row.track_id}: {e}")
            return "failed", [], not_unit
        if made:
            return "success", made, not_unit
        return ("failed" if failed else "skipped"), made, not_unit


def sonic_pass(
    limit: Optional[int] = None, batch_size: Optional[int] = None, order_by_date: bool = False,
    max_duration_seconds: Optional[int] = None, track_ids: Optional[list] = None,
    worker_id: Optional[int] = None, worker_count: Optional[int] = None,
    force: bool = False,
) -> Dict[str, int]:
    """The sonic pass (AudioEmbeddingGenerator.sonic_pass) with the default
    model; `batch_size` overrides the configured one."""
    generator = AudioEmbeddingGenerator(batch_size=batch_size)
    return generator.sonic_pass(limit=limit, order_by_date=order_by_date,
                                max_duration_seconds=max_duration_seconds, track_ids=track_ids,
                                worker_id=worker_id, worker_count=worker_count, force=force)
