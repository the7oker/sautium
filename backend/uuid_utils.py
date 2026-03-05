"""
Deterministic UUID generation for canonical entities.

Uses UUID v5 (SHA-1 based, deterministic) so the same artist/track/album
produces the same UUID on any user's system. This enables future data
exchange and deduplication across installations.
"""

import re
import unicodedata
import uuid

# Fixed namespace for this project — never change this value!
NAMESPACE = uuid.UUID('5ba7a9d0-1f8c-4c3d-9e7a-2b4f6c8d0e1f')


def normalize(text: str) -> str:
    """Normalize text for deterministic UUID generation.

    - NFC unicode normalization
    - strip + lowercase
    - collapse whitespace
    """
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFC', text.strip().lower()))


def artist_uuid(name: str) -> uuid.UUID:
    """Generate deterministic UUID for an artist."""
    return uuid.uuid5(NAMESPACE, f"artist:{normalize(name)}")


def track_uuid(title: str, artist_name: str) -> uuid.UUID:
    """Generate deterministic UUID for a track (title + primary artist).

    Note: internal seed uses 'song:' prefix for backward compatibility
    with UUIDs generated during initial migration.
    """
    return uuid.uuid5(NAMESPACE, f"song:{normalize(artist_name)}:{normalize(title)}")


def album_uuid(title: str, artist_name: str) -> uuid.UUID:
    """Generate deterministic UUID for an album (title + primary artist)."""
    return uuid.uuid5(NAMESPACE, f"album:{normalize(artist_name)}:{normalize(title)}")


def genre_uuid(name: str) -> uuid.UUID:
    """Generate deterministic UUID for a genre."""
    return uuid.uuid5(NAMESPACE, f"genre:{normalize(name)}")


def tag_uuid(name: str) -> uuid.UUID:
    """Generate deterministic UUID for a tag."""
    return uuid.uuid5(NAMESPACE, f"tag:{normalize(name)}")


def embedding_model_uuid(name: str) -> uuid.UUID:
    """Generate deterministic UUID for an embedding model."""
    return uuid.uuid5(NAMESPACE, f"embedding_model:{normalize(name)}")


# Lossless audio formats
LOSSLESS_FORMATS = {'flac', 'ape', 'alac', 'wav', 'aiff', 'wv', 'tta', 'dsf', 'dff'}


def is_lossless(file_format: str) -> bool:
    """Check if a file format is lossless."""
    return file_format.lower().strip('.') in LOSSLESS_FORMATS
