"""
Database models for Sautium.
SQLAlchemy ORM models matching the PostgreSQL schema.

Schema overview:
  CANONICAL (UUID PKs, shareable):       PHYSICAL (SERIAL PKs, per-user):
    artists  ←── track_artists ──→ tracks   album_variants (edition of album)
    albums   ←── album_artists              media_files (file on disk)
                                               ↑ track_id    ↑ album_variant_id
    albums ←── album_genres ──→ genres   (genre is album-grain, not track)
    embeddings ──→ track_id
    audio_features ──→ track_id
    text_embeddings ──→ track_id
    artist_bio_embeddings ──→ artist_id
    genre_desc_embeddings ──→ genre_id
"""

import uuid as _uuid
from typing import Optional, List

from sqlalchemy import (
    Column, Integer, String, Text, DateTime, Numeric, BigInteger, Float,
    Boolean, ForeignKey, CheckConstraint, Index, ARRAY, UniqueConstraint,
    LargeBinary, SmallInteger, CHAR, Double, func, text, event,
)
from sqlalchemy.dialects.postgresql import BYTEA, ENUM, JSONB, UUID
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
from pgvector.sqlalchemy import Vector

Base = declarative_base()


# ENUM types declared in the SQL migration; SQLAlchemy references them without
# re-creating (create_type=False) so table creation in tests stays lightweight.
ArtistGenderEnum = ENUM(
    "unknown", "female", "male", "mixed",
    name="artist_gender",
    create_type=False,
)
ArtistVocalistEnum = ENUM(
    "unknown", "vocal", "instrumental",
    name="artist_vocalist",
    create_type=False,
)
ArtistTypeEnum = ENUM(
    "unknown", "solo", "band", "collaboration", "orchestra", "other",
    name="artist_type",
    create_type=False,
)
VerificationStatusEnum = ENUM(
    "unverified", "suspicious", "verified_band", "verified_split", "verified_collab",
    name="verification_status",
    create_type=False,
)
MbMatchConfidenceEnum = ENUM(
    "name_fuzzy", "alias_exact", "name_exact", "overlap_verified",
    name="mb_match_confidence",
    create_type=False,
)
CreditRoleEnum = ENUM(
    "primary", "featured", "member",
    name="credit_role",
    create_type=False,
)
MusicalKeyEnum = ENUM(
    "C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B",
    name="musical_key",
    create_type=False,
)
MusicalModeEnum = ENUM(
    "major", "minor",
    name="musical_mode",
    create_type=False,
)
VocalClassEnum = ENUM(
    "vocal", "instrumental", "mixed",
    name="vocal_class",
    create_type=False,
)
AudioFileFormatEnum = ENUM(
    "FLAC", "APE", "WAV", "AIFF", "WV", "TTA", "DSF", "DFF", "MP3", "OGG", "M4A",
    name="audio_file_format",
    create_type=False,
)
CoverSourceTypeEnum = ENUM(
    "external", "embedded", "sentinel",
    name="cover_source_type",
    create_type=False,
)
MetadataEntityTypeEnum = ENUM(
    "artist", "album", "track", "genre",
    name="metadata_entity_type",
    create_type=False,
)
MetadataKindEnum = ENUM(
    "bio", "info", "stats", "lyrics", "description",
    name="metadata_kind",
    create_type=False,
)
FetchStatusEnum = ENUM(
    "success", "error", "not_found",
    name="fetch_status",
    create_type=False,
)


# ───────────────────────────────────────────────────────────────────────────
# Embedding models (shared metadata)
# ───────────────────────────────────────────────────────────────────────────

class EmbeddingModel(Base):
    """Embedding model metadata (CLAP, sentence-transformers, etc)."""
    __tablename__ = "embedding_models"

    id = Column(UUID(as_uuid=True), primary_key=True)
    name = Column(String(100), nullable=False, unique=True)
    description = Column(Text)
    dimension = Column(Integer, nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now())

    embeddings = relationship("Embedding", back_populates="model")

    def __repr__(self):
        return f"<EmbeddingModel(id={self.id}, name='{self.name}')>"


# ───────────────────────────────────────────────────────────────────────────
# Canonical entities (UUID PKs, shareable across users)
# ───────────────────────────────────────────────────────────────────────────

class Artist(Base):
    """Artist model. UUID PK generated deterministically from name."""
    __tablename__ = "artists"

    id = Column(UUID(as_uuid=True), primary_key=True)
    name = Column(String(500), nullable=False, unique=True)

    # Normalization metadata
    artist_type = Column(ArtistTypeEnum, default="unknown")
    verification_status = Column(VerificationStatusEnum, default="unverified")
    gender = Column(ArtistGenderEnum, default="unknown")        # unknown/female/male/mixed
    is_vocalist = Column(ArtistVocalistEnum, default="unknown") # unknown/vocal/instrumental
    raw_name = Column(String(500))                             # original tag value before normalization
    name_latin = Column(String(500))                           # Latin transliteration of name for cross-script fuzzy search (Phase 0a)

    # MB identity lives in artist_mbids (1:N — name-UUID may conflate namesakes).
    last_album_sync = Column(DateTime(timezone=True))          # freshness gate for new-album discovery
    last_mb_sync = Column(DateTime(timezone=True))             # freshness gate for MB canonicalization pass
    last_similar_sync = Column(DateTime(timezone=True))        # freshness gate for Last.fm similar-artists backfill (incl. phantoms)
    lastfm_mbid = Column(UUID(as_uuid=True))                   # MB artist Last.fm treats as canonical for this name; gates photo/similar to the matching namesake (artist_mbids)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    track_associations = relationship("TrackArtist", back_populates="artist", cascade="all, delete-orphan")
    album_associations = relationship("AlbumArtist", back_populates="artist", cascade="all, delete-orphan")
    similar_to = relationship("SimilarArtist", foreign_keys="SimilarArtist.artist_id", back_populates="artist", cascade="all, delete-orphan")
    similar_from = relationship("SimilarArtist", foreign_keys="SimilarArtist.similar_artist_id", back_populates="similar_artist", cascade="all, delete-orphan")
    bios = relationship("ArtistBio", back_populates="artist", cascade="all, delete-orphan")
    tag_associations = relationship("ArtistTag", back_populates="artist", cascade="all, delete-orphan")
    members_as_compound = relationship("ArtistMember", foreign_keys="ArtistMember.compound_artist_id", back_populates="compound_artist", cascade="all, delete-orphan")
    member_of = relationship("ArtistMember", foreign_keys="ArtistMember.member_artist_id", back_populates="member_artist", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_artists_verification_status", "verification_status"),
        Index("idx_artists_artist_type", "artist_type"),
        Index("idx_artists_gender", "gender"),
        Index("idx_artists_is_vocalist", "is_vocalist"),
    )

    def __repr__(self):
        return f"<Artist(id={self.id}, name='{self.name}')>"


