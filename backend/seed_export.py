"""Build the cold-start seed bundle from the master DB.

Master-only CLI. The 52 curated picks (backend/seed/manifest_v1.json) are
resolved to their minted rows through uuid_utils — never through SQL
normalization, which cannot reproduce the identity rule's punctuation
folding — and exported as backend/seed/seed_v1.json.gz for every node's
first-start import (backend/seed_import.py).

The bundle has two halves. The structural half (albums, tracklists,
artists, descriptions, picks) has no P2P wire representation — the v3
structural transport is deleted — so plain row dumps travel here, seals
included where the master holds them. The enrichment/analysis half is the
VERBATIM output of the pull handlers in desktop/p2p/sync_queries.py, so
the importer replays it through the ordinary verify-and-import gate with
zero format drift.

Reliability over coverage: a pick with any integrity gap — a slot without
a catalog length, a tracklist with position gaps (a partial mint), no
tracklist, no cover, no description, unsealed rows, unsealed or missing
analysis — is EXCLUDED from the bundle and named in the output, never
shipped half-right. The exit status is 1 while anything is excluded, so
the bundle is complete only when the run ends with 0.

Deterministic output: rows sorted by primary key, canonical JSON, gzip
mtime=0 — re-exporting unchanged data is byte-identical and makes no git
churn.

Run inside the backend container:
    docker exec sautium-backend python seed_export.py --report   # coverage only
    docker exec sautium-backend python seed_export.py            # export
"""

import argparse
import gzip
import json
import sys
from pathlib import Path

import psycopg2

from config import settings
from desktop.p2p import sync_queries as sq
from discography import _CAA_FRONT_URL
from uuid_utils import IDENTITY_RULE, album_uuid, artist_uuid

SEED_DIR = Path(__file__).resolve().parent / "seed"
MANIFEST_PATH = SEED_DIR / "manifest_v1.json"
BUNDLE_PATH = SEED_DIR / "seed_v1.json.gz"

BUNDLE_FORMAT = "sautium-seed"
BUNDLE_VERSION = 1


def _resolve_picks(conn, manifest: dict) -> list[dict]:
    picks = []
    for entry in manifest["picks"]:
        aid = str(album_uuid(entry["title"], entry["artist"]))
        row = sq.db_query(
            conn,
            """SELECT id::text AS id, title, musicbrainz_id::text AS mbid, cover_url,
                      EXISTS (SELECT 1 FROM album_variants av WHERE av.album_id = albums.id) AS owned
               FROM albums WHERE id = %s::uuid""",
            [aid],
        )
        picks.append({
            "rank": entry["rank"],
            "tier": entry["tier"],
            "artist": entry["artist"],
            "title": entry["title"],
            "album_id": aid,
            "artist_id": str(artist_uuid(entry["artist"])),
            "owned_on_master": bool(row and row[0]["owned"]),
            "_found": bool(row),
            "_row": row[0] if row else None,
        })
    return picks


