#!/usr/bin/env python3
"""
One-time migration: convert DB paths from container paths (/music/...) to native OS paths.

Usage:
    # Dry run (default) — shows what would change:
    python migrate_paths.py

    # Apply changes:
    python migrate_paths.py --apply

    # Custom prefixes:
    python migrate_paths.py --old-prefix /music --new-prefix E:/Music --apply
"""

import argparse
import sys

import psycopg2

from config import settings


def migrate_paths(old_prefix: str, new_prefix: str, apply: bool = False):
    """Convert paths in media_files and album_variants."""
    conn = psycopg2.connect(settings.database_url)
    conn.autocommit = False

    try:
        with conn.cursor() as cur:
            # Count affected rows in media_files
            cur.execute(
                "SELECT COUNT(*) FROM media_files WHERE file_path LIKE %s",
                (old_prefix + "%",),
            )
            mf_count = cur.fetchone()[0]

            # Count affected rows in album_variants
            cur.execute(
                "SELECT COUNT(*) FROM album_variants WHERE directory_path LIKE %s",
                (old_prefix + "%",),
            )
            av_count = cur.fetchone()[0]

            print(f"Old prefix: {old_prefix}")
            print(f"New prefix: {new_prefix}")
            print(f"media_files to update:   {mf_count}")
            print(f"album_variants to update: {av_count}")

            if mf_count == 0 and av_count == 0:
                print("\nNothing to migrate.")
                return

            # Show samples
            cur.execute(
                "SELECT file_path FROM media_files WHERE file_path LIKE %s LIMIT 3",
                (old_prefix + "%",),
            )
            samples = cur.fetchall()
            if samples:
                print("\nSample conversions (media_files):")
                for (path,) in samples:
                    new_path = new_prefix + path[len(old_prefix):]
                    print(f"  {path}")
                    print(f"  → {new_path}")

            if not apply:
                print("\nDry run. Use --apply to execute.")
                return

            # Update media_files
            cur.execute(
                """
                UPDATE media_files
                SET file_path = %s || substring(file_path FROM %s)
                WHERE file_path LIKE %s
                """,
                (new_prefix, len(old_prefix) + 1, old_prefix + "%"),
            )
            mf_updated = cur.rowcount

            # Update album_variants
            cur.execute(
                """
                UPDATE album_variants
                SET directory_path = %s || substring(directory_path FROM %s)
                WHERE directory_path LIKE %s
                """,
                (new_prefix, len(old_prefix) + 1, old_prefix + "%"),
            )
            av_updated = cur.rowcount

            conn.commit()
            print(f"\nMigration complete:")
            print(f"  media_files updated:    {mf_updated}")
            print(f"  album_variants updated: {av_updated}")

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Migrate DB paths from container to native OS paths")
    parser.add_argument(
        "--old-prefix",
        default="/music",
        help="Old path prefix to replace (default: /music)",
    )
    parser.add_argument(
        "--new-prefix",
        default=None,
        help="New path prefix (default: MUSIC_HOST_PATH or E:/Music)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually apply changes (default is dry run)",
    )
    args = parser.parse_args()

    new_prefix = args.new_prefix
    if new_prefix is None:
        new_prefix = settings.music_host_path or "E:/Music"

    migrate_paths(args.old_prefix, new_prefix, apply=args.apply)


if __name__ == "__main__":
    main()
