"""
Diagnostic: find local tracks without embeddings and check if their UUIDs
exist in the Docker database.

Run from Windows:
    python -m desktop.diag_missing_embeddings
"""

import json
import sys
from pathlib import Path

import psycopg2

# Launcher DB (local)
LAUNCHER_DSN = None  # Will be loaded from config

# Docker DB (source) — port 5432, password from .env
DOCKER_DSN = "postgresql://musicai:supervisor@localhost:5432/music_ai"


def get_launcher_dsn() -> str:
    """Build DSN from launcher config."""
    import os
    cfg_path = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "MusicAIDJ" / "config.json"
    if cfg_path.exists():
        with open(cfg_path) as f:
            cfg = json.load(f)
        pw = cfg.get("postgres_password", "changeme")
        port = cfg.get("ports", {}).get("postgres", 15432)
        return f"postgresql://musicai:{pw}@localhost:{port}/music_ai"
    return f"postgresql://musicai:changeme@localhost:15432/music_ai"


def main():
    launcher_dsn = get_launcher_dsn()
    print(f"Launcher DB: {launcher_dsn.split('@')[1]}")

    # Connect to launcher
    lconn = psycopg2.connect(launcher_dsn)
    lcur = lconn.cursor()

    # Get all launcher track UUIDs
    lcur.execute("SELECT id::text, title FROM tracks")
    launcher_tracks = {row[0]: row[1] for row in lcur.fetchall()}
    print(f"\nLauncher total tracks: {len(launcher_tracks)}")

    # Get tracks with embeddings
    lcur.execute("SELECT DISTINCT track_id::text FROM embeddings")
    launcher_embedded = {row[0] for row in lcur.fetchall()}
    print(f"Launcher tracks with embeddings: {len(launcher_embedded)}")

    # Tracks without embeddings
    missing = set(launcher_tracks.keys()) - launcher_embedded
    print(f"Launcher tracks WITHOUT embeddings: {len(missing)}")

    if missing:
        # Get artist info for missing tracks
        lcur.execute("""
            SELECT t.id::text, t.title, a.name
            FROM tracks t
            LEFT JOIN track_artists ta ON ta.track_id = t.id AND ta.role = 'primary'
            LEFT JOIN artists a ON a.id = ta.artist_id
            WHERE t.id = ANY(%s::uuid[])
            ORDER BY a.name, t.title
        """, [list(missing)])

        print("\n--- Missing embedding tracks ---")
        for row in lcur.fetchall():
            artist = row[2] or "Unknown"
            print(f"  {artist} - {row[1]}  (UUID: {row[0]})")

    # Also find tracks that exist in launcher but not in Docker
    # (this explains sync mismatches)
    lcur.execute("SELECT id::text FROM tracks")
    launcher_uuids = {row[0] for row in lcur.fetchall()}

    try:
        dconn = psycopg2.connect(DOCKER_DSN)
        dcur = dconn.cursor()

        dcur.execute("SELECT id::text FROM tracks")
        docker_uuids = {row[0] for row in dcur.fetchall()}

        only_launcher = launcher_uuids - docker_uuids
        only_docker_blues = set()

        if only_launcher:
            print(f"\n--- Tracks in launcher but NOT in Docker: {len(only_launcher)} ---")
            lcur.execute("""
                SELECT t.id::text, t.title, a.name
                FROM tracks t
                LEFT JOIN track_artists ta ON ta.track_id = t.id AND ta.role = 'primary'
                LEFT JOIN artists a ON a.id = ta.artist_id
                WHERE t.id = ANY(%s::uuid[])
                ORDER BY a.name, t.title
            """, [list(only_launcher)])
            for row in lcur.fetchall():
                artist = row[2] or "Unknown"
                print(f"  {artist} - {row[1]}  (UUID: {row[0]})")

        dconn.close()
    except Exception as e:
        print(f"\nCould not connect to Docker DB: {e}")
        print("(This is OK if Docker is not running or uses different credentials)")

    lconn.close()


if __name__ == "__main__":
    main()
