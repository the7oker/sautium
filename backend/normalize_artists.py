"""
Artist normalization script.
Splits compound artist names into individual artists and creates proper many-to-many relationships.
"""

import logging
import re
from typing import List, Tuple, Optional
from sqlalchemy import text
from sqlalchemy.orm import Session

from database import get_db_context
from models import Artist, TrackArtist

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def is_compound_artist(artist_name: str) -> bool:
    """
    Check if an artist name is potentially a compound/collaboration.

    Patterns:
    - "Artist A & Artist B"
    - "Artist A feat. Artist B"
    - "Artist A featuring Artist B"
    - "Artist A, Artist B"
    - "Artist A and Artist B"
    - "Artist A with Artist B"
    - "Artist A vs Artist B"
    - "Artist A / Artist B"
    """
    if not artist_name:
        return False

    # Patterns that indicate compound artists
    patterns = [
        r'\s+&\s+',           # " & "
        r'\s+feat\.?\s+',     # " feat. " or " feat "
        r'\s+featuring\s+',   # " featuring "
        r'\s+ft\.?\s+',       # " ft. " or " ft "
        r'\s+and\s+',         # " and " (but not at start/end)
        r'\s+with\s+',        # " with "
        r'\s+vs\.?\s+',       # " vs. " or " vs "
        r'\s+/\s+',           # " / "
        r',\s+',              # ", "
    ]

    for pattern in patterns:
        if re.search(pattern, artist_name, re.IGNORECASE):
            return True

    return False


def parse_compound_artist(artist_name: str) -> List[str]:
    """
    Parse a compound artist name into individual artist names.

    Examples:
        "Beth Hart & Joe Bonamassa" -> ["Beth Hart", "Joe Bonamassa"]
        "Klaus Schulze feat. Lisa Gerrard" -> ["Klaus Schulze", "Lisa Gerrard"]
        "Artist A, Artist B and Artist C" -> ["Artist A", "Artist B", "Artist C"]
    """
    # Replace all separator patterns with a standard delimiter
    normalized = artist_name

    # Replace patterns with ||
    separators = [
        (r'\s+&\s+', '||'),
        (r'\s+feat\.?\s+', '||'),
        (r'\s+featuring\s+', '||'),
        (r'\s+ft\.?\s+', '||'),
        (r'\s+and\s+', '||'),
        (r'\s+with\s+', '||'),
        (r'\s+vs\.?\s+', '||'),
        (r'\s+/\s+', '||'),
        (r',\s+', '||'),
    ]

    for pattern, replacement in separators:
        normalized = re.sub(pattern, replacement, normalized, flags=re.IGNORECASE)

    # Split by delimiter
    parts = [part.strip() for part in normalized.split('||')]

    # Remove empty strings and deduplicate while preserving order
    seen = set()
    result = []
    for part in parts:
        if part and part not in seen:
            seen.add(part)
            result.append(part)

    return result


def normalize_artist_name(name: str) -> str:
    """
    Normalize artist name for consistency.

    - Remove extra whitespace
    - Strip leading/trailing spaces
    - Handle special characters
    """
    # Remove trailing spaces after punctuation that might be artifacts
    name = re.sub(r'\s+', ' ', name)  # Multiple spaces -> single space
    name = name.strip()

    return name


def get_or_create_artist(db: Session, artist_name: str) -> Artist:
    """Get existing artist or create new one."""
    normalized_name = normalize_artist_name(artist_name)

    # Check if exists (case-insensitive)
    artist = db.query(Artist).filter(
        Artist.name.ilike(normalized_name)
    ).first()

    if artist:
        logger.debug(f"Found existing artist: {normalized_name}")
        return artist

    # Create new
    artist = Artist(name=normalized_name)
    db.add(artist)
    db.flush()  # Get ID without committing
    logger.info(f"Created new artist: {normalized_name} (ID: {artist.id})")

    return artist


