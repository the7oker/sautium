"""
Command-line interface for Music AI DJ.
"""

import logging
import logging.config
import sys
import time

import click
from sqlalchemy import text

from config import settings, LOGGING_CONFIG
from database import get_db_context, engine
from scanner import scan_library
from track_filter import get_filtered_track_ids, describe_filters, track_filter_options

# Configure logging
logging.config.dictConfig(LOGGING_CONFIG)
logger = logging.getLogger(__name__)


@click.group()
def cli():
    """Music AI DJ - AI-powered music library management."""
    pass


def _resolve_filters(filter_artist, filter_album, filter_genre, filter_path,
                     filter_tag, filter_track_number, filter_lossless):
    """
    Resolve track filter options into a list of track IDs.

    Returns None if no filters are active, or a list of matching track IDs.
    Prints filter info and early-exits with empty list if nothing matched.
    """
    filter_kwargs = dict(
        artist=filter_artist, album=filter_album, genre=filter_genre,
        path=filter_path, tag=filter_tag, track_number=filter_track_number,
        lossless=filter_lossless,
    )
    filter_desc = describe_filters(**filter_kwargs)
    if not filter_desc:
        return None

    click.echo(f"🔍 Filters: {filter_desc}")
    with get_db_context() as db:
        track_ids = get_filtered_track_ids(db, **filter_kwargs)

    if track_ids is not None and len(track_ids) == 0:
        click.echo("⚠️  No tracks match the specified filters")
        return track_ids

    click.echo(f"📋 {len(track_ids)} tracks match filters")
    return track_ids


@cli.command()
@click.option("--limit", "-l", type=int, default=None, help="Limit number of files to scan (for testing)")
@click.option("--no-skip", is_flag=True, help="Don't skip existing files (re-scan all)")
@click.option("--path", "-p", type=str, default=None, help="Scan specific subdirectory (e.g., 'Electronic/Berlin School/Klaus Schulze')")
def scan(limit, no_skip, path):
    """Scan music library and import metadata to database."""
    click.echo(f"🎵 Starting library scan...")
    click.echo(f"📁 Library path: {settings.music_library_path}")

    if path:
        click.echo(f"📂 Subdirectory: {path}")

    if limit:
        click.echo(f"⚠️  Limited to {limit} files (testing mode)")

    if no_skip:
        click.echo(f"⚠️  Re-scanning all files (not skipping existing)")

    try:
        stats = scan_library(limit=limit, skip_existing=not no_skip, subpath=path)

        click.echo("\n✅ Scan complete!")
        click.echo(f"📊 Statistics:")
        click.echo(f"   • Processed: {stats['processed']} files")
        click.echo(f"   • Added: {stats['added']} tracks")
        click.echo(f"   • Skipped: {stats['skipped']} tracks")
        click.echo(f"   • Errors: {stats['errors']} files")

        if stats['errors'] > 0:
            click.echo(f"\n⚠️  Check logs for error details")

    except Exception as e:
        click.echo(f"\n❌ Error: {e}", err=True)
        logger.exception("Scan failed")
        sys.exit(1)


@cli.command("generate-embeddings")
@click.option("--limit", "-l", type=int, default=None, help="Limit number of tracks to process (for testing)")
@click.option("--batch-size", "-b", type=int, default=None, help="Override batch size (default from config)")
@click.option("--newest-first", is_flag=True, help="Process newest tracks first (by file modification date)")
@click.option("--max-duration", "-d", type=int, default=None, help="Maximum duration in seconds (e.g., 1800 for 30 minutes)")
@click.option("--worker-id", type=int, default=None, help="Worker index (0-based) for parallel processing")
@click.option("--worker-count", type=int, default=None, help="Total number of workers for parallel processing")
@track_filter_options
def generate_embeddings(limit, batch_size, newest_first, max_duration,
                        worker_id, worker_count,
                        filter_artist, filter_album, filter_genre, filter_path,
                        filter_tag, filter_track_number, filter_lossless):
    """Generate audio embeddings for tracks using CLAP model."""
    from embeddings import generate_embeddings as do_generate

    click.echo("🎵 Starting audio embedding generation...")
    click.echo(f"🖥️  Model: {settings.embedding_model}")
    click.echo(f"📦 Batch size: {batch_size or settings.embedding_batch_size}")

    if limit:
        click.echo(f"⚠️  Limited to {limit} tracks (testing mode)")

    if newest_first:
        click.echo(f"🆕 Processing newest tracks first (by file modification date)")

    if max_duration:
        click.echo(f"⏱️  Time limit: {max_duration} seconds ({max_duration/60:.1f} minutes)")

    # Validate worker options
    if (worker_id is not None) != (worker_count is not None):
        click.echo("❌ Both --worker-id and --worker-count must be specified together", err=True)
        sys.exit(1)
    if worker_count is not None:
        if worker_count < 1:
            click.echo("❌ --worker-count must be >= 1", err=True)
            sys.exit(1)
        if worker_id < 0 or worker_id >= worker_count:
            click.echo(f"❌ --worker-id must be in range [0, {worker_count - 1}]", err=True)
            sys.exit(1)
        click.echo(f"👷 Worker {worker_id + 1}/{worker_count}")

    # Resolve track filters
    track_ids = _resolve_filters(
        filter_artist, filter_album, filter_genre, filter_path,
        filter_tag, filter_track_number, filter_lossless,
    )
    if track_ids is not None and len(track_ids) == 0:
        return

    try:
        stats = do_generate(limit=limit, batch_size=batch_size, order_by_date=newest_first, max_duration_seconds=max_duration, track_ids=track_ids, worker_id=worker_id, worker_count=worker_count)

        click.echo("\n✅ Embedding generation complete!")
        click.echo(f"📊 Statistics:")
        click.echo(f"   • Processed: {stats['processed']} tracks")
        click.echo(f"   • Success: {stats['success']} embeddings")
        click.echo(f"   • Failed: {stats['failed']} tracks")

        if stats['failed'] > 0:
            click.echo(f"\n⚠️  Check logs for error details")

    except Exception as e:
        click.echo(f"\n❌ Error: {e}", err=True)
        logger.exception("Embedding generation failed")
        sys.exit(1)


