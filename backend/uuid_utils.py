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

# Version of the identity rule (normalize / normalize_key below). Bump it with
# every change to either: backend/db_migrate.py re-normalizes a node's
# existing rows at startup when its recorded rule is older (marker row
# identity_rule_v{N} in _schema_migrations), so no node ever mints on one
# rule beside rows minted on another.
IDENTITY_RULE = 2

# Apostrophe-like marks are DROPPED, not spaced: "don’t" / "don't" / "dont"
# are one title. Every other character that is neither a letter, a digit, a
# combining mark nor whitespace becomes a space and runs collapse. Marks stay
# word characters because str.isalnum() rejects them and a Devanagari or
# Arabic title would otherwise shatter at every vowel sign.
_APOSTROPHES = re.compile("['’‘‚‛`´ʼʹ]")
_HAS_NON_WORD = re.compile(r"[^\w\s]|_")
_WS = re.compile(r"\s+")


def normalize_key(text: str) -> str:
    """Identifier keys: NFC, lower, trimmed, single spaces — nothing else.

    For strings whose punctuation is structure, not typography: a model
    name (`laion/clap-htsat-unfused`), a spec attribute key (`impedance_ohm`),
    a registry source. This is also the pre-2026-08-25 `normalize`, the rule
    every human-name entity was minted with before v2 below.
    """
    return _WS.sub(' ', unicodedata.normalize('NFC', text.strip().lower()))


def _is_word_char(ch: str) -> bool:
    return ch.isalnum() or ch.isspace() or unicodedata.category(ch).startswith('M')


def normalize(text: str) -> str:
    """Human names (titles, artists, albums, genres, tags, gear) → identity key.

    v2 (2026-08-25): on top of normalize_key, apostrophe-like marks are
    dropped and every other non-word character becomes a space, so
    typography stops forking identities — `Hello Dolly!` / `Hello Dolly`,
    `See - Line Woman` / `See-Line Woman`, `Don’t` / `Don't` / `Dont` are one
    UUID each. Measured on the master before the rule: 197 of the 1701
    same-recording track pairs differed by nothing else. A name that folds
    to nothing (`!!!`, `†††`, `¥$`) keeps its key form — those are distinct
    artists, not typography.

    Changing this function is an identity migration: every node must rewrite
    its UUIDs under the same rule at once (`canon.migrations --renormalize`)
    — a scan on one rule next to rows minted on the other forks every entity.
    """
    base = normalize_key(text)
    if not _HAS_NON_WORD.search(base):
        return base
    folded = _APOSTROPHES.sub('', base)
    folded = ''.join(ch if _is_word_char(ch) else ' ' for ch in folded)
    folded = _WS.sub(' ', folded).strip()
    return folded or base


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
    """Deterministic UUID for an embedding model — a key, not a name."""
    return uuid.uuid5(NAMESPACE, f"embedding_model:{normalize_key(name)}")


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
    """Deterministic UUID for an EAV spec attribute (`impedance_ohm`…) — a key."""
    return uuid.uuid5(NAMESPACE, f"gear_spec_attribute:{normalize_key(key)}")


def gear_technology_uuid(key: str) -> uuid.UUID:
    """Deterministic UUID for a proprietary technology entry — a key."""
    return uuid.uuid5(NAMESPACE, f"gear_technology:{normalize_key(key)}")


def gear_caveat_uuid(gear_model_id: str, text: str) -> uuid.UUID:
    """Deterministic UUID for a measured caveat — same finding on two
    nodes collapses to one row on P2P merge."""
    return uuid.uuid5(NAMESPACE, f"gear_caveat:{gear_model_id}:{normalize(text)}")


def gear_pair_uuid(model_a: str, model_b: str) -> uuid.UUID:
    """Deterministic UUID for a pair-synergy note. Order-insensitive:
    callers store the pair canonically (a < b), the id matches either way."""
    a, b = sorted((str(model_a), str(model_b)))
    return uuid.uuid5(NAMESPACE, f"gear_pair:{a}:{b}")


# Lossless audio formats
LOSSLESS_FORMATS = {'flac', 'ape', 'alac', 'wav', 'aiff', 'wv', 'tta', 'dsf', 'dff'}


def is_lossless(file_format: str) -> bool:
    """Check if a file format is lossless."""
    return file_format.lower().strip('.') in LOSSLESS_FORMATS