def _coverage(conn, picks: list[dict]) -> tuple[list[str], list[str], list[dict]]:
    """Per-pick integrity against the export gate. Every gap lands in the
    pick's `_problems` (the export excludes such picks) and in the returned
    fatal list. Returns (fatal, warns, report)."""
    fatal, warns, report = [], [], []
    for p in picks:
        label = f"#{p['rank']:>2} {p['artist']} — {p['title']}"
        r = {"label": label}
        report.append(r)
        fatal_before = len(fatal)
        p["_problems"] = []
        if not p["_found"]:
            r["status"] = "MISSING ALBUM"
            fatal.append(f"{label}: no albums row for {p['album_id']} — "
                         "manifest artist/title does not reproduce the minted uuid")
            p["_problems"].append("missing album")
            continue

        row = p["_row"]
        if not row["mbid"]:
            fatal.append(f"{label}: albums.musicbrainz_id is NULL")
        if not row["cover_url"] and not row["mbid"]:
            # An owned album keeps its art in local files and has no
            # cover_url; on a user node every pick is a phantom, so the
            # export gives it the Cover Art Archive front by release group —
            # exactly what the mint gives a phantom. Only a pick with
            # neither is art-less.
            fatal.append(f"{label}: no cover_url and no release group to derive "
                         "the Cover Art Archive front from")

        desc = sq.db_query(
            conn, "SELECT 1 AS x FROM album_descriptions WHERE album_id = %s::uuid",
            [p["album_id"]])
        if not desc:
            fatal.append(f"{label}: no album_descriptions row")

        slots = sq.db_query(
            conn,
            """SELECT COUNT(*) AS n,
                      COUNT(*) FILTER (WHERE length_ms IS NULL) AS untimed,
                      COUNT(*) FILTER (WHERE signature IS NULL OR batch_root IS NULL) AS unsealed,
                      COALESCE(SUM(d.gaps), 0) AS gaps
               FROM album_tracks at2
               LEFT JOIN (SELECT album_id, disc, MAX(position) - COUNT(*) AS gaps
                          FROM album_tracks WHERE album_id = %(aid)s::uuid
                          GROUP BY album_id, disc) d ON d.album_id = at2.album_id AND d.disc = at2.disc
               WHERE at2.album_id = %(aid)s::uuid""",
            {"aid": p["album_id"]})[0]
        if not slots["n"]:
            fatal.append(f"{label}: empty tracklist (no album_tracks rows)")
        if slots["untimed"]:
            fatal.append(f"{label}: {slots['untimed']}/{slots['n']} slots without "
                         "length_ms — streaming resolve has nothing to hold a hit to")
        if slots["gaps"]:
            fatal.append(f"{label}: tracklist has position gaps (a partial mint) — "
                         "rows the listener would be offered that resolve nothing")
        if slots["unsealed"]:
            fatal.append(f"{label}: {slots['unsealed']}/{slots['n']} album_tracks "
                         "rows unsealed — run sign_audio on the master first")

        album_seal = sq.db_query(
            conn,
            """SELECT 1 AS x FROM albums
               WHERE id = %s::uuid AND signature IS NOT NULL AND batch_root IS NOT NULL""",
            [p["album_id"]])
        if not album_seal:
            fatal.append(f"{label}: albums row unsealed — run sign_audio on the master first")

        analysis = sq.db_query(
            conn,
            """SELECT COUNT(*) AS n,
                      COUNT(*) FILTER (WHERE NOT EXISTS (
                          SELECT 1 FROM embeddings e
                          JOIN embedding_segments es ON es.embedding_id = e.id
                          WHERE e.track_id = at2.track_id
                            AND es.signature IS NOT NULL AND es.batch_root IS NOT NULL
                      )) AS no_segments,
                      COUNT(*) FILTER (WHERE NOT EXISTS (
                          SELECT 1 FROM audio_features af
                          WHERE af.track_id = at2.track_id
                            AND af.signature IS NOT NULL AND af.batch_root IS NOT NULL
                      )) AS no_features
               FROM album_tracks at2 WHERE at2.album_id = %s::uuid""",
            [p["album_id"]])[0]
        if analysis["no_segments"]:
            fatal.append(f"{label}: {analysis['no_segments']}/{analysis['n']} tracks "
                         "without sealed segments")
        if analysis["no_features"]:
            fatal.append(f"{label}: {analysis['no_features']}/{analysis['n']} tracks "
                         "without sealed audio_features")

        enrich = sq.db_query(
            conn,
            """SELECT EXISTS (SELECT 1 FROM artist_bios
                              WHERE artist_id = %(aid)s::uuid
                                AND signature IS NOT NULL AND batch_root IS NOT NULL) AS bios,
                      EXISTS (SELECT 1 FROM artist_tags
                              WHERE artist_id = %(aid)s::uuid
                                AND signature IS NOT NULL AND batch_root IS NOT NULL) AS tags,
                      EXISTS (SELECT 1 FROM similar_artists
                              WHERE artist_id = %(aid)s::uuid
                                AND signature IS NOT NULL AND batch_root IS NOT NULL) AS similars""",
            {"aid": p["artist_id"]})[0]
        if not enrich["bios"]:
            fatal.append(f"{label}: no sealed artist_bios for primary artist")
        if not enrich["tags"]:
            fatal.append(f"{label}: no sealed artist_tags for primary artist")
        if not enrich["similars"]:
            warns.append(f"{label}: no sealed similar_artists for primary artist")

        manifest_owned = next(
            e["owned_on_master"] for e in _manifest_cache["picks"]
            if e["rank"] == p["rank"])
        if manifest_owned != p["owned_on_master"]:
            warns.append(f"{label}: manifest owned_on_master={manifest_owned} but "
                         f"DB says {p['owned_on_master']} (informational)")

        gaps = len(fatal) - fatal_before
        p["_problems"].extend(fatal[fatal_before:])
        r["status"] = f"EXCLUDED ({gaps})" if gaps else "ok"
        r["tracks"] = slots["n"]
    return fatal, warns, report


