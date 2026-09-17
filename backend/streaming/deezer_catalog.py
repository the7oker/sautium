"""Deezer public API — catalog lookup and 30 s previews, no auth.

The recording's identity on the catalog, shared by every provider that fetches
from it: the bring-your-own lossless module (closed, out of tree — the
decryption lives in a tool the user installs) and the core excerpt provider
(``deezer_preview.py``) resolve the SAME way. A track is one catalog id
whichever of them serves it, and a fetch-time fallback to the excerpt finds
its id in the memo the first resolve filled — one API pass per track, one
pacer for the quota the two share with photo enrichment (``covers.py``).

Precision first. What streams from here is analysed, SIGNED against the
stream's own pcm_hash and synced — a wrong recording does not merely play
wrong: it is published as first-hand analysis of a track it is not. The audit
of 2026-08-30 caught a search-and-score resolve handing back another artist's
song at a fitting length (Björk's "Búkolla" → a Helgi Björnsson track sharing
not a word of the title) and one concerto's movements from two pianists'
recordings. Hence three tiers, each a positive identification, none a
ranking:

  0. barcode — the phantom's MB editions' barcodes against the catalog's UPC
     lookup: the release itself, nothing searched;
  1. album — a title search whose candidates are read for their tracklists,
     accepted only when the phantom's tracklist lines up with one (title,
     catalog length, track by track). This is how a composer-credited album
     resolves to the performance MB lists when no name we hold matches the
     pianist the catalog files it under;
  2. track — the per-track search, with the artist's name, the title and the
     version suffix each REQUIRED to agree and the length gate last. A
     candidate short of any one is no candidate.

Tiers 0–1 need the tracklist, so they run in ``resolve_batch`` — the host's
album-scoped pass. A lone ``resolve`` (a fetch-time fallback) has tier 2 and
whatever album a batch already established.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from abc import abstractmethod
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from .base import (FetchedAudio, ProviderError, ProviderUnavailable, ResolvedSource,
                   StreamProvider, TrackQuery, attested_lengths, fits_length,
                   length_offset, norm_key, other_recording_words, tokens_shared,
                   version_claims_same)

logger = logging.getLogger(__name__)

_SEARCH_API = "https://api.deezer.com/search"
_ALBUM_SEARCH_API = "https://api.deezer.com/search/album"
_ALBUM_API = "https://api.deezer.com/album/{}"
_TRACK_API = "https://api.deezer.com/track/{}"
_ISRC_API = "https://api.deezer.com/2.0/track/isrc:{}"

# The api_cooldown source every caller of this API host is metered under —
# photo enrichment arms it on a 429 (covers.py); providers naming it are
# demoted while it cools and reported silent once (service.providers_preferred).
COOLDOWN_SOURCE = "deezer"


class _Pace:
    """The API allows ~50 requests per 5 s per address, and past it answers
    HTTP 200 with error code 4 for EVERY call until the window clears. The
    host resolves several tracks at a time, two to three calls each — a big
    queue build burned the window in its first second and left the rest of
    the pass unanswered (38 of 625 on 2026-08-29). One bucket shared by every
    thread; holding the lock through the sleep is the point, it serialises the
    callers onto the API's pace."""

    def __init__(self, per_window: int = 40, window: float = 5.0):
        self._per_window, self._window = per_window, window
        self._lock = threading.Lock()
        self._stamps: deque = deque()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            while self._stamps and now - self._stamps[0] >= self._window:
                self._stamps.popleft()
            if len(self._stamps) >= self._per_window:
                time.sleep(self._window - (now - self._stamps[0]))
                now = time.monotonic()
                while self._stamps and now - self._stamps[0] >= self._window:
                    self._stamps.popleft()
            self._stamps.append(now)


