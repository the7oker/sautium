"""
Deterministic UUID generation for canonical entities.

Uses UUID v5 (SHA-1 based, deterministic) so the same artist/track/album
produces the same UUID on any user's system. This enables future data
exchange and deduplication across installations.
"""

import re
import unicodedata
import uuid

# Fixed namespace for Sautium — uuid5(NAMESPACE_DNS, "sautium"). Never change!
NAMESPACE = uuid.UUID('adc1ec0b-2c81-5e26-9938-a369c6f7a5e1')


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


def gear_brand_uuid(name: str) -> uuid.UUID:
    """Deterministic UUID for an audio gear brand (Sennheiser, Holo Audio…)."""
    return uuid.uuid5(NAMESPACE, f"gear_brand:{normalize(name)}")


def gear_model_uuid(brand_name: str, model: str, category: str) -> uuid.UUID:
    """Deterministic UUID for an audio gear model.

    Keyed by (brand_name, model, category) so two nodes adding the
    same product collapse to the same id — same canonicalization
    pattern as artists/genres."""
    return uuid.uuid5(NAMESPACE,
                      f"gear_model:{normalize(category)}:{normalize(brand_name)}:{normalize(model)}")


def gear_spec_attribute_uuid(key: str) -> uuid.UUID:
    """Deterministic UUID for an EAV spec attribute (`impedance_ohm`…)."""
    return uuid.uuid5(NAMESPACE, f"gear_spec_attribute:{normalize(key)}")


def gear_technology_uuid(key: str) -> uuid.UUID:
    """Deterministic UUID for a proprietary technology entry."""
    return uuid.uuid5(NAMESPACE, f"gear_technology:{normalize(key)}")


def gear_caveat_uuid(gear_model_id: str, text: str) -> uuid.UUID:
    """Deterministic UUID for a measured caveat — same finding on two
    nodes collapses to one row on P2P merge."""
    return uuid.uuid5(NAMESPACE, f"gear_caveat:{gear_model_id}:{normalize(text)}")


# Lossless audio formats
LOSSLESS_FORMATS = {'flac', 'ape', 'alac', 'wav', 'aiff', 'wv', 'tta', 'dsf', 'dff'}


def is_lossless(file_format: str) -> bool:
    """Check if a file format is lossless."""
    return file_format.lower().strip('.') in LOSSLESS_FORMATS