class Album(Base):
    """Album model (canonical — no physical file info). UUID PK."""
    __tablename__ = "albums"

    id = Column(UUID(as_uuid=True), primary_key=True)
    title = Column(String(500), nullable=False)
    title_latin = Column(String(500))                         # Latin transliteration of title for cross-script fuzzy search (Phase 0a)

    release_year = Column(Integer)
    label = Column(String(200))
    catalog_number = Column(String(100))
    total_tracks = Column(Integer)

    musicbrainz_id = Column(UUID(as_uuid=False))              # MusicBrainz release-GROUP MBID (canonical album)
    mb_match_confidence = Column(MbMatchConfidenceEnum)       # how musicbrainz_id was matched (NULL = none)
    cover_url = Column(Text)                  # external cover (Cover Art Archive) for phantom albums with no local files

    user_rating = Column(Numeric(3, 2))

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    variants = relationship("AlbumVariant", back_populates="album", cascade="all, delete-orphan")
    artist_associations = relationship("AlbumArtist", back_populates="album", cascade="all, delete-orphan")
    genre_associations = relationship("AlbumGenre", back_populates="album", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint("user_rating >= 0 AND user_rating <= 5", name="check_album_rating"),
        Index("idx_albums_title", "title"),
        Index("idx_albums_release_year", "release_year"),
    )

    def __repr__(self):
        return f"<Album(id={self.id}, title='{self.title}')>"


class Track(Base):
    """Canonical track — one per unique (title, primary_artist). UUID PK."""
    __tablename__ = "tracks"

    id = Column(UUID(as_uuid=True), primary_key=True)
    title = Column(String(500), nullable=False)
    title_latin = Column(String(500))                         # Latin transliteration of title for cross-script fuzzy search (Phase 0a)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    artist_associations = relationship("TrackArtist", back_populates="track", cascade="all, delete-orphan")
    media_files = relationship("MediaFile", back_populates="track", cascade="all, delete-orphan")
    embedding = relationship("Embedding", back_populates="track", uselist=False, cascade="all, delete-orphan")
    text_embedding = relationship("TextEmbedding", back_populates="track", uselist=False, cascade="all, delete-orphan")
    audio_feature = relationship("AudioFeature", back_populates="track", uselist=False, cascade="all, delete-orphan")
    stats = relationship("TrackStats", back_populates="track", cascade="all, delete-orphan")
    local_play_stats = relationship("LocalPlayStats", back_populates="track", uselist=False, cascade="all, delete-orphan")
    lyrics = relationship("TrackLyrics", back_populates="track", cascade="all, delete-orphan")
    lyrics_embeddings = relationship("LyricsEmbedding", back_populates="track", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_tracks_title", "title"),
    )

    def __repr__(self):
        return f"<Track(id={self.id}, title='{self.title}')>"


# ── name_latin / title_latin population (Phase 0a) ──────────────────────────
# ORM write-path hook: every Artist/Album/Track inserted or renamed through a
# session (the scanner path) gets its Latin search form recomputed here. Raw-SQL
# write sites (canon, phantom minting, sync) call latinize() explicitly. The
# identical latinize() transforms the query string at search time — see
# transliterate.py and docs/design/DISCOVERY-SEARCH-ENGINE.md.
from transliterate import latinize as _latinize


@event.listens_for(Artist, "before_insert")
@event.listens_for(Artist, "before_update")
def _fill_artist_name_latin(mapper, connection, target):
    target.name_latin = _latinize(target.name)


@event.listens_for(Album, "before_insert")
@event.listens_for(Album, "before_update")
def _fill_album_title_latin(mapper, connection, target):
    target.title_latin = _latinize(target.title)


@event.listens_for(Track, "before_insert")
@event.listens_for(Track, "before_update")
def _fill_track_title_latin(mapper, connection, target):
    target.title_latin = _latinize(target.title)


class TrackArtist(Base):
    """Track-Artist association (many-to-many with role)."""
    __tablename__ = "track_artists"

    track_id = Column(UUID(as_uuid=True), ForeignKey("tracks.id", ondelete="CASCADE", onupdate="CASCADE"), primary_key=True)
    artist_id = Column(UUID(as_uuid=True), ForeignKey("artists.id", ondelete="CASCADE", onupdate="CASCADE"), primary_key=True)
    role = Column(CreditRoleEnum, primary_key=True, default="primary")

    track = relationship("Track", back_populates="artist_associations")
    artist = relationship("Artist", back_populates="track_associations")

    __table_args__ = (
        Index("idx_track_artists_artist_id", "artist_id"),
    )

    def __repr__(self):
        return f"<TrackArtist(track_id={self.track_id}, artist_id={self.artist_id}, role='{self.role}')>"


class ArtistNameAlias(Base):
    """Extra Latin reading of an artist name for kana-less Han (Phase 0b) — the
    Japanese romanization kept alongside the pinyin name_latin so the name is
    findable either way. Populated by the name_latin backfill."""
    __tablename__ = "artist_name_aliases"

    artist_id = Column(UUID(as_uuid=True), ForeignKey("artists.id", ondelete="CASCADE", onupdate="CASCADE"), primary_key=True)
    alias_latin = Column(String(500), primary_key=True)
    source = Column(String(20), nullable=False, default="cutlet")


