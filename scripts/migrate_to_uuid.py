#!/usr/bin/env python3
"""
Migration script: old Track-centric → new Track(UUID)/MediaFile canonical schema.

Migrates the existing database in 4 phases:
  A - Create new tables with temporary names (songs/song_artists/song_genres — non-breaking)
  B - Populate new tables from existing data
  C - Switch to UUID PKs for artists and albums
  D - Cleanup legacy tables, rename to final names (tracks/track_artists/track_genres/track_id)

Pre-migration: creates a pg_dump backup.
Post-migration: verification queries.

Usage:
    python scripts/migrate_to_uuid.py [--db-url postgresql://...]
    python scripts/migrate_to_uuid.py --phase A   # run only Phase A
    python scripts/migrate_to_uuid.py --phase B
    python scripts/migrate_to_uuid.py --phase C
    python scripts/migrate_to_uuid.py --phase D
"""

import argparse
import logging
import os
import re
import subprocess
import sys
import unicodedata
import uuid
from datetime import datetime

import psycopg2
import psycopg2.extras

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# --- UUID generation (same as backend/uuid_utils.py) ---

NAMESPACE = uuid.UUID('5ba7a9d0-1f8c-4c3d-9e7a-2b4f6c8d0e1f')

def _normalize(text: str) -> str:
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFC', text.strip().lower()))

def _artist_uuid(name: str) -> str:
    return str(uuid.uuid5(NAMESPACE, f"artist:{_normalize(name)}"))

def _song_uuid(title: str, artist_name: str) -> str:
    return str(uuid.uuid5(NAMESPACE, f"song:{_normalize(artist_name)}:{_normalize(title)}"))

def _album_uuid(title: str, artist_name: str) -> str:
    return str(uuid.uuid5(NAMESPACE, f"album:{_normalize(artist_name)}:{_normalize(title)}"))

LOSSLESS_FORMATS = {'flac', 'ape', 'alac', 'wav', 'aiff', 'wv', 'tta', 'dsf', 'dff'}

def _is_lossless_format(fmt: str) -> bool:
    return fmt.lower().strip('.') in LOSSLESS_FORMATS

def _is_lossless_quality(quality_source: str) -> bool:
    return quality_source in ('CD', 'Vinyl', 'Hi-Res')


def _verify_count(cur, table, expected_min=None, label=""):
    cur.execute(f"SELECT COUNT(*) FROM {table}")
    count = cur.fetchone()[0]
    status = "OK" if expected_min is None or count >= expected_min else "MISMATCH"
    logger.info(f"  [{status}] {label or table}: {count} rows" +
                (f" (expected >= {expected_min})" if expected_min else ""))
    return count


# ═══════════════════════════════════════════════════════════════════════════
# Phase A: Add new tables (non-breaking)
# ═══════════════════════════════════════════════════════════════════════════

