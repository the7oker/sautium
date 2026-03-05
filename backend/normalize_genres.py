"""
Genre normalization script.
Splits compound genre names into individual genres and creates proper many-to-many relationships.
"""

import logging
import re
from typing import List, Set
from sqlalchemy import text
from sqlalchemy.orm import Session

from database import get_db_context
from models import Genre, TrackGenre
from uuid_utils import genre_uuid

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def parse_genre_string(genre_name: str) -> List[str]:
    """
    Parse a compound genre string into individual genre names.

    Handles delimiters: '/', ',', '&', '+', and variations with spaces

    Examples:
        "Progressive Electronic/Berlin School" -> ["Progressive Electronic", "Berlin School"]
        "Electronic, Ambient" -> ["Electronic", "Ambient"]
        "Krautrock/Electro/Experimental/Ambient" -> ["Krautrock", "Electro", "Experimental", "Ambient"]
    """
    # Replace common delimiters with a standard one
    normalized = genre_name

    # Replace '/', ' & ', ' + ' with comma
    normalized = re.sub(r'\s*/\s*', ', ', normalized)
    normalized = re.sub(r'\s*&\s*', ', ', normalized)
    normalized = re.sub(r'\s*\+\s*', ', ', normalized)

    # Split by comma
    parts = [part.strip() for part in normalized.split(',')]

    # Remove empty strings and deduplicate while preserving order
    seen = set()
    result = []
    for part in parts:
        if part and part not in seen:
            seen.add(part)
            result.append(part)

    return result


def normalize_genre_name(genre: str) -> str:
    """
    Normalize genre name for consistency.

    - Titlecase for standard genres
    - Special handling for abbreviations (IDM, etc.)
    - Remove extra whitespace
    """
    genre = genre.strip()

    # Special cases
    abbreviations = {'idm': 'IDM', 'edm': 'EDM', 'rnb': 'R&B', 'hiphop': 'Hip-Hop'}

    lower = genre.lower()
    if lower in abbreviations:
        return abbreviations[lower]

    # Titlecase for most genres
    return genre.title()


def get_or_create_genre(db: Session, genre_name: str) -> Genre:
    """Get existing genre or create new one (deterministic UUID PK)."""
    # Normalize name
    normalized_name = normalize_genre_name(genre_name)

    # Deterministic UUID lookup
    gid = genre_uuid(normalized_name)
    genre = db.query(Genre).filter(Genre.id == gid).first()

    if genre:
        logger.debug(f"Found existing genre: {normalized_name}")
        return genre

    # Create new
    genre = Genre(id=gid, name=normalized_name)
    db.add(genre)
    db.flush()
    logger.info(f"Created new genre: {normalized_name} (ID: {genre.id})")

    return genre


def normalize_genres(db: Session, dry_run: bool = False) -> dict:
    """
    Normalize all compound genres in the database.

    Process:
    1. Find all compound genre names
    2. Split them into individual genres
    3. Create individual genres if they don't exist
    4. Update track_genres relationships
    5. Delete compound genres (if not dry_run)

    Args:
        db: Database session
        dry_run: If True, only show what would be done without making changes

    Returns:
        Statistics dict
    """
    stats = {
        'compound_genres_found': 0,
        'compound_genres_processed': 0,
        'new_genres_created': 0,
        'track_relationships_updated': 0,
        'compound_genres_deleted': 0,
    }

    # Find all genres
    all_genres = db.query(Genre).all()

    # Identify compound genres (contain delimiters)
    compound_genres = []
    for genre in all_genres:
        parsed = parse_genre_string(genre.name)
        if len(parsed) > 1:
            compound_genres.append((genre, parsed))

    stats['compound_genres_found'] = len(compound_genres)

    if not compound_genres:
        logger.info("No compound genres found. Database is already normalized.")
        return stats

    logger.info(f"Found {len(compound_genres)} compound genres to normalize")

    for compound_genre, individual_names in compound_genres:
        logger.info(f"\nProcessing: '{compound_genre.name}' -> {individual_names}")

        if dry_run:
            logger.info(f"  [DRY RUN] Would split into: {', '.join(individual_names)}")
            stats['compound_genres_processed'] += 1
            continue

        # Get or create individual genres
        individual_genres = []
        for name in individual_names:
            try:
                genre = get_or_create_genre(db, name)
                individual_genres.append(genre)
            except Exception as e:
                logger.error(f"Error creating genre '{name}': {e}")
                continue

        if not individual_genres:
            logger.warning(f"No individual genres created for '{compound_genre.name}'")
            continue

        # Find all tracks with this compound genre
        track_genres = db.query(TrackGenre).filter(
            TrackGenre.genre_id == compound_genre.id
        ).all()

        logger.info(f"  Found {len(track_genres)} tracks with genre '{compound_genre.name}'")

        # Update relationships
        for tg in track_genres:
            track_id = tg.track_id

            # Add relationships for each individual genre
            for individual_genre in individual_genres:
                # Check if relationship already exists
                existing = db.query(TrackGenre).filter(
                    TrackGenre.track_id == track_id,
                    TrackGenre.genre_id == individual_genre.id
                ).first()

                if not existing:
                    new_tg = TrackGenre(track_id=tg.track_id, genre_id=individual_genre.id)
                    db.add(new_tg)
                    stats['track_relationships_updated'] += 1

            # Delete old compound relationship
            db.delete(tg)

        # Delete compound genre
        db.delete(compound_genre)
        stats['compound_genres_deleted'] += 1
        stats['compound_genres_processed'] += 1

        logger.info(f"  ✓ Normalized '{compound_genre.name}' into {len(individual_genres)} genres")

    if not dry_run:
        db.commit()
        logger.info("\n✅ Genre normalization complete!")
    else:
        logger.info("\n[DRY RUN] No changes made to database")

    return stats


def show_genre_statistics(db: Session):
    """Show statistics about genres and their usage."""
    result = db.execute(text("""
        SELECT
            g.id,
            g.name,
            COUNT(tg.track_id) as track_count
        FROM genres g
        LEFT JOIN track_genres tg ON g.id = tg.genre_id
        GROUP BY g.id, g.name
        ORDER BY track_count DESC, g.name
    """)).fetchall()

    logger.info("\n📊 Genre Statistics:")
    logger.info(f"{'ID':<5} {'Genre':<40} {'Tracks':<10}")
    logger.info("-" * 60)

    total_tracks = 0
    for row in result:
        logger.info(f"{row.id:<5} {row.name:<40} {row.track_count:<10}")
        total_tracks += row.track_count

    logger.info("-" * 60)
    logger.info(f"Total: {len(result)} genres, {total_tracks} track-genre relationships")


if __name__ == "__main__":
    import sys

    dry_run = '--dry-run' in sys.argv

    with get_db_context() as db:
        logger.info("=== Genre Normalization Script ===\n")

        # Show current state
        logger.info("Current state:")
        show_genre_statistics(db)

        # Normalize
        logger.info("\n" + "="*60)
        stats = normalize_genres(db, dry_run=dry_run)

        # Show results
        logger.info("\n" + "="*60)
        logger.info("Statistics:")
        for key, value in stats.items():
            logger.info(f"  {key}: {value}")

        if not dry_run:
            logger.info("\nAfter normalization:")
            show_genre_statistics(db)