class AlbumGenre(Base):
    """Album-Genre association (many-to-many). Genre is album-grain, not
    track-grain — see album_genres in the schema. `source` is part of the PK
    so 'filetag' (count = media-file occurrences in the album) and 'mb'
    (count = release-group tag votes) coexist; readers dedup by genre_id."""
    __tablename__ = "album_genres"

    album_id = Column(UUID(as_uuid=True), ForeignKey("albums.id", ondelete="CASCADE", onupdate="CASCADE"), primary_key=True)
    genre_id = Column(UUID(as_uuid=True), ForeignKey("genres.id", ondelete="CASCADE", onupdate="CASCADE"), primary_key=True)
    source = Column(String(20), primary_key=True)
    count = Column(Integer)

    album = relationship("Album", back_populates="genre_associations")
    genre = relationship("Genre", back_populates="album_associations")

    __table_args__ = (
        Index("idx_album_genres_genre_id", "genre_id"),
    )

    def __repr__(self):
        return f"<AlbumGenre(album_id={self.album_id}, genre_id={self.genre_id}, source='{self.source}')>"


class AlbumArtist(Base):
    """Album-Artist association (many-to-many with role)."""
    __tablename__ = "album_artists"

    album_id = Column(UUID(as_uuid=True), ForeignKey("albums.id", ondelete="CASCADE", onupdate="CASCADE"), primary_key=True)
    artist_id = Column(UUID(as_uuid=True), ForeignKey("artists.id", ondelete="CASCADE", onupdate="CASCADE"), primary_key=True)
    role = Column(CreditRoleEnum, primary_key=True, default="primary")
    mbid = Column(UUID(as_uuid=False))  # materialized MB artist MBID for THIS album (which namesake); NULL = MB unavailable

    album = relationship("Album", back_populates="artist_associations")
    artist = relationship("Artist", back_populates="album_associations")

    __table_args__ = (
        Index("idx_album_artists_artist_id", "artist_id"),
        Index("idx_album_artists_mbid", "mbid", postgresql_where=text("mbid IS NOT NULL")),
    )

    def __repr__(self):
        return f"<AlbumArtist(album_id={self.album_id}, artist_id={self.artist_id}, role='{self.role}')>"


class ArtistMember(Base):
    """Compound artist → member artist junction table.

    Records which individual artists make up a compound/collaboration artist.
    E.g. "Beth Hart & Joe Bonamassa" → [Beth Hart (primary), Joe Bonamassa (featured)]
    """
    __tablename__ = "artist_members"

    compound_artist_id = Column(UUID(as_uuid=True), ForeignKey("artists.id", ondelete="CASCADE", onupdate="CASCADE"), primary_key=True)
    member_artist_id = Column(UUID(as_uuid=True), ForeignKey("artists.id", ondelete="CASCADE", onupdate="CASCADE"), primary_key=True)
    role = Column(CreditRoleEnum, nullable=False, default="member")

    compound_artist = relationship("Artist", foreign_keys=[compound_artist_id], back_populates="members_as_compound")
    member_artist = relationship("Artist", foreign_keys=[member_artist_id], back_populates="member_of")

    __table_args__ = (
        Index("idx_artist_members_member", "member_artist_id"),
    )

    def __repr__(self):
        return f"<ArtistMember(compound={self.compound_artist_id}, member={self.member_artist_id}, role='{self.role}')>"


class ArtistMbid(Base):
    """MB entities a name-derived Sautium artist maps to (1:N).

    The artist UUID is name-derived, so it conflates namesakes and
    case-variants — one Sautium artist may correspond to several real MB
    entities. `mbid` is the PK (one MB entity → one artist; the inverse is the
    1:N). MB-verified sources only — Last.fm is out of the MBID contour.
    """
    __tablename__ = "artist_mbids"

    mbid = Column(UUID(as_uuid=False), primary_key=True)
    artist_id = Column(UUID(as_uuid=True), ForeignKey("artists.id", ondelete="CASCADE", onupdate="CASCADE"), nullable=False)
    confidence = Column(MbMatchConfidenceEnum, nullable=False)
    # MB-canonical name + disambiguation for THIS entity, denormalized per mbid
    # (namesake spelling; survives to dump-less/P2P nodes). Auto-filled by the
    # fill_artist_mbid_meta trigger, so canon inserts don't set them.
    name = Column(Text)
    about = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    artist = relationship("Artist", foreign_keys=[artist_id])

    __table_args__ = (
        Index("idx_artist_mbids_artist", "artist_id"),
    )

    def __repr__(self):
        return f"<ArtistMbid({self.mbid} -> {self.artist_id} [{self.confidence}])>"


class TrackMbid(Base):
    """MB recordings a track (song) maps to (1:N).

    Our `tracks` is a song (release-independent, same UUID across albums) = MB's
    `recording`. One song can have several recordings (studio/live/remaster);
    `recording_mbid` is the PK (one recording → one song; the inverse is the
    1:N). The per-file recording is materialized on `media_files.recording_mbid`.
    """
    __tablename__ = "track_mbids"

    recording_mbid = Column(UUID(as_uuid=False), primary_key=True)
    track_id = Column(UUID(as_uuid=True), ForeignKey("tracks.id", ondelete="CASCADE", onupdate="CASCADE"), nullable=False)
    confidence = Column(MbMatchConfidenceEnum)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    track = relationship("Track", foreign_keys=[track_id])

    __table_args__ = (
        Index("idx_track_mbids_track", "track_id"),
    )

    def __repr__(self):
        return f"<TrackMbid({self.recording_mbid} -> {self.track_id})>"


# ───────────────────────────────────────────────────────────────────────────
# Physical entities (SERIAL PKs, per-user)
# ───────────────────────────────────────────────────────────────────────────