@cli.command()
def stats():
    """Show library statistics."""
    try:
        with get_db_context() as db:
            result = db.execute(text("SELECT * FROM library_stats")).fetchone()

            if result:
                click.echo("\n📊 Library Statistics:")
                click.echo(f"   • Artists: {result[0]:,}")
                click.echo(f"   • Albums: {result[1]:,}")
                click.echo(f"   • Tracks: {result[2]:,}")
                click.echo(f"   • Tracks with embeddings: {result[3]:,}")

                if result[4]:  # total_duration_seconds
                    hours = int(result[4] / 3600)
                    minutes = int((result[4] % 3600) / 60)
                    click.echo(f"   • Total duration: {hours}h {minutes}m")

                if result[5]:  # total_file_size_bytes
                    size_gb = result[5] / (1024**3)
                    click.echo(f"   • Total size: {size_gb:.2f} GB")

                click.echo(f"   • Unique genres: {result[6]}")
            else:
                click.echo("No data in database yet. Run 'scan' first.")

    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)
        logger.exception("Stats failed")
        sys.exit(1)


@cli.command()
@click.option("--limit", "-l", type=int, default=10, help="Number of tracks to show")
def list_tracks(limit):
    """List recently added tracks."""
    try:
        from models import Track, MediaFile, AlbumVariant, Album, TrackArtist, Artist

        with get_db_context() as db:
            rows = (
                db.query(Track, MediaFile, Album)
                .join(MediaFile, MediaFile.track_id == Track.id)
                .join(AlbumVariant, MediaFile.album_variant_id == AlbumVariant.id)
                .join(Album, AlbumVariant.album_id == Album.id)
                .order_by(Track.created_at.desc())
                .limit(limit)
                .all()
            )

            if not rows:
                click.echo("No tracks in database yet. Run 'scan' first.")
                return

            click.echo(f"\n🎵 Recently added tracks (showing {len(rows)}):\n")

            for track, mf, album in rows:
                artist_row = (
                    db.query(Artist.name)
                    .join(TrackArtist, TrackArtist.artist_id == Artist.id)
                    .filter(TrackArtist.track_id == track.id, TrackArtist.role == "primary")
                    .first()
                )
                artist_name = artist_row[0] if artist_row else "Unknown"
                duration = f"{int(mf.duration_seconds // 60)}:{int(mf.duration_seconds % 60):02d}" if mf.duration_seconds else "?"
                quality = f"{mf.sample_rate//1000}kHz/{mf.bit_depth}bit" if mf.sample_rate and mf.bit_depth else "?"

                click.echo(f"   • {artist_name} - {track.title}")
                click.echo(f"     Album: {album.title} | {duration} | {quality}")

    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)
        logger.exception("List tracks failed")
        sys.exit(1)


@cli.command()
def check_db():
    """Check database connection and schema."""
    click.echo("🔍 Checking database connection...")

    try:
        # Test connection
        with engine.connect() as conn:
            result = conn.execute(text("SELECT version()")).fetchone()
            click.echo(f"✅ PostgreSQL connected: {result[0][:50]}...")

            # Check pgvector
            result = conn.execute(
                text("SELECT extname, extversion FROM pg_extension WHERE extname = 'vector'")
            ).fetchone()

            if result:
                click.echo(f"✅ pgvector extension: v{result[1]}")
            else:
                click.echo("❌ pgvector extension not found!")
                sys.exit(1)

            # Check tables
            result = conn.execute(
                text("""
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = 'public'
                    ORDER BY table_name
                """)
            ).fetchall()

            click.echo(f"\n📋 Database tables ({len(result)}):")
            for row in result:
                click.echo(f"   • {row[0]}")

        click.echo("\n✅ Database is ready!")

    except Exception as e:
        click.echo(f"\n❌ Database error: {e}", err=True)
        logger.exception("Database check failed")
        sys.exit(1)


@cli.command()
@click.option("--path", "-p", type=str, required=True, help="Path to FLAC file")
def test_file(path):
    """Test metadata extraction from a single file."""
    from scanner import LibraryScanner
    from pathlib import Path

    file_path = Path(path)

    if not file_path.exists():
        click.echo(f"❌ File not found: {path}", err=True)
        sys.exit(1)

    if not file_path.suffix.lower() == ".flac":
        click.echo(f"❌ Not a FLAC file: {path}", err=True)
        sys.exit(1)

    click.echo(f"🎵 Extracting metadata from: {file_path.name}\n")

    metadata = LibraryScanner.extract_metadata(file_path)

    if not metadata:
        click.echo("❌ Failed to extract metadata", err=True)
        sys.exit(1)

    # Display metadata
    click.echo("📋 Metadata:")
    click.echo(f"   Title: {metadata.get('title')}")
    click.echo(f"   Artist: {metadata.get('artist')}")
    click.echo(f"   Album: {metadata.get('album')}")
    click.echo(f"   Album Artist: {metadata.get('album_artist')}")
    click.echo(f"   Genre: {metadata.get('genre')}")
    click.echo(f"   Year: {metadata.get('release_year')}")
    click.echo(f"   Track: {metadata.get('track_number')}")
    click.echo(f"   Disc: {metadata.get('disc_number')}")

    click.echo(f"\n🎧 Audio:")
    click.echo(f"   Duration: {metadata.get('duration_seconds')}s")
    click.echo(f"   Sample Rate: {metadata.get('sample_rate')} Hz")
    click.echo(f"   Bit Depth: {metadata.get('bit_depth')} bit")
    click.echo(f"   Channels: {metadata.get('channels')}")
    click.echo(f"   Bitrate: {metadata.get('bitrate')} kbps")

    click.echo(f"\n💿 Quality:")
    click.echo(f"   Format: {metadata.get('file_format', '?')}")
    click.echo(f"   File Size: {metadata.get('file_size_bytes') / (1024*1024):.2f} MB")


@cli.command("search-similar")
@click.option("--track-id", "-t", type=int, required=True, help="Source track ID")
@click.option("--limit", "-l", type=int, default=10, help="Number of results")
@click.option("--min-similarity", type=float, default=None, help="Minimum similarity (0-1)")
@click.option("--artist", type=str, default=None, help="Filter by artist name (partial match)")
@click.option("--genre", type=str, default=None, help="Filter by genre (partial match)")
@click.option("--lossless/--lossy", default=None, help="Filter by lossless/lossy format")
def search_similar(track_id, limit, min_similarity, artist, genre, lossless):
    """Find tracks similar to a given track by audio similarity."""
    from search import search_similar_tracks

    filters = {}
    if artist:
        filters["artist"] = artist
    if genre:
        filters["genre"] = genre
    if lossless is not None:
        filters["is_lossless"] = lossless

    try:
        with get_db_context() as db:
            result = search_similar_tracks(
                db, track_id, limit=limit, min_similarity=min_similarity, filters=filters
            )

        if "error" in result:
            click.echo(f"❌ {result['error']}", err=True)
            sys.exit(1)

        qt = result["query_track"]
        click.echo(f"\n🎵 Tracks similar to: {qt['artist']} - {qt['title']}")
        click.echo(f"   Album: {qt['album']} | Genre: {qt.get('genre', 'N/A')}")
        click.echo(f"\n{'#':<4} {'Sim':>5}  {'Artist':<25} {'Title':<35} {'Album':<30} {'Format':<10}")
        click.echo("-" * 113)

        for i, track in enumerate(result["results"], 1):
            sim = f"{track['similarity']:.2f}" if track["similarity"] else "N/A"
            artist_name = (track["artist"] or "Unknown")[:24]
            title = (track["title"] or "?")[:34]
            album_name = (track["album"] or "?")[:29]
            fmt = "Lossless" if track.get("is_lossless") else "Lossy" if track.get("is_lossless") is False else "?"
            click.echo(f"{i:<4} {sim:>5}  {artist_name:<25} {title:<35} {album_name:<30} {fmt:<10}")

        click.echo(f"\n📊 Found {result['count']} similar tracks")

    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)
        logger.exception("Similar search failed")
        sys.exit(1)