def _collect_ids(conn, album_ids: list[str]) -> tuple[list[str], list[str]]:
    """(track_ids, artist_ids) referenced by the picked albums."""
    tracks = sq.db_query(
        conn,
        """SELECT DISTINCT track_id::text AS id FROM album_tracks
           WHERE album_id = ANY(%s::uuid[]) ORDER BY 1""",
        [album_ids])
    track_ids = [r["id"] for r in tracks]
    artists = sq.db_query(
        conn,
        """SELECT DISTINCT artist_id::text AS id FROM (
               SELECT artist_id FROM album_artists WHERE album_id = ANY(%(albums)s::uuid[])
               UNION
               SELECT artist_id FROM track_artists WHERE track_id = ANY(%(tracks)s::uuid[])
           ) u ORDER BY 1""",
        {"albums": album_ids, "tracks": track_ids})
    return track_ids, [r["id"] for r in artists]


def _structural(conn, picks: list[dict], album_ids: list[str],
                track_ids: list[str], artist_ids: list[str]) -> dict:
    q = sq.db_query
    caa_pre, caa_suf = _CAA_FRONT_URL.split("{rg}")
    out = {
        "artists": q(conn, """
            SELECT id::text AS id, name, raw_name, name_latin,
                   artist_type::text AS artist_type, gender::text AS gender,
                   is_vocalist::text AS is_vocalist
            FROM artists WHERE id = ANY(%s::uuid[]) ORDER BY id""", [artist_ids]),
        "albums": q(conn, """
            SELECT id::text AS id, title, title_latin, release_year, label,
                   catalog_number, total_tracks, musicbrainz_id::text AS musicbrainz_id,
                   mb_match_confidence::text AS mb_match_confidence,
                   COALESCE(cover_url, %(caa_pre)s || musicbrainz_id::text || %(caa_suf)s) AS cover_url,
                   author_pubkey, signature, batch_root, merkle_proof
            FROM albums WHERE id = ANY(%(ids)s::uuid[]) ORDER BY id""",
            {"ids": album_ids, "caa_pre": caa_pre, "caa_suf": caa_suf}),
        "tracks": q(conn, """
            SELECT id::text AS id, title, title_latin
            FROM tracks WHERE id = ANY(%s::uuid[]) ORDER BY id""", [track_ids]),
        "album_tracks": q(conn, """
            SELECT album_id::text AS album_id, track_id::text AS track_id,
                   disc, position, recording_mbid::text AS recording_mbid, length_ms,
                   author_pubkey, signature, batch_root, merkle_proof
            FROM album_tracks WHERE album_id = ANY(%s::uuid[])
            ORDER BY album_id, disc, position""", [album_ids]),
        "track_artists": q(conn, """
            SELECT track_id::text AS track_id, artist_id::text AS artist_id,
                   role::text AS role
            FROM track_artists WHERE track_id = ANY(%s::uuid[])
            ORDER BY track_id, artist_id, role""", [track_ids]),
        "album_artists": q(conn, """
            SELECT album_id::text AS album_id, artist_id::text AS artist_id,
                   role::text AS role, mbid::text AS mbid
            FROM album_artists WHERE album_id = ANY(%s::uuid[])
            ORDER BY album_id, artist_id, role""", [album_ids]),
        "artist_mbids": q(conn, """
            SELECT mbid::text AS mbid, artist_id::text AS artist_id,
                   confidence::text AS confidence, name, about
            FROM artist_mbids WHERE artist_id = ANY(%s::uuid[])
            ORDER BY mbid""", [artist_ids]),
        "genres": q(conn, """
            SELECT DISTINCT g.id::text AS id, g.name
            FROM genres g JOIN album_genres ag ON ag.genre_id = g.id
            WHERE ag.album_id = ANY(%s::uuid[]) ORDER BY id""", [album_ids]),
        "album_genres": q(conn, """
            SELECT album_id::text AS album_id, genre_id::text AS genre_id,
                   source, count
            FROM album_genres WHERE album_id = ANY(%s::uuid[])
            ORDER BY album_id, genre_id, source""", [album_ids]),
        "album_descriptions": q(conn, """
            SELECT album_id::text AS album_id, source, summary, content, url
            FROM album_descriptions WHERE album_id = ANY(%s::uuid[])
            ORDER BY album_id, source""", [album_ids]),
        "seed_picks": [
            {"album_id": p["album_id"], "tier": p["tier"], "rank": p["rank"]}
            for p in sorted(picks, key=lambda p: p["rank"])
        ],
    }
    roots = {r["batch_root"] for r in out["albums"] if r["batch_root"]}
    roots |= {r["batch_root"] for r in out["album_tracks"] if r["batch_root"]}
    out["batches"] = sq._batches_map(conn, roots)
    return out