class AlbumVariant(Base):
    """A physical edition of an album (CD, Vinyl, Hi-Res, etc.)."""
    __tablename__ = "album_variants"

    id = Column(Integer, primary_key=True)
    album_id = Column(UUID(as_uuid=True), ForeignKey("albums.id", ondelete="CASCADE", onupdate="CASCADE"), nullable=False)
    directory_path = Column(Text, nullable=False)  # one variant per (directory, album) — a box set is many albums in one folder
    raw_title = Column(Text)  # original (pre-canon) album title for this variant — source of truth;
                              # makes album rename/merge reversible (albums are local, not synced)
    edition = Column(Text)  # named edition extracted from a dirty title on canon; NULL = standard
    release_mbid = Column(UUID(as_uuid=False))  # MB release MBID (specific edition); best-effort, often NULL

    sample_rate = Column(Integer)
    bit_depth = Column(Integer)
    is_lossless = Column(Boolean, default=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    album = relationship("Album", back_populates="variants")
    media_files = relationship("MediaFile", back_populates="album_variant", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_album_variants_album_id", "album_id"),
        UniqueConstraint("directory_path", "album_id", name="album_variants_dir_album_key"),
    )

    def __repr__(self):
        return f"<AlbumVariant(id={self.id}, album_id={self.album_id})>"


class AlbumTrack(Base):
    """Phantom-album tracklist slot (Phantom Discovery, Stage B).

    Owned albums keep the authoritative albums → album_variants →
    media_files → tracks chain; this junction links a PHANTOM album (no
    variants/files) to its canonical MB tracklist. A track is "owned" iff
    it has media_files — a later rip collapses onto the same UUID v5 row.
    """
    __tablename__ = "album_tracks"

    album_id = Column(UUID(as_uuid=True), ForeignKey("albums.id", ondelete="CASCADE", onupdate="CASCADE"), primary_key=True)
    track_id = Column(UUID(as_uuid=True), ForeignKey("tracks.id", ondelete="CASCADE", onupdate="CASCADE"), nullable=False)
    disc = Column(Integer, primary_key=True, default=1)
    position = Column(Integer, primary_key=True)
    recording_mbid = Column(UUID(as_uuid=False))  # MB recording gid (dedup / future preview)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_album_tracks_track", "track_id"),
    )

    def __repr__(self):
        return f"<AlbumTrack(album_id={self.album_id}, disc={self.disc}, position={self.position})>"


class MediaFile(Base):
    """A physical audio file on disk."""
    __tablename__ = "media_files"

    id = Column(Integer, primary_key=True)
    track_id = Column(UUID(as_uuid=True), ForeignKey("tracks.id", ondelete="CASCADE", onupdate="CASCADE"), nullable=False)
    album_variant_id = Column(Integer, ForeignKey("album_variants.id", ondelete="CASCADE", onupdate="CASCADE"), nullable=False)

    # File information
    file_path = Column(Text, nullable=False)
    file_format = Column(AudioFileFormatEnum, default="FLAC")
    is_lossless = Column(Boolean, default=True)
    file_size_bytes = Column(BigInteger)
    file_modified_at = Column(DateTime(timezone=True))

    # Audio characteristics
    sample_rate = Column(Integer)
    bit_depth = Column(Integer)
    bitrate = Column(Integer)
    channels = Column(Integer)
    duration_seconds = Column(Numeric(10, 2))

    # Track position within album variant
    track_number = Column(Integer)
    disc_number = Column(Integer, default=1)

    # CUE image slice: a rip stored as one audio image + .cue imports as N
    # virtual rows sharing one file_path. [start, end) seconds within the
    # image; NULL start = regular whole-file row; NULL end = to EOF.
    cue_start_seconds = Column(Double)
    cue_end_seconds = Column(Double)

    # Analysis source flag: TRUE for the preferred file to use for embeddings/analysis
    is_analysis_source = Column(Boolean, default=False)

    # User data
    play_count = Column(Integer, default=0)
    last_played_at = Column(DateTime(timezone=True))

    # External IDs
    isrc = Column(String(20))

    # Original tags as read from the file — ground truth for re-normalization / correction
    raw_track_name = Column(Text)
    raw_artist = Column(Text)
    raw_album_artist = Column(Text)
    raw_album = Column(Text)
    raw_year = Column(Text)
    recording_mbid = Column(UUID(as_uuid=False))  # materialized MB recording for THIS file's performance

    # Cover art (resolved by background worker)
    cover_id = Column(UUID(as_uuid=True), ForeignKey("covers.id", ondelete="SET NULL"))
    cover_processed_at = Column(DateTime(timezone=True))  # NULL = pending

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    track = relationship("Track", back_populates="media_files")
    album_variant = relationship("AlbumVariant", back_populates="media_files")
    cover = relationship("Cover")

    __table_args__ = (
        Index("idx_media_files_track_id", "track_id"),
        Index("idx_media_files_album_variant_id", "album_variant_id"),
        Index("idx_media_files_play_count", "play_count"),
        Index("uq_media_files_analysis_source", "track_id", unique=True,
              postgresql_where="is_analysis_source"),
        Index("idx_media_files_cover_id", "cover_id"),
        Index("idx_media_files_cover_pending", "id",
              postgresql_where="cover_processed_at IS NULL"),
        UniqueConstraint("file_path", "cue_start_seconds",
                         name="uq_media_files_path_cue",
                         postgresql_nulls_not_distinct=True),
        CheckConstraint("(cue_start_seconds IS NULL AND cue_end_seconds IS NULL)"
                        " OR (cue_start_seconds >= 0 AND (cue_end_seconds IS NULL"
                        " OR cue_end_seconds > cue_start_seconds))",
                        name="chk_mf_cue_span"),
        CheckConstraint("file_size_bytes IS NULL OR file_size_bytes >= 0", name="chk_mf_file_size"),
        CheckConstraint("duration_seconds IS NULL OR duration_seconds >= 0", name="chk_mf_duration"),
        CheckConstraint("play_count >= 0", name="chk_mf_play_count"),
    )

    def __repr__(self):
        return f"<MediaFile(id={self.id}, track_id={self.track_id})>"