@cli.command("search-text")
@click.option("--query", "-q", type=str, required=True, help="Text description to search for")
@click.option("--limit", "-l", type=int, default=10, help="Number of results")
@click.option("--min-similarity", type=float, default=None, help="Minimum similarity (0-1)")
@click.option("--artist", type=str, default=None, help="Filter by artist name (partial match)")
@click.option("--genre", type=str, default=None, help="Filter by genre (partial match)")
@click.option("--lossless/--lossy", default=None, help="Filter by lossless/lossy format")
def search_text(query, limit, min_similarity, artist, genre, lossless):
    """Search tracks by text description using CLAP text-to-audio similarity."""
    from search import search_by_text

    click.echo(f"🔍 Searching for: \"{query}\"")
    click.echo("⏳ Loading CLAP model for text encoding...")

    filters = {}
    if artist:
        filters["artist"] = artist
    if genre:
        filters["genre"] = genre
    if lossless is not None:
        filters["is_lossless"] = lossless

    try:
        with get_db_context() as db:
            result = search_by_text(
                db, query, limit=limit, min_similarity=min_similarity, filters=filters
            )

        click.echo(f"\n{'#':<4} {'Sim':>5}  {'Artist':<25} {'Title':<35} {'Album':<30} {'Format':<10}")
        click.echo("-" * 113)

        for i, track in enumerate(result["results"], 1):
            sim = f"{track['similarity']:.2f}" if track["similarity"] else "N/A"
            artist_name = (track["artist"] or "Unknown")[:24]
            title = (track["title"] or "?")[:34]
            album_name = (track["album"] or "?")[:29]
            fmt = "Lossless" if track.get("is_lossless") else "Lossy" if track.get("is_lossless") is False else "?"
            click.echo(f"{i:<4} {sim:>5}  {artist_name:<25} {title:<35} {album_name:<30} {fmt:<10}")

        click.echo(f"\n📊 Found {result['count']} matching tracks")

    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)
        logger.exception("Text search failed")
        sys.exit(1)


@cli.command("normalize-artists")
@click.option("--dry-run", is_flag=True, help="Show what would be done without making changes")
@click.option("--verify-lastfm", is_flag=True, help="Verify individual artists exist on Last.fm (slow)")
def normalize_artists_cmd(dry_run, verify_lastfm):
    """Normalize compound artist names (split 'A & B' into separate artists)."""
    from normalize_artists import normalize_artists, show_artist_statistics

    click.echo("🎵 Normalizing compound artist names...")
    if dry_run:
        click.echo("⚠️  DRY RUN MODE - no changes will be made")
    if verify_lastfm:
        click.echo("🔍 Will verify artists on Last.fm (this will be slow)")
    click.echo()

    try:
        with get_db_context() as db:
            click.echo("📊 Current state:")
            show_artist_statistics(db)

            click.echo("\n" + "="*60)

            stats = normalize_artists(db, dry_run=dry_run, verify_on_lastfm=verify_lastfm)

            click.echo("\n" + "="*60)
            click.echo("\n📊 Statistics:")
            click.echo(f"   • Compound artists found: {stats['compound_artists_found']}")
            click.echo(f"   • Compound artists processed: {stats['compound_artists_processed']}")
            click.echo(f"   • New artists created: {stats['new_artists_created']}")
            click.echo(f"   • Track relationships updated: {stats['track_relationships_updated']}")
            click.echo(f"   • Compound artists deleted: {stats['compound_artists_deleted']}")

            if verify_lastfm:
                click.echo(f"   • Last.fm verified: {stats['lastfm_verified']}")
                click.echo(f"   • Last.fm not found: {stats['lastfm_not_found']}")

            if not dry_run and stats['compound_artists_processed'] > 0:
                click.echo("\n📊 After normalization:")
                show_artist_statistics(db)
                click.echo("\n✅ Artist normalization complete!")
                click.echo("💡 Tip: Run 'enrich-lastfm' to fetch data for new artists")
            elif stats['compound_artists_found'] == 0:
                click.echo("\n✅ No compound artists found - database is already normalized")
            elif dry_run:
                click.echo("\n⚠️  Dry run complete - no changes made")

    except Exception as e:
        click.echo(f"\n❌ Error: {e}", err=True)
        logger.exception("Artist normalization failed")
        sys.exit(1)


@cli.command("normalize-genres")
@click.option("--dry-run", is_flag=True, help="Show what would be done without making changes")
def normalize_genres_cmd(dry_run):
    """Normalize compound genre names (split 'A/B/C' into separate genres)."""
    from normalize_genres import normalize_genres, show_genre_statistics

    click.echo("🎵 Normalizing compound genre names...")
    if dry_run:
        click.echo("⚠️  DRY RUN MODE - no changes will be made\n")

    try:
        with get_db_context() as db:
            click.echo("📊 Current state:")
            show_genre_statistics(db)

            click.echo("\n" + "="*60)

            stats = normalize_genres(db, dry_run=dry_run)

            click.echo("\n" + "="*60)
            click.echo("\n📊 Statistics:")
            click.echo(f"   • Compound genres found: {stats['compound_genres_found']}")
            click.echo(f"   • Compound genres processed: {stats['compound_genres_processed']}")
            click.echo(f"   • New genres created: {stats['new_genres_created']}")
            click.echo(f"   • Track relationships updated: {stats['track_relationships_updated']}")
            click.echo(f"   • Compound genres deleted: {stats['compound_genres_deleted']}")

            if not dry_run and stats['compound_genres_processed'] > 0:
                click.echo("\n📊 After normalization:")
                show_genre_statistics(db)
                click.echo("\n✅ Genre normalization complete!")
                click.echo("💡 Tip: Run 'enrich-lastfm --genres' to fetch descriptions for new genres")
            elif stats['compound_genres_found'] == 0:
                click.echo("\n✅ No compound genres found - database is already normalized")
            elif dry_run:
                click.echo("\n⚠️  Dry run complete - no changes made")

    except Exception as e:
        click.echo(f"\n❌ Error: {e}", err=True)
        logger.exception("Genre normalization failed")
        sys.exit(1)


