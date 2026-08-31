"""Streaming-provider framework — pluggable sources that turn a phantom track
identity into HQPlayer-playable audio.

The contract is deliberately tiny and VERSIONED so external "bring-your-own"
provider modules (e.g. a closed-repo Deezer module) implement it against a
stable boundary — that stability is the future-proofing, not a generic plugin
framework. The registry routes by ``type``; only ``stream_provider`` exists
today (MCP and other module kinds are separate subsystems, intentionally not
unified). See the ``project_spotify_preview`` design notes.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


def norm_key(s: str) -> str:
    """Comparison key for names across catalogs: case-folded alphanumerics
    with the accents folded away, every script kept. The catalog credits
    細野晴臣 and ሙላቱ አስታጥቄ under their own scripts; a key that strips non-Latin
    letters compares every such artist as the empty string and lets any
    channel through.

    Accents fold because catalogs drop them and we cannot tell which spelling
    a given one kept: MusicBrainz's "Tsegué-Maryam Guebrou" is Deezer's
    "Tsegue-maryam Guebrou", and holding the artist gate to the accent
    rejected an exact title at an exact length. This is a comparison key
    only — identity keys on uuid_utils.normalize, which is a different
    function and unaffected."""
    bare = "".join(ch for ch in unicodedata.normalize("NFD", s or "")
                   if not unicodedata.combining(ch))
    return "".join(ch for ch in unicodedata.normalize("NFKC", bare).casefold()
                   if ch.isalnum())


def recording_tolerance(expected_s: float) -> float:
    """How far a source may sit from a catalog length and still be the same
    recording: 5 %, never under 7 s. Room for a remaster's fade or a trimmed
    lead-in; not for an edit, a live take or a different song."""
    return max(7.0, 0.05 * expected_s)


def same_recording(actual_s: float, expected_s: float) -> bool:
    return abs(actual_s - expected_s) <= recording_tolerance(expected_s)


def attested_lengths(query: "TrackQuery") -> tuple:
    """Every catalog length the query vouches for, in seconds."""
    if query.lengths:
        return tuple(query.lengths)
    return (query.duration,) if query.duration else ()


def fits_length(query: "TrackQuery", actual_s: Optional[float]) -> bool:
    """Whether audio of `actual_s` seconds can be the query's recording. True
    when either side has no length to hold the other to."""
    lengths = attested_lengths(query)
    if not lengths or not actual_s:
        return True
    return any(same_recording(actual_s, want) for want in lengths)


def length_offset(query: "TrackQuery", actual_s: Optional[float]) -> Optional[float]:
    """Distance from the nearest attested length as a fraction of its
    tolerance (0 = exact, 1 = at the edge) — for ranking sources that already
    fit. None when there is nothing to measure against."""
    lengths = attested_lengths(query)
    if not lengths or not actual_s:
        return None
    return min(abs(actual_s - want) / recording_tolerance(want) for want in lengths)


# A listing's version suffix — "(2011 Remastered)", "(feat. Tracey Thorn)",
# "(Karaoke Version)", "(UNKLE Situation)" — says WHICH recording it is. The
# words a catalog spends on the release recording itself are few and stable;
# the words for another recording (a live take, a remixer's name, a
# re-recording's "Taylor's Version") are without end. So a suffix is read by
# its safe words: every word of it must be one, or the listing claims a
# recording the query does not — the direction that fails closed. A credit
# ("feat. X", "from Y") says nothing about the recording and swallows what
# follows it. An unknown word is logged by the caller, so the list grows from
# evidence, never from a guess.
_SAFE_VERSION_WORDS = frozenset("""
    remaster remastered remastering remasterised remasterized remasterisé
    remasterisée master masters digital album original version versions explicit clean bonus
    track mono stereo deluxe édition edition expanded anniversary single lp
    main full length uk us international japanese japan european europe
    """.split())
_CREDIT_WORDS = frozenset(("feat", "featuring", "ft", "with", "from", "avec"))


def version_claims_same(version: Optional[str], title: str) -> bool:
    """Whether a listing's version suffix can still be the query's recording.
    A word the query's own title carries is claimed on both sides and never
    disqualifies: "Light My Fire (live)" against "(Live From United
    Kingdom/1994)" is one recording described twice."""
    ver = " ".join((version or "").casefold().split())
    if not ver:
        return True
    claimed = title_tokens(title)
    for word in re.findall(r"[^\W\d_]+", ver.replace("original mix", "original")):
        if word in _CREDIT_WORDS:
            return True
        if word not in _SAFE_VERSION_WORDS and word not in claimed:
            return False
    return True


