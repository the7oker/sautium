"""YouTube provider — ships in core (yt-dlp on non-DRM content is a ToS matter,
not §1201 anti-circumvention; it survived the 2020 RIAA takedown). DRM
providers (Deezer/Spotify) are NOT bundled — they live as external BYO modules.

HQPlayer doesn't decode AAC/m4a (tested), so we transcode YouTube's m4a/opus
source to FLAC via yt-dlp's built-in ffmpeg pass (``-x --audio-format flac``) —
lossless of the lossy source, and HQPlayer's native format.

yt-dlp runs as ``<our interpreter> -m yt_dlp``: a child process (a wedged
extraction dies on the timeout instead of hanging the single fetch worker),
but OUR Python, so it is the pip package every runtime already installs
rather than a binary to find on PATH. The standalone Windows exe it replaced
is a PyInstaller onefile that unpacks itself into %TEMP% on every run —
measured 0.95 s before the first line of Python, against 0.26 s here.

yt-dlp is a moving target by design: YouTube changes its side every few weeks
and upstream's *stable* channel lags ("often stale and prone to external
breakage" — their words; the 2026-08 android_vr 403 hit stable only). Every
runtime therefore tracks the *nightly* channel and refreshes at start (Docker:
entrypoint.py; launcher: desktop/db_init.py), and ships deno — the sandboxed
JS runtime yt-dlp solves YouTube's player challenges in. Without a runtime
yt-dlp falls back to a deprecated, runtime-less extraction path that is
exactly what breaks when YouTube moves."""
from __future__ import annotations

import glob
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from .base import (FetchedAudio, ProviderError, ProviderManifest, ProviderUnavailable,
                   ResolvedSource, StreamProvider, TrackQuery, attested_lengths,
                   fits_length, length_offset, norm_key)

logger = logging.getLogger(__name__)


# A download that dies on the signed URL — the failure shape of a yt-dlp that
# has fallen behind YouTube. Deliberately narrow: "video unavailable", geo
# blocks and no-match errors say something about the TRACK, not about us.
_STALE_BUILD_RE = re.compile(r"HTTP Error 403|nsig|signature", re.I)


class _GateWipeout(ProviderError):
    """The channel gate admitted nothing — the one miss an album can still
    overturn (resolve_batch), so it is told apart from a length miss."""


_WIPEOUT = object()