@cli.command("enrich-lastfm")
@click.option("--limit", "-l", type=int, default=None, help="Limit number of artists/genres to enrich")
@click.option("--artist", "-a", type=str, default=None, help="Enrich specific artist by name")
@click.option("--genres", is_flag=True, help="Enrich genres instead of artists")
@click.option("--no-skip", is_flag=True, help="Re-fetch data that already exists")
@click.option("--delay", type=float, default=0.2, help="Delay between requests (seconds)")
def enrich_lastfm(limit, artist, genres, no_skip, delay):
    """Enrich artists or genres with Last.fm data."""
    from lastfm import LastFmService

    if not settings.lastfm_api_key:
        click.echo("❌ LASTFM_API_KEY is not configured. Set it in .env file.", err=True)
        sys.exit(1)

    try:
        service = LastFmService()

        with get_db_context() as db:
            if genres:
                # Enrich genres
                click.echo("🎵 Enriching genres with Last.fm tag data...")
                skip_existing = not no_skip
                click.echo(f"{'⚠️  Re-fetching all genres' if no_skip else '✓ Skipping genres with existing data'}")
                click.echo(f"⏱️  Rate limit: {delay}s between requests")
                if limit:
                    click.echo(f"⚠️  Limited to {limit} genres")

                click.echo()

                stats = service.enrich_genres_batch(
                    db, limit=limit, skip_existing=skip_existing, rate_limit_delay=delay
                )

                click.echo("\n✅ Last.fm genre enrichment complete!")
                click.echo(f"📊 Statistics:")
                click.echo(f"   • Processed: {stats['processed']} genres")
                click.echo(f"   • Success: {stats['success']}")
                click.echo(f"   • Not found: {stats['not_found']}")
                click.echo(f"   • Errors: {stats['errors']}")

            elif artist:
                # Enrich specific artist
                click.echo(f"🔍 Looking for artist: {artist}")
                result = db.execute(
                    text("SELECT id, name FROM artists WHERE name ILIKE :name"),
                    {"name": f"%{artist}%"}
                ).fetchone()

                if not result:
                    click.echo(f"❌ Artist '{artist}' not found in database", err=True)
                    sys.exit(1)

                artist_id, artist_name = result
                click.echo(f"✓ Found: {artist_name} (ID: {artist_id})")

                result = service.enrich_artist(db, artist_id, artist_name)

                if result["status"] == "success":
                    click.echo(f"\n✅ Successfully enriched {artist_name}:")
                    click.echo(f"   • Bio: {'✓' if result['stored'].get('bio') else '✗'}")
                    click.echo(f"   • Tags: {result.get('tags_count', 0)} tags")
                    click.echo(f"   • Similar artists: {result.get('similar_count', 0)}")
                elif result["status"] == "not_found":
                    click.echo(f"\n⚠️  Artist not found on Last.fm: {artist_name}")
                else:
                    click.echo(f"\n❌ Error enriching {artist_name}: {result.get('error')}")

            else:
                # Batch enrich artists
                click.echo("🎵 Enriching artists with Last.fm data...")
                skip_existing = not no_skip
                click.echo(f"{'⚠️  Re-fetching all artists' if no_skip else '✓ Skipping artists with existing data'}")
                click.echo(f"⏱️  Rate limit: {delay}s between requests")
                if limit:
                    click.echo(f"⚠️  Limited to {limit} artists")

                click.echo()

                stats = service.enrich_artists_batch(
                    db, limit=limit, skip_existing=skip_existing, rate_limit_delay=delay
                )

                click.echo("\n✅ Last.fm artist enrichment complete!")
                click.echo(f"📊 Statistics:")
                click.echo(f"   • Processed: {stats['processed']} artists")
                click.echo(f"   • Success: {stats['success']}")
                click.echo(f"   • Not found: {stats['not_found']}")
                click.echo(f"   • Errors: {stats['errors']}")

    except Exception as e:
        click.echo(f"\n❌ Error: {e}", err=True)
        logger.exception("Last.fm enrichment failed")
        sys.exit(1)