# An album title has no version field — the qualifier is the title's own
# words. These, in a catalog's album title and absent from the query's, name
# another album of the same songs: another recording of them.
_OTHER_ALBUM_WORDS = re.compile(
    r"\b(karaoke|tribute|live|remix|remixes|remixed|instrumental|instrumentals|"
    r"acoustic|unplugged|demo|demos|cover|covers|session|sessions|backing tracks?|"
    r"in the style of|originally performed|made famous)\b")


def other_recording_words(title: str) -> frozenset:
    return frozenset(_OTHER_ALBUM_WORDS.findall(" ".join((title or "").casefold().split())))


def title_tokens(s: str) -> frozenset:
    return frozenset(re.findall(r"\w+", unicodedata.normalize("NFKC", s or "").casefold()))


def tokens_shared(a: str, b: str) -> float:
    """Jaccard overlap of two titles' words — for titles no key equality
    survives (a catalog fronting a classical movement with its work, or its
    composer), where an album whose lengths and order already line up leaves
    the shared words to say the rest."""
    ta, tb = title_tokens(a), title_tokens(b)
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0

# Bump only on breaking changes to TrackQuery / FetchedAudio / fetch(). External
# modules declare the version they were built against so the host can warn.
CONTRACT_VERSION = 1


@dataclass(frozen=True)
class ProviderManifest:
    id: str               # "youtube"
    name: str             # "YouTube"
    kind: str             # "direct_url" | "decrypt_bridge"
    lossless: bool        # source-quality hint — UI + feature provenance tier
    version: str = "1.0.0"
    type: str = "stream_provider"
    contract_version: int = CONTRACT_VERSION


@dataclass(frozen=True)
class TrackQuery:
    """Everything a provider needs to find + fetch ONE track. Sautium already
    knows the metadata from the phantom album's MusicBrainz tracklist, so
    providers don't resolve metadata — they resolve the playable *source*."""
    artist: str
    title: str
    album: str = ""
    artist_alts: tuple = ()            # MB-canonical name + aliases — retried when
                                       # the credited artist name misses (catalogs
                                       # file lineup/spelling variants under one name)
    duration: Optional[float] = None   # seconds — the display length, and the
                                       # recording's length when it is the only one known
    lengths: tuple = ()                # every catalog length this track is attested at, when
                                       # the caller has no album context: a canonical track sits
                                       # on every album that lists it, and editions list it at
                                       # their own lengths — the recording is ANY of them. Empty
                                       # = hold the source to `duration` alone.
    isrc: Optional[str] = None
    track_id: Optional[str] = None     # Sautium track UUID — for preview enrichment
    cover_url: Optional[str] = None    # phantom album art (CAA) — display only, not for resolution
    album_id: Optional[str] = None     # the albums.id this track was queued FROM. A canonical track
                                       # belongs to every album that lists it, so Now Playing cannot
                                       # re-derive it — it has to ride along from the enqueue.
    media_file_id: Optional[int] = None  # set for an OWNED file transcoded on play (m4a) —
                                         # makes Now Playing / queue render it as owned, not preview
    album_artists: tuple = ()          # the album's own primary artists — what a provider's
                                       # album-level lookup searches under, and one more name
                                       # a catalog may file the slot's recording under
    barcodes: tuple = ()               # MB barcodes of the editions carrying this very
                                       # tracklist — the release itself, where a catalog answers
                                       # to a barcode (Deezer /album/upc:); no search to second-guess


@dataclass
class FetchedAudio:
    """Audio in a format HQPlayer plays over http. HQPlayer streams FLAC and
    MP3 fine but NOT AAC/m4a (tested), so providers transcode lossy sources to
    FLAC (lossless of the lossy source)."""
    data: bytes
    mime: str          # "audio/flac" | "audio/mpeg"
    # ACTUAL quality of THIS fetch, not the provider's best — a provider may
    # degrade within its own tiers (Deezer FLAC→320 when no FLAC for the region),
    # so enrichment provenance reads this, not manifest.lossless.
    lossless: bool = False


@dataclass(frozen=True)
class ResolvedSource:
    """Availability-pass result: the provider source id to download, plus the
    metadata the resolve already saw. ``duration`` (seconds) backfills phantom
    track lengths MusicBrainz left blank — see player._backfill_phantom_durations.
    A provider's ``_resolve`` may return a bare source-id str (back-compat) or
    this richer object; the host normalises both in ``resolve()``."""
    source_id: str
    duration: Optional[float] = None
    artwork_url: Optional[str] = None   # provider album art — fallback when the CAA cover 404s


