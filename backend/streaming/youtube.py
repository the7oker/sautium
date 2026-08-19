"""YouTube provider — ships in core (yt-dlp on non-DRM content is a ToS matter,
not §1201 anti-circumvention; it survived the 2020 RIAA takedown). DRM
providers (Deezer/Spotify) are NOT bundled — they live as external BYO modules.

HQPlayer doesn't decode AAC/m4a (tested), so we transcode YouTube's m4a/opus
source to FLAC via yt-dlp's built-in ffmpeg pass (``-x --audio-format flac``) —
lossless of the lossy source, and HQPlayer's native format. yt-dlp and ffmpeg
are external tools; their paths are configurable (user-installed).

yt-dlp is a moving target by design: YouTube changes its side every few weeks
and upstream's *stable* channel lags ("often stale and prone to external
breakage" — their words; the 2026-08 android_vr 403 hit stable only). Both
runtimes therefore track the *nightly* channel and refresh it at every start
(Docker: entrypoint.py; launcher: desktop/db_init.py), and ship deno — the
sandboxed JS runtime yt-dlp solves YouTube's player challenges in. Without a
runtime yt-dlp falls back to a deprecated, runtime-less extraction path that
is exactly what breaks when YouTube moves."""
from __future__ import annotations

import glob
import logging
import os
import re
import shutil
import subprocess
import tempfile

from .base import (FetchedAudio, ProviderError, ProviderManifest, ResolvedSource,
                   StreamProvider, TrackQuery)

logger = logging.getLogger(__name__)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


