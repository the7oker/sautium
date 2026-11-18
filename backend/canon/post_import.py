"""The canon that follows every import of files — a scan of the music folder
or a sync of the HQPlayer library. Factored out of the scan worker
(2026-09-27) so both importers run the same passes in the same order:
artist normalization pass 1 (safe patterns), the MB canon over artists
whose content is newer than their last canon, the AI judgment tier over
what the deterministic canon left (scoped to this run's files), the re-link
of returned files to their analysis, then the
notary and the scrobble placer are woken — new tracks are signable state
and may be exactly what imported listens wait for."""

import logging
from datetime import datetime
from typing import Any, Dict

logger = logging.getLogger(__name__)


def run(state: Dict[str, Any], result: Dict[str, Any], since: datetime,
        take_arriving: bool = True) -> None:
    """`state` is the worker's live state (progress, cancel_requested),
    `result` its result dict (receives `mb_canon`), `since` the run's start
    — the AI tier looks only at files imported after it.

    `take_arriving` False is a scan a folder watcher started: an album
    still arriving by its own files' words (canon.content.arriving_albums)
    is left to a later pass, and the AI tier — which judges whole albums —
    waits for an explicit scan or the start, which takes every file the
    deferred runs imported (`canon.ai_pending_since`)."""
    from routers.settings import mb_load_active, notify_library_subscribers, start_aicanon_job

    state["progress"] = "Normalizing artists..."
    try:
        from canon.migrations import normalize_artists as do_normalize
        from database import get_db_context
        with get_db_context() as db:
            norm_stats = do_normalize(db, pass1=True)
            if norm_stats.get("pass1", {}).get("split", 0) > 0:
                logger.info(f"Post-import normalization: {norm_stats}")
    except Exception as e:
        logger.error(f"Post-import normalization failed: {e}")
    if state["cancel_requested"]:
        return
    # MB canonicalization (local dump): resolve RG/MBID + collapse editions
    # for artists with new content since their last canon. No-op without the
    # dump. Runs HERE — after the import, before sync (peers get canonical
    # content) and before enrich (dedup + correction cut wasted fetches).
    # Renames are local + reversible (album_variants.raw_title), so this
    # auto-applies without a review gate.
    if mb_load_active():
        # A dump op is downloading/loading concurrently — running canon now
        # would read a stale/half-loaded dump and watermark these artists
        # out of the dump's own fresh post-load canon. Defer; that pass
        # covers them.
        logger.info("Post-import canon deferred — MB dump operation in progress")
    else:
        state["progress"] = "Canonicalizing (MusicBrainz)..."
        notify_library_subscribers()
        try:
            from canon.content import canonicalize_pending
            from canon import algo_canon
            canon_stats: Dict[str, Any] = {}
            with algo_canon() as _ok:   # priority over AI; holds the dump lock
                if _ok:
                    canon_stats = canonicalize_pending(take_arriving=take_arriving)
                    # Catch the NULL-rg owned-album residue the main matcher
                    # rejects on its bidirectional size gate — first by
                    # edition-stripped name, then the content-only studio
                    # uniques the name pass can't reach (cross-script /
                    # reworded titles). Free, algorithmic, event-driven.
                    from canon.content import distill_album_residue, distill_album_coverage
                    bound = distill_album_residue(take_arriving=take_arriving).get("bound", 0)
                    bound += distill_album_coverage(take_arriving=take_arriving).get("bound", 0)
                    if bound:
                        canon_stats["album_residue_bound"] = bound
            if canon_stats.get("artists") or canon_stats.get("album_residue_bound"):
                result["mb_canon"] = canon_stats
                logger.info(f"Post-import MB canon: {canon_stats}")
            # AI judgment tier on what the deterministic canon left — scoped
            # to the new files since the last run that took them, async + gated.
            ai_since = _ai_since(since, take_arriving)
            if ai_since is not None and start_aicanon_job(since=ai_since):
                logger.info("Post-import: AI canonization started (new files)")
        except Exception as e:
            logger.error(f"Post-import MB canonicalization failed: {e}")
    if not state["cancel_requested"]:
        relink(state, result)
    # The import's analysis and its canon are signable state: wake the
    # sealing owner (full — canon may have anchored albums whose tracks were
    # signed long ago). New files can be the tracks imported scrobbles wait for.
    if not state["cancel_requested"]:
        import notary
        notary.wake("scan", full=True)
        from canon import scrobbles
        scrobbles.wake()


def _ai_since(since: datetime, take_arriving: bool):
    """The AI tier's `since`: a watcher's run keeps the earliest start of the
    runs it defers (user_settings `canon.ai_pending_since`) and starts
    nothing; a run that takes arriving albums starts from that earliest
    start, so no file a deferred run imported escapes the tier."""
    import json
    from db_pool import db_execute, db_query_one
    row = db_query_one("SELECT value FROM user_settings WHERE key = 'canon.ai_pending_since'")
    pending = datetime.fromisoformat(row["value"]) if row else None
    if not take_arriving:
        if pending is None or since < pending:
            db_execute("""INSERT INTO user_settings (key, value) VALUES ('canon.ai_pending_since', %s::jsonb)
                          ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value,
                                                          updated_at = CURRENT_TIMESTAMP""",
                       (json.dumps(since.isoformat()),))
        return None
    if pending is not None:
        db_execute("DELETE FROM user_settings WHERE key = 'canon.ai_pending_since'")
        return min(pending, since)
    return since


def relink(state: Dict[str, Any], result: Dict[str, Any]) -> None:
    """A returned file's analysis follows it by its fingerprint — after the
    canon, which moves a returned file's raw-tag track onto the track that
    kept the analysis (a split credit, an MB name). The look is the state's,
    not this run's files, so what a cancel, a restart or a missing fpcalc
    left is found again by the next import."""
    from provenance import relink_returned_files
    from routers.settings import notify_library_subscribers

    def progress(done, total):
        state["progress"] = f"Linking returned files to their analysis: {done}/{total}"
        notify_library_subscribers()

    try:
        relinked = relink_returned_files(
            cancel_check=lambda: state["cancel_requested"], progress_cb=progress)
    except Exception as e:
        logger.error(f"Post-import re-link failed: {e}", exc_info=True)
        return
    if relinked:
        result["relinked"] = relinked