def _pull_segments_chunked(conn, track_ids: list[str]) -> dict:
    merged = {"category": "segments", "items": [], "batches": {}}
    for i in range(0, len(track_ids), sq.SEGMENTS_MAX_UUIDS):
        part = sq.pull_segments(conn, track_ids[i:i + sq.SEGMENTS_MAX_UUIDS])
        merged["items"].extend(part["items"])
        merged["batches"].update(part["batches"])
    return merged


_ENVELOPE_SORT_KEYS = {
    "segments": lambda i: i["track_uuid"],
    "audio_features": lambda i: i["track_uuid"],
    "track_mbids": lambda i: (i["track_uuid"], i["recording_mbid"]),
    "artist_bios": lambda i: (i["artist_uuid"], i["source"]),
    "artist_tags": lambda i: (i["artist_uuid"], i["tag_uuid"], i["source"]),
    "similar_artists": lambda i: (i["artist_uuid"], i["similar_artist_uuid"], i["source"]),
}


def _envelope(pull_result: dict) -> dict:
    pull_result["items"].sort(key=_ENVELOPE_SORT_KEYS[pull_result["category"]])
    return pull_result


def build_bundle(conn, picks: list[dict]) -> dict:
    album_ids = sorted(p["album_id"] for p in picks)
    track_ids, artist_ids = _collect_ids(conn, album_ids)
    return {
        "format": BUNDLE_FORMAT,
        "version": BUNDLE_VERSION,
        "identity_rule": IDENTITY_RULE,
        "manifest": [
            {"rank": p["rank"], "tier": p["tier"], "artist": p["artist"],
             "title": p["title"], "album_id": p["album_id"],
             "owned_on_master": p["owned_on_master"]}
            for p in sorted(picks, key=lambda p: p["rank"])
        ],
        "structural": _structural(conn, picks, album_ids, track_ids, artist_ids),
        "enrichment": {
            "artist_bios": _envelope(sq.pull_artist_bios(conn, artist_ids)),
            "artist_tags": _envelope(sq.pull_artist_tags(conn, artist_ids)),
            "similar_artists": _envelope(sq.pull_similar_artists(conn, artist_ids)),
        },
        "analysis": {
            "segments": _envelope(_pull_segments_chunked(conn, track_ids)),
            "audio_features": _envelope(sq.pull_audio_features(conn, track_ids)),
            "track_mbids": _envelope(sq.pull_track_mbids(conn, track_ids)),
        },
    }


_manifest_cache: dict = {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", action="store_true",
                        help="print the coverage table and exit without exporting")
    args = parser.parse_args()

    _manifest_cache.update(json.loads(MANIFEST_PATH.read_text(encoding="utf-8")))
    conn = psycopg2.connect(settings.database_url)
    try:
        picks = _resolve_picks(conn, _manifest_cache)
        fatal, warns, report = _coverage(conn, picks)

        shipped = [p for p in picks if not p["_problems"]]
        excluded = [p for p in picks if p["_problems"]]
        if args.report:
            for r in report:
                suffix = f" ({r['tracks']} tracks)" if r.get("tracks") else ""
                print(f"{r['status']:>14}  {r['label']}{suffix}")
            print()
        for w in warns:
            print(f"WARN  {w}")
        if fatal:
            print(f"\n{len(excluded)} pick(s) excluded for {len(fatal)} integrity gap(s):")
            for f in fatal:
                print(f"EXCLUDED  {f}")
        if args.report:
            print(f"\n{len(shipped)}/{len(picks)} picks would ship"
                  + ("" if excluded else " — coverage green"))
            return 1 if excluded else 0
        if not shipped:
            print("nothing to export — every pick is excluded")
            return 1

        bundle = build_bundle(conn, shipped)
    finally:
        conn.close()

    raw = json.dumps(bundle, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False).encode("utf-8")
    with open(BUNDLE_PATH, "wb") as fh:
        with gzip.GzipFile(fileobj=fh, mode="wb", mtime=0) as gz:
            gz.write(raw)
    seg_tracks = len(bundle["analysis"]["segments"]["items"])
    print(f"exported {len(shipped)}/{len(picks)} picks, {seg_tracks} analyzed tracks: "
          f"{BUNDLE_PATH} ({BUNDLE_PATH.stat().st_size / 1_048_576:.1f} MB gz, "
          f"{len(raw) / 1_048_576:.1f} MB raw)")
    return 1 if excluded else 0


if __name__ == "__main__":
    sys.exit(main())