@cli.command("enrich-albums")
@click.option("--limit", "-l", type=int, default=None, help="Limit number of albums to enrich")
@click.option("--album", "-a", type=str, default=None, help="Enrich specific album by title")
@click.option("--no-skip", is_flag=True, help="Re-fetch data that already exists")
@click.option("--delay", type=float, default=0.2, help="Delay between requests (seconds)")
def enrich_albums(limit, album, no_skip, delay):
    """Enrich albums with Last.fm data (wiki, tags, stats)."""
    from lastfm import LastFmService

    if not settings.lastfm_api_key:
        click.echo("❌ LASTFM_API_KEY is not configured. Set it in .env file.", err=True)
        sys.exit(1)

    try:
        service = LastFmService()

        with get_db_context() as db:
            if album:
                # Enrich specific album
                click.echo(f"🔍 Looking for album: {album}")
                result = db.execute(
                    text("""
                        SELECT DISTINCT
                            al.id,
                            al.title,
                            STRING_AGG(DISTINCT a.name, ', ') as artist_names
                        FROM albums al
                        JOIN album_artists aa ON al.id = aa.album_id AND aa.role = 'primary'
                        JOIN artists a ON aa.artist_id = a.id
                        WHERE al.title ILIKE :title
                        GROUP BY al.id, al.title
                        LIMIT 1
                    """),
                    {"title": f"%{album}%"}
                ).fetchone()

                if not result:
                    click.echo(f"❌ Album '{album}' not found in database", err=True)
                    sys.exit(1)

                album_id, album_title, artist_names = result
                # Take first artist if multiple
                artist_name = artist_names.split(', ')[0]

                click.echo(f"✓ Found: {album_title} by {artist_name} (ID: {album_id})")
                click.echo()

                enrichment_result = service.enrich_album(db, album_id, artist_name, album_title)

                if enrichment_result["status"] == "success":
                    click.echo(f"\n✅ Successfully enriched {album_title}:")
                    click.echo(f"   • Wiki: {'✓' if enrichment_result.get('has_wiki') else '✗'}")
                    click.echo(f"   • Tags: {enrichment_result.get('tags_count', 0)} tags")
                    if enrichment_result.get('listeners'):
                        click.echo(f"   • Listeners: {enrichment_result['listeners']:,}")
                    if enrichment_result.get('playcount'):
                        click.echo(f"   • Playcount: {enrichment_result['playcount']:,}")
                elif enrichment_result["status"] == "not_found":
                    click.echo(f"\n⚠️  Album not found on Last.fm: {album_title}")
                else:
                    click.echo(f"\n❌ Error enriching album: {enrichment_result.get('error')}")
                    sys.exit(1)

            else:
                # Enrich multiple albums
                skip_existing = not no_skip
                click.echo("💿 Enriching albums with Last.fm data...")
                click.echo(f"{'⚠️  Re-fetching all albums' if no_skip else '✓ Skipping albums with existing data'}")
                click.echo(f"⏱️  Rate limit: {delay}s between requests")
                if limit:
                    click.echo(f"⚠️  Limited to {limit} albums")

                click.echo()

                # Get albums to enrich
                if skip_existing:
                    query = text("""
                        SELECT DISTINCT
                            al.id,
                            al.title,
                            STRING_AGG(DISTINCT a.name, ', ') as artist_names
                        FROM albums al
                        JOIN album_artists aa ON al.id = aa.album_id AND aa.role = 'primary'
                        JOIN artists a ON aa.artist_id = a.id
                        WHERE NOT EXISTS (
                            SELECT 1 FROM album_info ai
                            WHERE ai.album_id = al.id AND ai.source = 'lastfm'
                        )
                        GROUP BY al.id, al.title
                        ORDER BY al.title
                    """)
                else:
                    query = text("""
                        SELECT DISTINCT
                            al.id,
                            al.title,
                            STRING_AGG(DISTINCT a.name, ', ') as artist_names
                        FROM albums al
                        JOIN album_artists aa ON al.id = aa.album_id AND aa.role = 'primary'
                        JOIN artists a ON aa.artist_id = a.id
                        GROUP BY al.id, al.title
                        ORDER BY al.title
                    """)

                if limit:
                    query = text(str(query) + f" LIMIT {limit}")

                albums = db.execute(query).fetchall()

                if not albums:
                    click.echo("✓ No albums to enrich")
                    return

                click.echo(f"Found {len(albums)} albums to enrich\n")

                stats = {"processed": 0, "success": 0, "not_found": 0, "errors": 0}

                for album_id, album_title, artist_names in albums:
                    # Take first artist if multiple
                    artist_name = artist_names.split(', ')[0]

                    result = service.enrich_album(db, album_id, artist_name, album_title)

                    stats["processed"] += 1

                    if result["status"] == "success":
                        stats["success"] += 1
                    elif result["status"] == "not_found":
                        stats["not_found"] += 1
                    elif result["status"] == "error":
                        stats["errors"] += 1

                    # Rate limiting
                    if delay > 0:
                        time.sleep(delay)

                click.echo("\n✅ Last.fm album enrichment complete!")
                click.echo(f"📊 Statistics:")
                click.echo(f"   • Processed: {stats['processed']} albums")
                click.echo(f"   • Success: {stats['success']}")
                click.echo(f"   • Not found: {stats['not_found']}")
                click.echo(f"   • Errors: {stats['errors']}")

    except Exception as e:
        click.echo(f"\n❌ Error: {e}", err=True)
        logger.exception("Album enrichment failed")
        sys.exit(1)


@cli.command("analyze-audio")
@click.option("--limit", "-l", type=int, default=None, help="Limit number of tracks to process")
@click.option("--batch-size", "-b", type=int, default=None, help="Override batch size (default from config)")
@click.option("--force", is_flag=True, help="Re-analyze tracks that already have features")
@click.option("--newest-first", is_flag=True, help="Process newest tracks first (by file modification date)")
@click.option("--librosa-only", is_flag=True, help="Skip CLAP classification (faster, DSP features only)")
@click.option("--max-duration", "-d", type=int, default=None, help="Maximum duration in seconds (e.g., 1800 for 30 minutes)")
@click.option("--worker-id", type=int, default=None, help="Worker index (0-based) for parallel processing")
@click.option("--worker-count", type=int, default=None, help="Total number of workers for parallel processing")
@track_filter_options
def analyze_audio(limit, batch_size, force, newest_first, librosa_only, max_duration,
                  worker_id, worker_count,
                  filter_artist, filter_album, filter_genre, filter_path,
                  filter_tag, filter_track_number, filter_lossless):
    """Extract audio features (BPM, key, instruments, mood, etc.) from tracks."""
    from audio_analysis import AudioAnalyzer

    click.echo("🎵 Starting audio feature extraction...")
    click.echo(f"🖥️  librosa DSP + {'CLAP zero-shot' if not librosa_only else 'librosa only'}")

    if limit:
        click.echo(f"⚠️  Limited to {limit} tracks")
    if force:
        click.echo(f"⚠️  Force mode: re-analyzing all tracks")
    if newest_first:
        click.echo(f"🆕 Processing newest tracks first")
    if librosa_only:
        click.echo(f"⚡ Skipping CLAP classification (DSP features only)")
    if max_duration:
        click.echo(f"⏱️  Time limit: {max_duration} seconds ({max_duration/60:.1f} minutes)")

    # Validate worker options
    if (worker_id is not None) != (worker_count is not None):
        click.echo("❌ Both --worker-id and --worker-count must be specified together", err=True)
        sys.exit(1)
    if worker_count is not None:
        if worker_count < 1:
            click.echo("❌ --worker-count must be >= 1", err=True)
            sys.exit(1)
        if worker_id < 0 or worker_id >= worker_count:
            click.echo(f"❌ --worker-id must be in range [0, {worker_count - 1}]", err=True)
            sys.exit(1)
        click.echo(f"👷 Worker {worker_id + 1}/{worker_count}")

    # Resolve track filters
    track_ids = _resolve_filters(
        filter_artist, filter_album, filter_genre, filter_path,
        filter_tag, filter_track_number, filter_lossless,
    )
    if track_ids is not None and len(track_ids) == 0:
        return

    try:
        analyzer = AudioAnalyzer()
        stats = analyzer.analyze_all(
            limit=limit, force=force,
            order_by_date=newest_first, librosa_only=librosa_only,
            max_duration_seconds=max_duration, track_ids=track_ids,
            worker_id=worker_id, worker_count=worker_count,
        )

        click.echo("\n✅ Audio analysis complete!")
        click.echo(f"📊 Statistics:")
        click.echo(f"   • Processed: {stats['processed']} tracks")
        click.echo(f"   • Success: {stats['success']}")
        click.echo(f"   • Failed: {stats['failed']}")

        # Show sample results
        if stats['success'] > 0:
            try:
                with get_db_context() as db:
                    rows = db.execute(text("""
                        SELECT t.title, a2.name as artist,
                               af.bpm, af.key, af.mode, af.vocal_instrumental,
                               af.danceability, af.instruments
                        FROM audio_features af
                        JOIN tracks t ON af.track_id = t.id
                        JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
                        JOIN artists a2 ON ta.artist_id = a2.id
                        ORDER BY af.created_at DESC
                        LIMIT 5
                    """)).fetchall()

                    if rows:
                        click.echo(f"\n🎧 Sample results:")
                        for row in rows:
                            instruments = ""
                            if row.instruments:
                                top3 = sorted(row.instruments.items(), key=lambda x: -x[1])[:3]
                                instruments = ", ".join(k for k, v in top3)
                            click.echo(
                                f"   • {row.artist} - {row.title}"
                                f"\n     BPM: {row.bpm or '?'} | Key: {row.key or '?'} {row.mode or ''}"
                                f" | {row.vocal_instrumental or '?'}"
                                f" | Dance: {row.danceability or '?'}"
                                f"\n     Instruments: {instruments or 'N/A'}"
                            )
            except Exception:
                pass  # sample display is best-effort

    except Exception as e:
        click.echo(f"\n❌ Error: {e}", err=True)
        logger.exception("Audio analysis failed")
        sys.exit(1)


