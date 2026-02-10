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

# Configure logging
logging.config.dictConfig(LOGGING_CONFIG)
logger = logging.getLogger(__name__)


@click.group()
def cli():
    """Music AI DJ - AI-powered music library management."""
    pass


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
def generate_embeddings(limit, batch_size):
    """Generate audio embeddings for tracks using CLAP model."""
    from embeddings import generate_embeddings as do_generate

    click.echo("🎵 Starting audio embedding generation...")
    click.echo(f"🖥️  Model: {settings.embedding_model}")
    click.echo(f"📦 Batch size: {batch_size or settings.embedding_batch_size}")

    if limit:
        click.echo(f"⚠️  Limited to {limit} tracks (testing mode)")

    try:
        stats = do_generate(limit=limit, batch_size=batch_size)

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
        from models import Track, Album, TrackArtist, Artist

        with get_db_context() as db:
            tracks = (
                db.query(Track, Album)
                .join(Album, Track.album_id == Album.id)
                .order_by(Track.created_at.desc())
                .limit(limit)
                .all()
            )

            if not tracks:
                click.echo("No tracks in database yet. Run 'scan' first.")
                return

            click.echo(f"\n🎵 Recently added tracks (showing {len(tracks)}):\n")

            for track, album in tracks:
                # Get primary artist via track_artists
                artist_row = (
                    db.query(Artist.name)
                    .join(TrackArtist, TrackArtist.artist_id == Artist.id)
                    .filter(TrackArtist.track_id == track.id, TrackArtist.role == "primary")
                    .first()
                )
                artist_name = artist_row[0] if artist_row else "Unknown"
                duration = f"{int(track.duration_seconds // 60)}:{int(track.duration_seconds % 60):02d}" if track.duration_seconds else "?"
                quality = f"{track.sample_rate//1000}kHz/{track.bit_depth}bit" if track.sample_rate and track.bit_depth else "?"

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
    click.echo(f"   Source: {metadata.get('quality_source')}")
    click.echo(f"   File Size: {metadata.get('file_size_bytes') / (1024*1024):.2f} MB")


@cli.command("search-similar")
@click.option("--track-id", "-t", type=int, required=True, help="Source track ID")
@click.option("--limit", "-l", type=int, default=10, help="Number of results")
@click.option("--min-similarity", type=float, default=None, help="Minimum similarity (0-1)")
@click.option("--artist", type=str, default=None, help="Filter by artist name (partial match)")
@click.option("--genre", type=str, default=None, help="Filter by genre (partial match)")
@click.option("--quality", type=str, default=None, help="Filter by quality source (CD, Vinyl, Hi-Res, MP3)")
def search_similar(track_id, limit, min_similarity, artist, genre, quality):
    """Find tracks similar to a given track by audio similarity."""
    from search import search_similar_tracks

    filters = {}
    if artist:
        filters["artist"] = artist
    if genre:
        filters["genre"] = genre
    if quality:
        filters["quality_source"] = quality

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
        click.echo(f"\n{'#':<4} {'Sim':>5}  {'Artist':<25} {'Title':<35} {'Album':<30} {'Quality':<7}")
        click.echo("-" * 110)

        for i, track in enumerate(result["results"], 1):
            sim = f"{track['similarity']:.2f}" if track["similarity"] else "N/A"
            artist_name = (track["artist"] or "Unknown")[:24]
            title = (track["title"] or "?")[:34]
            album_name = (track["album"] or "?")[:29]
            qs = track.get("quality_source") or "?"
            click.echo(f"{i:<4} {sim:>5}  {artist_name:<25} {title:<35} {album_name:<30} {qs:<7}")

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
@click.option("--quality", type=str, default=None, help="Filter by quality source (CD, Vinyl, Hi-Res, MP3)")
def search_text(query, limit, min_similarity, artist, genre, quality):
    """Search tracks by text description using CLAP text-to-audio similarity."""
    from search import search_by_text

    click.echo(f"🔍 Searching for: \"{query}\"")
    click.echo("⏳ Loading CLAP model for text encoding...")

    filters = {}
    if artist:
        filters["artist"] = artist
    if genre:
        filters["genre"] = genre
    if quality:
        filters["quality_source"] = quality

    try:
        with get_db_context() as db:
            result = search_by_text(
                db, query, limit=limit, min_similarity=min_similarity, filters=filters
            )

        click.echo(f"\n{'#':<4} {'Sim':>5}  {'Artist':<25} {'Title':<35} {'Album':<30} {'Quality':<7}")
        click.echo("-" * 110)

        for i, track in enumerate(result["results"], 1):
            sim = f"{track['similarity']:.2f}" if track["similarity"] else "N/A"
            artist_name = (track["artist"] or "Unknown")[:24]
            title = (track["title"] or "?")[:34]
            album_name = (track["album"] or "?")[:29]
            qs = track.get("quality_source") or "?"
            click.echo(f"{i:<4} {sim:>5}  {artist_name:<25} {title:<35} {album_name:<30} {qs:<7}")

        click.echo(f"\n📊 Found {result['count']} matching tracks")

    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)
        logger.exception("Text search failed")
        sys.exit(1)