class YouTubeProvider(StreamProvider):
    manifest = ProviderManifest(
        id="youtube", name="YouTube", kind="direct_url", lossless=False,
    )

    def __init__(self, ffmpeg_location: str | None = None, timeout: float = 120.0):
        # ffmpeg_location: directory containing ffmpeg, or None to use PATH.
        self._ytdlp = [sys.executable, "-m", "yt_dlp"]
        self._ffmpeg_location = ffmpeg_location
        self._timeout = timeout
        self._searches: dict = {}          # search string -> (ts, candidates)
        self._search_lock = threading.Lock()
        # Advisory only — yt-dlp decides at run time; this just surfaces a
        # degraded install at boot instead of one cryptic 403 per track.
        if not shutil.which("deno"):
            logger.warning("youtube: deno (yt-dlp's JS runtime) is not on PATH — "
                           "extraction runs on the deprecated runtime-less path "
                           "and breaks whenever YouTube moves")

    # Candidates to score before downloading one. Deep enough that an archive
    # channel holding a whole album surfaces on SEVERAL of its tracks — that
    # recurrence is the evidence _album_consensus is built on, and at 5 the
    # top hits of a track are crowded out by live takes and full-album rips.
    SEARCH_N = 10
    TITLE_CAP = 100  # YouTube's hard limit on a video title
    # Distinct tracks of one album a channel must carry before its uploads
    # count as that album's rip. One length-exact title match is a coincidence
    # a generic title ("Sleep") can produce; two on the same tracklist is a
    # rip. Raising it costs whole 2-track albums, lowering it buys guesses.
    ALBUM_CONSENSUS = 2

    def fetch(self, query: TrackQuery) -> FetchedAudio:
        return self._download(self._resolve(query).source_id)

    def _resolve(self, query: TrackQuery) -> ResolvedSource:
        """Pick the best video for ONE track, on the channel gate alone. A
        single track carries no album to corroborate an unattested upload
        against, so the consensus tier is out of reach here — resolve_batch is
        where a tracklist earns it."""
        return self._pick(query, self._flat_search(
            f"{query.artist} {query.title}".strip()), frozenset())

    def resolve_batch(self, queries: list, wanted: list) -> list:
        """Resolve the `wanted` indices of `queries`; the rest of the list is
        the albums those tracks sit on — evidence for the consensus tier, never
        work of its own. Returns a list parallel to `wanted`: ResolvedSource,
        None (no match) or a ProviderUnavailable (no answer — not a miss).

        Lazy on purpose: a track the channel gate admits costs its album no
        search; only a wipe-out on a track WITH an album searches that album's
        other rows. So a lone click on such a band's track still reaches the
        consensus tier, at the price of one album's searches — and an artist
        with a channel of their own never pays it."""
        searched: dict = {}                 # index -> candidates | ProviderUnavailable

        def search(idxs):
            need = [i for i in idxs if i not in searched]
            for i, cs in zip(need, self._search_all([queries[i] for i in need])):
                searched[i] = cs

        search(wanted)
        results = {i: self._try_pick(queries[i], searched[i], frozenset()) for i in wanted}
        albums = {queries[i].album_id for i, r in results.items()
                  if r is _WIPEOUT and queries[i].album_id}
        if albums:
            search([j for j, q in enumerate(queries) if q.album_id in albums])
            trusted = self._album_consensus(queries, searched)
            for i, r in results.items():
                if r is _WIPEOUT:
                    vouched = trusted.get(queries[i].album_id, frozenset())
                    results[i] = (self._try_pick(queries[i], searched[i], vouched)
                                  if vouched else None)
        return [None if results[i] is _WIPEOUT else results[i] for i in wanted]

    def _try_pick(self, query: TrackQuery, cands, trusted: frozenset):
        """_pick as a value: the source, None for no match, the
        ProviderUnavailable a search raised, or _WIPEOUT — the channel gate
        admitted nothing, and the album may yet vouch for a channel."""
        if isinstance(cands, ProviderUnavailable):
            return cands
        try:
            return self._pick(query, cands, trusted)
        except _GateWipeout:
            return _WIPEOUT
        except ProviderUnavailable as e:
            return e
        except ProviderError:
            return None

    def _search_all(self, queries: list) -> list:
        """One flat search per query, concurrently. An entry is the candidate
        list, or the ProviderUnavailable that search raised."""
        from concurrent.futures import ThreadPoolExecutor

        def one(q):
            try:
                return self._flat_search(f"{q.artist} {q.title}".strip())
            except ProviderUnavailable as e:
                return e
        if not queries:
            return []
        with ThreadPoolExecutor(max_workers=min(self.resolve_workers, len(queries)),
                                thread_name_prefix="yt-search") as ex:
            return list(ex.map(one, queries))

    def _album_consensus(self, queries: list, searched: dict) -> dict:
        """Per album_id, the channels that carry this album — `{album_id:
        {channel_key, ...}}` — read off every search made so far.

        A band outside the distributor layer has no channel to gate on: no
        "- Topic" Art Tracks, no VEVO, no channel of its own, so every copy of
        its catalog sits on a listener's archive channel and the name gate
        wipes out the whole discography (Godspeed You! Black Emperor: ten
        albums unavailable while a length-exact rip sat at the top of every
        search). The evidence that replaces the name is STRUCTURAL: a channel
        that answers ALBUM_CONSENSUS different tracks of one tracklist, each
        under that track's own title and each at its catalog length, is
        holding a rip of that album — a coincidence no cover, no live take and
        no same-title stranger reproduces across a tracklist. It is corroboration,
        never a relaxation: a channel vouched for by nothing but its own search
        ranking is exactly what this refuses to trust.
        """
        votes: dict = {}
        for i, cs in searched.items():
            q = queries[i]
            if isinstance(cs, ProviderUnavailable) or not q.album_id:
                continue
            gates = self._gates(q)
            for c in self._title_attested(q, cs, gates):
                if fits_length(q, c["duration"]):
                    (votes.setdefault(q.album_id, {})
                          .setdefault(norm_key(c["channel"]), set()).add(q.track_id or q.title))
        return {aid: frozenset(ch for ch, tracks in by_ch.items()
                               if len(tracks) >= self.ALBUM_CONSENSUS)
                for aid, by_ch in votes.items()}

    @staticmethod
    def _gates(query: TrackQuery) -> list:
        """The names a source must carry to be this query's artist: the credit,
        its MB alternates, and — where the catalog names them — the performers.
        A release can be filed under either (a work is issued under its composer
        as readily as under the ensemble playing it), so both are admitted."""
        names = (query.artist, *query.artist_alts, *query.performers)
        return [g for g in (norm_key(a) for a in names) if g]

    def _title_attested(self, query: TrackQuery, cands: list, gates: list) -> list:
        """Candidates whose TITLE stands for this track: it is the work and
        nothing else (an album rip names the track alone), it names the artist
        beside the work, or — for a work whose own title outruns YouTube's
        100-character cap — it is our title cut off there. Length is not tested
        here; the caller does that, because the two readers weigh it
        differently (a vote must fit, a pick is ranked by how well it fits)."""
        title_k = norm_key(query.title)
        if not title_k:
            return []
        out = []
        for c in cands:
            if not c["duration"]:
                continue
            t = norm_key(c["title"])
            if (t == title_k
                    or (len(c["title"]) >= self.TITLE_CAP and title_k.startswith(t))
                    or (title_k in t and any(g in t for g in gates))):
                out.append(c)
        return out

    def _pick(self, query: TrackQuery, cands: list, trusted: frozenset) -> ResolvedSource:
        """Choose the source among one track's candidates. `trusted` = channels
        this track's album has vouched for (empty for a lone track)."""
        search = f"{query.artist} {query.title}".strip()

        # Artist gate on the CHANNEL: YouTube full-text search returns wrong-artist
        # covers and same-title different songs for obscure artists; with duration-
        # dominant scoring one of those would win and stream the WRONG recording
        # (often not even downloadable — "video not available"). The reliable artist
        # signal is the channel — official "<Artist> - Topic" Art Tracks, VEVO, or
        # the artist's own channel — NOT the title (covers name the original artist
        # in their title, e.g. a "Duo Diamanti" upload titled "Musica Nuda - Lunedì").
        # Any MB-canonical alternate satisfies the gate — YouTube channels a band
        # under its canonical name even when our credit is a lineup variant. A
        # gate wipe-out retries the search ONCE under the canonical name. Where
        # the credit names PERFORMERS they are the gate instead (see _gates):
        # "Steve Reich and Musicians - Topic" carries the composer's name and so
        # passed for an album played by Ensemble Contrechamps — another reading
        # of the same work, two of whose sections happened to fit the length.
        gates = self._gates(query)
        matched = ([c for c in cands if any(g in norm_key(c["channel"]) for g in gates)]
                   if gates else cands)
        if not matched and query.artist_alts and not query.performers:
            alt = self._flat_search(f"{query.artist_alts[0]} {query.title}".strip())
            matched = [c for c in alt if any(g in norm_key(c["channel"]) for g in gates)]
            seen = {c["id"] for c in cands}
            cands = cands + [c for c in alt if c["id"] not in seen]
        if not cands:
            raise ProviderError(f"youtube: no results for {search!r}")
        if not matched and trusted and not query.performers:
            # No channel names the artist, but this track's ALBUM has vouched
            # for one (_album_consensus): a channel holding the tracklist under
            # its own titles at its own lengths. Its upload for this track is
            # admissible — under the same title test, and the length gate below
            # still decides. Corroborated, never merely plausible: with `trusted`
            # empty (a lone track, or an album that vouched for nobody) this
            # tier does not exist and the resolve fails instead of guessing.
            #
            # It is also refused outright for a performed work. The tier reads
            # a channel answering several of one tracklist as holding a rip of
            # it — true for a band whose titles are its own, false for a work
            # every ensemble records under the SAME section names at nearly the
            # same lengths: Colin Currie's Music for 18 Musicians matched four
            # sections of Ensemble Contrechamps' reading (291 s against 289,
            # 81 against 85) and that was enough to vouch for the whole
            # channel. Where MB names the performers, only a channel that names
            # them will do.
            matched = [c for c in self._title_attested(query, cands, gates)
                       if norm_key(c["channel"]) in trusted]
            if matched:
                logger.info("youtube: no channel carries %s — %r admitted on the "
                            "album's rip channel %r", query.artist, query.title,
                            matched[0]["channel"])
        if not matched:
            raise _GateWipeout(
                f"youtube: no artist match for {search!r} "
                f"(top hit channel {cands[0]['channel']!r})")

        # Length gates the pool before anything is ranked: a listing whose
        # length is not the catalog's is a different recording (an edit, a live
        # take, the wrong song under a shared title) however well its title and
        # channel read, and it must not out-score a plain listing at the right
        # length. A listing with no length cannot be checked here — the
        # enrichment guard measures the audio — so it stays, ranked last.
        fit = [c for c in matched if fits_length(query, c["duration"])]
        if not fit:
            # Every survivor carries a length here (one without would have fit),
            # and so does the catalog — measure against the nearest attested one:
            # `duration` alone is blank whenever the display edition left it out.
            lens = attested_lengths(query)
            nearest = min(matched, key=lambda c: min(abs(c["duration"] - w) for w in lens))
            raise ProviderError(
                f"youtube: no length match for {search!r} (catalog "
                f"{'/'.join(f'{w:.0f}s' for w in lens)}, "
                f"nearest {nearest['duration']:.0f}s)")
        best = max(fit, key=lambda c: self._score(c, query))
        logger.info("youtube resolve %r -> %s (%ss, %s)",
                    search, best["id"], best["duration"], best["channel"])
        return ResolvedSource(
            source_id=best["id"], duration=best["duration"],
            artwork_url=f"https://i.ytimg.com/vi/{best['id']}/hqdefault.jpg")

    _SEARCH_TTL_S = 3600.0

    def _flat_search(self, search: str) -> list[dict]:
        # Remembered for the chain's lifetime: the consensus tier re-reads an
        # album's searches on every row of it a listener clicks, and each
        # search is a yt-dlp process.
        now = time.time()
        with self._search_lock:
            hit = self._searches.get(search)
        if hit and now - hit[0] < self._SEARCH_TTL_S:
            return hit[1]
        cmd = [*self._ytdlp, f"ytsearch{self.SEARCH_N}:{search}", "--flat-playlist",
               "--no-warnings", "--quiet",
               "--print", "%(id)s\t%(title)s\t%(duration)s\t%(channel)s"]
        try:
            out = subprocess.run(cmd, check=True, timeout=self._timeout,
                                 capture_output=True, text=True).stdout
        except subprocess.TimeoutExpired as e:
            raise ProviderUnavailable(f"youtube search timeout: {search!r}") from e
        except subprocess.CalledProcessError as e:
            # An empty search exits 0 with no lines; a non-zero exit is the
            # search itself failing, which says nothing about the track.
            raise ProviderUnavailable(f"youtube search failed for {search!r}") from e

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
        with self._search_lock:
            for k, (ts, _c) in list(self._searches.items()):
                if now - ts >= self._SEARCH_TTL_S:
                    self._searches.pop(k, None)
            self._searches[search] = (now, cands)
        return cands

    def _score(self, c: dict, query: TrackQuery) -> float:
        # The pool already fits the catalog length (_resolve); length here only
        # orders the survivors — the closest verified listing first, one with
        # no length last. Title, artist and channel signals break the ties.
        s = 0.0
        off = length_offset(query, c["duration"])
        if off is not None:
            s += 100 - off * 15
        title_k = norm_key(query.title)
        names_k = [k for k in (norm_key(a) for a in (query.artist, *query.artist_alts)) if k]
        ct = norm_key(c["title"])
        if title_k and title_k in ct:
            s += 20
        if any(a in ct for a in names_k):
            s += 12
        ch = norm_key(c["channel"])
        if ch.endswith("topic") or "vevo" in ch or any(a in ch for a in names_k):
            s += 12                                 # official Art Track / channel
        return s

    def _download(self, video_id: str) -> FetchedAudio:
        # Download bestaudio -> ffmpeg -> FLAC (HQPlayer can't decode AAC). Temp
        # dir is a transient buffer, deleted at once — no persisted rip.
        url = f"https://www.youtube.com/watch?v={video_id}"
        with tempfile.TemporaryDirectory(prefix="sautium-yt-") as tmp:
            # Warnings stay on: they are read only on failure, and yt-dlp's
            # "no JS runtime" one names the actual cause of a 403.
            # formats=dashy re-expresses the same audio stream as range
            # fragments, which download concurrently — YouTube shapes a single
            # connection well below the link (measured 2.9 s -> 2.6 s for a
            # 4.5-min track, byte-identical output). Fragments of ONE track a
            # listener is waiting on; the cross-track fetch stays sequential,
            # which is the part that must not look like a bulk pull.
            cmd = [*self._ytdlp, url, "-x", "--audio-format", "flac",
                   "--no-playlist", "--quiet",
                   "--extractor-args", "youtube:formats=dashy",
                   "--concurrent-fragments", "8",
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
                if _STALE_BUILD_RE.search(err):
                    # How YouTube tells us our yt-dlp no longer speaks its
                    # protocol (2026-08-18: every download 403'd on a stable
                    # build). Asking costs this track nothing — it already
                    # failed — and the refresh is rate-limited upstream.
                    from . import service
                    service.request_ytdlp_refresh()
                raise ProviderError(f"youtube download failed for {video_id}: {detail}") from e
            flacs = glob.glob(os.path.join(tmp, "*.flac"))
            if not flacs:
                raise ProviderError(f"youtube: no audio for {video_id}")
            with open(flacs[0], "rb") as f:
                data = f.read()
        logger.info("youtube fetch ok: %s (%d KiB FLAC)", video_id, len(data) // 1024)
        # FLAC container, but a lossy source — lossless=False for provenance.
        return FetchedAudio(data=data, mime="audio/flac", lossless=False)