class Cover(Base):
    """Resized + encoded album cover (WebP), dedup'd by BLAKE2b content hash."""
    __tablename__ = "covers"

    id = Column(UUID(as_uuid=True), primary_key=True)
    content_hash = Column(BYTEA, nullable=False, unique=True)
    perceptual_hash = Column(BigInteger)             # imagehash.phash, 64-bit

    source_type = Column(CoverSourceTypeEnum, nullable=False)
    source_path = Column(Text)                        # fs path or 'flac:{path}#{idx}'
    source_mtime = Column(DateTime(timezone=True))

    orig_width = Column(Integer)
    orig_height = Column(Integer)
    orig_format = Column(String(16))                  # 'jpeg' | 'png' | 'tiff' | ...
    orig_bytes = Column(Integer)

    width = Column(Integer, nullable=False)
    height = Column(Integer, nullable=False)
    data = Column(BYTEA, nullable=False)
    bytes = Column(Integer, nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_covers_phash", "perceptual_hash",
              postgresql_where="perceptual_hash IS NOT NULL"),
    )

    def __repr__(self):
        return f"<Cover(id={self.id}, {self.width}x{self.height}, {self.bytes} bytes)>"


# ───────────────────────────────────────────────────────────────────────────
# Embeddings & Analysis (linked to tracks, not files)
# ───────────────────────────────────────────────────────────────────────────

AnalysisOriginEnum = ENUM(
    "local", "deezer", "youtube",
    name="analysis_origin",
    create_type=False,
)


class AnalysisSource(Base):
    """Provenance of one audio-analysis pass: the physical source material
    (local file or streamed provider audio) content-addressed by pcm_hash +
    chromaprint. embeddings / audio_features rows link here; segments inherit
    it through their embeddings row. Record signatures bind these values, so
    a row is written once at analysis time and never mutated afterwards."""
    __tablename__ = "analysis_sources"

    id = Column(Integer, primary_key=True)
    track_id = Column(UUID(as_uuid=True), ForeignKey("tracks.id", ondelete="CASCADE", onupdate="CASCADE"), nullable=False)
    origin = Column(AnalysisOriginEnum, nullable=False)
    media_file_id = Column(Integer, ForeignKey("media_files.id", ondelete="SET NULL"))
    pcm_hash = Column(CHAR(64), nullable=False)
    chromaprint = Column(Text)
    # Whole seconds of the source material — with pcm_hash + chromaprint it
    # forms the signed material declaration; receivers use it as the cheap
    # (no-decode) import gate against their own file's duration.
    duration_seconds = Column(Integer)
    grid_version = Column(SmallInteger, nullable=False, server_default="1")
    sample_rate = Column(Integer)
    bit_depth = Column(Integer)
    is_lossless = Column(Boolean)
    # True = this row arrived over P2P sync (the sender's material, not ours).
    # Imported sources are never signable; a first-hand registration of the
    # same bytes flips it back to false.
    imported = Column(Boolean, nullable=False, server_default="false")
    computed_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("track_id", "pcm_hash"),
        Index("idx_analysis_sources_media_file", "media_file_id"),
        CheckConstraint("origin = 'local' OR media_file_id IS NULL",
                        name="chk_asrc_stream_no_file"),
    )

    def __repr__(self):
        return f"<AnalysisSource(id={self.id}, track_id={self.track_id}, origin={self.origin})>"


class Embedding(Base):
    """Audio embedding (512-dimensional vectors for CLAP). One per track.
    Seal columns (author_pubkey/signature/...) live only on the signed tables
    (embedding_segments, audio_features) and stay raw-SQL — see 001_initial.sql."""
    __tablename__ = "embeddings"

    id = Column(Integer, primary_key=True)
    vector = Column(Vector(512), nullable=False)
    model_id = Column(UUID(as_uuid=True), ForeignKey("embedding_models.id", ondelete="CASCADE", onupdate="CASCADE"), nullable=False)
    track_id = Column(UUID(as_uuid=True), ForeignKey("tracks.id", ondelete="CASCADE", onupdate="CASCADE"), nullable=False)

    analysis_source_id = Column(Integer, ForeignKey("analysis_sources.id", ondelete="SET NULL"))

    # Analysis methodology version (v2 = segment-mean portrait) — synced so
    # peers re-pull methodology upgrades; see embeddings.EMBEDDING_ANALYSIS_VERSION
    analysis_version = Column(SmallInteger, nullable=False, server_default="1")

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now())

    model = relationship("EmbeddingModel", back_populates="embeddings")
    track = relationship("Track", back_populates="embedding")

    __table_args__ = (
        UniqueConstraint("track_id", "model_id", name="uq_embeddings_track_model"),
        Index("idx_embeddings_vector", "vector", postgresql_using="hnsw",
              postgresql_with={"m": 16, "ef_construction": 64},
              postgresql_ops={"vector": "vector_cosine_ops"}),
        Index("idx_embeddings_model_id", "model_id"),
        Index("idx_embeddings_analysis_source", "analysis_source_id"),
    )

    def __repr__(self):
        return f"<Embedding(id={self.id}, track_id={self.track_id})>"


class EmbeddingSegment(Base):
    """Windowed CLAP segment embedding. segment_index addresses the canonical
    10s grid (window i covers [i*10s, i*10s+10s)), so sampling strategies of
    any density write compatible, top-uppable subsets of one grid. Keyed to
    the track-level Embedding row (whose vector is the materialized mean of
    these); track/model/provenance resolve through it. Seal columns
    (author_pubkey/signature/batch_root/merkle_proof) are raw-SQL only —
    written by sign_audio.py, cleared by the seal-guard trigger — see
    001_initial.sql."""
    __tablename__ = "embedding_segments"

    id = Column(Integer, primary_key=True)
    vector = Column(Vector(512), nullable=False)
    embedding_id = Column(Integer, ForeignKey("embeddings.id", ondelete="CASCADE"), nullable=False)
    segment_index = Column(SmallInteger, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("embedding_id", "segment_index",
                         name="uq_embedding_segments"),
    )

    def __repr__(self):
        return f"<EmbeddingSegment(embedding_id={self.embedding_id}, i={self.segment_index})>"