@cli.command("enrich-tracks")
@click.option("--limit", "-l", type=int, default=None, help="Limit number of tracks to process")
@click.option("--newest-first", is_flag=True, help="Process newest tracks first (by file modification date)")
@click.option("--max-duration", "-d", type=int, default=None, help="Maximum duration in seconds (e.g., 1800 for 30 minutes)")
@click.option("--skip-embeddings", is_flag=True, help="Skip audio embedding generation")
@click.option("--skip-lastfm", is_flag=True, help="Skip Last.fm enrichment (artist, album, track)")
@click.option("--skip-audio-analysis", is_flag=True, help="Skip audio feature extraction")
@click.option("--force-embeddings", is_flag=True, help="Regenerate audio embeddings even if exist")
@click.option("--force-audio-analysis", is_flag=True, help="Re-analyze audio even if features exist")
@click.option("--lastfm-delay", type=float, default=0.2, help="Delay between Last.fm requests (seconds)")
@click.option("--worker-id", type=int, default=None, help="Worker ID for parallel processing (0-indexed, use with --worker-count)")
@click.option("--worker-count", type=int, default=None, help="Total number of workers for parallel processing (use with --worker-id)")
@track_filter_options
def enrich_tracks(limit, newest_first, max_duration, skip_embeddings, skip_lastfm,
                  skip_audio_analysis, force_embeddings,
                  force_audio_analysis, lastfm_delay,
                  worker_id, worker_count,
                  filter_artist, filter_album, filter_genre, filter_path,
                  filter_tag, filter_track_number, filter_lossless):
    """
    Comprehensive track enrichment pipeline.

    Runs all data aggregation steps in correct order for each track:
    1. Audio Embedding (CLAP) - if missing
    2. Last.fm Artist Info - if missing
    3. Last.fm Album Info - if missing
    4. Last.fm Track Stats - if missing
    5. Audio Analysis - if missing

    Only processes missing data unless --force flags are used.
    Supports all filter options for targeted processing.

    Parallel processing: Use --worker-id and --worker-count to split work across multiple processes.
    """
    from track_enrichment import TrackEnrichmentPipeline

    # Validate worker parameters
    if (worker_id is not None) != (worker_count is not None):
        click.echo("❌ Error: --worker-id and --worker-count must be used together", err=True)
        sys.exit(1)

    if worker_count is not None:
        if worker_count < 1:
            click.echo("❌ Error: --worker-count must be at least 1", err=True)
            sys.exit(1)
        if worker_id < 0 or worker_id >= worker_count:
            click.echo(f"❌ Error: --worker-id must be between 0 and {worker_count - 1}", err=True)
            sys.exit(1)

    click.echo("🎵 Starting comprehensive track enrichment...")

    if worker_count is not None:
        click.echo(f"👷 Worker mode: {worker_id + 1}/{worker_count} (processing tracks where id % {worker_count} == {worker_id})")

    # Show configuration
    skip_steps = []
    if skip_embeddings:
        skip_steps.append("audio embeddings")
    if skip_lastfm:
        skip_steps.append("Last.fm")
    if skip_audio_analysis:
        skip_steps.append("audio analysis")

    if skip_steps:
        click.echo(f"⚠️  Skipping: {', '.join(skip_steps)}")

    force_steps = []
    if force_embeddings:
        force_steps.append("audio embeddings")
    if force_audio_analysis:
        force_steps.append("audio analysis")

    if force_steps:
        click.echo(f"🔄 Force regenerating: {', '.join(force_steps)}")

    if limit:
        click.echo(f"⚠️  Limited to {limit} tracks")

    if newest_first:
        click.echo(f"🆕 Processing newest tracks first")

    if max_duration:
        click.echo(f"⏱️  Time limit: {max_duration} seconds ({max_duration/60:.1f} minutes)")

    if not skip_lastfm:
        click.echo(f"⏱️  Last.fm delay: {lastfm_delay}s between requests")

    # Resolve track filters
    track_ids = _resolve_filters(
        filter_artist, filter_album, filter_genre, filter_path,
        filter_tag, filter_track_number, filter_lossless,
    )
    if track_ids is not None and len(track_ids) == 0:
        return

    try:
        pipeline = TrackEnrichmentPipeline(
            skip_embeddings=skip_embeddings,
            skip_lastfm=skip_lastfm,
            skip_audio_analysis=skip_audio_analysis,
            force_embeddings=force_embeddings,
            force_audio_analysis=force_audio_analysis,
            lastfm_delay=lastfm_delay,
        )

        stats = pipeline.enrich_tracks(
            limit=limit,
            order_by_date=newest_first,
            max_duration_seconds=max_duration,
            track_ids=track_ids,
            worker_id=worker_id,
            worker_count=worker_count,
        )

        click.echo("\n✅ Track enrichment complete!")
        click.echo(f"📊 Statistics:")
        click.echo(f"   • Tracks processed: {stats['processed']}")

        if not skip_embeddings:
            click.echo(f"   • Audio embeddings: {stats['audio_embedding_success']} success, {stats['audio_embedding_failed']} failed")

        if not skip_lastfm:
            click.echo(f"   • Last.fm artists: {stats['lastfm_artist_success']} enriched")
            click.echo(f"   • Last.fm albums: {stats['lastfm_album_success']} enriched")
            click.echo(f"   • Last.fm tracks: {stats['lastfm_track_success']} enriched")

        if not skip_audio_analysis:
            click.echo(f"   • Audio features: {stats['audio_features_success']} success, {stats['audio_features_failed']} failed")

    except Exception as e:
        click.echo(f"\n❌ Error: {e}", err=True)
        logger.exception("Track enrichment failed")
        sys.exit(1)


