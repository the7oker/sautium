"""Canon orchestrator (Layer 2 entry point).

`distill_uncanonized` progressively canonicalizes the uncanonized residue —
OWNED artists via content overlap (`content.canonicalize_pending`) and TRACKLESS
phantoms via genre overlap (`phantom.canonize_phantom_similars`) — in one pass.

It is a behavior-preserving COMPOSITION of the existing per-purpose runners, not
a reimplementation: each runner keeps its own watermark/idempotence. Genre
materialization is wired at its two natural seams (discography mints phantom
albums → `genres.materialize_album_genres`; dump reload → `refresh_album_mb_genres`),
so the pass itself does not re-run a genre sweep.
"""
import logging

logger = logging.getLogger(__name__)


def distill_uncanonized(limit: int = None, dry_run: bool = False) -> dict:
    """Run the canon pipeline over the uncanonized set. Returns per-layer stats.

    `limit` caps each layer's batch. `dry_run` previews the phantom layer only
    (content canon auto-applies — MB-optional + idempotent — and has no dry-run)."""
    from canon.content import canonicalize_pending
    from canon.phantom import canonize_phantom_similars

    out: dict = {}
    if dry_run:
        out["content"] = {"skipped": "dry_run (content canon has no preview mode)"}
        out["phantom"] = canonize_phantom_similars(limit=limit, dry_run=True)
        return out

    out["content"] = canonicalize_pending(limit=limit)
    out["phantom"] = canonize_phantom_similars(limit=limit, dry_run=False)
    return out