class TextEmbedding(Base):
    """Text embeddings (384-dimensional vectors from sentence-transformers). One per track."""
    __tablename__ = "text_embeddings"

    id = Column(Integer, primary_key=True)
    vector = Column(Vector(1024), nullable=False)
    model_id = Column(UUID(as_uuid=True), ForeignKey("embedding_models.id", ondelete="CASCADE", onupdate="CASCADE"), nullable=False)
    track_id = Column(UUID(as_uuid=True), ForeignKey("tracks.id", ondelete="CASCADE", onupdate="CASCADE"), nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now())

    model = relationship("EmbeddingModel", backref="text_embeddings")
    track = relationship("Track", back_populates="text_embedding")

    __table_args__ = (
        UniqueConstraint("track_id", "model_id", name="uq_text_embeddings_track_model"),
        Index("idx_text_embeddings_vector", "vector",
              postgresql_using="hnsw",
              postgresql_with={"m": 16, "ef_construction": 64},
              postgresql_ops={"vector": "vector_cosine_ops"}),
        Index("idx_text_embeddings_model_id", "model_id"),
    )

    def __repr__(self):
        return f"<TextEmbedding(id={self.id}, track_id={self.track_id})>"


class LyricsEmbedding(Base):
    """Lyrics embeddings (384-dimensional, multiple chunks per track for long lyrics)."""
    __tablename__ = "lyrics_embeddings"

    id = Column(Integer, primary_key=True)
    track_id = Column(UUID(as_uuid=True), ForeignKey("tracks.id", ondelete="CASCADE", onupdate="CASCADE"), nullable=False)
    model_id = Column(UUID(as_uuid=True), ForeignKey("embedding_models.id", ondelete="CASCADE", onupdate="CASCADE"), nullable=False)
    vector = Column(Vector(1024), nullable=False)
    chunk_index = Column(Integer, nullable=False, default=0)
    chunk_text = Column(Text)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now())

    model = relationship("EmbeddingModel", backref="lyrics_embeddings")
    track = relationship("Track", back_populates="lyrics_embeddings")

    __table_args__ = (
        UniqueConstraint("track_id", "model_id", "chunk_index", name="uq_lyrics_embeddings_track_model_chunk"),
        Index("idx_lyrics_embeddings_vector", "vector",
              postgresql_using="hnsw",
              postgresql_with={"m": 16, "ef_construction": 64},
              postgresql_ops={"vector": "vector_cosine_ops"}),
        Index("idx_lyrics_embeddings_model_id", "model_id"),
    )

    def __repr__(self):
        return f"<LyricsEmbedding(id={self.id}, track_id={self.track_id}, chunk={self.chunk_index})>"


class ArtistBioEmbedding(Base):
    """Artist bio embeddings (384-dimensional, chunked). One or more per artist."""
    __tablename__ = "artist_bio_embeddings"

    id = Column(Integer, primary_key=True)
    artist_id = Column(UUID(as_uuid=True), ForeignKey("artists.id", ondelete="CASCADE", onupdate="CASCADE"), nullable=False)
    model_id = Column(UUID(as_uuid=True), ForeignKey("embedding_models.id", ondelete="CASCADE", onupdate="CASCADE"), nullable=False)
    vector = Column(Vector(1024), nullable=False)
    chunk_index = Column(Integer, nullable=False, default=0)
    chunk_text = Column(Text)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now())

    model = relationship("EmbeddingModel")
    artist = relationship("Artist")

    __table_args__ = (
        UniqueConstraint("artist_id", "model_id", "chunk_index", name="uq_artist_bio_emb_artist_model_chunk"),
        Index("idx_artist_bio_emb_vector", "vector",
              postgresql_using="hnsw",
              postgresql_with={"m": 16, "ef_construction": 64},
              postgresql_ops={"vector": "vector_cosine_ops"}),
        Index("idx_artist_bio_emb_model", "model_id"),
    )


class GenreDescEmbedding(Base):
    """Genre description embeddings (384-dimensional, chunked). One or more per genre."""
    __tablename__ = "genre_desc_embeddings"

    id = Column(Integer, primary_key=True)
    genre_id = Column(UUID(as_uuid=True), ForeignKey("genres.id", ondelete="CASCADE", onupdate="CASCADE"), nullable=False)
    model_id = Column(UUID(as_uuid=True), ForeignKey("embedding_models.id", ondelete="CASCADE", onupdate="CASCADE"), nullable=False)
    vector = Column(Vector(1024), nullable=False)
    chunk_index = Column(Integer, nullable=False, default=0)
    chunk_text = Column(Text)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now())

    model = relationship("EmbeddingModel")
    genre = relationship("Genre")

    __table_args__ = (
        UniqueConstraint("genre_id", "model_id", "chunk_index", name="uq_genre_desc_emb_genre_model_chunk"),
        Index("idx_genre_desc_emb_vector", "vector",
              postgresql_using="hnsw",
              postgresql_with={"m": 16, "ef_construction": 64},
              postgresql_ops={"vector": "vector_cosine_ops"}),
        Index("idx_genre_desc_emb_model", "model_id"),
    )


class AudioFeature(Base):
    """Audio features extracted from FLAC files using librosa DSP and CLAP zero-shot classification."""
    __tablename__ = "audio_features"

    id = Column(Integer, primary_key=True)
    track_id = Column(UUID(as_uuid=True), ForeignKey("tracks.id", ondelete="CASCADE", onupdate="CASCADE"), unique=True, nullable=False)

    # librosa DSP features
    bpm = Column(Float)
    key = Column(MusicalKeyEnum)
    mode = Column(MusicalModeEnum)
    key_confidence = Column(Float)
    energy = Column(Float)
    energy_db = Column(Float)
    brightness = Column(Float)
    dynamic_range_db = Column(Float)
    zero_crossing_rate = Column(Float)

    # CLAP zero-shot classifications
    instruments = Column(JSONB)
    moods = Column(JSONB)
    vocal_instrumental = Column(VocalClassEnum)
    vocal_score = Column(Float)
    danceability = Column(Float)

    analysis_source_id = Column(Integer, ForeignKey("analysis_sources.id", ondelete="SET NULL"))

    # Analysis methodology version (v2 = whole-track amplitude + windowed
    # instruments) — synced so peers re-pull methodology upgrades; see
    # audio_analysis.ANALYSIS_VERSION. Seal columns are raw-SQL only (written
    # by sign_audio.py, cleared by the seal-guard trigger) — see 001_initial.sql.
    analysis_version = Column(SmallInteger, nullable=False, server_default="1")

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now())

    track = relationship("Track", back_populates="audio_feature")

    __table_args__ = (
        Index("idx_audio_features_bpm", "bpm"),
        Index("idx_audio_features_key", "key", "mode"),
        Index("idx_audio_features_energy", "energy_db"),
        Index("idx_audio_features_danceability", "danceability"),
        Index("idx_audio_features_vocal", "vocal_instrumental"),
        Index("idx_audio_features_analysis_source", "analysis_source_id"),
        CheckConstraint("bpm IS NULL OR bpm > 0", name="chk_af_bpm"),
        CheckConstraint("key_confidence IS NULL OR (key_confidence >= 0 AND key_confidence <= 1)", name="chk_af_key_confidence"),
        CheckConstraint("danceability IS NULL OR (danceability >= 0 AND danceability <= 1)", name="chk_af_danceability"),
        CheckConstraint("vocal_score IS NULL OR (vocal_score >= 0 AND vocal_score <= 1)", name="chk_af_vocal_score"),
    )

    def __repr__(self):
        return f"<AudioFeature(track_id={self.track_id}, bpm={self.bpm}, key={self.key} {self.mode})>"


