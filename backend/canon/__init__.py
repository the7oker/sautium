"""Layer 2 — canonicalization.

One home for turning raw artists/albums (owned and out-of-catalog phantom) into
canonical MusicBrainz identities, organized by purpose:

  match      — general candidate-finding (name → MB MBIDs); shared by all algos
  identity   — deterministic UUID (re)writes / merges (the base primitives)
  split      — collaboration splitting (feat./vs./…)
  content    — content-overlap canon for OWNED artists (track verification)
  phantom    — genre-overlap canon for TRACKLESS phantom artists
  genres     — MB album-genre materialization (folded into canon)
  migrations — one-shot historical sweeps + Pass-1 normalization orchestrator
  pipeline   — distill_uncanonized(): the orchestrator entry point

Layer 1 (lastfm enrichment) feeds phantoms in; Layer 3 (discography) fills
missing albums for whatever this layer canonizes.
"""
import threading
from contextlib import contextmanager

# Canon-mutation concurrency. The algorithmic tiers (canonicalize_pending /
# distill_uncanonized) and the AI tier (ai_canonize) both rewrite the SAME
# uncanonized residue via recanonicalize_artist (DELETE source + cascade UUIDs +
# merge); run concurrently they deadlock or half-process an artist. Algorithmic has
# PRIORITY (it's fast + free; the AI tier is slow + paid), so it PREEMPTS the AI:
#   • algo_canon()   — algorithmic call sites run inside this. Flags intent (so the
#                      AI worker bails between batches) then holds _mutate_lock; it
#                      waits at most one AI batch's brief DB-write phase, never an
#                      LLM call, so it effectively goes first.
#   • ai_mutations() — the AI worker holds this ONLY around each batch's DB writes;
#                      the (slow) LLM call stays lock-free so algorithmic never
#                      waits on the network.
#   • ai_should_yield() — the AI worker checks this between batches and aborts the
#                      run when algorithmic wants to go.
# Used at the THREAD CALL SITES (scan, dump post-load, background loop) + inside the
# AI batch loop — never nested inside a held canon function, so no re-entrancy.
_mutate_lock = threading.Lock()
_algo_wants = 0                       # how many algorithmic runs are waiting/active
_algo_wants_lock = threading.Lock()


@contextmanager
def algo_canon():
    """Algorithmic canon, with priority over the AI tier (preempts it)."""
    global _algo_wants
    with _algo_wants_lock:
        _algo_wants += 1
    try:
        with _mutate_lock:
            yield
    finally:
        with _algo_wants_lock:
            _algo_wants -= 1


@contextmanager
def ai_mutations():
    """Hold the mutation lock around one AI batch's DB writes (LLM call stays out)."""
    with _mutate_lock:
        yield


def ai_should_yield() -> bool:
    """True when an algorithmic canon run wants the lock — the AI worker aborts."""
    with _algo_wants_lock:
        return _algo_wants > 0


from canon import (
    match, identity, split, genres, phantom, content, migrations, pipeline,
)

from canon.pipeline import distill_uncanonized
from canon.content import (
    resolve_artist, apply_artist, canonicalize_trackonly, canonicalize_pending,
    merge_collisions, apply_editions, rename_to_canonical,
)
from canon.phantom import canonize_phantom_similars
from canon.split import detect_compound_type, normalize_compound_artist
from canon.identity import (
    recanonicalize_artist, recanonicalize_album, recanonicalize_album_variants,
)
from canon.genres import refresh_album_mb_genres, materialize_album_genres
from canon.migrations import normalize_artists

__all__ = [
    "algo_canon", "ai_mutations", "ai_should_yield",
    "distill_uncanonized",
    "resolve_artist", "apply_artist", "canonicalize_trackonly",
    "canonicalize_pending", "merge_collisions", "apply_editions",
    "rename_to_canonical",
    "canonize_phantom_similars",
    "detect_compound_type", "normalize_compound_artist",
    "recanonicalize_artist", "recanonicalize_album", "recanonicalize_album_variants",
    "refresh_album_mb_genres", "materialize_album_genres",
    "normalize_artists",
]