@cli.command()
@click.option("--query", "-q", type=str, required=True, help="Natural language question about your music library")
@click.option("--limit", "-l", type=int, default=20, help="Max tracks to retrieve for context")
def ask(query, limit):
    """Ask the AI DJ assistant about your music library."""
    from assistant import ask_assistant

    if not settings.anthropic_api_key:
        click.echo("❌ ANTHROPIC_API_KEY is not configured. Set it in .env file.", err=True)
        sys.exit(1)

    click.echo(f"🎵 Asking AI DJ: \"{query}\"")
    click.echo("⏳ Retrieving tracks and consulting Claude...\n")

    try:
        with get_db_context() as db:
            result = ask_assistant(db, query, limit=limit)

        # Display Claude's response
        click.echo("🤖 AI DJ Response:\n")
        click.echo(result["answer"])

        # Display retrieved tracks table
        tracks = result.get("tracks", [])
        if tracks:
            click.echo(f"\n📋 Retrieved tracks ({result['tracks_retrieved']}):\n")
            click.echo(f"{'#':<4} {'Sim':>5}  {'Artist':<25} {'Title':<35} {'Album':<30} {'Quality':<7}")
            click.echo("-" * 110)

            for i, track in enumerate(tracks, 1):
                sim = f"{track['similarity']:.2f}" if track.get("similarity") else "  -  "
                artist_name = (track.get("artist") or "Unknown")[:24]
                title = (track.get("title") or "?")[:34]
                album_name = (track.get("album") or "?")[:29]
                qs = track.get("quality_source") or "?"
                click.echo(f"{i:<4} {sim:>5}  {artist_name:<25} {title:<35} {album_name:<30} {qs:<7}")

        click.echo(f"\n📊 Model: {result['model']} | Tracks in context: {result['tracks_retrieved']}")

    except Exception as e:
        click.echo(f"\n❌ Error: {e}", err=True)
        logger.exception("Ask failed")
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
                            a.id,
                            a.title,
                            STRING_AGG(DISTINCT ar.name, ', ') as artist_names
                        FROM albums a
                        JOIN tracks t ON a.id = t.album_id
                        JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
                        JOIN artists ar ON ta.artist_id = ar.id
                        WHERE a.title ILIKE :title
                        GROUP BY a.id, a.title
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
                            a.id,
                            a.title,
                            STRING_AGG(DISTINCT ar.name, ', ') as artist_names
                        FROM albums a
                        JOIN tracks t ON a.id = t.album_id
                        JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
                        JOIN artists ar ON ta.artist_id = ar.id
                        WHERE NOT EXISTS (
                            SELECT 1 FROM album_info ai
                            WHERE ai.album_id = a.id AND ai.source = 'lastfm'
                        )
                        GROUP BY a.id, a.title
                        ORDER BY a.title
                    """)
                else:
                    query = text("""
                        SELECT DISTINCT
                            a.id,
                            a.title,
                            STRING_AGG(DISTINCT ar.name, ', ') as artist_names
                        FROM albums a
                        JOIN tracks t ON a.id = t.album_id
                        JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
                        JOIN artists ar ON ta.artist_id = ar.id
                        GROUP BY a.id, a.title
                        ORDER BY a.title
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


