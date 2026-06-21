#!/usr/bin/env python3
"""
HQPlayer Playback Tracker Daemon

Subscribes to HQPlayer events and tracks listening history:
- Monitors playback via HQPlayer XML API (subscribe mode)
- Records listening sessions to database
- Updates play_count when tracks are completed (>50% listened)

Architecture:
- Event-driven: subscribes to HQPlayer status updates (~1/sec)
- Playlist mapping: derived from HQPlayer's own PlaylistGet
  (URI → media_file), rebuilt on every track change — no external
  registration, so it survives tracker restarts and out-of-band queues
- Session tracking: monitors current track progress
- Database: writes to listening_history and updates tracks.play_count

Usage:
    python playback_tracker.py --hqplayer-host 172.26.80.1 --hqplayer-port 4321
"""

import argparse
import asyncio
import logging
import os
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Optional, Dict
from contextlib import asynccontextmanager

import psycopg2
import psycopg2.extras
import pylast
from aiohttp import web

# Add backend to path for imports
sys.path.insert(0, '/app')
from hqplayer_client import PlaybackState, HQPlayerClient, uri_to_file_path

# Logging to stderr only (never stdout in daemon)
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("playback-tracker")


class PlaybackSession:
    """Tracks a single listening session for a track"""

    # Last.fm scrobble rule: >50% OR >4 minutes (240 seconds), whichever comes first
    SCROBBLE_MIN_SECONDS = 240

    def __init__(self, track_id: int, started_at: datetime, track_length: float):
        self.track_id = track_id
        self.started_at = started_at
        self.track_length = track_length
        self.last_position = 0.0
        self.max_position = 0.0  # highest position reached
        self.scrobbled = False  # whether scrobble was already sent for this session

    @property
    def duration_listened(self) -> float:
        """Approximate seconds listened (may be less than max_position if seeking)"""
        return self.max_position

    @property
    def percent_listened(self) -> float:
        """Percentage of track listened (0-100)"""
        if self.track_length > 0:
            return min(100.0, (self.max_position / self.track_length) * 100)
        return 0.0

    @property
    def scrobble_ready(self) -> bool:
        """Last.fm rule: scrobble after >50% OR >4 minutes listened (min 30s)"""
        if self.max_position < 30:
            return False
        return (
            self.percent_listened >= 50
            or self.max_position >= self.SCROBBLE_MIN_SECONDS
        )

    @property
    def completed(self) -> bool:
        """Track counts as 'played' if scrobble-ready or reached end"""
        return self.scrobble_ready or (
            self.track_length > 0 and self.max_position >= self.track_length - 5
        )

    @property
    def skipped(self) -> bool:
        """Track was skipped if not scrobble-ready"""
        return not self.scrobble_ready

    def update_position(self, position: float):
        """Update playback position"""
        self.last_position = position
        self.max_position = max(self.max_position, position)


class LastFmScrobbler:
    """Last.fm scrobbling client — sends 'now playing' and scrobble events"""

    def __init__(self, api_key: str, api_secret: str, session_key: str, username: str):
        self.network = pylast.LastFMNetwork(
            api_key=api_key,
            api_secret=api_secret,
            session_key=session_key,
            username=username,
        )
        self.enabled = True
        logger.info(f"Last.fm scrobbler initialized for user '{username}'")

    def update_now_playing(self, artist: str, title: str, album: Optional[str] = None,
                           duration: Optional[int] = None):
        """Send 'now playing' notification to Last.fm"""
        try:
            self.network.update_now_playing(
                artist=artist,
                title=title,
                album=album,
                duration=duration,
            )
            logger.info(f"🎵 Last.fm now playing: {artist} - {title}")
        except Exception as e:
            logger.error(f"Last.fm now playing failed: {e}")

    def scrobble(self, artist: str, title: str, timestamp: int,
                 album: Optional[str] = None, duration: Optional[int] = None):
        """Scrobble a track to Last.fm"""
        try:
            self.network.scrobble(
                artist=artist,
                title=title,
                timestamp=timestamp,
                album=album,
                duration=duration,
            )
            logger.info(f"📡 Last.fm scrobbled: {artist} - {title}")
        except Exception as e:
            logger.error(f"Last.fm scrobble failed: {e}")


