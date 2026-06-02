"""Artist alias resolution — the persistence layer for canonicalization.

A 1:1 map from a dirty/variant artist name to its canonical artist. Every
ingestion chokepoint (scanner, Last.fm similar, P2P import) resolves names
through here first, so once normalization (or the MB background pass) has
decided that "H. Mancini" is "Henry Mancini", a later rescan of a new album
credited "H. Mancini" converges on the canonical artist instead of
re-creating a fragment.

Identity stays name-derived (`artist_uuid(canonical_name)`) — available
immediately and syncable without waiting for the slow MB phase. Aliases are
the convergence layer for the messy minority and are themselves shareable
over P2P. Collaboration 1:many decomposition is NOT here — that lives in
`artist_members`; this map is strictly 1:1 rename/dedup.
"""

import uuid as _uuid
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from uuid_utils import normalize


def resolve_alias(db: Session, raw_name: str) -> Optional[_uuid.UUID]:
    """Return the canonical artist_id a name is aliased to, or None.

    None means "no alias" — the caller falls back to artist_uuid(raw_name),
    i.e. the name is its own canonical (the common case).
    """
    key = normalize(raw_name)
    if not key:
        return None
    row = db.execute(
        text("SELECT artist_id FROM artist_aliases WHERE alias_normalized = :k"),
        {"k": key},
    ).first()
    return row[0] if row else None


def record_alias(
    db: Session,
    alias_name: str,
    canonical_id,
    source: str,
    mbid: Optional[str] = None,
    confidence: Optional[float] = None,
) -> None:
    """Persist a variant name → canonical artist mapping so future ingests
    of that name resolve directly.

    No-op when the variant's normalized form already equals the canonical
    artist's — there's no fragmentation to bridge, and a self-alias would
    only shadow the artist_uuid fast path.
    """
    key = normalize(alias_name)
    if not key:
        return
    canonical_id = str(canonical_id)
    if key == normalize(_canonical_name(db, canonical_id) or ""):
        return
    db.execute(text("""
        INSERT INTO artist_aliases
            (alias_normalized, artist_id, alias_name, source, mbid, confidence)
        VALUES (:k, :aid, :name, :src, :mbid, :conf)
        ON CONFLICT (alias_normalized) DO UPDATE SET
            artist_id  = EXCLUDED.artist_id,
            alias_name = EXCLUDED.alias_name,
            source     = EXCLUDED.source,
            mbid       = COALESCE(EXCLUDED.mbid, artist_aliases.mbid),
            confidence = EXCLUDED.confidence,
            updated_at = now()
    """), {"k": key, "aid": canonical_id, "name": alias_name,
           "src": source, "mbid": mbid, "conf": confidence})


def _canonical_name(db: Session, artist_id: str) -> Optional[str]:
    row = db.execute(
        text("SELECT name FROM artists WHERE id = :id"), {"id": artist_id},
    ).first()
    return row[0] if row else None