@cli.command("enrich-tracks")
@click.option("--limit", "-l", type=int, default=None, help="Limit number of tracks to enrich")
@click.option("--artist", "-a", type=str, default=None, help="Enrich tracks by specific artist")
@click.option("--album", "-al", type=str, default=None, help="Enrich tracks from specific album")
@click.option("--no-skip", is_flag=True, help="Re-fetch data that already exists")
@click.option("--delay", type=float, default=0.2, help="Delay between requests (seconds)")
def enrich_tracks(limit, artist, album, no_skip, delay):
    """Enrich tracks with Last.fm statistics (listeners, playcount)."""
    from lastfm import LastFmService

    if not settings.lastfm_api_key:
        click.echo("❌ LASTFM_API_KEY is not configured. Set it in .env file.", err=True)
        sys.exit(1)

    try:
        service = LastFmService()

        with get_db_context() as db:
            # Build query based on filters
            query = text("""
                SELECT DISTINCT
                    t.id,
                    t.title,
                    STRING_AGG(DISTINCT ar.name, ', ' ORDER BY ar.name) as artist_names,
                    al.title as album_title
                FROM tracks t
                JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
                JOIN artists ar ON ta.artist_id = ar.id
                JOIN albums al ON t.album_id = al.id
                WHERE 1=1
            """)

            params = {}

            # Filter by artist if specified
            if artist:
                query = text(str(query).replace("WHERE 1=1", "WHERE ar.name ILIKE :artist_name"))
                params["artist_name"] = f"%{artist}%"

            # Filter by album if specified
            if album:
                if "WHERE ar.name" in str(query):
                    query = text(str(query) + " AND al.title ILIKE :album_title")
                else:
                    query = text(str(query).replace("WHERE 1=1", "WHERE al.title ILIKE :album_title"))
                params["album_title"] = f"%{album}%"

            # Skip already enriched unless --no-skip
            if not no_skip:
                query = text(str(query) + """
                    AND NOT EXISTS (
                        SELECT 1 FROM track_stats ts
                        WHERE ts.track_id = t.id AND ts.source = 'lastfm'
                    )
                """)

            query = text(str(query) + """
                GROUP BY t.id, t.title, al.title
                ORDER BY t.title
            """)

            if limit:
                query = text(str(query) + f" LIMIT {limit}")

            tracks = db.execute(query, params).fetchall()

            if not tracks:
                click.echo("✓ No tracks to enrich")
                return

            click.echo(f"Found {len(tracks)} tracks to enrich\n")

            stats = {"processed": 0, "success": 0, "not_found": 0, "errors": 0}

            for track_id, track_title, artist_names, album_title in tracks:
                # Take first artist if multiple
                artist_name = artist_names.split(', ')[0]

                result = service.enrich_track(db, track_id, artist_name, track_title)

                stats["processed"] += 1

                if result["status"] == "success":
                    stats["success"] += 1
                    listeners = result.get("listeners", 0)
                    playcount = result.get("playcount", 0)
                    click.echo(f"✓ {artist_name} - {track_title}: {listeners:,} listeners, {playcount:,} plays")
                elif result["status"] == "not_found":
                    stats["not_found"] += 1
                    click.echo(f"⚠ {artist_name} - {track_title}: not found")
                elif result["status"] == "error":
                    stats["errors"] += 1
                    click.echo(f"✗ {artist_name} - {track_title}: {result.get('error', 'unknown error')}")

                # Rate limiting
                if delay > 0:
                    time.sleep(delay)

            click.echo("\n✅ Last.fm track enrichment complete!")
            click.echo(f"📊 Statistics:")
            click.echo(f"   • Processed: {stats['processed']} tracks")
            click.echo(f"   • Success: {stats['success']}")
            click.echo(f"   • Not found: {stats['not_found']}")
            click.echo(f"   • Errors: {stats['errors']}")

    except Exception as e:
        click.echo(f"\n❌ Error: {e}", err=True)
        logger.exception("Track enrichment failed")
        sys.exit(1)


if __name__ == "__main__":
    cli()