def phase_a(cur):
    logger.info("=" * 60)
    logger.info("PHASE A: Create new tables")
    logger.info("=" * 60)

    cur.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"')

    # 1. songs table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS songs (
            id UUID PRIMARY KEY,
            title VARCHAR(500) NOT NULL,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_songs_title ON songs(title)")
    logger.info("  Created: songs")

    # 2. song_artists (temporarily INT FK to existing artists)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS song_artists (
            song_id UUID NOT NULL REFERENCES songs(id) ON DELETE CASCADE,
            artist_id INTEGER NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
            role VARCHAR(50) NOT NULL DEFAULT 'primary',
            PRIMARY KEY (song_id, artist_id, role)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_song_artists_song_id ON song_artists(song_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_song_artists_artist_id ON song_artists(artist_id)")
    logger.info("  Created: song_artists")

    # 3. song_genres
    cur.execute("""
        CREATE TABLE IF NOT EXISTS song_genres (
            song_id UUID NOT NULL REFERENCES songs(id) ON DELETE CASCADE,
            genre_id INTEGER NOT NULL REFERENCES genres(id) ON DELETE CASCADE,
            PRIMARY KEY (song_id, genre_id)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_song_genres_song_id ON song_genres(song_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_song_genres_genre_id ON song_genres(genre_id)")
    logger.info("  Created: song_genres")

    # 4. album_artists
    cur.execute("""
        CREATE TABLE IF NOT EXISTS album_artists (
            album_id INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
            artist_id INTEGER NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
            role VARCHAR(50) NOT NULL DEFAULT 'primary',
            PRIMARY KEY (album_id, artist_id, role)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_album_artists_album_id ON album_artists(album_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_album_artists_artist_id ON album_artists(artist_id)")
    logger.info("  Created: album_artists")

    # 5. album_variants
    cur.execute("""
        CREATE TABLE IF NOT EXISTS album_variants (
            id SERIAL PRIMARY KEY,
            album_id INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
            directory_path TEXT NOT NULL UNIQUE,
            sample_rate INTEGER,
            bit_depth INTEGER,
            is_lossless BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_album_variants_album_id ON album_variants(album_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_album_variants_directory ON album_variants(directory_path)")
    logger.info("  Created: album_variants")

    # 6. media_files
    cur.execute("""
        CREATE TABLE IF NOT EXISTS media_files (
            id SERIAL PRIMARY KEY,
            song_id UUID NOT NULL REFERENCES songs(id) ON DELETE CASCADE,
            album_variant_id INTEGER NOT NULL REFERENCES album_variants(id) ON DELETE CASCADE,
            file_path TEXT NOT NULL UNIQUE,
            file_format VARCHAR(10) DEFAULT 'FLAC',
            is_lossless BOOLEAN DEFAULT TRUE,
            sample_rate INTEGER,
            bit_depth INTEGER,
            bitrate INTEGER,
            channels INTEGER,
            duration_seconds NUMERIC(10, 2),
            file_size_bytes BIGINT,
            file_modified_at TIMESTAMP,
            track_number INTEGER,
            disc_number INTEGER DEFAULT 1,
            is_analysis_source BOOLEAN DEFAULT FALSE,
            play_count INTEGER DEFAULT 0,
            last_played_at TIMESTAMP,
            isrc VARCHAR(20),
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_media_files_song_id ON media_files(song_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_media_files_album_variant_id ON media_files(album_variant_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_media_files_file_path ON media_files(file_path)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_media_files_play_count ON media_files(play_count)")
    logger.info("  Created: media_files")

    # 7. Add song_id column to embeddings, text_embeddings, audio_features
    for table in ('embeddings', 'text_embeddings', 'audio_features'):
        cur.execute(f"""
            DO $$ BEGIN
                ALTER TABLE {table} ADD COLUMN IF NOT EXISTS song_id UUID;
            EXCEPTION WHEN others THEN NULL;
            END $$
        """)
    logger.info("  Added song_id columns to embeddings, text_embeddings, audio_features")

    # Add source quality columns to embeddings and audio_features
    for table in ('embeddings', 'audio_features'):
        for col in ('source_bit_depth INTEGER', 'source_sample_rate INTEGER', 'source_is_lossless BOOLEAN'):
            col_name = col.split()[0]
            cur.execute(f"""
                DO $$ BEGIN
                    ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col};
                EXCEPTION WHEN others THEN NULL;
                END $$
            """)
    logger.info("  Added source quality columns")

    # 8. Add media_file_id, song_id to listening_history
    cur.execute("""
        DO $$ BEGIN
            ALTER TABLE listening_history ADD COLUMN IF NOT EXISTS media_file_id INTEGER;
            ALTER TABLE listening_history ADD COLUMN IF NOT EXISTS song_id UUID;
        EXCEPTION WHEN others THEN NULL;
        END $$
    """)
    logger.info("  Added media_file_id, song_id to listening_history")

    # Create song_stats table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS song_stats (
            id SERIAL PRIMARY KEY,
            song_id UUID NOT NULL,
            source VARCHAR(50) NOT NULL,
            listeners INTEGER,
            playcount BIGINT,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(song_id, source),
            CHECK(listeners IS NOT NULL OR playcount IS NOT NULL)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_song_stats_song ON song_stats(song_id)")
    logger.info("  Created: song_stats")

    logger.info("Phase A complete.")


# ═══════════════════════════════════════════════════════════════════════════
# Phase B: Populate new tables from existing data
# ═══════════════════════════════════════════════════════════════════════════

def phase_b(cur):
    logger.info("=" * 60)
    logger.info("PHASE B: Populate new tables from existing data")
    logger.info("=" * 60)

    # Count existing data
    cur.execute("SELECT COUNT(*) FROM tracks")
    total_tracks = cur.fetchone()[0]
    logger.info(f"  Existing tracks: {total_tracks}")

    # 9. Generate songs from unique (title, primary_artist) pairs
    logger.info("  Generating songs...")
    cur.execute("""
        SELECT DISTINCT t.title, a.name as artist_name
        FROM tracks t
        JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
        JOIN artists a ON ta.artist_id = a.id
    """)
    unique_songs = cur.fetchall()
    logger.info(f"  Found {len(unique_songs)} unique (title, artist) pairs")

    batch = []
    for title, artist_name in unique_songs:
        sid = _song_uuid(title, artist_name)
        batch.append((sid, title))

    # Bulk insert songs (skip duplicates)
    psycopg2.extras.execute_values(
        cur,
        "INSERT INTO songs (id, title) VALUES %s ON CONFLICT (id) DO NOTHING",
        batch,
        template="(%s, %s)",
        page_size=5000,
    )
    _verify_count(cur, "songs", len(unique_songs), "songs")

    # 10. Populate song_artists
    logger.info("  Populating song_artists...")
    cur.execute("""
        SELECT DISTINCT t.title, a.name as artist_name, ta.artist_id, ta.role
        FROM tracks t
        JOIN track_artists ta ON t.id = ta.track_id
        JOIN artists a ON ta.artist_id = a.id
    """)
    ta_rows = cur.fetchall()

    # Build song_id lookup
    song_id_map = {}  # (title_lower, artist_lower) -> song_uuid
    for title, artist_name in unique_songs:
        song_id_map[(_normalize(title), _normalize(artist_name))] = _song_uuid(title, artist_name)

    # We need to map each (title, artist_name) to the song with its primary artist
    # First get primary artist for each track
    cur.execute("""
        SELECT t.id, t.title, a.name as primary_artist
        FROM tracks t
        JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
        JOIN artists a ON ta.artist_id = a.id
    """)
    track_primary = {}  # track.title -> primary_artist (might differ)
    # Actually, we need track_id -> (title, primary_artist)
    track_to_song = {}  # track_id -> song_uuid
    for track_id, title, primary_artist in cur.fetchall():
        sid = _song_uuid(title, primary_artist)
        track_to_song[track_id] = sid

    # Now populate song_artists from track_artists
    cur.execute("""
        SELECT ta.track_id, ta.artist_id, ta.role
        FROM track_artists ta
    """)
    sa_batch = set()
    for track_id, artist_id, role in cur.fetchall():
        sid = track_to_song.get(track_id)
        if sid:
            sa_batch.add((sid, artist_id, role))

    psycopg2.extras.execute_values(
        cur,
        "INSERT INTO song_artists (song_id, artist_id, role) VALUES %s ON CONFLICT DO NOTHING",
        list(sa_batch),
        template="(%s, %s, %s)",
        page_size=5000,
    )
    _verify_count(cur, "song_artists", 0, "song_artists")

    # 11. Populate song_genres
    logger.info("  Populating song_genres...")
    cur.execute("""
        SELECT tg.track_id, tg.genre_id
        FROM track_genres tg
    """)
    sg_batch = set()
    for track_id, genre_id in cur.fetchall():
        sid = track_to_song.get(track_id)
        if sid:
            sg_batch.add((sid, genre_id))

    psycopg2.extras.execute_values(
        cur,
        "INSERT INTO song_genres (song_id, genre_id) VALUES %s ON CONFLICT DO NOTHING",
        list(sg_batch),
        template="(%s, %s)",
        page_size=5000,
    )
    _verify_count(cur, "song_genres", 0, "song_genres")

    # 12. Create album_variants from albums (1:1)
    logger.info("  Creating album_variants...")
    cur.execute("""
        SELECT id, directory_path, sample_rate, bit_depth, quality_source
        FROM albums
    """)
    av_batch = []
    album_to_variant = {}  # album.id -> variant.id (will fill after insert)
    for album_id, dir_path, sr, bd, qs in cur.fetchall():
        lossless = _is_lossless_quality(qs) if qs else True
        av_batch.append((album_id, dir_path, sr, bd, lossless))

    psycopg2.extras.execute_values(
        cur,
        """INSERT INTO album_variants (album_id, directory_path, sample_rate, bit_depth, is_lossless)
           VALUES %s ON CONFLICT (directory_path) DO NOTHING""",
        av_batch,
        template="(%s, %s, %s, %s, %s)",
        page_size=5000,
    )
    # Build album_to_variant mapping
    cur.execute("SELECT id, album_id, directory_path FROM album_variants")
    dir_to_variant = {}
    for vid, aid, dpath in cur.fetchall():
        dir_to_variant[dpath] = vid
        album_to_variant[aid] = vid

    _verify_count(cur, "album_variants", 0, "album_variants")

    # 12b. Populate album_artists from track_artists (deduplicated)
    logger.info("  Populating album_artists...")
    cur.execute("""
        SELECT DISTINCT t.album_id, ta.artist_id, ta.role
        FROM tracks t
        JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
    """)
    aa_batch = set()
    for album_id, artist_id, role in cur.fetchall():
        aa_batch.add((album_id, artist_id, role))

    psycopg2.extras.execute_values(
        cur,
        "INSERT INTO album_artists (album_id, artist_id, role) VALUES %s ON CONFLICT DO NOTHING",
        list(aa_batch),
        template="(%s, %s, %s)",
        page_size=5000,
    )
    _verify_count(cur, "album_artists", 0, "album_artists")

    # 13. Create media_files from tracks
    logger.info("  Creating media_files...")
    cur.execute("""
        SELECT t.id, t.title, t.album_id, t.file_path, t.file_format,
               t.sample_rate, t.bit_depth, t.bitrate, t.channels,
               t.duration_seconds, t.file_size_bytes, t.file_modified_at,
               t.track_number, t.disc_number, t.play_count, t.last_played_at,
               t.isrc, al.directory_path
        FROM tracks t
        JOIN albums al ON t.album_id = al.id
    """)

    mf_batch = []
    track_to_mf = {}  # will map track_id -> media_file data for later

    for row in cur.fetchall():
        (track_id, title, album_id, file_path, file_format,
         sr, bd, bitrate, channels, duration, file_size, file_modified,
         track_num, disc_num, play_count, last_played, isrc, dir_path) = row

        song_id = track_to_song.get(track_id)
        if not song_id:
            logger.warning(f"  Track {track_id} has no song mapping, skipping")
            continue

        variant_id = dir_to_variant.get(dir_path)
        if not variant_id:
            logger.warning(f"  Track {track_id} has no variant for dir {dir_path}, skipping")
            continue

        lossless = _is_lossless_format(file_format) if file_format else True

        mf_batch.append((
            song_id, variant_id, file_path, file_format, lossless,
            sr, bd, bitrate, channels, duration, file_size, file_modified,
            track_num, disc_num, play_count or 0, last_played, isrc
        ))
        track_to_mf[track_id] = {
            'song_id': song_id, 'file_path': file_path,
            'bit_depth': bd, 'is_lossless': lossless, 'sample_rate': sr,
        }

    psycopg2.extras.execute_values(
        cur,
        """INSERT INTO media_files
           (song_id, album_variant_id, file_path, file_format, is_lossless,
            sample_rate, bit_depth, bitrate, channels, duration_seconds,
            file_size_bytes, file_modified_at, track_number, disc_number,
            play_count, last_played_at, isrc)
           VALUES %s ON CONFLICT (file_path) DO NOTHING""",
        mf_batch,
        template="(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        page_size=5000,
    )
    _verify_count(cur, "media_files", total_tracks, "media_files")

    # 14. Set is_analysis_source on best file per song
    logger.info("  Setting is_analysis_source flags...")
    cur.execute("""
        WITH ranked AS (
            SELECT id, song_id,
                   ROW_NUMBER() OVER (
                       PARTITION BY song_id
                       ORDER BY
                           (bit_depth = 16 AND is_lossless) DESC,
                           is_lossless DESC,
                           id
                   ) as rn
            FROM media_files
        )
        UPDATE media_files SET is_analysis_source = TRUE
        WHERE id IN (SELECT id FROM ranked WHERE rn = 1)
    """)
    cur.execute("SELECT COUNT(*) FROM media_files WHERE is_analysis_source = TRUE")
    analysis_count = cur.fetchone()[0]
    logger.info(f"  Analysis sources set: {analysis_count}")

    # Build file_path -> media_file.id mapping
    cur.execute("SELECT id, file_path, song_id FROM media_files")
    filepath_to_mf_id = {}
    song_to_mf_id = {}  # song_id -> first media_file.id
    for mf_id, fp, sid in cur.fetchall():
        filepath_to_mf_id[fp] = mf_id
        if sid not in song_to_mf_id:
            song_to_mf_id[str(sid)] = mf_id

    # Build track_id -> media_file_id mapping
    track_id_to_mf_id = {}
    for track_id, info in track_to_mf.items():
        mf_id = filepath_to_mf_id.get(info['file_path'])
        if mf_id:
            track_id_to_mf_id[track_id] = mf_id

    # 15. Update embeddings.song_id, text_embeddings.song_id, audio_features.song_id
    logger.info("  Updating embeddings.song_id...")
    # Set song_id using Python-computed track_to_song mapping
    cur.execute("SELECT id, track_id FROM embeddings WHERE song_id IS NULL AND track_id IS NOT NULL")
    emb_updates = []
    for emb_id, track_id in cur.fetchall():
        sid = track_to_song.get(track_id)
        if sid:
            emb_updates.append((sid, emb_id))
    if emb_updates:
        psycopg2.extras.execute_batch(
            cur,
            "UPDATE embeddings SET song_id = %s WHERE id = %s",
            emb_updates,
            page_size=5000,
        )

    # Also set source quality info on embeddings from track data
    cur.execute("""
        UPDATE embeddings e
        SET source_bit_depth = mf.bit_depth,
            source_sample_rate = mf.sample_rate,
            source_is_lossless = mf.is_lossless
        FROM media_files mf
        WHERE mf.song_id = e.song_id AND mf.is_analysis_source = TRUE
          AND e.source_bit_depth IS NULL
    """)

    cur.execute("SELECT COUNT(*) FROM embeddings WHERE song_id IS NOT NULL")
    logger.info(f"  Embeddings with song_id: {cur.fetchone()[0]}")

    logger.info("  Updating text_embeddings.song_id...")
    cur.execute("SELECT id, track_id FROM text_embeddings WHERE song_id IS NULL AND track_id IS NOT NULL")
    te_updates = []
    for te_id, track_id in cur.fetchall():
        sid = track_to_song.get(track_id)
        if sid:
            te_updates.append((sid, te_id))
    if te_updates:
        psycopg2.extras.execute_batch(
            cur,
            "UPDATE text_embeddings SET song_id = %s WHERE id = %s",
            te_updates,
            page_size=5000,
        )

    logger.info("  Updating audio_features.song_id...")
    cur.execute("SELECT id, track_id FROM audio_features WHERE song_id IS NULL AND track_id IS NOT NULL")
    af_updates = []
    for af_id, track_id in cur.fetchall():
        sid = track_to_song.get(track_id)
        if sid:
            af_updates.append((sid, af_id))
    if af_updates:
        psycopg2.extras.execute_batch(
            cur,
            "UPDATE audio_features SET song_id = %s WHERE id = %s",
            af_updates,
            page_size=5000,
        )

    # Set source quality info on audio_features
    cur.execute("""
        UPDATE audio_features af
        SET source_bit_depth = mf.bit_depth,
            source_sample_rate = mf.sample_rate,
            source_is_lossless = mf.is_lossless
        FROM media_files mf
        WHERE mf.song_id = af.song_id AND mf.is_analysis_source = TRUE
          AND af.source_bit_depth IS NULL
    """)

    # 16. Deduplicate: for songs with multiple embeddings, keep the one from analysis_source
    logger.info("  Deduplicating embeddings...")
    cur.execute("""
        WITH dupes AS (
            SELECT e.id, e.song_id, e.track_id,
                   ROW_NUMBER() OVER (
                       PARTITION BY e.song_id
                       ORDER BY
                           (mf.is_analysis_source IS TRUE) DESC NULLS LAST,
                           e.id
                   ) as rn
            FROM embeddings e
            LEFT JOIN media_files mf ON mf.file_path = (
                SELECT t.file_path FROM tracks t WHERE t.id = e.track_id
            )
            WHERE e.song_id IS NOT NULL
        )
        DELETE FROM embeddings WHERE id IN (
            SELECT id FROM dupes WHERE rn > 1
        )
    """)
    logger.info(f"  Deduplicated embeddings (deleted {cur.rowcount} duplicates)")

    # Same for text_embeddings
    cur.execute("""
        WITH dupes AS (
            SELECT id, song_id,
                   ROW_NUMBER() OVER (PARTITION BY song_id ORDER BY id) as rn
            FROM text_embeddings
            WHERE song_id IS NOT NULL
        )
        DELETE FROM text_embeddings WHERE id IN (
            SELECT id FROM dupes WHERE rn > 1
        )
    """)
    logger.info(f"  Deduplicated text_embeddings (deleted {cur.rowcount} duplicates)")

    # Same for audio_features
    cur.execute("""
        WITH dupes AS (
            SELECT id, song_id,
                   ROW_NUMBER() OVER (PARTITION BY song_id ORDER BY id) as rn
            FROM audio_features
            WHERE song_id IS NOT NULL
        )
        DELETE FROM audio_features WHERE id IN (
            SELECT id FROM dupes WHERE rn > 1
        )
    """)
    logger.info(f"  Deduplicated audio_features (deleted {cur.rowcount} duplicates)")

    # 17. Update listening_history
    logger.info("  Updating listening_history...")
    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'listening_history' AND column_name = 'track_id'
    """)
    if cur.fetchone():
        cur.execute("""
            UPDATE listening_history lh
            SET song_id = sub.song_id,
                media_file_id = sub.mf_id
            FROM (
                SELECT t.id as track_id,
                       mf.song_id,
                       mf.id as mf_id
                FROM tracks t
                JOIN media_files mf ON mf.file_path = t.file_path
            ) sub
            WHERE lh.track_id = sub.track_id AND lh.song_id IS NULL
        """)
        logger.info(f"  Updated {cur.rowcount} listening_history rows")

    # Migrate track_stats → song_stats
    logger.info("  Migrating track_stats → song_stats...")
    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'track_stats' AND column_name = 'track_id'
    """)
    if cur.fetchone():
        cur.execute("""
            INSERT INTO song_stats (song_id, source, listeners, playcount)
            SELECT DISTINCT mf.song_id, ts.source, ts.listeners, ts.playcount
            FROM track_stats ts
            JOIN tracks t ON ts.track_id = t.id
            JOIN media_files mf ON mf.file_path = t.file_path
            ON CONFLICT (song_id, source) DO NOTHING
        """)
        logger.info(f"  Migrated {cur.rowcount} song_stats rows")

    logger.info("Phase B complete.")


# ═══════════════════════════════════════════════════════════════════════════
# Phase C: Switch to UUID PKs for artists and albums
# ═══════════════════════════════════════════════════════════════════════════

def phase_c(cur):
    logger.info("=" * 60)
    logger.info("PHASE C: Switch artists and albums to UUID PKs")
    logger.info("=" * 60)

    # 18. Add uuid column to artists, populate
    logger.info("  Adding UUID column to artists...")
    cur.execute("ALTER TABLE artists ADD COLUMN IF NOT EXISTS uuid UUID")
    cur.execute("SELECT id, name FROM artists")
    artist_rows = cur.fetchall()
    artist_id_to_uuid = {}
    for old_id, name in artist_rows:
        u = _artist_uuid(name)
        artist_id_to_uuid[old_id] = u

    if artist_rows:
        psycopg2.extras.execute_batch(
            cur,
            "UPDATE artists SET uuid = %s WHERE id = %s",
            [(u, old_id) for old_id, u in zip(
                [r[0] for r in artist_rows],
                [artist_id_to_uuid[r[0]] for r in artist_rows]
            )],
            page_size=5000,
        )
    logger.info(f"  Set UUIDs for {len(artist_rows)} artists")

    # 19. Add uuid column to albums, populate
    logger.info("  Adding UUID column to albums...")
    cur.execute("ALTER TABLE albums ADD COLUMN IF NOT EXISTS uuid UUID")

    # Get primary artist for each album
    cur.execute("""
        SELECT DISTINCT al.id, al.title, a.name as artist_name
        FROM albums al
        JOIN album_artists aa ON al.id = aa.album_id AND aa.role = 'primary'
        JOIN artists a ON aa.artist_id = a.id
    """)
    album_rows = cur.fetchall()
    album_id_to_uuid = {}
    for old_id, title, artist_name in album_rows:
        u = _album_uuid(title, artist_name)
        album_id_to_uuid[old_id] = u

    # For albums without album_artists, try via tracks
    cur.execute("""
        SELECT al.id, al.title, a.name as artist_name
        FROM albums al
        LEFT JOIN album_artists aa ON al.id = aa.album_id
        JOIN tracks t ON t.album_id = al.id
        JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
        JOIN artists a ON ta.artist_id = a.id
        WHERE aa.album_id IS NULL
        GROUP BY al.id, al.title, a.name
    """)
    for old_id, title, artist_name in cur.fetchall():
        if old_id not in album_id_to_uuid:
            album_id_to_uuid[old_id] = _album_uuid(title, artist_name)

    if album_id_to_uuid:
        psycopg2.extras.execute_batch(
            cur,
            "UPDATE albums SET uuid = %s WHERE id = %s",
            [(u, old_id) for old_id, u in album_id_to_uuid.items()],
            page_size=5000,
        )
    logger.info(f"  Set UUIDs for {len(album_id_to_uuid)} albums")

    # 20-22. Create new tables with UUID PKs and migrate references
    # Strategy: rename old tables, create new, copy data, update FKs

    # --- Artists ---
    logger.info("  Migrating artists to UUID PK...")
    cur.execute("ALTER TABLE artists RENAME TO artists_legacy")
    # Drop old indexes that would conflict with new table's indexes
    cur.execute("DROP INDEX IF EXISTS idx_artists_name")
    cur.execute("DROP INDEX IF EXISTS idx_artists_name_trgm")
    cur.execute("""
        CREATE TABLE artists (
            id UUID PRIMARY KEY,
            name VARCHAR(500) NOT NULL UNIQUE,
            lastfm_id VARCHAR(100),
            musicbrainz_id VARCHAR(100),
            country VARCHAR(100),
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("CREATE INDEX idx_artists_name ON artists(name)")
    cur.execute("CREATE INDEX idx_artists_name_trgm ON artists USING gin (name gin_trgm_ops)")
    cur.execute("""
        INSERT INTO artists (id, name, lastfm_id, musicbrainz_id, country, created_at, updated_at)
        SELECT DISTINCT ON (uuid) uuid, name, lastfm_id, musicbrainz_id, country, created_at, updated_at
        FROM artists_legacy
        WHERE uuid IS NOT NULL
        ORDER BY uuid, id
    """)
    _verify_count(cur, "artists", 0, "artists (UUID)")

    # Migrate artist_bios FK
    logger.info("  Migrating artist_bios FK...")
    cur.execute("ALTER TABLE artist_bios ADD COLUMN IF NOT EXISTS artist_uuid UUID")
    cur.execute("""
        UPDATE artist_bios ab SET artist_uuid = al.uuid
        FROM artists_legacy al WHERE ab.artist_id = al.id
    """)
    cur.execute("ALTER TABLE artist_bios DROP CONSTRAINT IF EXISTS artist_bios_artist_id_fkey")
    cur.execute("ALTER TABLE artist_bios DROP COLUMN IF EXISTS artist_id")
    cur.execute("ALTER TABLE artist_bios RENAME COLUMN artist_uuid TO artist_id")
    cur.execute("ALTER TABLE artist_bios ADD CONSTRAINT artist_bios_artist_id_fkey FOREIGN KEY (artist_id) REFERENCES artists(id) ON DELETE CASCADE")

    # Migrate artist_tags FK
    logger.info("  Migrating artist_tags FK...")
    cur.execute("ALTER TABLE artist_tags ADD COLUMN IF NOT EXISTS artist_uuid UUID")
    cur.execute("""
        UPDATE artist_tags at2 SET artist_uuid = al.uuid
        FROM artists_legacy al WHERE at2.artist_id = al.id
    """)
    cur.execute("ALTER TABLE artist_tags DROP CONSTRAINT IF EXISTS uq_artist_tags")
    cur.execute("ALTER TABLE artist_tags DROP CONSTRAINT IF EXISTS artist_tags_artist_id_fkey")
    cur.execute("ALTER TABLE artist_tags DROP COLUMN IF EXISTS artist_id")
    cur.execute("ALTER TABLE artist_tags RENAME COLUMN artist_uuid TO artist_id")
    # Deduplicate before adding constraints
    cur.execute("""
        DELETE FROM artist_tags at1 USING artist_tags at2
        WHERE at1.ctid > at2.ctid AND at1.artist_id = at2.artist_id AND at1.tag_id = at2.tag_id AND at1.source = at2.source
    """)
    cur.execute("ALTER TABLE artist_tags ADD CONSTRAINT artist_tags_artist_id_fkey FOREIGN KEY (artist_id) REFERENCES artists(id) ON DELETE CASCADE")
    cur.execute("ALTER TABLE artist_tags ADD CONSTRAINT uq_artist_tags UNIQUE (artist_id, tag_id, source)")

    # Migrate similar_artists FKs
    logger.info("  Migrating similar_artists FK...")
    cur.execute("ALTER TABLE similar_artists ADD COLUMN IF NOT EXISTS artist_uuid UUID")
    cur.execute("ALTER TABLE similar_artists ADD COLUMN IF NOT EXISTS similar_artist_uuid UUID")
    cur.execute("""
        UPDATE similar_artists sa SET
            artist_uuid = al1.uuid,
            similar_artist_uuid = al2.uuid
        FROM artists_legacy al1, artists_legacy al2
        WHERE sa.artist_id = al1.id AND sa.similar_artist_id = al2.id
    """)
    cur.execute("ALTER TABLE similar_artists DROP CONSTRAINT IF EXISTS uq_similar_artists")
    cur.execute("ALTER TABLE similar_artists DROP CONSTRAINT IF EXISTS similar_artists_artist_id_fkey")
    cur.execute("ALTER TABLE similar_artists DROP CONSTRAINT IF EXISTS similar_artists_similar_artist_id_fkey")
    cur.execute("ALTER TABLE similar_artists DROP COLUMN IF EXISTS artist_id")
    cur.execute("ALTER TABLE similar_artists DROP COLUMN IF EXISTS similar_artist_id")
    cur.execute("ALTER TABLE similar_artists RENAME COLUMN artist_uuid TO artist_id")
    cur.execute("ALTER TABLE similar_artists RENAME COLUMN similar_artist_uuid TO similar_artist_id")
    # Deduplicate before adding constraints
    cur.execute("""
        DELETE FROM similar_artists s1 USING similar_artists s2
        WHERE s1.ctid > s2.ctid AND s1.artist_id = s2.artist_id
          AND s1.similar_artist_id = s2.similar_artist_id AND s1.source = s2.source
    """)
    cur.execute("ALTER TABLE similar_artists ADD CONSTRAINT similar_artists_artist_id_fkey FOREIGN KEY (artist_id) REFERENCES artists(id) ON DELETE CASCADE")
    cur.execute("ALTER TABLE similar_artists ADD CONSTRAINT similar_artists_similar_artist_id_fkey FOREIGN KEY (similar_artist_id) REFERENCES artists(id) ON DELETE CASCADE")
    cur.execute("ALTER TABLE similar_artists ADD CONSTRAINT uq_similar_artists UNIQUE (artist_id, similar_artist_id, source)")

    # Migrate song_artists FK
    logger.info("  Migrating song_artists FK...")
    cur.execute("ALTER TABLE song_artists ADD COLUMN IF NOT EXISTS artist_uuid UUID")
    cur.execute("""
        UPDATE song_artists sa SET artist_uuid = al.uuid
        FROM artists_legacy al WHERE sa.artist_id = al.id
    """)
    cur.execute("DELETE FROM song_artists WHERE artist_uuid IS NULL")
    cur.execute("ALTER TABLE song_artists DROP CONSTRAINT IF EXISTS song_artists_pkey")
    cur.execute("ALTER TABLE song_artists DROP CONSTRAINT IF EXISTS song_artists_artist_id_fkey")
    cur.execute("ALTER TABLE song_artists DROP COLUMN artist_id")
    cur.execute("ALTER TABLE song_artists RENAME COLUMN artist_uuid TO artist_id")
    # Deduplicate: multiple old artist_ids may map to same UUID
    cur.execute("""
        DELETE FROM song_artists sa1 USING song_artists sa2
        WHERE sa1.ctid > sa2.ctid
          AND sa1.song_id = sa2.song_id
          AND sa1.artist_id = sa2.artist_id
          AND sa1.role = sa2.role
    """)
    logger.info(f"  Deduplicated {cur.rowcount} song_artists rows")
    cur.execute("ALTER TABLE song_artists ADD PRIMARY KEY (song_id, artist_id, role)")
    cur.execute("ALTER TABLE song_artists ADD CONSTRAINT song_artists_artist_id_fkey FOREIGN KEY (artist_id) REFERENCES artists(id) ON DELETE CASCADE")

    # Migrate album_artists FK
    logger.info("  Migrating album_artists FK...")
    cur.execute("ALTER TABLE album_artists ADD COLUMN IF NOT EXISTS artist_uuid UUID")
    cur.execute("""
        UPDATE album_artists aa SET artist_uuid = al.uuid
        FROM artists_legacy al WHERE aa.artist_id = al.id
    """)
    cur.execute("DELETE FROM album_artists WHERE artist_uuid IS NULL")

    # --- Albums ---
    logger.info("  Migrating albums to UUID PK...")
    cur.execute("ALTER TABLE albums RENAME TO albums_legacy")
    # Drop old indexes that would conflict with new table's indexes
    cur.execute("DROP INDEX IF EXISTS idx_albums_title")
    cur.execute("DROP INDEX IF EXISTS idx_albums_title_trgm")
    cur.execute("DROP INDEX IF EXISTS idx_albums_release_year")
    cur.execute("DROP INDEX IF EXISTS idx_albums_lastfm_id")
    cur.execute("DROP INDEX IF EXISTS idx_albums_quality_source")
    cur.execute("""
        CREATE TABLE albums (
            id UUID PRIMARY KEY,
            title VARCHAR(500) NOT NULL,
            release_year INTEGER,
            label VARCHAR(200),
            catalog_number VARCHAR(100),
            total_tracks INTEGER,
            musicbrainz_id VARCHAR(100),
            lastfm_id VARCHAR(100),
            user_rating NUMERIC(3, 2),
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW(),
            CONSTRAINT check_album_rating CHECK (user_rating >= 0 AND user_rating <= 5)
        )
    """)
    cur.execute("CREATE INDEX idx_albums_title ON albums(title)")
    cur.execute("CREATE INDEX idx_albums_title_trgm ON albums USING gin (title gin_trgm_ops)")
    cur.execute("CREATE INDEX idx_albums_release_year ON albums(release_year)")
    cur.execute("CREATE INDEX idx_albums_lastfm_id ON albums(lastfm_id)")
    cur.execute("""
        INSERT INTO albums (id, title, release_year, label, catalog_number, total_tracks,
                           musicbrainz_id, lastfm_id, user_rating, created_at, updated_at)
        SELECT DISTINCT ON (uuid) uuid, title, release_year, label, catalog_number, total_tracks,
               musicbrainz_id, lastfm_id, user_rating, created_at, updated_at
        FROM albums_legacy
        WHERE uuid IS NOT NULL
        ORDER BY uuid, id
    """)
    _verify_count(cur, "albums", 0, "albums (UUID)")

    # Migrate album_info FK
    logger.info("  Migrating album_info FK...")
    cur.execute("ALTER TABLE album_info ADD COLUMN IF NOT EXISTS album_uuid UUID")
    cur.execute("""
        UPDATE album_info ai SET album_uuid = al.uuid
        FROM albums_legacy al WHERE ai.album_id = al.id
    """)
    cur.execute("ALTER TABLE album_info DROP CONSTRAINT IF EXISTS album_info_album_id_fkey")
    cur.execute("ALTER TABLE album_info DROP CONSTRAINT IF EXISTS uq_album_info")
    cur.execute("ALTER TABLE album_info DROP COLUMN IF EXISTS album_id")
    cur.execute("ALTER TABLE album_info RENAME COLUMN album_uuid TO album_id")
    # Deduplicate before adding constraints
    cur.execute("""
        DELETE FROM album_info ai1 USING album_info ai2
        WHERE ai1.ctid > ai2.ctid AND ai1.album_id = ai2.album_id AND ai1.source = ai2.source
    """)
    cur.execute("ALTER TABLE album_info ADD CONSTRAINT album_info_album_id_fkey FOREIGN KEY (album_id) REFERENCES albums(id) ON DELETE CASCADE")
    cur.execute("ALTER TABLE album_info ADD CONSTRAINT uq_album_info UNIQUE (album_id, source)")

    # Migrate album_tags FK
    logger.info("  Migrating album_tags FK...")
    cur.execute("ALTER TABLE album_tags ADD COLUMN IF NOT EXISTS album_uuid UUID")
    cur.execute("""
        UPDATE album_tags at2 SET album_uuid = al.uuid
        FROM albums_legacy al WHERE at2.album_id = al.id
    """)
    cur.execute("ALTER TABLE album_tags DROP CONSTRAINT IF EXISTS album_tags_album_id_fkey")
    cur.execute("ALTER TABLE album_tags DROP CONSTRAINT IF EXISTS uq_album_tags")
    cur.execute("ALTER TABLE album_tags DROP COLUMN IF EXISTS album_id")
    cur.execute("ALTER TABLE album_tags RENAME COLUMN album_uuid TO album_id")
    # Deduplicate before adding constraints
    cur.execute("""
        DELETE FROM album_tags at1 USING album_tags at2
        WHERE at1.ctid > at2.ctid AND at1.album_id = at2.album_id AND at1.tag_id = at2.tag_id AND at1.source = at2.source
    """)
    cur.execute("ALTER TABLE album_tags ADD CONSTRAINT album_tags_album_id_fkey FOREIGN KEY (album_id) REFERENCES albums(id) ON DELETE CASCADE")
    cur.execute("ALTER TABLE album_tags ADD CONSTRAINT uq_album_tags UNIQUE (album_id, tag_id, source)")

    # Migrate album_variants FK
    logger.info("  Migrating album_variants FK...")
    cur.execute("ALTER TABLE album_variants ADD COLUMN IF NOT EXISTS album_uuid UUID")
    cur.execute("""
        UPDATE album_variants av SET album_uuid = al.uuid
        FROM albums_legacy al WHERE av.album_id = al.id
    """)
    cur.execute("ALTER TABLE album_variants DROP CONSTRAINT IF EXISTS album_variants_album_id_fkey")
    cur.execute("ALTER TABLE album_variants DROP COLUMN album_id")
    cur.execute("ALTER TABLE album_variants RENAME COLUMN album_uuid TO album_id")
    cur.execute("ALTER TABLE album_variants ADD CONSTRAINT album_variants_album_id_fkey FOREIGN KEY (album_id) REFERENCES albums(id) ON DELETE CASCADE")

    # Migrate album_artists FK (album side)
    cur.execute("ALTER TABLE album_artists ADD COLUMN IF NOT EXISTS album_uuid UUID")
    cur.execute("""
        UPDATE album_artists aa SET album_uuid = al.uuid
        FROM albums_legacy al WHERE aa.album_id = al.id
    """)
    cur.execute("DELETE FROM album_artists WHERE album_uuid IS NULL OR artist_uuid IS NULL")
    cur.execute("ALTER TABLE album_artists DROP CONSTRAINT IF EXISTS album_artists_pkey")
    cur.execute("ALTER TABLE album_artists DROP CONSTRAINT IF EXISTS album_artists_album_id_fkey")
    cur.execute("ALTER TABLE album_artists DROP COLUMN album_id")
    cur.execute("ALTER TABLE album_artists DROP COLUMN artist_id")
    cur.execute("ALTER TABLE album_artists RENAME COLUMN album_uuid TO album_id")
    cur.execute("ALTER TABLE album_artists RENAME COLUMN artist_uuid TO artist_id")
    # Deduplicate: multiple old IDs may map to same UUID
    cur.execute("""
        DELETE FROM album_artists aa1 USING album_artists aa2
        WHERE aa1.ctid > aa2.ctid
          AND aa1.album_id = aa2.album_id
          AND aa1.artist_id = aa2.artist_id
          AND aa1.role = aa2.role
    """)
    logger.info(f"  Deduplicated {cur.rowcount} album_artists rows")
    cur.execute("ALTER TABLE album_artists ADD PRIMARY KEY (album_id, artist_id, role)")
    cur.execute("ALTER TABLE album_artists ADD CONSTRAINT album_artists_album_id_fkey FOREIGN KEY (album_id) REFERENCES albums(id) ON DELETE CASCADE")
    cur.execute("ALTER TABLE album_artists ADD CONSTRAINT album_artists_artist_id_fkey FOREIGN KEY (artist_id) REFERENCES artists(id) ON DELETE CASCADE")

    # 23. Update external_metadata.entity_id to TEXT (already TEXT in new models)
    cur.execute("""
        SELECT data_type FROM information_schema.columns
        WHERE table_name = 'external_metadata' AND column_name = 'entity_id'
    """)
    row = cur.fetchone()
    if row and row[0] == 'integer':
        logger.info("  Converting external_metadata.entity_id to TEXT...")
        cur.execute("ALTER TABLE external_metadata ALTER COLUMN entity_id TYPE TEXT USING entity_id::TEXT")

    # 24. Update external_metadata.entity_id values from old INTEGER strings to new UUID strings
    logger.info("  Updating external_metadata.entity_id values to UUIDs...")

    # Drop the unique constraint temporarily (entity_id values are changing)
    cur.execute("ALTER TABLE external_metadata DROP CONSTRAINT IF EXISTS uq_external_metadata")

    # Artist entity_ids: old integer → UUID
    cur.execute("""
        UPDATE external_metadata em
        SET entity_id = al.uuid::TEXT
        FROM artists_legacy al
        WHERE em.entity_type = 'artist'
          AND em.entity_id = al.id::TEXT
          AND al.uuid IS NOT NULL
    """)
    logger.info(f"  Updated {cur.rowcount} artist entity_ids")

    # Album entity_ids: old integer → UUID
    cur.execute("""
        UPDATE external_metadata em
        SET entity_id = al.uuid::TEXT
        FROM albums_legacy al
        WHERE em.entity_type = 'album'
          AND em.entity_id = al.id::TEXT
          AND al.uuid IS NOT NULL
    """)
    logger.info(f"  Updated {cur.rowcount} album entity_ids")

    # Track entity_ids: old integer → track UUID (via media_files.song_id)
    # First deduplicate: multiple old tracks can map to the same song UUID
    # Keep only the newest entry per (entity_type, new_entity_id, source, metadata_type)
    cur.execute("""
        WITH mapped AS (
            SELECT em.id, mf.song_id::TEXT as new_entity_id,
                   em.source, em.metadata_type,
                   ROW_NUMBER() OVER (
                       PARTITION BY mf.song_id, em.source, em.metadata_type
                       ORDER BY em.fetched_at DESC NULLS LAST, em.id DESC
                   ) as rn
            FROM external_metadata em
            JOIN tracks t ON em.entity_id = t.id::TEXT
            JOIN media_files mf ON mf.file_path = t.file_path
            WHERE em.entity_type = 'track'
        )
        DELETE FROM external_metadata
        WHERE id IN (SELECT id FROM mapped WHERE rn > 1)
    """)
    logger.info(f"  Deduplicated {cur.rowcount} track entity_id entries")

    # Now update remaining track entity_ids
    cur.execute("""
        UPDATE external_metadata em
        SET entity_id = mf.song_id::TEXT
        FROM tracks t
        JOIN media_files mf ON mf.file_path = t.file_path
        WHERE em.entity_type = 'track'
          AND em.entity_id = t.id::TEXT
          AND mf.song_id IS NOT NULL
    """)
    logger.info(f"  Updated {cur.rowcount} track entity_ids")

    # Restore unique constraint
    cur.execute("""
        ALTER TABLE external_metadata
        ADD CONSTRAINT uq_external_metadata UNIQUE (entity_type, entity_id, source, metadata_type)
    """)

    # Add song_stats FK to songs (renamed to track_stats/track_id in Phase D)
    cur.execute("ALTER TABLE song_stats ADD CONSTRAINT song_stats_song_id_fkey FOREIGN KEY (song_id) REFERENCES songs(id) ON DELETE CASCADE")

    logger.info("Phase C complete.")


# ═══════════════════════════════════════════════════════════════════════════
# Phase D: Cleanup legacy tables
# ═══════════════════════════════════════════════════════════════════════════

def phase_d(cur):
    logger.info("=" * 60)
    logger.info("PHASE D: Cleanup legacy tables")
    logger.info("=" * 60)

    # 25. Drop old tables and dependent views
    logger.info("  Dropping legacy views and tables...")

    # Drop views that depend on old schema
    cur.execute("DROP VIEW IF EXISTS track_listening_stats CASCADE")
    cur.execute("DROP VIEW IF EXISTS library_stats CASCADE")
    cur.execute("DROP VIEW IF EXISTS artists_enriched CASCADE")

    # Drop old track_id columns from embeddings/text_embeddings/audio_features
    cur.execute("ALTER TABLE embeddings DROP COLUMN IF EXISTS track_id")
    cur.execute("ALTER TABLE text_embeddings DROP COLUMN IF EXISTS track_id")
    cur.execute("ALTER TABLE audio_features DROP COLUMN IF EXISTS track_id")

    # Drop track_id from listening_history
    cur.execute("ALTER TABLE listening_history DROP COLUMN IF EXISTS track_id")

    # Set NOT NULL constraints (still song_id at this point — renamed to track_id later)
    cur.execute("ALTER TABLE embeddings ALTER COLUMN song_id SET NOT NULL")
    cur.execute("ALTER TABLE text_embeddings ALTER COLUMN song_id SET NOT NULL")
    cur.execute("ALTER TABLE audio_features ALTER COLUMN song_id SET NOT NULL")

    # Add unique constraints (song_id — will be renamed to track_id later in this phase)
    cur.execute("""
        DO $$ BEGIN
            ALTER TABLE embeddings ADD CONSTRAINT uq_embeddings_song_model UNIQUE (song_id, model_id);
        EXCEPTION WHEN duplicate_table THEN NULL;
        END $$
    """)
    cur.execute("""
        DO $$ BEGIN
            ALTER TABLE text_embeddings ADD CONSTRAINT uq_text_embeddings_song_model UNIQUE (song_id, model_id);
        EXCEPTION WHEN duplicate_table THEN NULL;
        END $$
    """)

    # Add FK constraints for embeddings → songs (renamed to tracks later)
    cur.execute("""
        DO $$ BEGIN
            ALTER TABLE embeddings ADD CONSTRAINT embeddings_song_id_fkey FOREIGN KEY (song_id) REFERENCES songs(id) ON DELETE CASCADE;
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """)
    cur.execute("""
        DO $$ BEGIN
            ALTER TABLE text_embeddings ADD CONSTRAINT text_embeddings_song_id_fkey FOREIGN KEY (song_id) REFERENCES songs(id) ON DELETE CASCADE;
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """)
    cur.execute("""
        DO $$ BEGIN
            ALTER TABLE audio_features ADD CONSTRAINT audio_features_song_id_fkey FOREIGN KEY (song_id) REFERENCES songs(id) ON DELETE CASCADE;
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
    """)

    # Drop legacy tables (tracks depends on albums_legacy via FK)
    for table in ['track_stats', 'track_genres', 'track_artists', 'tracks', 'albums_legacy', 'artists_legacy']:
        cur.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
        logger.info(f"  Dropped: {table}")

    # Drop quality_source_type enum
    cur.execute("DROP TYPE IF EXISTS quality_source_type CASCADE")
    logger.info("  Dropped: quality_source_type enum")

    # 31. Rename temporary tables/columns to final names
    logger.info("  Renaming tables to final names...")

    # Rename tables: songs → tracks, song_artists → track_artists, etc.
    cur.execute("ALTER TABLE songs RENAME TO tracks")
    cur.execute("ALTER TABLE song_artists RENAME TO track_artists")
    cur.execute("ALTER TABLE song_genres RENAME TO track_genres")
    cur.execute("ALTER TABLE song_stats RENAME TO track_stats")
    logger.info("  Renamed: songs→tracks, song_artists→track_artists, song_genres→track_genres, song_stats→track_stats")

    # Rename song_id columns → track_id
    for table in ('media_files', 'embeddings', 'text_embeddings', 'audio_features', 'listening_history',
                  'track_artists', 'track_genres', 'track_stats'):
        try:
            cur.execute("SAVEPOINT sp_rename_col")
            cur.execute(f"ALTER TABLE {table} RENAME COLUMN song_id TO track_id")
            cur.execute("RELEASE SAVEPOINT sp_rename_col")
            logger.info(f"  Renamed: {table}.song_id → track_id")
        except Exception as e:
            cur.execute("ROLLBACK TO SAVEPOINT sp_rename_col")
            logger.warning(f"  Could not rename {table}.song_id: {e}")

    # Rename indexes that reference old names
    rename_indexes = [
        ("idx_songs_title", "idx_tracks_title"),
        ("idx_song_artists_song_id", "idx_track_artists_track_id"),
        ("idx_song_artists_artist_id", "idx_track_artists_artist_id"),
        ("idx_song_genres_song_id", "idx_track_genres_track_id"),
        ("idx_song_genres_genre_id", "idx_track_genres_genre_id"),
        ("idx_song_stats_song", "idx_track_stats_track"),
        ("idx_media_files_song_id", "idx_media_files_track_id"),
    ]
    for old_name, new_name in rename_indexes:
        try:
            cur.execute("SAVEPOINT sp_rename_idx")
            cur.execute(f"ALTER INDEX {old_name} RENAME TO {new_name}")
            cur.execute("RELEASE SAVEPOINT sp_rename_idx")
        except Exception:
            cur.execute("ROLLBACK TO SAVEPOINT sp_rename_idx")

    # Rename constraints
    rename_constraints = [
        ("tracks", "songs_pkey", "tracks_pkey"),
        ("track_artists", "song_artists_pkey", "track_artists_pkey"),
        ("track_genres", "song_genres_pkey", "track_genres_pkey"),
        ("track_artists", "song_artists_artist_id_fkey", "track_artists_artist_id_fkey"),
        ("track_stats", "song_stats_song_id_fkey", "track_stats_track_id_fkey"),
        ("track_stats", "uq_song_stats", "uq_track_stats"),
    ]
    for table, old_name, new_name in rename_constraints:
        try:
            cur.execute("SAVEPOINT sp_rename_con")
            cur.execute(f"ALTER TABLE {table} RENAME CONSTRAINT {old_name} TO {new_name}")
            cur.execute("RELEASE SAVEPOINT sp_rename_con")
        except Exception:
            cur.execute("ROLLBACK TO SAVEPOINT sp_rename_con")

    # Rename FK constraints on embeddings/text_embeddings/audio_features
    for table, old_fk, new_fk in [
        ("embeddings", "embeddings_song_id_fkey", "embeddings_track_id_fkey"),
        ("text_embeddings", "text_embeddings_song_id_fkey", "text_embeddings_track_id_fkey"),
        ("audio_features", "audio_features_song_id_fkey", "audio_features_track_id_fkey"),
    ]:
        try:
            cur.execute("SAVEPOINT sp_rename_fk")
            cur.execute(f"ALTER TABLE {table} RENAME CONSTRAINT {old_fk} TO {new_fk}")
            cur.execute("RELEASE SAVEPOINT sp_rename_fk")
        except Exception:
            cur.execute("ROLLBACK TO SAVEPOINT sp_rename_fk")

    # Rename unique constraints on embeddings
    for old_uq, new_uq, table in [
        ("uq_embeddings_song_model", "uq_embeddings_track_model", "embeddings"),
        ("uq_text_embeddings_song_model", "uq_text_embeddings_track_model", "text_embeddings"),
    ]:
        try:
            cur.execute("SAVEPOINT sp_rename_uq")
            cur.execute(f"ALTER TABLE {table} RENAME CONSTRAINT {old_uq} TO {new_uq}")
            cur.execute("RELEASE SAVEPOINT sp_rename_uq")
        except Exception:
            cur.execute("ROLLBACK TO SAVEPOINT sp_rename_uq")

    logger.info("  Table/column renames complete.")

    # 32. Recreate library_stats view
    logger.info("  Recreating library_stats view...")
    cur.execute("DROP VIEW IF EXISTS library_stats")
    cur.execute("""
        CREATE VIEW library_stats AS
        SELECT
            (SELECT COUNT(*) FROM artists) as total_artists,
            (SELECT COUNT(*) FROM albums) as total_albums,
            (SELECT COUNT(*) FROM tracks) as total_tracks,
            (SELECT COUNT(*) FROM media_files) as total_media_files,
            (SELECT COUNT(*) FROM embeddings) as tracks_with_embeddings,
            (SELECT COALESCE(SUM(duration_seconds), 0) FROM media_files) as total_duration_seconds,
            (SELECT COALESCE(SUM(file_size_bytes), 0) FROM media_files) as total_file_size_bytes,
            (SELECT COUNT(*) FROM genres) as unique_genres
    """)

    # 33. Rebuild HNSW indexes
    logger.info("  Rebuilding HNSW indexes...")
    cur.execute("REINDEX INDEX idx_embeddings_vector")
    cur.execute("REINDEX INDEX idx_text_embeddings_vector")

    # Create indexes for track_id on embeddings tables
    cur.execute("CREATE INDEX IF NOT EXISTS idx_embeddings_track_id ON embeddings(track_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_text_embeddings_track_id ON text_embeddings(track_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_audio_features_track_id ON audio_features(track_id)")

    # Create analysis source index
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_media_files_analysis_source
        ON media_files(track_id, is_analysis_source)
        WHERE is_analysis_source = true
    """)

    # 34. Recreate database functions with UUID parameter types
    logger.info("  Recreating database functions for UUID PKs...")

    # Drop old INTEGER versions
    for func_name in ('get_artist_bio', 'get_artist_tags', 'get_artist_similar', 'get_artist_stats'):
        cur.execute(f"DROP FUNCTION IF EXISTS {func_name}(INTEGER)")

    cur.execute("""
        CREATE OR REPLACE FUNCTION get_artist_bio(artist_id_param UUID)
        RETURNS TEXT
        LANGUAGE plpgsql STABLE AS $$
        DECLARE
            combined_bio TEXT;
        BEGIN
            SELECT string_agg(
                COALESCE(data->>'summary', data->>'content', '') ||
                E'\\n\\n[Source: ' || source || ']',
                E'\\n\\n---\\n\\n'
                ORDER BY
                    CASE source
                        WHEN 'lastfm' THEN 1
                        WHEN 'musicbrainz' THEN 2
                        ELSE 99
                    END
            )
            INTO combined_bio
            FROM external_metadata
            WHERE entity_type = 'artist'
              AND entity_id = artist_id_param::TEXT
              AND metadata_type = 'bio'
              AND fetch_status = 'success'
              AND (data->>'summary' IS NOT NULL OR data->>'content' IS NOT NULL);

            RETURN combined_bio;
        END;
        $$
    """)

    cur.execute("""
        CREATE OR REPLACE FUNCTION get_artist_tags(artist_id_param UUID)
        RETURNS TABLE(tag_name VARCHAR, sources TEXT[], weight INTEGER)
        LANGUAGE sql STABLE AS $$
            SELECT
                tag,
                array_agg(DISTINCT source ORDER BY source) as sources,
                COUNT(*)::INTEGER as weight
            FROM (
                SELECT entity_id, source,
                       jsonb_array_elements(data->'tags')->>'name' as tag
                FROM external_metadata
                WHERE entity_type = 'artist'
                  AND metadata_type = 'tags'
                  AND fetch_status = 'success'
                  AND data ? 'tags'
                UNION ALL
                SELECT entity_id, source,
                       jsonb_array_elements_text(data->'genres') as tag
                FROM external_metadata
                WHERE entity_type = 'artist'
                  AND metadata_type = 'genres'
                  AND fetch_status = 'success'
                  AND data ? 'genres'
            ) combined
            WHERE entity_id = artist_id_param::TEXT
            GROUP BY tag
            ORDER BY weight DESC, tag;
        $$
    """)

    cur.execute("""
        CREATE OR REPLACE FUNCTION get_artist_similar(artist_id_param UUID)
        RETURNS TABLE(similar_artist_name VARCHAR, sources TEXT[], avg_match NUMERIC)
        LANGUAGE sql STABLE AS $$
            SELECT
                artist_name,
                array_agg(DISTINCT source ORDER BY source) as sources,
                AVG(match_score)::NUMERIC(5,4) as avg_match
            FROM (
                SELECT entity_id, source,
                       elem->>'name' as artist_name,
                       COALESCE((elem->>'match')::NUMERIC, 1.0) as match_score
                FROM external_metadata,
                     jsonb_array_elements(data->'similar') as elem
                WHERE entity_type = 'artist'
                  AND metadata_type = 'similar_artists'
                  AND fetch_status = 'success'
                  AND data ? 'similar'
            ) combined
            WHERE entity_id = artist_id_param::TEXT
            GROUP BY artist_name
            ORDER BY avg_match DESC, artist_name;
        $$
    """)

    cur.execute("""
        CREATE OR REPLACE FUNCTION get_artist_stats(artist_id_param UUID)
        RETURNS TABLE(source VARCHAR, listeners BIGINT, playcount BIGINT, url TEXT)
        LANGUAGE sql STABLE AS $$
            SELECT
                source,
                COALESCE((data->'stats'->>'listeners')::BIGINT, 0) as listeners,
                COALESCE((data->'stats'->>'playcount')::BIGINT, 0) as playcount,
                data->>'url' as url
            FROM external_metadata
            WHERE entity_type = 'artist'
              AND entity_id = artist_id_param::TEXT
              AND metadata_type = 'bio'
              AND fetch_status = 'success'
              AND data ? 'stats'
            ORDER BY
                CASE source
                    WHEN 'lastfm' THEN 1
                    WHEN 'musicbrainz' THEN 2
                    ELSE 99
                END;
        $$
    """)
    logger.info("  Recreated 4 artist functions with UUID parameters")

    # 35. Recreate artists_enriched view
    logger.info("  Recreating artists_enriched view...")
    cur.execute("DROP VIEW IF EXISTS artists_enriched")
    cur.execute("""
        CREATE VIEW artists_enriched AS
        SELECT a.id,
            a.name,
            a.country,
            get_artist_bio(a.id) AS bio_combined,
            (SELECT jsonb_agg(
                jsonb_build_object('tag', t.tag_name, 'weight', t.weight, 'sources', t.sources)
                ORDER BY t.weight DESC)
             FROM get_artist_tags(a.id) t
             LIMIT 30) AS tags_combined,
            (SELECT jsonb_agg(
                jsonb_build_object('name', s.similar_artist_name, 'match', s.avg_match, 'sources', s.sources)
                ORDER BY s.avg_match DESC)
             FROM get_artist_similar(a.id) s
             LIMIT 20) AS similar_artists,
            a.created_at,
            a.updated_at
        FROM artists a
    """)
    logger.info("  Recreated artists_enriched view")

    logger.info("Phase D complete.")


# ═══════════════════════════════════════════════════════════════════════════
# Verification
# ═══════════════════════════════════════════════════════════════════════════

def verify(cur):
    logger.info("=" * 60)
    logger.info("VERIFICATION")
    logger.info("=" * 60)

    _verify_count(cur, "tracks", label="tracks (canonical)")
    _verify_count(cur, "media_files", label="media_files")
    _verify_count(cur, "album_variants", label="album_variants")
    _verify_count(cur, "artists", label="artists (UUID)")
    _verify_count(cur, "albums", label="albums (UUID)")

    cur.execute("SELECT COUNT(*) FROM embeddings WHERE track_id IS NOT NULL")
    logger.info(f"  embeddings with track_id: {cur.fetchone()[0]}")

    cur.execute("SELECT COUNT(*) FROM media_files WHERE track_id IS NULL")
    null_tracks = cur.fetchone()[0]
    logger.info(f"  media_files with NULL track_id: {null_tracks}" + (" ERROR!" if null_tracks > 0 else " OK"))

    cur.execute("SELECT COUNT(*) FROM media_files WHERE is_analysis_source = TRUE")
    logger.info(f"  analysis source files: {cur.fetchone()[0]}")

    # Check for orphan embeddings
    cur.execute("""
        SELECT COUNT(*) FROM embeddings e
        LEFT JOIN tracks t ON e.track_id = t.id
        WHERE t.id IS NULL
    """)
    orphans = cur.fetchone()[0]
    logger.info(f"  orphan embeddings: {orphans}" + (" ERROR!" if orphans > 0 else " OK"))

    # Check external_metadata entity_ids are UUID format
    cur.execute("""
        SELECT entity_type, COUNT(*) as total,
               COUNT(*) FILTER (WHERE length(entity_id) = 36) as uuid_format
        FROM external_metadata
        GROUP BY entity_type
    """)
    for row in cur.fetchall():
        ok = "OK" if row[1] == row[2] else "WARN"
        logger.info(f"  [{ok}] external_metadata {row[0]}: {row[1]} total, {row[2]} UUID-format")

    # Check views exist
    for view_name in ('library_stats', 'artists_enriched'):
        cur.execute(f"SELECT COUNT(*) FROM pg_views WHERE viewname = '{view_name}'")
        exists = cur.fetchone()[0] > 0
        logger.info(f"  view {view_name}: {'OK' if exists else 'MISSING'}")

    # Check functions exist with UUID parameter
    cur.execute("""
        SELECT proname FROM pg_proc
        WHERE proname IN ('get_artist_bio', 'get_artist_tags', 'get_artist_similar', 'get_artist_stats')
    """)
    funcs = [r[0] for r in cur.fetchall()]
    logger.info(f"  artist functions: {len(funcs)}/4 exist")

    logger.info("Verification complete.")


# ═══════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Migrate to UUID canonical schema")
    parser.add_argument("--db-url", default=os.environ.get("DATABASE_URL", "postgresql://musicai:supervisor@localhost:5432/music_ai"))
    parser.add_argument("--phase", choices=["A", "B", "C", "D", "all", "verify"], default="all")
    parser.add_argument("--no-backup", action="store_true", help="Skip pg_dump backup")
    args = parser.parse_args()

    # Backup
    if not args.no_backup and args.phase in ("all", "A"):
        logger.info("Creating database backup...")
        backup_file = f"backup_pre_uuid_{datetime.now().strftime('%Y%m%d_%H%M%S')}.sql"
        backed_up = False

        # Try local pg_dump first
        try:
            subprocess.run(
                ["pg_dump", args.db_url, "-f", backup_file],
                check=True, capture_output=True, text=True
            )
            logger.info(f"Backup saved to: {backup_file}")
            backed_up = True
        except FileNotFoundError:
            logger.info("pg_dump not found locally, trying docker exec...")
        except subprocess.CalledProcessError as e:
            logger.warning(f"pg_dump failed: {e}")

        # Fallback: try docker exec
        if not backed_up:
            try:
                with open(backup_file, 'w') as f:
                    subprocess.run(
                        ["docker", "exec", "music-ai-postgres",
                         "pg_dump", "-U", "musicai", "music_ai"],
                        stdout=f, check=True, text=True
                    )
                logger.info(f"Backup saved via docker: {backup_file}")
                backed_up = True
            except (subprocess.CalledProcessError, FileNotFoundError) as e:
                logger.warning(f"Docker pg_dump also failed: {e}. Continuing without backup.")

    conn = psycopg2.connect(args.db_url)
    conn.autocommit = False  # Use transactions

    try:
        with conn.cursor() as cur:
            if args.phase in ("all", "A"):
                phase_a(cur)
                conn.commit()
                logger.info("Phase A committed.")

            if args.phase in ("all", "B"):
                phase_b(cur)
                conn.commit()
                logger.info("Phase B committed.")

            if args.phase in ("all", "C"):
                phase_c(cur)
                conn.commit()
                logger.info("Phase C committed.")

            if args.phase in ("all", "D"):
                phase_d(cur)
                conn.commit()
                logger.info("Phase D committed.")

            if args.phase in ("all", "verify"):
                verify(cur)

        logger.info("Migration complete!")

    except Exception as e:
        conn.rollback()
        logger.error(f"Migration FAILED: {e}", exc_info=True)
        logger.error("Transaction rolled back.")
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
