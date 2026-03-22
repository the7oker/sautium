"""
Diagnostic: find local tracks without embeddings and compare UUIDs
between launcher and Docker databases.

Run from Windows:
    python -m desktop.diag_missing_embeddings
"""

import json
import sys
from collections import Counter
from pathlib import Path

import psycopg2

# Docker DB (source) — port 5432, password from .env
DOCKER_DSN = "postgresql://sautium:supervisor@localhost:5432/sautium"


def get_launcher_dsn() -> str:
    """Build DSN from launcher config."""
    import os
    cfg_path = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Sautium" / "config.json"
    if cfg_path.exists():
        with open(cfg_path) as f:
            cfg = json.load(f)
        pw = cfg.get("postgres_password", "changeme")
        port = cfg.get("ports", {}).get("postgres", 15432)
        return f"postgresql://sautium:{pw}@localhost:{port}/sautium"
    return f"postgresql://sautium:changeme@localhost:15432/sautium"


def main():
    launcher_dsn = get_launcher_dsn()
    print(f"Launcher DB: {launcher_dsn.split('@')[1]}")

    # Connect to both DBs
    lconn = psycopg2.connect(launcher_dsn)
    lcur = lconn.cursor()

    try:
        dconn = psycopg2.connect(DOCKER_DSN)
        dcur = dconn.cursor()
    except Exception as e:
        print(f"Could not connect to Docker DB: {e}")
        lconn.close()
        return

    # Basic counts
    lcur.execute("SELECT COUNT(*) FROM tracks")
    l_tracks = lcur.fetchone()[0]
    lcur.execute("SELECT COUNT(*) FROM embeddings")
    l_embeds = lcur.fetchone()[0]
    dcur.execute("SELECT COUNT(*) FROM tracks")
    d_tracks = dcur.fetchone()[0]
    dcur.execute("SELECT COUNT(*) FROM embeddings")
    d_embeds = dcur.fetchone()[0]

    print(f"\n{'':>20} {'Launcher':>10} {'Docker':>10}")
    print(f"{'Tracks':>20} {l_tracks:>10} {d_tracks:>10}")
    print(f"{'Embeddings':>20} {l_embeds:>10} {d_embeds:>10}")

    # UUID comparison
    lcur.execute("SELECT id::text FROM tracks")
    launcher_uuids = {row[0] for row in lcur.fetchall()}
    dcur.execute("SELECT id::text FROM tracks")
    docker_uuids = {row[0] for row in dcur.fetchall()}

    only_launcher = launcher_uuids - docker_uuids
    only_docker = docker_uuids - launcher_uuids
    common = launcher_uuids & docker_uuids

    print(f"\n{'Common UUIDs':>20} {len(common):>10}")
    print(f"{'Only in launcher':>20} {len(only_launcher):>10}")
    print(f"{'Only in Docker':>20} {len(only_docker):>10}")

    if not only_launcher:
        print("\nAll launcher UUIDs found in Docker — no mismatches!")
        lconn.close()
        dconn.close()
        return

    # Get artists for mismatched launcher tracks — summary by artist
    lcur.execute("""
        SELECT a.name, COUNT(*) as cnt
        FROM tracks t
        LEFT JOIN track_artists ta ON ta.track_id = t.id AND ta.role = 'primary'
        LEFT JOIN artists a ON a.id = ta.artist_id
        WHERE t.id = ANY(%s::uuid[])
        GROUP BY a.name
        ORDER BY cnt DESC
    """, [list(only_launcher)])

    rows = lcur.fetchall()
    print(f"\n--- Mismatched tracks by artist (top 30) ---")
    for artist, cnt in rows[:30]:
        print(f"  {cnt:>4}  {artist or 'Unknown'}")
    if len(rows) > 30:
        print(f"  ... and {len(rows) - 30} more artists")

    # Sample: show first 5 mismatched tracks with Docker comparison
    print(f"\n--- Sample mismatches (first 10) ---")
    lcur.execute("""
        SELECT t.id::text, t.title, a.name
        FROM tracks t
        LEFT JOIN track_artists ta ON ta.track_id = t.id AND ta.role = 'primary'
        LEFT JOIN artists a ON a.id = ta.artist_id
        WHERE t.id = ANY(%s::uuid[])
        ORDER BY a.name, t.title
        LIMIT 10
    """, [list(only_launcher)])

    for uuid, title, artist in lcur.fetchall():
        print(f"\n  Launcher: {artist} - {title}")
        print(f"    UUID: {uuid}")

        # Find same title in Docker
        dcur.execute("""
            SELECT t.id::text, a.name
            FROM tracks t
            LEFT JOIN track_artists ta ON ta.track_id = t.id AND ta.role = 'primary'
            LEFT JOIN artists a ON a.id = ta.artist_id
            WHERE t.title = %s
            ORDER BY a.name
        """, [title])
        docker_matches = dcur.fetchall()
        if docker_matches:
            for d_uuid, d_artist in docker_matches:
                match = " <-- MATCH" if d_artist == artist else ""
                print(f"    Docker: {d_artist} - {title} UUID: {d_uuid}{match}")
        else:
            print(f"    Docker: title not found!")

    lconn.close()
    dconn.close()


if __name__ == "__main__":
    main()