# ───────────────────────────────────────────────────────────────────────────
# Genres
# ───────────────────────────────────────────────────────────────────────────

class Genre(Base):
    """Genre model (normalized). UUID PK generated deterministically from name."""
    __tablename__ = "genres"

    id = Column(UUID(as_uuid=True), primary_key=True)
    name = Column(String(100), nullable=False, unique=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now())

    album_associations = relationship("AlbumGenre", back_populates="genre", cascade="all, delete-orphan")
    descriptions = relationship("GenreDescription", back_populates="genre", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Genre(id={self.id}, name='{self.name}')>"


class GenreDescription(Base):
    """Normalized genre/tag descriptions from multiple sources."""
    __tablename__ = "genre_descriptions"

    id = Column(Integer, primary_key=True)
    genre_id = Column(UUID(as_uuid=True), ForeignKey("genres.id", ondelete="CASCADE", onupdate="CASCADE"), nullable=False)
    source = Column(String(50), nullable=False)

    summary = Column(Text)
    content = Column(Text)
    url = Column(Text)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now())

    genre = relationship("Genre", back_populates="descriptions")

    __table_args__ = (
        Index("idx_genre_descriptions_source", "source"),
        UniqueConstraint("genre_id", "source", name="uq_genre_descriptions"),
        CheckConstraint("summary IS NOT NULL OR content IS NOT NULL", name="chk_has_description"),
    )

    def __repr__(self):
        return f"<GenreDescription(genre_id={self.genre_id}, source='{self.source}')>"


# ───────────────────────────────────────────────────────────────────────────
# Tags
# ───────────────────────────────────────────────────────────────────────────

class Tag(Base):
    """Universal tag system for artists, albums. UUID PK generated deterministically from name."""
    __tablename__ = "tags"

    id = Column(UUID(as_uuid=True), primary_key=True)
    name = Column(String(100), nullable=False, unique=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now())

    artist_associations = relationship("ArtistTag", back_populates="tag", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_tags_name_lower", "name", postgresql_ops={"name": "text_pattern_ops"}),
        CheckConstraint("LENGTH(TRIM(name)) > 0", name="chk_tag_name_not_empty"),
    )

    def __repr__(self):
        return f"<Tag(id={self.id}, name='{self.name}')>"


# ───────────────────────────────────────────────────────────────────────────
# Artist metadata tables (UUID FKs)
# ───────────────────────────────────────────────────────────────────────────

class ArtistTag(Base):
    """Many-to-many relationship between artists and tags with weight."""
    __tablename__ = "artist_tags"

    id = Column(Integer, primary_key=True)
    artist_id = Column(UUID(as_uuid=True), ForeignKey("artists.id", ondelete="CASCADE", onupdate="CASCADE"), nullable=False)
    tag_id = Column(UUID(as_uuid=True), ForeignKey("tags.id", ondelete="CASCADE", onupdate="CASCADE"), nullable=False)
    weight = Column(Integer, nullable=False)
    source = Column(String(50), nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now())

    artist = relationship("Artist", back_populates="tag_associations")
    tag = relationship("Tag", back_populates="artist_associations")

    __table_args__ = (
        Index("idx_artist_tags_tag", "tag_id"),
        Index("idx_artist_tags_source", "source"),
        Index("idx_artist_tags_weight", "weight"),
        UniqueConstraint("artist_id", "tag_id", "source", name="uq_artist_tags"),
        CheckConstraint("weight >= 0 AND weight <= 100", name="chk_weight_range"),
    )

    def __repr__(self):
        return f"<ArtistTag(artist_id={self.artist_id}, tag_id={self.tag_id}, weight={self.weight})>"


class ArtistBio(Base):
    """Normalized artist biographies from multiple sources."""
    __tablename__ = "artist_bios"

    id = Column(Integer, primary_key=True)
    artist_id = Column(UUID(as_uuid=True), ForeignKey("artists.id", ondelete="CASCADE", onupdate="CASCADE"), nullable=False)
    source = Column(String(50), nullable=False)

    summary = Column(Text)
    content = Column(Text)
    url = Column(Text)
    listeners = Column(Integer)
    playcount = Column(BigInteger)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now())

    artist = relationship("Artist", back_populates="bios")

    __table_args__ = (
        Index("idx_artist_bios_source", "source"),
        Index("idx_artist_bios_listeners", "listeners", postgresql_where=(Column("listeners") != None)),
        Index("idx_artist_bios_playcount", "playcount", postgresql_where=(Column("playcount") != None)),
        UniqueConstraint("artist_id", "source", name="uq_artist_bios"),
        CheckConstraint("summary IS NOT NULL OR content IS NOT NULL OR listeners IS NOT NULL OR playcount IS NOT NULL", name="chk_has_bio"),
    )

    def __repr__(self):
        return f"<ArtistBio(artist_id={self.artist_id}, source='{self.source}')>"


