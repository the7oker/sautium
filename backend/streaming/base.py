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
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

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
    duration: Optional[float] = None   # seconds — for match disambiguation
    isrc: Optional[str] = None
    track_id: Optional[str] = None     # Sautium track UUID — for preview enrichment
    cover_url: Optional[str] = None    # phantom album art (CAA) — display only, not for resolution


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


class ProviderError(Exception):
    """No match found, or the source fetch/transcode failed."""


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
    @property
    def supports_resolve(self) -> bool:
        return callable(getattr(self, "_resolve", None)) and \
            callable(getattr(self, "_download", None))

    def resolve(self, query: TrackQuery) -> Optional["ResolvedSource"]:
        """The provider's source for the query, or None if no acceptable match
        (availability check — no download). Normalises a bare source-id str from
        ``_resolve`` (back-compat) into a ResolvedSource."""
        rfn = getattr(self, "_resolve", None)
        if not callable(rfn):
            raise NotImplementedError(f"{self.manifest.id} cannot pre-resolve")
        try:
            r = rfn(query)
        except ProviderError:
            return None
        if r is None:
            return None
        return r if isinstance(r, ResolvedSource) else ResolvedSource(source_id=r)

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