@cli.command("search-features")
@click.option("--bpm-min", type=float, default=None, help="Minimum BPM")
@click.option("--bpm-max", type=float, default=None, help="Maximum BPM")
@click.option("--key", "-k", type=str, default=None, help="Musical key (e.g. 'Am', 'C', 'F#m', 'D major')")
@click.option("--instrument", type=str, default=None, help="Instrument name (e.g. 'piano', 'saxophone')")
@click.option("--vocal", is_flag=True, default=False, help="Only vocal tracks")
@click.option("--instrumental", is_flag=True, default=False, help="Only instrumental tracks")
@click.option("--danceable", is_flag=True, default=False, help="Only danceable tracks (danceability >= 0.5)")
@click.option("--limit", "-l", type=int, default=20, help="Number of results")
@click.option("--artist", type=str, default=None, help="Filter by artist name")
@click.option("--genre", type=str, default=None, help="Filter by genre")
def search_features(bpm_min, bpm_max, key, instrument, vocal, instrumental, danceable, limit, artist, genre):
    """Search tracks by audio features (BPM, key, instruments, etc.)."""
    from search import search_by_features

    filters = {}
    if bpm_min:
        filters["bpm_min"] = bpm_min
    if bpm_max:
        filters["bpm_max"] = bpm_max
    if instrument:
        filters["instrument"] = instrument
    if vocal:
        filters["vocal"] = "vocal"
    elif instrumental:
        filters["vocal"] = "instrumental"
    if danceable:
        filters["danceable"] = True
    if artist:
        filters["artist"] = artist
    if genre:
        filters["genre"] = genre

    # Parse key string (e.g. "Am" -> key=A, mode=minor; "C" -> key=C; "F# major" -> key=F#, mode=major)
    if key:
        key_str = key.strip()
        if " " in key_str:
            parts = key_str.split()
            filters["key"] = parts[0]
            filters["mode"] = parts[1].lower()
        elif key_str.endswith("m") and len(key_str) >= 2 and key_str[-2] != "#":
            filters["key"] = key_str[:-1]
            filters["mode"] = "minor"
        else:
            filters["key"] = key_str

    if not filters:
        click.echo("❌ Specify at least one filter (--bpm-min, --key, --instrument, etc.)", err=True)
        sys.exit(1)

    desc_parts = []
    if filters.get("bpm_min") or filters.get("bpm_max"):
        bpm_str = f"{filters.get('bpm_min', '?')}-{filters.get('bpm_max', '?')} BPM"
        desc_parts.append(bpm_str)
    if filters.get("key"):
        k = filters["key"]
        m = filters.get("mode", "")
        desc_parts.append(f"Key: {k} {m}".strip())
    if filters.get("instrument"):
        desc_parts.append(f"Instrument: {filters['instrument']}")
    if filters.get("vocal"):
        desc_parts.append(filters["vocal"])
    if filters.get("danceable"):
        desc_parts.append("danceable")

    click.echo(f"🔍 Feature search: {', '.join(desc_parts)}")

    try:
        with get_db_context() as db:
            result = search_by_features(db, filters=filters, limit=limit)

        click.echo(f"\n{'#':<4} {'BPM':>5} {'Key':>4} {'Mode':>5} {'Vocal':>12} {'Dance':>5}  {'Artist':<25} {'Title':<35}")
        click.echo("-" * 100)

        for i, track in enumerate(result["results"], 1):
            bpm = f"{track.get('bpm', 0):.0f}" if track.get("bpm") else "?"
            k = track.get("key") or "?"
            m = track.get("mode") or "?"
            v = (track.get("vocal_instrumental") or "?")[:12]
            d = f"{track.get('danceability', 0):.2f}" if track.get("danceability") is not None else "?"
            artist_name = (track.get("artist") or "Unknown")[:24]
            title = (track.get("title") or "?")[:34]
            click.echo(f"{i:<4} {bpm:>5} {k:>4} {m:>5} {v:>12} {d:>5}  {artist_name:<25} {title:<35}")

        click.echo(f"\n📊 Found {result['count']} matching tracks")

    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)
        logger.exception("Feature search failed")
        sys.exit(1)


@cli.command()
@click.option("--limit", "-l", type=int, default=None, help="Limit number of tracks to update")
def update_file_dates(limit):
    """Update file_modified_at for existing media files from filesystem."""
    from pathlib import Path
    from datetime import datetime
    from models import MediaFile

    click.echo("🔄 Updating file modification dates from filesystem...")

    try:
        with get_db_context() as db:
            query = db.query(MediaFile).filter(MediaFile.file_modified_at.is_(None))

            if limit:
                query = query.limit(limit)

            files = query.all()
            total = len(files)

            if total == 0:
                click.echo("✅ All files already have file modification dates!")
                return

            click.echo(f"📊 Found {total} files to update")

            updated = 0
            errors = 0

            for mf in files:
                try:
                    file_path = Path(mf.file_path)

                    if file_path.exists():
                        mtime = file_path.stat().st_mtime
                        mf.file_modified_at = datetime.fromtimestamp(mtime)
                        updated += 1

                        if updated % 100 == 0:
                            click.echo(f"   • Updated {updated}/{total} tracks...")
                            db.commit()
                    else:
                        logger.warning(f"File not found: {mf.file_path}")
                        errors += 1

                except Exception as e:
                    logger.error(f"Failed to update media file {mf.id}: {e}")
                    errors += 1

            # Final commit
            db.commit()

            click.echo("\n✅ File dates update complete!")
            click.echo(f"📊 Statistics:")
            click.echo(f"   • Updated: {updated} files")
            click.echo(f"   • Errors: {errors}")

    except Exception as e:
        click.echo(f"\n❌ Error: {e}", err=True)
        logger.exception("File dates update failed")
        sys.exit(1)


