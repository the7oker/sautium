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
def scan(limit, no_skip):
    """Scan music library and import metadata to database."""
    click.echo(f"🎵 Starting library scan...")
    click.echo(f"📁 Library path: {settings.music_library_path}")

    if limit:
        click.echo(f"⚠️  Limited to {limit} files (testing mode)")

    if no_skip:
        click.echo(f"⚠️  Re-scanning all files (not skipping existing)")

    try:
        stats = scan_library(limit=limit, skip_existing=not no_skip)

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
        from models import Track, Album, Artist

        with get_db_context() as db:
            tracks = (
                db.query(Track, Album, Artist)
                .join(Album, Track.album_id == Album.id)
                .join(Artist, Album.artist_id == Artist.id)
                .order_by(Track.created_at.desc())
                .limit(limit)
                .all()
            )

            if not tracks:
                click.echo("No tracks in database yet. Run 'scan' first.")
                return

            click.echo(f"\n🎵 Recently added tracks (showing {len(tracks)}):\n")

            for track, album, artist in tracks:
                duration = f"{int(track.duration_seconds // 60)}:{int(track.duration_seconds % 60):02d}" if track.duration_seconds else "?"
                quality = f"{track.sample_rate//1000}kHz/{track.bit_depth}bit" if track.sample_rate and track.bit_depth else "?"

                click.echo(f"   • {artist.name} - {track.title}")
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


if __name__ == "__main__":
    cli()