class PlaybackTracker:
    """Main daemon that tracks HQPlayer playback and records to database"""

    def __init__(
        self,
        hqplayer_host: str,
        hqplayer_port: int,
        db_host: str,
        db_port: int,
        db_user: str,
        db_password: str,
        db_name: str,
        http_port: int = 8765,
        lastfm_api_key: Optional[str] = None,
        lastfm_api_secret: Optional[str] = None,
        lastfm_session_key: Optional[str] = None,
        lastfm_username: Optional[str] = None,
    ):
        self.hqplayer_host = hqplayer_host
        self.hqplayer_port = hqplayer_port
        self.http_port = http_port

        # Database connection parameters
        self.db_params = {
            "host": db_host,
            "port": db_port,
            "user": db_user,
            "password": db_password,
            "dbname": db_name,
        }
        self.db_conn: Optional[psycopg2.extensions.connection] = None

        # Playlist mapping: queue position (0-based) → media_files.id, derived
        # from HQPlayer's own queue (PlaylistGet), not pushed by the backend —
        # stays correct no matter who filled the queue and survives restarts.
        self.playlist: Dict[int, int] = {}
        # Total length of HQPlayer's current queue (incl. positions whose file
        # isn't in our library) — lets us tell "past the end" from "in the
        # queue but unresolved".
        self.playlist_size: int = 0

        # Dedicated query connection to HQPlayer (separate socket from the
        # subscribe stream) for fetching the live playlist on demand.
        self.hqp_query = HQPlayerClient(
            host=hqplayer_host, port=hqplayer_port, timeout=5.0
        )

        # Current session
        self.current_session: Optional[PlaybackSession] = None
        self.current_track_index: Optional[int] = None

        # Last.fm scrobbler
        self.scrobbler: Optional[LastFmScrobbler] = None
        if lastfm_api_key and lastfm_api_secret and lastfm_session_key:
            try:
                self.scrobbler = LastFmScrobbler(
                    api_key=lastfm_api_key,
                    api_secret=lastfm_api_secret,
                    session_key=lastfm_session_key,
                    username=lastfm_username or "",
                )
            except Exception as e:
                logger.error(f"Failed to initialize Last.fm scrobbler: {e}")
        else:
            logger.info("Last.fm scrobbling disabled (missing credentials)")

        # Stats
        self.sessions_recorded = 0
        self.plays_counted = 0
        self.scrobbles_sent = 0

    def _get_db(self) -> psycopg2.extensions.connection:
        """Get or create database connection"""
        if self.db_conn is None or self.db_conn.closed:
            self.db_conn = psycopg2.connect(**self.db_params)
            self.db_conn.autocommit = True
            logger.info("Connected to PostgreSQL")
        return self.db_conn

    def _scrobbling_enabled(self) -> bool:
        """Read the lastfm.scrobbling_enabled preference from user_settings.

        Default True if absent so connected users keep prior behaviour.
        Read fresh on every scrobble attempt so toggling in the UI takes
        effect without restarting the tracker process."""
        try:
            conn = self._get_db()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT value FROM user_settings WHERE key = %s",
                    ("lastfm.scrobbling_enabled",),
                )
                row = cur.fetchone()
            if not row or row[0] is None:
                return True
            return bool(row[0])
        except Exception as e:
            logger.warning(f"Failed to read scrobbling toggle: {e}")
            return True

    def _get_track_metadata(self, track_id: int) -> Optional[dict]:
        """Get track artist, title, album, duration from DB for scrobbling.

        track_id is media_files.id (integer).
        """
        conn = self._get_db()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT t.title, mf.duration_seconds as duration, al.title as album,
                           a.name as artist, mf.track_id
                    FROM media_files mf
                    JOIN tracks t ON mf.track_id = t.id
                    JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
                    JOIN artists a ON ta.artist_id = a.id
                    JOIN album_variants av ON mf.album_variant_id = av.id
                    LEFT JOIN albums al ON av.album_id = al.id
                    WHERE mf.id = %s
                    """,
                    (track_id,),
                )
                return cur.fetchone()
        except Exception as e:
            logger.error(f"Failed to get track metadata: {e}")
            return None

    def _save_session(self, session: PlaybackSession):
        """Save listening session to database and scrobble to Last.fm"""
        conn = self._get_db()

        try:
            # Get track_id for the media_file
            meta = self._get_track_metadata(session.track_id)
            track_id = meta["track_id"] if meta else None

            with conn.cursor() as cur:
                # Insert listening history
                cur.execute(
                    """
                    INSERT INTO listening_history
                        (media_file_id, track_id, started_at, ended_at,
                         duration_listened, percent_listened, completed, skipped)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        session.track_id,
                        track_id,
                        session.started_at,
                        datetime.now(),
                        session.duration_listened,
                        session.percent_listened,
                        session.completed,
                        session.skipped,
                    ),
                )

                # Update local_play_stats
                if session.completed:
                    if track_id:
                        cur.execute(
                            """
                            INSERT INTO local_play_stats
                                (track_id, play_count, skip_count,
                                 total_listen_time, avg_percent_listened, last_played_at)
                            VALUES (%s, 1, 0, %s, %s, %s)
                            ON CONFLICT (track_id) DO UPDATE SET
                                play_count = local_play_stats.play_count + 1,
                                total_listen_time = local_play_stats.total_listen_time + EXCLUDED.total_listen_time,
                                avg_percent_listened = (
                                    local_play_stats.avg_percent_listened * local_play_stats.play_count
                                    + EXCLUDED.avg_percent_listened
                                ) / (local_play_stats.play_count + 1),
                                last_played_at = EXCLUDED.last_played_at,
                                updated_at = CURRENT_TIMESTAMP
                            """,
                            (track_id, session.duration_listened,
                             session.percent_listened, datetime.now()),
                        )
                    self.plays_counted += 1
                    logger.info(
                        f"✅ Track {session.track_id} completed "
                        f"({session.percent_listened:.1f}% listened) — play_count++"
                    )

                    # Scrobble to Last.fm (if not already scrobbled in real-time).
                    # Check the user_settings toggle each time so the UI's
                    # Scrobbling switch takes effect without restarting the
                    # tracker.
                    if self.scrobbler and not session.scrobbled and self._scrobbling_enabled():
                        meta = self._get_track_metadata(session.track_id)
                        if meta:
                            timestamp = int(session.started_at.timestamp())
                            duration = int(meta["duration"]) if meta.get("duration") else None
                            self.scrobbler.scrobble(
                                artist=meta["artist"],
                                title=meta["title"],
                                timestamp=timestamp,
                                album=meta.get("album"),
                                duration=duration,
                            )
                            session.scrobbled = True
                            self.scrobbles_sent += 1
                else:
                    # Track was skipped — update skip_count
                    if track_id:
                        cur.execute(
                            """
                            INSERT INTO local_play_stats
                                (track_id, play_count, skip_count,
                                 total_listen_time, avg_percent_listened, last_played_at)
                            VALUES (%s, 0, 1, %s, %s, %s)
                            ON CONFLICT (track_id) DO UPDATE SET
                                skip_count = local_play_stats.skip_count + 1,
                                updated_at = CURRENT_TIMESTAMP
                            """,
                            (track_id, session.duration_listened,
                             session.percent_listened, datetime.now()),
                        )
                    logger.info(
                        f"⏭️  Track {session.track_id} skipped "
                        f"({session.percent_listened:.1f}% listened)"
                    )

                self.sessions_recorded += 1

        except Exception as e:
            logger.error(f"Failed to save session: {e}")

    async def _connect_hqplayer(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Connect to HQPlayer and subscribe to events"""
        reader, writer = await asyncio.open_connection(self.hqplayer_host, self.hqplayer_port)

        # Send subscribe command
        subscribe_cmd = '<Status subscribe="1" />'
        writer.write(subscribe_cmd.encode("utf-8"))
        await writer.drain()

        logger.info(f"Subscribed to HQPlayer events at {self.hqplayer_host}:{self.hqplayer_port}")
        return reader, writer

    async def _handle_event(self, event: ET.Element):
        """Process HQPlayer status event"""
        if event.tag != "Status":
            return

        try:
            state = PlaybackState(int(event.get("state", 0)))
            track_index = int(event.get("track", -1))
            position = float(event.get("position", 0.0))
            length = float(event.get("length", 0.0))

            # Only track when playing
            if state != PlaybackState.PLAYING:
                # If we had a session, finish it
                if self.current_session:
                    logger.info(f"Playback stopped/paused — finishing session")
                    self._save_session(self.current_session)
                    self.current_session = None
                    self.current_track_index = None
                return

            # Track changed?
            if track_index != self.current_track_index:
                # Finish previous session
                if self.current_session:
                    self._save_session(self.current_session)

                # Rebuild the position→media_file map from HQPlayer's live
                # queue so it always reflects what's actually playing,
                # independent of how the queue was filled (web UI, desktop,
                # pre-existing).
                await self._rebuild_playlist_from_hqplayer()
                # HQPlayer's Status 'track' is 1-based: track=1 is the first
                # queue item; track=0 is a transient pre-play/stopped marker.
                # The map is 0-based (enumerate over PlaylistGet), so shift by
                # one to recover the queue position.
                queue_pos = track_index - 1
                track_id = self.playlist.get(queue_pos)
                if track_id is None:
                    if 0 <= queue_pos < self.playlist_size:
                        # In the queue, but this file isn't in our library.
                        logger.warning(
                            f"HQPlayer track={track_index} not in library — cannot track."
                        )
                    else:
                        # track=0 pre-play marker, or HQPlayer stepped past the
                        # end of the queue — normal lifecycle, not an anomaly.
                        logger.debug(
                            f"HQPlayer track={track_index} outside queue "
                            f"(pos {queue_pos}, size {self.playlist_size}) — no session"
                        )
                    self.current_session = None
                    self.current_track_index = track_index
                    return

                self.current_session = PlaybackSession(
                    track_id=track_id, started_at=datetime.now(), track_length=length
                )
                self.current_track_index = track_index

                # Get track metadata for logging and Last.fm "now playing"
                meta = self._get_track_metadata(track_id)
                if meta:
                    logger.info(
                        f"▶️  Started tracking: {meta['artist']} - {meta['title']} "
                        f"(track_id={track_id}, pos={queue_pos})"
                    )
                    # Send "now playing" to Last.fm
                    if self.scrobbler:
                        duration = int(meta["duration"]) if meta.get("duration") else None
                        self.scrobbler.update_now_playing(
                            artist=meta["artist"],
                            title=meta["title"],
                            album=meta.get("album"),
                            duration=duration,
                        )
                else:
                    logger.info(
                        f"▶️  Started tracking track_id={track_id} (index={track_index})"
                    )

            # Update position and check for real-time scrobble
            if self.current_session:
                self.current_session.update_position(position)

                # Scrobble as soon as threshold is reached (don't wait for track end)
                if (
                    self.scrobbler
                    and not self.current_session.scrobbled
                    and self.current_session.scrobble_ready
                ):
                    meta = self._get_track_metadata(self.current_session.track_id)
                    if meta:
                        timestamp = int(self.current_session.started_at.timestamp())
                        duration = int(meta["duration"]) if meta.get("duration") else None
                        self.scrobbler.scrobble(
                            artist=meta["artist"],
                            title=meta["title"],
                            timestamp=timestamp,
                            album=meta.get("album"),
                            duration=duration,
                        )
                        self.current_session.scrobbled = True
                        self.scrobbles_sent += 1

        except Exception as e:
            logger.error(f"Error handling event: {e}")

    async def _rebuild_playlist_from_hqplayer(self):
        """Rebuild index→media_file from HQPlayer's live PlaylistGet.

        HQPlayer is the single source of truth for the queue; each position
        maps to a media_files row by the file path decoded from its URI, via
        one batched lookup over the unique file_path index."""
        tracks = await asyncio.to_thread(self._fetch_hqp_playlist)
        paths = {
            idx: uri_to_file_path(t["uri"])
            for idx, t in enumerate(tracks)
            if t.get("uri")
        }
        mapping: Dict[int, int] = {}
        if paths:
            conn = self._get_db()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT file_path, id FROM media_files WHERE file_path = ANY(%s)",
                    (list(set(paths.values())),),
                )
                path_to_id = {fp: mid for fp, mid in cur.fetchall()}
            mapping = {
                idx: path_to_id[p] for idx, p in paths.items() if p in path_to_id
            }
        self.playlist = mapping
        self.playlist_size = len(tracks)
        resolved, total = len(mapping), len(tracks)
        msg = f"Playlist mapping rebuilt from HQPlayer: {resolved}/{total} tracks resolved"
        if total - resolved:
            msg += f" ({total - resolved} not in library)"
        logger.info(msg)

    def _fetch_hqp_playlist(self) -> list:
        """Blocking PlaylistGet over the dedicated query socket."""
        try:
            if not self.hqp_query.is_connected() and not self.hqp_query.connect():
                logger.warning("Could not connect to HQPlayer for PlaylistGet")
                return []
            return self.hqp_query.get_playlist()
        except Exception as e:
            logger.warning(f"PlaylistGet failed: {e}")
            self.hqp_query.disconnect()  # force a clean reconnect next time
            return []

    async def _event_loop(self):
        """Main event loop: connect to HQPlayer and process events"""
        while True:
            try:
                reader, writer = await self._connect_hqplayer()

                buffer = b""
                while True:
                    chunk = await reader.read(4096)
                    if not chunk:
                        logger.warning("HQPlayer connection closed")
                        break

                    buffer += chunk

                    # Process all complete messages
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        xml_str = line.decode("utf-8").strip()

                        if not xml_str:
                            continue

                        try:
                            event = ET.fromstring(xml_str)
                            await self._handle_event(event)
                        except ET.ParseError as e:
                            logger.error(f"XML parse error: {e}")

            except Exception as e:
                logger.error(f"Event loop error: {e}")
                logger.info("Reconnecting in 5 seconds...")
                await asyncio.sleep(5)

    # ========== HTTP API for playlist registration ==========

    async def _http_get_stats(self, request):
        """HTTP endpoint: GET /stats"""
        return web.json_response(
            {
                "status": "running",
                "playlist_tracks": len(self.playlist),
                "sessions_recorded": self.sessions_recorded,
                "plays_counted": self.plays_counted,
                "scrobbles_sent": self.scrobbles_sent,
                "lastfm_enabled": self.scrobbler is not None,
                "current_session": {
                    "track_id": self.current_session.track_id if self.current_session else None,
                    "track_index": self.current_track_index,
                    "percent_listened": (
                        self.current_session.percent_listened if self.current_session else 0
                    ),
                }
                if self.current_session
                else None,
            }
        )

    async def run(self):
        """Run daemon: start HTTP server + event loop"""
        # Setup HTTP server
        app = web.Application()
        app.router.add_get("/stats", self._http_get_stats)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.http_port)
        await site.start()

        logger.info(f"🌐 HTTP API listening on port {self.http_port}")
        logger.info(f"   GET  /stats    - get daemon statistics")

        # Run event loop
        await self._event_loop()


async def main():
    parser = argparse.ArgumentParser(description="HQPlayer playback tracker daemon")
    parser.add_argument(
        "--hqplayer-host", default="localhost", help="HQPlayer host (default: localhost)"
    )
    parser.add_argument(
        "--hqplayer-port", type=int, default=4321, help="HQPlayer port (default: 4321)"
    )
    parser.add_argument("--db-host", default="localhost", help="PostgreSQL host")
    parser.add_argument("--db-port", type=int, default=5432, help="PostgreSQL port")
    parser.add_argument("--db-user", default="musicai", help="PostgreSQL user")
    parser.add_argument("--db-password", default="supervisor", help="PostgreSQL password")
    parser.add_argument("--db-name", default="music_ai", help="PostgreSQL database name")
    parser.add_argument(
        "--http-port", type=int, default=8765, help="HTTP API port (default: 8765)"
    )

    args = parser.parse_args()

    tracker = PlaybackTracker(
        hqplayer_host=args.hqplayer_host,
        hqplayer_port=args.hqplayer_port,
        db_host=args.db_host,
        db_port=args.db_port,
        db_user=args.db_user,
        db_password=args.db_password,
        db_name=args.db_name,
        http_port=args.http_port,
        lastfm_api_key=os.environ.get("LASTFM_API_KEY"),
        lastfm_api_secret=os.environ.get("LASTFM_API_SECRET"),
        lastfm_session_key=os.environ.get("LASTFM_SESSION_KEY"),
        lastfm_username=os.environ.get("LASTFM_USERNAME"),
    )

    logger.info("🎵 HQPlayer Playback Tracker starting...")
    await tracker.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 Shutting down...")
        sys.exit(0)