@cli.command("fetch-lyrics")
@click.option("--limit", "-l", type=int, default=None, help="Limit number of tracks to process")
@click.option("--no-skip", is_flag=True, help="Re-fetch lyrics even if already attempted")
@click.option("--delay", type=float, default=0.1, help="Delay between requests (seconds)")
def fetch_lyrics(limit, no_skip, delay):
    """Fetch lyrics from LRCLIB for tracks that don't have them yet."""
    from lrclib import LrclibService

    click.echo("🎤 Fetching lyrics from LRCLIB...")
    skip_existing = not no_skip
    click.echo(f"{'⚠️  Re-fetching all tracks' if no_skip else '✓ Skipping tracks already fetched'}")
    if limit:
        click.echo(f"⚠️  Limited to {limit} tracks")
    click.echo(f"⏱️  Rate limit: {delay}s between requests")
    click.echo()

    try:
        with get_db_context() as db:
            # Get tracks to process
            if skip_existing:
                # Skip tracks that already have lyrics or a previous fetch attempt
                query = text("""
                    SELECT t.id as track_id, t.title,
                           a.name as artist,
                           al.title as album,
                           mf.duration_seconds
                    FROM tracks t
                    JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
                    JOIN artists a ON ta.artist_id = a.id
                    JOIN media_files mf ON mf.track_id = t.id
                    JOIN album_variants av ON mf.album_variant_id = av.id
                    JOIN albums al ON av.album_id = al.id
                    WHERE NOT EXISTS (
                        SELECT 1 FROM track_lyrics tl
                        WHERE tl.track_id = t.id AND tl.source = 'lrclib'
                    )
                    AND NOT EXISTS (
                        SELECT 1 FROM external_metadata em
                        WHERE em.entity_type = 'track'
                          AND em.entity_id = t.id::text
                          AND em.source = 'lrclib'
                          AND em.metadata_type = 'lyrics'
                    )
                    GROUP BY t.id, t.title, a.name, al.title, mf.duration_seconds
                    ORDER BY a.name, al.title, t.title
                """)
            else:
                query = text("""
                    SELECT t.id as track_id, t.title,
                           a.name as artist,
                           al.title as album,
                           mf.duration_seconds
                    FROM tracks t
                    JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
                    JOIN artists a ON ta.artist_id = a.id
                    JOIN media_files mf ON mf.track_id = t.id
                    JOIN album_variants av ON mf.album_variant_id = av.id
                    JOIN albums al ON av.album_id = al.id
                    GROUP BY t.id, t.title, a.name, al.title, mf.duration_seconds
                    ORDER BY a.name, al.title, t.title
                """)

            if limit:
                query = text(str(query) + f" LIMIT {limit}")

            tracks = db.execute(query).fetchall()

            if not tracks:
                click.echo("✓ No tracks to process")
                return

            total = len(tracks)
            click.echo(f"Found {total} tracks to process\n")

            stats = {
                "processed": 0,
                "synced": 0,
                "plain_only": 0,
                "instrumental": 0,
                "not_found": 0,
                "errors": 0,
            }

            with LrclibService() as service:
                for i, row in enumerate(tracks, 1):
                    track_id = row.track_id
                    artist = row.artist
                    title = row.title
                    album = row.album
                    duration = int(row.duration_seconds) if row.duration_seconds else None

                    try:
                        result = service.fetch_and_store(
                            db,
                            track_id=track_id,
                            track_name=title,
                            artist_name=artist,
                            album_name=album,
                            duration=duration,
                        )

                        status = result["status"]
                        stats["processed"] += 1

                        if status == "synced":
                            stats["synced"] += 1
                            icon = "🎵"
                        elif status == "plain":
                            stats["plain_only"] += 1
                            icon = "📝"
                        elif status == "instrumental":
                            stats["instrumental"] += 1
                            icon = "🎹"
                        elif status == "not_found":
                            stats["not_found"] += 1
                            icon = "❌"
                        else:
                            stats["errors"] += 1
                            icon = "⚠️"

                        if i % 50 == 0 or i == total:
                            click.echo(
                                f"  [{i}/{total}] {icon} {artist} - {title} → {status}"
                            )

                    except Exception as e:
                        stats["errors"] += 1
                        stats["processed"] += 1
                        logger.error(f"Error processing {artist} - {title}: {e}")
                        if i % 50 == 0:
                            click.echo(f"  [{i}/{total}] ⚠️ {artist} - {title} → error")

                    # Rate limiting
                    if delay > 0:
                        time.sleep(delay)

            click.echo(f"\n✅ LRCLIB lyrics fetch complete!")
            click.echo(f"📊 Statistics:")
            click.echo(f"   • Processed: {stats['processed']} tracks")
            click.echo(f"   • Synced lyrics: {stats['synced']}")
            click.echo(f"   • Plain only: {stats['plain_only']}")
            click.echo(f"   • Instrumental: {stats['instrumental']}")
            click.echo(f"   • Not found: {stats['not_found']}")
            click.echo(f"   • Errors: {stats['errors']}")

    except Exception as e:
        click.echo(f"\n❌ Error: {e}", err=True)
        logger.exception("Lyrics fetch failed")
        sys.exit(1)


@cli.command("generate-lyrics-embeddings")
@click.option("--limit", "-l", type=int, default=None, help="Limit number of tracks to process")
@click.option("--batch-size", "-b", type=int, default=None, help="Override batch size (default from config)")
@click.option("--force", is_flag=True, help="Regenerate embeddings even if they already exist")
def generate_lyrics_embeddings_cmd(limit, batch_size, force):
    """Generate embeddings from track lyrics for semantic lyrics search."""
    from lyrics_embeddings import generate_lyrics_embeddings

    click.echo("🎤 Generating lyrics embeddings...")
    if limit:
        click.echo(f"⚠️  Limited to {limit} tracks")
    if force:
        click.echo(f"🔄 Force regenerating existing embeddings")

    try:
        stats = generate_lyrics_embeddings(
            limit=limit,
            batch_size=batch_size,
            force=force,
        )

        click.echo(f"\n✅ Lyrics embedding generation complete!")
        click.echo(f"📊 Statistics:")
        click.echo(f"   • Processed: {stats['processed']} tracks")
        click.echo(f"   • Success: {stats['success']} tracks")
        click.echo(f"   • Chunks: {stats['chunks']} embeddings")
        click.echo(f"   • Skipped: {stats['skipped']} (empty after processing)")
        click.echo(f"   • Failed: {stats['failed']} tracks")

    except Exception as e:
        click.echo(f"\n❌ Error: {e}", err=True)
        logger.exception("Lyrics embedding generation failed")
        sys.exit(1)


if __name__ == "__main__":
    cli()