class DeezerCatalog:
    """Resolution against the public API — no provider contract, no audio:
    the piece a lossless fetcher and an excerpt fetcher have in common."""

    SEARCH_N = 6
    ARTIST_SCAN_N = 50   # broad artist-catalog fallback when token search misses
    # Two threads saturate the API's ~8 req/s at ~150 ms per call (measured
    # 2026-08-29: 1 thread 161 ms/track, 2 -> 136, 8 -> 130). More only
    # queues behind the pacer and shares the quota with Discovery's own calls.
    RESOLVE_WORKERS = 2
    # The album tiers (see the module docstring). Barcodes tried, then album
    # candidates a title search yields, of which the likeliest are read for
    # their tracklists — each read is an API call against the same quota.
    UPC_TRIES = 2
    ALBUM_SEARCH_N = 6
    ALBUM_FETCH_N = 3
    # Tracks of the phantom's tracklist a catalog album must line up on — same
    # title, catalog length — before it is that album: ALBUM_CONSENSUS (and
    # half the tracklist) when a name we hold already vouches for it,
    # ALBUM_STRONG (and three quarters) when nothing but the tracklist does.
    ALBUM_CONSENSUS = 2
    ALBUM_STRONG = 3
    _ALBUM_TTL_S = 3600.0

    def __init__(self) -> None:
        self._pace = _Pace()
        self._albums: dict = {}             # our album_id -> (ts, {track_id: ResolvedSource})
        self._albums_lock = threading.Lock()
        # Catalog track id -> (ts, 30 s preview URL), noted off every track
        # object that passes through a resolve, so the excerpt fetch of a
        # track the pass already identified costs no further call. The URLs
        # are signed with an expiry of hours; the album TTL keeps well inside.
        self._previews: dict = {}
        self._previews_lock = threading.Lock()

    # ---- the resolve entry points -----------------------------------------
    def resolve(self, query: TrackQuery) -> ResolvedSource:
        if query.isrc:
            hit = self._by_isrc(query.isrc)
            if hit:
                tid, dur, art = hit
                logger.info("deezer resolve %r via ISRC -> %s", query.title, tid)
                return ResolvedSource(source_id=tid, duration=dur, artwork_url=art)
        known = self._album_cached(query.album_id) if query.album_id else None
        if known and query.track_id in known:
            return known[query.track_id]
        return self._resolve_track(query)

    def resolve_batch(self, queries: list, wanted: list) -> list:
        """Resolve the `wanted` indices of `queries`; the rest of the list is
        the albums those tracks sit on, the evidence of tiers 0–1. Returns a
        list parallel to `wanted`: ResolvedSource, None (no match) or a
        ProviderUnavailable (no answer — not a miss)."""
        if not wanted:
            return []
        wanted_set = set(wanted)
        groups: dict = {}
        for i, q in enumerate(queries):
            if q.album_id and q.album:
                groups.setdefault(q.album_id, []).append(i)
        found: dict = {}
        for aid, idxs in groups.items():
            if not wanted_set.intersection(idxs):
                continue
            try:
                found[aid] = self._album(aid, [queries[i] for i in idxs])
            except ProviderUnavailable as e:
                found[aid] = e

        def one(i):
            q = queries[i]
            hit = found.get(q.album_id)
            if isinstance(hit, ProviderUnavailable):
                return hit
            if hit and q.track_id in hit:
                return hit[q.track_id]
            try:
                return self._resolve_track(q)
            except ProviderUnavailable as e:
                return e
            except ProviderError as e:
                logger.info("%s", e)
                return None

        with ThreadPoolExecutor(max_workers=min(self.RESOLVE_WORKERS, len(wanted)),
                                thread_name_prefix="deezer-resolve") as ex:
            return list(ex.map(one, wanted))

    # ---- the 30 s preview clip ---------------------------------------------
    def preview_url(self, track_id: str) -> str:
        """The catalog's own 30 s clip of a track — the URL every track
        object carries, remembered from the resolve that found the track or
        read off the track itself. A track the catalog publishes no clip for
        is a ProviderError: nothing to fetch."""
        with self._previews_lock:
            hit = self._previews.get(str(track_id))
        if hit and time.monotonic() - hit[0] < self._ALBUM_TTL_S:
            return hit[1]
        data = self._get(_TRACK_API.format(urllib.parse.quote(str(track_id))))
        url = self._note_preview(data)
        if not url:
            raise ProviderError(f"deezer: no preview clip for {track_id}")
        return url

    def _note_preview(self, track: dict) -> str:
        url = (track or {}).get("preview") or ""
        tid = (track or {}).get("id")
        if url and tid:
            with self._previews_lock:
                now = time.monotonic()
                if len(self._previews) > 4096:
                    for k, (ts, _u) in list(self._previews.items()):
                        if now - ts >= self._ALBUM_TTL_S:
                            self._previews.pop(k, None)
                self._previews[str(tid)] = (now, url)
        return url

    # ---- tiers 0–1: the album ---------------------------------------------
    def _album(self, album_id: str, group: list) -> dict:
        """The catalog's recordings for one phantom album — `{track_id:
        ResolvedSource}`, empty when the catalog has no album that lines up
        with the tracklist. Remembered per album for the chain's lifetime: the
        album page, its Stream all and every row clicked on it ask for the
        same album, and an empty answer is an answer too."""
        known = self._album_cached(album_id)
        if known is not None:
            return known
        found = self._find_album(group)
        now = time.time()
        with self._albums_lock:
            for k, (ts, _m) in list(self._albums.items()):
                if now - ts >= self._ALBUM_TTL_S:
                    self._albums.pop(k, None)
            self._albums[album_id] = (now, found)
        return found

    def _album_cached(self, album_id: str):
        with self._albums_lock:
            hit = self._albums.get(album_id)
        if hit and time.time() - hit[0] < self._ALBUM_TTL_S:
            return hit[1]
        return None

    def _find_album(self, group: list) -> dict:
        lead, n = group[0], len(group)
        for code in lead.barcodes[:self.UPC_TRIES]:
            album = self._album_by_upc(code)
            if album is None:
                continue
            pairs, _, refused = self._corroborate(group, album)
            if 2 * len(pairs) >= n:
                logger.info("deezer album %r -> %s by barcode %s (%d/%d tracks line up)",
                            lead.album, album["id"], code, len(pairs), n)
                return self._mapping(pairs, album)
            logger.info("deezer: barcode %s names album %s (%r) but %d/%d tracks line "
                        "up — not the tracklist%s", code, album["id"], album.get("title"),
                        len(pairs), n, self._refused_note(refused))
        best = None
        for stub in self._search_albums(lead, n)[:self.ALBUM_FETCH_N]:
            album = self._album_by_id(stub["id"])
            if album is None:
                continue
            pairs, attested, refused = self._corroborate(group, album)
            m = len(pairs)
            ok = ((m >= min(self.ALBUM_CONSENSUS, n) and 2 * m >= n) if attested
                  else (m >= self.ALBUM_STRONG and 4 * m >= 3 * n))
            logger.info("deezer album %r ~ %s (%r by %s): %d/%d tracks line up, %s%s%s",
                        lead.album, album["id"], album.get("title"),
                        (album.get("artist") or {}).get("name"), m, n,
                        "attested" if attested else "unattested",
                        "" if ok else " — not it", self._refused_note(refused))
            if ok and (best is None or m > len(best[0])):
                best = (pairs, album)
                if m == n:
                    break
        return self._mapping(*best) if best else {}

    @staticmethod
    def _refused_note(refused: set) -> str:
        """For the log: the version suffixes that kept length-fitting tracks
        out — the evidence the safe-word list grows from."""
        return ("; versions refused: " + ", ".join(sorted(refused))) if refused else ""

    def _mapping(self, pairs: list, album: dict) -> dict:
        art = album.get("cover_xl") or album.get("cover_big")
        out = {}
        for q, t in pairs:
            self._note_preview(t)
            if q.track_id:
                out[q.track_id] = ResolvedSource(source_id=str(t["id"]),
                                                 duration=t.get("duration"), artwork_url=art)
        return out

    def _search_albums(self, lead: TrackQuery, n: int) -> list:
        """Album candidates for the phantom, likeliest first: filed under a
        name we hold, titled like ours, sized like ours. The artist-filtered
        search first; the loose one only when it yields no such album — for
        a composer-credited album the artist filter answers with every
        pianist's recording of the works, while the bare words find the
        performance MB lists (Le Sage's Mozart, 2026-08-30)."""
        owners = [a for a in lead.album_artists if a] or [lead.artist]
        names = self._names([lead])
        stubs, seen = [], set()

        def add(rows):
            for s in rows:
                if s["id"] not in seen:
                    seen.add(s["id"])
                    stubs.append(s)

        add(self._search_albums_one(f'artist:"{owners[0]}" album:"{lead.album}"'))
        if not any(norm_key(s["artist"]) in names and tokens_shared(lead.album, s["title"]) >= 0.5
                   for s in stubs):
            add(self._search_albums_one(f"{owners[0]} {lead.album}"))
        ours = other_recording_words(lead.album)
        stubs = [s for s in stubs if not (other_recording_words(s["title"]) - ours)]
        stubs.sort(key=lambda s: (norm_key(s["artist"]) not in names,
                                  -tokens_shared(lead.album, s["title"]),
                                  abs((s.get("nb_tracks") or 0) - n)))
        return stubs

    def _search_albums_one(self, q: str) -> list:
        url = _ALBUM_SEARCH_API + "?" + urllib.parse.urlencode(
            {"q": q, "limit": self.ALBUM_SEARCH_N})
        return [{"id": d["id"], "title": d.get("title", ""),
                 "artist": (d.get("artist") or {}).get("name", ""),
                 "nb_tracks": d.get("nb_tracks")}
                for d in self._get(url).get("data", []) if d.get("id")]

    def _album_by_upc(self, code: str):
        try:
            return self._album_complete(
                self._get(_ALBUM_API.format("upc:" + urllib.parse.quote(code))))
        except ProviderError:
            return None                     # "no data": no album under that barcode

    def _album_by_id(self, album_id):
        try:
            return self._album_complete(self._get(_ALBUM_API.format(album_id)))
        except ProviderError:
            return None

    def _album_complete(self, album: dict) -> dict:
        """The album with its WHOLE tracklist — the embedded one is a page,
        and a 40-track soundtrack lines up on the rows past it."""
        tracks = (album.get("tracks") or {}).get("data") or []
        nb = album.get("nb_tracks") or 0
        if nb > len(tracks):
            url = _ALBUM_API.format(album["id"]) + "/tracks?" + urllib.parse.urlencode({"limit": nb})
            tracks = self._get(url).get("data") or tracks
        album["tracks"] = {"data": tracks}
        return album

    def _corroborate(self, group: list, album: dict) -> tuple:
        """Line the phantom's tracklist up against a catalog album's. Returns
        `(pairs, attested, refused)`: pairs `[(query, catalog track)]` matched
        one to one on title and catalog length, closest length first; attested — a
        name we hold is the album's artist or one of its contributors, or the
        album's own track credits agree with ours on ALBUM_CONSENSUS tracks
        (a compilation's identity is its tracks'). "Various Artists" vouches
        for nothing: a karaoke compilation carries it too. `refused` — the
        version suffixes that alone kept a length-fitting track out."""
        ours = self._names(group)
        refused: set = set()
        fronted = [album.get("artist") or {}] + list(album.get("contributors") or [])
        attested = any(norm_key(p.get("name", "")) in ours for p in fronted)
        tracks = [t for t in album["tracks"]["data"] if t.get("duration")]
        aligned = len(tracks) == len(group)
        used, pairs, credited = set(), [], 0
        for pos, q in enumerate(group):
            best = None
            for j, t in enumerate(tracks):
                if j in used or not fits_length(q, t["duration"]):
                    continue
                if not self._title_agrees(q, t, aligned and j == pos, refused):
                    continue
                off = length_offset(q, t["duration"]) or 0.0
                if best is None or off < best[0]:
                    best = (off, j)
            if best is not None:
                used.add(best[1])
                t = tracks[best[1]]
                pairs.append((q, t))
                if norm_key((t.get("artist") or {}).get("name", "")) in ours:
                    credited += 1
        return pairs, attested or credited >= self.ALBUM_CONSENSUS, refused

    def _title_agrees(self, q: TrackQuery, t: dict, aligned: bool, refused: set) -> bool:
        """Whether a catalog track's title is the query's — the same claim.
        Equal outright, version and all; or equal on the short title with a
        version suffix that can be the same recording (base.version_claims_same);
        or, on an album whose length and order already line up with ours, a
        title sharing half its words — a catalog fronts a classical movement
        with its work, or the work with its composer, and no key equality
        survives "Te Deum, Op. 22 : Berlioz: Te Deum, Op. 22: Tibi omnes"
        against MB's "Te Deum, op. 22: Tibi omnes (Hymn)". The length was
        tested before this is asked, so a version rejection here is the
        deciding one and goes into `refused` for the caller's log — that is
        how the safe-word list grows."""
        ours = norm_key(q.title)
        if not ours:
            return False
        title = t.get("title") or ""
        if norm_key(title) == ours:
            return True
        version = t.get("title_version") or ""
        if not version_claims_same(version, q.title):
            refused.add(version)
            return False
        short = norm_key(t.get("title_short") or "")
        if short and short == ours:
            return True
        return (aligned
                and not (other_recording_words(title) - other_recording_words(q.title))
                and tokens_shared(q.title, title) >= 0.5)

    def _names(self, group: list) -> set:
        """Every name a catalog may file the group's recordings under.

        Includes the PERFORMERS where MB names them apart from the composer: a
        catalog files a work under the ensemble playing it as readily as under
        whoever wrote it, and our album knows only the latter."""
        return {norm_key(x) for q in group
                for x in (q.artist, *q.artist_alts, *q.album_artists, *q.performers)
                if x} - {"", "variousartists"}

    # ---- tier 2: the track ------------------------------------------------
    def _resolve_track(self, query: TrackQuery) -> ResolvedSource:
        names = self._names([query])
        # The SEARCH still runs under the credit — the catalog files a work
        # under its performers, and the composer's name is what finds every
        # reading of it; `names` is the gate that then keeps only ours.
        credits = [a for a in (query.artist, *query.artist_alts) if a]
        listed: list = []
        refused: set = set()
        for artist in credits or [""]:
            listed = self._search(query, artist)
            if listed:
                break
        fit = [c for c in listed if self._track_agrees(query, c, names, refused)]
        if not fit:
            for artist in credits[:2]:
                scan = self._artist_scan(query, artist)
                listed = listed or scan
                fit = [c for c in scan if self._track_agrees(query, c, names, refused)]
                if fit:
                    break
        if not listed:
            raise ProviderError(f"deezer: no results for {query.artist} - {query.title}")
        if not fit:
            top = listed[0]
            raise ProviderError(
                f"deezer: nothing attested for {query.artist} - {query.title} (catalog "
                f"{'/'.join(f'{w:.0f}s' for w in attested_lengths(query)) or '?'}; "
                f"top hit {top['artist']!r} - {top['title']!r}, {top['duration']}s"
                f"{self._refused_note(refused)})")
        best = max(fit, key=lambda c: self._score(c, query))
        logger.info("deezer resolve %r -> %s (%ss, %s)",
                    query.title, best["id"], best["duration"], best["artist"])
        return ResolvedSource(source_id=str(best["id"]), duration=best["duration"],
                              artwork_url=best.get("artwork"))

    def _track_agrees(self, query: TrackQuery, c: dict, names: set, refused: set) -> bool:
        """The per-track gate: the artist's name, the length, then the title
        and version — each a positive identification. A hit that scores well
        on two of them is not the recording."""
        return (norm_key(c["artist"]) in names
                and fits_length(query, c["duration"])
                and self._title_agrees(query, c, False, refused))

    def _by_isrc(self, isrc: str):
        """(source_id, duration_seconds, artwork_url) for an ISRC, or None. The
        ISRC track object carries the exact catalog duration + album cover."""
        try:
            data = self._get(_ISRC_API.format(urllib.parse.quote(isrc)))
            tid = data.get("id")
            album = data.get("album") or {}
            self._note_preview(data)
            return ((str(tid), data.get("duration"),
                     album.get("cover_xl") or album.get("cover_big"))
                    if tid else None)
        except Exception:
            return None

    def _search(self, query: TrackQuery, artist: str) -> list:
        advanced = f'artist:"{artist}" track:"{query.title}"'
        loose = f"{artist} {query.title}".strip()
        # With a known duration we can disambiguate, so widen the net: the right
        # recording is often credited to a RELATED artist (an OST track credited to
        # the singer but released under their band) — absent from the artist-exact
        # search, present only in the loose one. Merge both pools and let the
        # per-track gate pick; the artist-exact pool alone can be a mis-credited
        # cover at the wrong length (a "Last Christmas" kids cover credited to
        # George Michael beat the real Wham! recording). Without a duration we
        # can't tell them apart, so keep the precise-first short-circuit.
        if query.duration:
            pool = self._search_one(advanced, self.SEARCH_N)
            # The artist-exact pool is enough when it already holds the
            # recording — right length, right artist; the wider search costs
            # a second API call per track against a quota the resolve pass
            # shares across threads. Anything less (a mis-credited cover at
            # the wrong length, a recording filed under a related artist)
            # still merges the loose pool in.
            na = self._norm(artist)
            if any(fits_length(query, c["duration"]) and na and na in self._norm(c["artist"])
                   for c in pool):
                return pool
            seen = {c["id"] for c in pool}
            pool += [c for c in self._search_one(loose, self.SEARCH_N) if c["id"] not in seen]
            return pool
        for q in (advanced, loose):
            cands = self._search_one(q, self.SEARCH_N)
            if cands:
                return cands
        return []

    def _artist_scan(self, query: TrackQuery, artist: str) -> list:
        """Last resort: scan the artist's catalog and keep only tracks whose title
        matches ours once spacing/punctuation is normalized away. The token
        search misses spelling splits like a catalog's "Ghost Swimming" vs our
        MusicBrainz "Ghostswimming", but the artist's catalog still holds the
        track; the per-track gate then disambiguates by duration. Title-anchored
        so it can't false-positive on a same-length but different song."""
        nt = self._norm(query.title)
        na = self._norm(artist)
        if not (na and nt):
            return []
        scan = self._search_one(f'artist:"{artist}"', self.ARTIST_SCAN_N)
        return [c for c in scan
                if self._title_matches(nt, c["title"])
                and na in self._norm(c["artist"])]

    def _search_one(self, q: str, limit: int) -> list:
        url = _SEARCH_API + "?" + urllib.parse.urlencode({"q": q, "limit": limit})
        data = self._get(url).get("data", [])
        out = []
        for d in data:
            if not d.get("id"):
                continue
            self._note_preview(d)
            out.append({"id": d["id"], "title": d.get("title", ""),
                        "title_short": d.get("title_short"),
                        "title_version": d.get("title_version"),
                        "artist": (d.get("artist") or {}).get("name", ""),
                        "duration": d.get("duration"),
                        "artwork": (d.get("album") or {}).get("cover_xl")
                                   or (d.get("album") or {}).get("cover_big")})
        return out

    @staticmethod
    def _norm(s: str) -> str:
        return norm_key(s)

    @classmethod
    def _title_matches(cls, nt_query: str, cand_title: str) -> bool:
        nt = cls._norm(cand_title)
        return bool(nt) and (nt == nt_query or nt in nt_query or nt_query in nt)

    def _get(self, url: str) -> dict:
        # The API not answering is not the track not existing: a timeout, a
        # network error or the quota (error code 4, served as HTTP 200) must
        # reach the host as ProviderUnavailable so it is retried, not cached
        # as a miss for the chain TTL.
        self._pace.wait()
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                data = json.load(r)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            raise ProviderUnavailable(f"deezer api: {e}") from e
        err = data.get("error") if isinstance(data, dict) else None
        if err:
            if err.get("code") == 4:
                raise ProviderUnavailable(f"deezer api quota: {err.get('message')}")
            raise ProviderError(f"deezer api error: {err}")
        return data

    def _score(self, c: dict, query: TrackQuery) -> float:
        # Every survivor agrees on artist, title and version and fits the
        # length: the closest length wins, an exact title over a versioned one.
        off = length_offset(query, c["duration"])
        return ((100 - off * 15) if off is not None else 0.0) + \
            (15 if self._norm(c["title"]) == self._norm(query.title) else 0)


# One pacer, one album memo, one preview memo per process — for every
# provider on this catalog.
catalog = DeezerCatalog()


class DeezerCatalogProvider(StreamProvider):
    """The provider half every fetcher from this catalog shares: resolution
    is the catalog's, a subclass supplies its manifest and ``_download``.
    ``resolve_workers`` is the catalog's — the pacer is what the API answers
    to, however many threads the host runs."""

    resolve_workers = DeezerCatalog.RESOLVE_WORKERS

    def fetch(self, query: TrackQuery) -> FetchedAudio:
        return self._download(self._resolve(query).source_id)

    def _resolve(self, query: TrackQuery) -> ResolvedSource:
        return catalog.resolve(query)

    def resolve_batch(self, queries: list, wanted: list) -> list:
        return catalog.resolve_batch(queries, wanted)

    @abstractmethod
    def _download(self, track_id: str) -> FetchedAudio:
        """Audio for a catalog track id — the one thing the providers differ on."""