class ProviderError(Exception):
    """No match found, or the source fetch/transcode failed."""


class ProviderUnavailable(ProviderError):
    """The provider could not be consulted — timeout, network, quota. Says
    nothing about the track, so the host must not remember it as a miss."""


class StreamProvider(ABC):
    manifest: ProviderManifest

    @abstractmethod
    def fetch(self, query: TrackQuery) -> FetchedAudio:
        """Resolve the query on this service, download it, and return
        HQPlayer-playable audio bytes. Blocking and possibly slow (network +
        transcode) — the proxy calls it off the request path via prefetch.
        Raises ProviderError on no-match / failure."""
        ...

    # Split resolve/download so the host can run a fast availability pass (which
    # tracks exist on this provider) BEFORE the slow downloads — to mark missing
    # tracks in the UI. Providers expose this for free by implementing the
    # internal `_resolve` (query -> source id, raising ProviderError) and
    # `_download` (source id -> audio); fetch() == download(_resolve(query)).
    # How many resolves the host may run against this catalog at once. The
    # fan-out exists for YouTube — a yt-dlp search is ~1.4 s of process and
    # network, eight abreast is the difference between a minute and eight for
    # an album. A JSON API answers in ~150 ms and meters requests per address:
    # measured 2026-08-29, Deezer at one thread runs 161 ms/track, at two 136,
    # at eight 130 — the extra threads buy nothing and burn the quota. Each
    # provider states what it can use.
    resolve_workers: int = 8

    @property
    def supports_resolve(self) -> bool:
        return callable(getattr(self, "_resolve", None)) and \
            callable(getattr(self, "_download", None))

    def resolve(self, query: TrackQuery) -> Optional["ResolvedSource"]:
        """The provider's source for the query, or None if no acceptable match
        (availability check — no download). Normalises a bare source-id str from
        ``_resolve`` (back-compat) into a ResolvedSource.

        Acceptance is decided HERE, not in the provider: a provider ranks the
        catalog's candidates, the host says what counts as the recording — the
        same ``same_recording`` test the enrichment poison guard applies to the
        downloaded audio, so nothing streams that would not analyse. A
        ProviderUnavailable propagates: not an answer, so not a miss."""
        rfn = getattr(self, "_resolve", None)
        if not callable(rfn):
            raise NotImplementedError(f"{self.manifest.id} cannot pre-resolve")
        try:
            r = rfn(query)
        except ProviderUnavailable:
            raise
        except ProviderError:
            return None
        if r is None:
            return None
        r = r if isinstance(r, ResolvedSource) else ResolvedSource(source_id=r)
        if not fits_length(query, r.duration):
            logger.info("%s: %s — %s is %.0fs against catalog %s — not the recording",
                        self.manifest.id, query.artist, query.title, r.duration,
                        "/".join(f"{w:.0f}s" for w in attested_lengths(query)))
            return None
        return r

    def download(self, source_id: str) -> FetchedAudio:
        """Fetch by the provider's own source id, skipping resolution."""
        dfn = getattr(self, "_download", None)
        if not callable(dfn):
            raise NotImplementedError(f"{self.manifest.id} has no _download")
        return dfn(source_id)

    @property
    def feature_source(self) -> str:
        """Provenance tag for audio features derived from this provider's
        (lossy, non-canonical) audio — a lower trust tier than owned files."""
        return f"preview:{self.manifest.id}"


class ProviderRegistry:
    """Type-keyed registry. Holds ``stream_provider`` modules only; the type
    tag is the seam for future module kinds, which live in their own
    subsystems rather than being forced through this contract."""

    def __init__(self) -> None:
        self._by_id: dict[str, StreamProvider] = {}

    def register(self, provider: StreamProvider) -> None:
        m = provider.manifest
        if m.type != "stream_provider":
            raise ValueError(f"registry holds stream_provider only, got {m.type!r}")
        if m.contract_version != CONTRACT_VERSION:
            logger.warning("provider %s built for contract v%d, host is v%d",
                           m.id, m.contract_version, CONTRACT_VERSION)
        self._by_id[m.id] = provider
        logger.info("registered stream provider: %s (%s, lossless=%s)",
                    m.id, m.kind, m.lossless)

    def get(self, provider_id: str) -> Optional[StreamProvider]:
        return self._by_id.get(provider_id)

    def enabled(self) -> list[StreamProvider]:
        return list(self._by_id.values())