class SimilarArtist(Base):
    """Similar artist relationships from multiple sources."""
    __tablename__ = "similar_artists"

    id = Column(Integer, primary_key=True)
    artist_id = Column(UUID(as_uuid=True), ForeignKey("artists.id", ondelete="CASCADE", onupdate="CASCADE"), nullable=False)
    similar_artist_id = Column(UUID(as_uuid=True), ForeignKey("artists.id", ondelete="CASCADE", onupdate="CASCADE"), nullable=False)
    match_score = Column(Numeric(5, 4), nullable=False)
    source = Column(String(50), nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now())

    artist = relationship("Artist", foreign_keys=[artist_id], back_populates="similar_to")
    similar_artist = relationship("Artist", foreign_keys=[similar_artist_id], back_populates="similar_from")

    __table_args__ = (
        Index("idx_similar_artists_similar", "similar_artist_id"),
        Index("idx_similar_artists_source", "source"),
        Index("idx_similar_artists_match", "match_score"),
        UniqueConstraint("artist_id", "similar_artist_id", "source", name="uq_similar_artists"),
        CheckConstraint("artist_id != similar_artist_id", name="chk_not_self_similar"),
        CheckConstraint("match_score >= 0 AND match_score <= 1", name="chk_match_score_range"),
    )

    def __repr__(self):
        return f"<SimilarArtist(artist_id={self.artist_id}, similar_artist_id={self.similar_artist_id})>"


# ───────────────────────────────────────────────────────────────────────────
# Album metadata tables (UUID FKs)
# ───────────────────────────────────────────────────────────────────────────

class LocalPlayStats(Base):
    """Aggregated local listening statistics per track (from HQPlayer)."""
    __tablename__ = "local_play_stats"

    track_id = Column(UUID(as_uuid=True), ForeignKey("tracks.id", ondelete="CASCADE", onupdate="CASCADE"), primary_key=True)
    play_count = Column(Integer, nullable=False, default=0)
    skip_count = Column(Integer, nullable=False, default=0)
    total_listen_time = Column(Numeric(12, 2), nullable=False, default=0)
    avg_percent_listened = Column(Numeric(5, 2), nullable=False, default=0)
    last_played_at = Column(DateTime(timezone=True))

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now())

    track = relationship("Track", back_populates="local_play_stats")

    __table_args__ = (
        Index("idx_local_play_stats_last_played", "last_played_at"),
        Index("idx_local_play_stats_play_count", "play_count"),
        CheckConstraint("play_count >= 0", name="chk_lps_play_count"),
        CheckConstraint("skip_count >= 0", name="chk_lps_skip_count"),
        CheckConstraint("total_listen_time >= 0", name="chk_lps_listen_time"),
        CheckConstraint("avg_percent_listened >= 0 AND avg_percent_listened <= 100", name="chk_lps_avg_pct"),
    )

    def __repr__(self):
        return f"<LocalPlayStats(track_id={self.track_id}, play_count={self.play_count})>"


class TrackStats(Base):
    """Track popularity statistics from external sources (Last.fm, etc)."""
    __tablename__ = "track_stats"

    id = Column(Integer, primary_key=True)
    track_id = Column(UUID(as_uuid=True), ForeignKey("tracks.id", ondelete="CASCADE", onupdate="CASCADE"), nullable=False)
    source = Column(String(50), nullable=False)

    listeners = Column(Integer)
    playcount = Column(BigInteger)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now())

    track = relationship("Track", back_populates="stats")

    __table_args__ = (
        UniqueConstraint("track_id", "source", name="uq_track_stats"),
        CheckConstraint("listeners IS NOT NULL OR playcount IS NOT NULL", name="chk_has_track_stats"),
        Index("idx_track_stats_source", "source"),
        Index("idx_track_stats_listeners", "listeners"),
        Index("idx_track_stats_playcount", "playcount"),
    )

    def __repr__(self):
        return f"<TrackStats(track_id={self.track_id}, source='{self.source}')>"


class TrackLyrics(Base):
    """Track lyrics from external sources (LRCLIB, Genius, etc.)."""
    __tablename__ = "track_lyrics"

    id = Column(Integer, primary_key=True)
    track_id = Column(UUID(as_uuid=True), ForeignKey("tracks.id", ondelete="CASCADE", onupdate="CASCADE"), nullable=False)
    source = Column(String(50), nullable=False)

    plain_lyrics = Column(Text)
    synced_lyrics = Column(Text)
    instrumental = Column(Boolean, default=False)
    external_id = Column(Integer)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now())

    track = relationship("Track", back_populates="lyrics")

    __table_args__ = (
        UniqueConstraint("track_id", "source", name="uq_track_lyrics"),
        Index("idx_track_lyrics_track", "track_id"),
        Index("idx_track_lyrics_source", "source"),
    )

    def __repr__(self):
        return f"<TrackLyrics(track_id={self.track_id}, source='{self.source}')>"


class ExternalMetadata(Base):
    """External metadata from various sources (Last.fm, MusicBrainz, etc.)."""
    __tablename__ = "external_metadata"

    id = Column(Integer, primary_key=True)

    entity_type = Column(MetadataEntityTypeEnum, nullable=False)
    entity_id = Column(Text, nullable=False)  # UUID as string for artist/album/track, int as string for genre

    source = Column(String(50), nullable=False)
    metadata_type = Column(MetadataKindEnum, nullable=False)
    data = Column(JSONB, nullable=False)

    fetched_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now())
    fetch_status = Column(FetchStatusEnum, default='success')
    error_message = Column(Text)

    __table_args__ = (
        Index('idx_external_metadata_entity', 'entity_type', 'entity_id'),
        Index('idx_external_metadata_source', 'source'),
        Index('idx_external_metadata_type', 'metadata_type'),
        Index('idx_external_metadata_status', 'fetch_status'),
        Index('idx_external_metadata_data', 'data', postgresql_using='gin'),
        UniqueConstraint('entity_type', 'entity_id', 'source', 'metadata_type',
                        name='uq_external_metadata'),
    )

    def __repr__(self):
        return f"<ExternalMetadata(id={self.id}, entity={self.entity_type}/{self.entity_id})>"