class YouTubeProvider(StreamProvider):
    manifest = ProviderManifest(
        id="youtube", name="YouTube", kind="direct_url", lossless=False,
    )

    def __init__(self, ytdlp_path: str = "yt-dlp",
                 ffmpeg_location: str | None = None, timeout: float = 120.0):
        # ffmpeg_location: directory containing ffmpeg, or None to use PATH.
        self._ytdlp = ytdlp_path
        self._ffmpeg_location = ffmpeg_location
        self._timeout = timeout
        # Advisory only — yt-dlp decides at run time; this just surfaces a
        # degraded install at boot instead of one cryptic 403 per track.
        if not shutil.which("deno"):
            logger.warning("youtube: deno (yt-dlp's JS runtime) is not on PATH — "
                           "extraction runs on the deprecated runtime-less path "
                           "and breaks whenever YouTube moves")

    SEARCH_N = 5    # candidates to score before downloading one

    def fetch(self, query: TrackQuery) -> FetchedAudio:
        return self._download(self._resolve(query).source_id)

    def _resolve(self, query: TrackQuery) -> ResolvedSource:
        """Pick the best video via a flat (no-download) search. We prefer the
        candidate whose length matches the MusicBrainz duration — this rejects
        remixes, extended/edited cuts and wrong tracks that the bare top hit
        would otherwise grab. Title and official-channel signals break ties."""
        search = f"{query.artist} {query.title}".strip()
        cands = self._flat_search(search)

        # Artist gate on the CHANNEL: YouTube full-text search returns wrong-artist
        # covers and same-title different songs for obscure artists; with duration-
        # dominant scoring one of those would win and stream the WRONG recording
        # (often not even downloadable — "video not available"). The reliable artist
        # signal is the channel — official "<Artist> - Topic" Art Tracks, VEVO, or
        # the artist's own channel — NOT the title (covers name the original artist
        # in their title, e.g. a "Duo Diamanti" upload titled "Musica Nuda - Lunedì").
        # Any MB-canonical alternate satisfies the gate — YouTube channels a band
        # under its canonical name even when our credit is a lineup variant. A
        # gate wipe-out retries the search ONCE under the canonical name; still
        # nothing → the real recording isn't on YouTube — reject rather than
        # stream a cover.
        gates = [g for g in (_norm(a) for a in (query.artist, *query.artist_alts)) if g]
        matched = ([c for c in cands if any(g in _norm(c["channel"]) for g in gates)]
                   if gates else cands)
        if not matched and query.artist_alts:
            alt = self._flat_search(f"{query.artist_alts[0]} {query.title}".strip())
            matched = [c for c in alt if any(g in _norm(c["channel"]) for g in gates)]
            cands = cands or alt
        if not cands:
            raise ProviderError(f"youtube: no results for {search!r}")
        if not matched:
            raise ProviderError(
                f"youtube: no artist match for {search!r} "
                f"(top hit channel {cands[0]['channel']!r})")

        best = max(matched, key=lambda c: self._score(c, query))
        # Known length but even the best is >50% off (different recording / DJ
        # mix / truncation) → no clean match; skip rather than stream the wrong
        # thing. The endpoint drops the track from the queue.
        if (query.duration and best["duration"]
                and abs(best["duration"] - query.duration) > 0.5 * query.duration):
            raise ProviderError(
                f"youtube: no length match for {search!r} "
                f"(want ~{query.duration:.0f}s, best {best['duration']:.0f}s)")
        logger.info("youtube resolve %r -> %s (%ss, %s)",
                    search, best["id"], best["duration"], best["channel"])
        return ResolvedSource(
            source_id=best["id"], duration=best["duration"],
            artwork_url=f"https://i.ytimg.com/vi/{best['id']}/hqdefault.jpg")

    def _flat_search(self, search: str) -> list[dict]:
        cmd = [self._ytdlp, f"ytsearch{self.SEARCH_N}:{search}", "--flat-playlist",
               "--no-warnings", "--quiet",
               "--print", "%(id)s\t%(title)s\t%(duration)s\t%(channel)s"]
        try:
            out = subprocess.run(cmd, check=True, timeout=self._timeout,
                                 capture_output=True, text=True).stdout
        except subprocess.TimeoutExpired as e:
            raise ProviderError(f"youtube search timeout: {search!r}") from e
        except subprocess.CalledProcessError as e:
            raise ProviderError(f"youtube search failed for {search!r}") from e

        cands = []
        for line in out.splitlines():
            p = line.split("\t")
            if len(p) < 4 or not p[0]:
                continue
            try:
                dur = float(p[2]) if p[2] not in ("NA", "", "None") else None
            except ValueError:
                dur = None
            cands.append({"id": p[0], "title": p[1], "duration": dur, "channel": p[3]})
        return cands

    def _score(self, c: dict, query: TrackQuery) -> float:
        s = 0.0
        if query.duration and c["duration"]:
            delta = abs(c["duration"] - query.duration)
            tol = max(7.0, 0.06 * query.duration)
            if delta <= tol:
                s += 100 - (delta / tol) * 15      # tight length match dominates
            elif delta <= 0.5 * query.duration:
                s += 50 - (delta - tol) * 0.5       # plausible but off
            else:
                s -= 200                            # mix / truncation / wrong
        title_l = query.title.lower()
        names_l = [a.lower() for a in (query.artist, *query.artist_alts) if a]
        ct = (c["title"] or "").lower()
        if title_l and title_l in ct:
            s += 20
        if any(a in ct for a in names_l):
            s += 12
        ch = (c["channel"] or "").lower()
        if ch.endswith("- topic") or "vevo" in ch or any(a in ch for a in names_l):
            s += 12                                 # official Art Track / channel
        return s

    def _download(self, video_id: str) -> FetchedAudio:
        # Download bestaudio -> ffmpeg -> FLAC (HQPlayer can't decode AAC). Temp
        # dir is a transient buffer, deleted at once — no persisted rip.
        url = f"https://www.youtube.com/watch?v={video_id}"
        with tempfile.TemporaryDirectory(prefix="sautium-yt-") as tmp:
            # Warnings stay on: they are read only on failure, and yt-dlp's
            # "no JS runtime" one names the actual cause of a 403.
            cmd = [self._ytdlp, url, "-x", "--audio-format", "flac",
                   "--no-playlist", "--quiet",
                   "-o", os.path.join(tmp, "t.%(ext)s")]
            if self._ffmpeg_location:
                cmd += ["--ffmpeg-location", self._ffmpeg_location]
            try:
                subprocess.run(cmd, check=True, timeout=self._timeout,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            except subprocess.TimeoutExpired as e:
                raise ProviderError(f"youtube download timeout: {video_id}") from e
            except subprocess.CalledProcessError as e:
                err = e.stderr.decode("utf-8", "replace") if e.stderr else ""
                lines = [ln for ln in err.splitlines() if ln.strip()]
                detail = lines[-1][-300:] if lines else ""
                if "No supported JavaScript runtime" in err:
                    detail += " (yt-dlp found no JS runtime — install deno)"
                raise ProviderError(f"youtube download failed for {video_id}: {detail}") from e
            flacs = glob.glob(os.path.join(tmp, "*.flac"))
            if not flacs:
                raise ProviderError(f"youtube: no audio for {video_id}")
            with open(flacs[0], "rb") as f:
                data = f.read()
        logger.info("youtube fetch ok: %s (%d KiB FLAC)", video_id, len(data) // 1024)
        # FLAC container, but a lossy source — lossless=False for provenance.
        return FetchedAudio(data=data, mime="audio/flac", lossless=False)
