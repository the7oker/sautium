"""
Command-line interface for Music AI DJ.
"""

import logging
import logging.config
import sys

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


if __name__ == "__main__":
    cli()
