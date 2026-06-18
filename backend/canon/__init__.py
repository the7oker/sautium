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