def normalize_artists(
    db: Session,
    dry_run: bool = False,
    verify_on_lastfm: bool = False
) -> dict:
    """
    Normalize compound artist names.

    Process:
    1. Find all compound artist names
    2. Split them into individual artists
    3. Create individual artists if they don't exist
    4. Update track_artists relationships (change primary -> featured)
    5. Optionally: verify individual artists exist on Last.fm
    6. Delete or mark compound artists

    Args:
        db: Database session
        dry_run: If True, only show what would be done
        verify_on_lastfm: If True, verify individual artists on Last.fm (slow)

    Returns:
        Statistics dict
    """
    stats = {
        'compound_artists_found': 0,
        'compound_artists_processed': 0,
        'new_artists_created': 0,
        'track_relationships_updated': 0,
        'compound_artists_deleted': 0,
        'lastfm_verified': 0,
        'lastfm_not_found': 0,
    }

    # Find all artists
    all_artists = db.query(Artist).all()

    # Identify compound artists
    compound_artists = []
    for artist in all_artists:
        if is_compound_artist(artist.name):
            individual_names = parse_compound_artist(artist.name)
            if len(individual_names) > 1:
                compound_artists.append((artist, individual_names))

    stats['compound_artists_found'] = len(compound_artists)

    if not compound_artists:
        logger.info("No compound artists found. Database is already normalized.")
        return stats

    logger.info(f"Found {len(compound_artists)} compound artists to normalize")

    # Collect unique individual artist names
    unique_names = set()
    for _, individual_names in compound_artists:
        unique_names.update(individual_names)

    # Step 1: Check if artists already exist in local database
    # verified_artists[name] = ('in_db', artist_id) | ('lastfm', True/False) | ('unknown', None)
    verified_artists = {}
    logger.info(f"Checking {len(unique_names)} unique artist names...")

    for name in unique_names:
        # Check if artist exists in database (case-insensitive)
        existing = db.query(Artist).filter(
            Artist.name.ilike(normalize_artist_name(name))
        ).first()

        if existing:
            verified_artists[name] = ('in_db', existing.id)
            logger.info(f"✓ Found in local DB: {name} (ID: {existing.id})")
        else:
            # Not in local DB, will need verification
            verified_artists[name] = ('unknown', None)
            logger.debug(f"? Not in local DB: {name}")

    # Step 2: Verify unknown artists on Last.fm (only if requested and not dry-run)
    if verify_on_lastfm and not dry_run:
        unknown_artists = [name for name, (source, _) in verified_artists.items() if source == 'unknown']

        if unknown_artists:
            logger.info(f"\nVerifying {len(unknown_artists)} unknown artists on Last.fm...")
            from lastfm import LastFmService
            import time

            service = LastFmService()

            for name in unknown_artists:
                try:
                    data = service.get_artist_info(name)
                    if data:
                        verified_artists[name] = ('lastfm', True)
                        stats['lastfm_verified'] += 1
                        logger.info(f"✓ Found on Last.fm: {name}")
                    else:
                        verified_artists[name] = ('lastfm', False)
                        stats['lastfm_not_found'] += 1
                        logger.warning(f"✗ Not found on Last.fm: {name}")

                    time.sleep(0.25)  # Rate limiting
                except Exception as e:
                    logger.error(f"Error verifying {name}: {e}")
                    verified_artists[name] = ('lastfm', False)
        else:
            logger.info("All artists already exist in database, no Last.fm verification needed")

    # Process compound artists
    for compound_artist, individual_names in compound_artists:
        logger.info(f"\nProcessing: '{compound_artist.name}' -> {individual_names}")

        if dry_run:
            logger.info(f"  [DRY RUN] Would split into:")
            for name in individual_names:
                source, value = verified_artists.get(name, ('unknown', None))
                if source == 'in_db':
                    logger.info(f"    ✓ {name} (already in DB, ID: {value})")
                elif source == 'lastfm':
                    if value:
                        logger.info(f"    ✓ {name} (verified on Last.fm)")
                    else:
                        logger.info(f"    ✗ {name} (not found on Last.fm)")
                else:  # unknown
                    logger.info(f"    ? {name} (would create without verification)")
            stats['compound_artists_processed'] += 1
            continue

        # Filter out artists that failed Last.fm verification
        valid_names = []
        for name in individual_names:
            source, value = verified_artists.get(name, ('unknown', None))

            if source == 'in_db':
                # Already exists in DB - always valid
                valid_names.append(name)
            elif source == 'lastfm':
                if value:
                    # Verified on Last.fm - valid
                    valid_names.append(name)
                else:
                    # Not found on Last.fm - skip
                    logger.warning(f"  ✗ Skipping '{name}' (not found on Last.fm)")
            else:  # unknown
                # Not verified, but accept if --verify-lastfm not used
                valid_names.append(name)

        individual_names = valid_names

        if not individual_names:
            logger.warning(f"  No valid artists found for '{compound_artist.name}', skipping")
            continue

        # Get or create individual artists
        individual_artists = []
        for name in individual_names:
            try:
                artist = get_or_create_artist(db, name)
                individual_artists.append(artist)
            except Exception as e:
                logger.error(f"Error creating artist '{name}': {e}")
                continue

        if not individual_artists:
            logger.warning(f"No individual artists created for '{compound_artist.name}'")
            continue

        # Find all tracks with this compound artist
        track_artists = db.query(TrackArtist).filter(
            TrackArtist.artist_id == compound_artist.id
        ).all()

        logger.info(f"  Found {len(track_artists)} tracks with artist '{compound_artist.name}'")

        # Update relationships
        for ta in track_artists:
            track_id = ta.track_id

            # First artist is primary, rest are featured
            for i, individual_artist in enumerate(individual_artists):
                role = 'primary' if i == 0 else 'featured'

                # Check if relationship already exists
                existing = db.query(TrackArtist).filter(
                    TrackArtist.track_id == track_id,
                    TrackArtist.artist_id == individual_artist.id,
                    TrackArtist.role == role
                ).first()

                if not existing:
                    new_ta = TrackArtist(
                        track_id=track_id,
                        artist_id=individual_artist.id,
                        role=role
                    )
                    db.add(new_ta)
                    stats['track_relationships_updated'] += 1

            # Delete old compound relationship
            db.delete(ta)

        # Delete compound artist (if no other relationships exist)
        # Check if artist is used elsewhere (e.g., in albums)
        remaining_tracks = db.query(TrackArtist).filter(
            TrackArtist.artist_id == compound_artist.id
        ).count()

        if remaining_tracks == 0:
            db.delete(compound_artist)
            stats['compound_artists_deleted'] += 1
            logger.info(f"  ✓ Deleted compound artist '{compound_artist.name}'")
        else:
            logger.warning(f"  ⚠ Compound artist '{compound_artist.name}' still has {remaining_tracks} relationships")

        stats['compound_artists_processed'] += 1
        logger.info(f"  ✓ Normalized '{compound_artist.name}' into {len(individual_artists)} artists")

    if not dry_run:
        db.commit()
        logger.info("\n✅ Artist normalization complete!")
    else:
        logger.info("\n[DRY RUN] No changes made to database")

    return stats


def show_artist_statistics(db: Session):
    """Show statistics about artists."""
    result = db.execute(text("""
        SELECT
            a.id,
            a.name,
            COUNT(DISTINCT ta.track_id) as track_count
        FROM artists a
        LEFT JOIN track_artists ta ON a.id = ta.artist_id
        GROUP BY a.id, a.name
        ORDER BY track_count DESC, a.name
    """)).fetchall()

    logger.info("\n📊 Artist Statistics:")
    logger.info(f"{'ID':<40} {'Artist':<45} {'Songs':<8}")
    logger.info("-" * 95)

    compound_count = 0
    total_tracks = 0
    for row in result:
        is_compound = '🔗' if is_compound_artist(row.name) else '  '
        if is_compound_artist(row.name):
            compound_count += 1
        logger.info(f"{str(row.id):<40} {is_compound} {row.name:<43} {row.track_count:<8}")
        total_tracks += row.track_count

    logger.info("-" * 95)
    logger.info(f"Total: {len(result)} artists, {compound_count} compound, {total_tracks} track relationships")


if __name__ == "__main__":
    import sys

    dry_run = '--dry-run' in sys.argv
    verify = '--verify-lastfm' in sys.argv

    with get_db_context() as db:
        logger.info("=== Artist Normalization Script ===\n")

        # Show current state
        logger.info("Current state:")
        show_artist_statistics(db)

        # Normalize
        logger.info("\n" + "="*60)
        stats = normalize_artists(db, dry_run=dry_run, verify_on_lastfm=verify)

        # Show results
        logger.info("\n" + "="*60)
        logger.info("Statistics:")
        for key, value in stats.items():
            logger.info(f"  {key}: {value}")

        if not dry_run and stats['compound_artists_processed'] > 0:
            logger.info("\nAfter normalization:")
            show_artist_statistics(db)
