"""
Database models for Music AI DJ.
SQLAlchemy ORM models matching the PostgreSQL schema.

Schema overview:
  CANONICAL (UUID PKs, shareable):       PHYSICAL (SERIAL PKs, per-user):
    artists  ←── track_artists ──→ tracks   album_variants (edition of album)
    albums   ←── album_artists              media_files (file on disk)
                                               ↑ track_id    ↑ album_variant_id
    genres ←── track_genres
    embeddings ──→ track_id
    audio_features ──→ track_id
    text_embeddings ──→ track_id
"""

import uuid as _uuid
from datetime import datetime
from typing import Optional, List

from sqlalchemy import (
    Column, Integer, String, Text, DateTime, Numeric, BigInteger, Float,
    Boolean, ForeignKey, CheckConstraint, Index, ARRAY, UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
from pgvector.sqlalchemy import Vector

Base = declarative_base()


# ───────────────────────────────────────────────────────────────────────────
# Embedding models (shared metadata)
# ───────────────────────────────────────────────────────────────────────────

class EmbeddingModel(Base):
    """Embedding model metadata (CLAP, sentence-transformers, etc)."""
    __tablename__ = "embedding_models"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False, unique=True)
    description = Column(Text)
    dimension = Column(Integer, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    embeddings = relationship("Embedding", back_populates="model")

    __table_args__ = (
        Index("idx_embedding_models_name", "name"),
    )

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

    # External service IDs
    lastfm_id = Column(String(100))
    musicbrainz_id = Column(String(100))

    country = Column(String(100))

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    track_associations = relationship("TrackArtist", back_populates="artist", cascade="all, delete-orphan")
    album_associations = relationship("AlbumArtist", back_populates="artist", cascade="all, delete-orphan")
    similar_to = relationship("SimilarArtist", foreign_keys="SimilarArtist.artist_id", back_populates="artist", cascade="all, delete-orphan")
    similar_from = relationship("SimilarArtist", foreign_keys="SimilarArtist.similar_artist_id", back_populates="similar_artist", cascade="all, delete-orphan")
    bios = relationship("ArtistBio", back_populates="artist", cascade="all, delete-orphan")
    tag_associations = relationship("ArtistTag", back_populates="artist", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_artists_name", "name"),
    )

    def __repr__(self):
        return f"<Artist(id={self.id}, name='{self.name}')>"


class Album(Base):
    """Album model (canonical — no physical file info). UUID PK."""
    __tablename__ = "albums"

    id = Column(UUID(as_uuid=True), primary_key=True)
    title = Column(String(500), nullable=False)

    release_year = Column(Integer)
    label = Column(String(200))
    catalog_number = Column(String(100))
    total_tracks = Column(Integer)

    # External service IDs
    musicbrainz_id = Column(String(100))
    lastfm_id = Column(String(100))

    user_rating = Column(Numeric(3, 2))

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    variants = relationship("AlbumVariant", back_populates="album", cascade="all, delete-orphan")
    artist_associations = relationship("AlbumArtist", back_populates="album", cascade="all, delete-orphan")
    info_records = relationship("AlbumInfo", back_populates="album", cascade="all, delete-orphan")
    tag_associations = relationship("AlbumTag", back_populates="album", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint("user_rating >= 0 AND user_rating <= 5", name="check_album_rating"),
        Index("idx_albums_title", "title"),
        Index("idx_albums_release_year", "release_year"),
        Index("idx_albums_lastfm_id", "lastfm_id"),
    )

    def __repr__(self):
        return f"<Album(id={self.id}, title='{self.title}')>"


class Track(Base):
    """Canonical track — one per unique (title, primary_artist). UUID PK."""
    __tablename__ = "tracks"

    id = Column(UUID(as_uuid=True), primary_key=True)
    title = Column(String(500), nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    artist_associations = relationship("TrackArtist", back_populates="track", cascade="all, delete-orphan")
    genre_associations = relationship("TrackGenre", back_populates="track", cascade="all, delete-orphan")
    media_files = relationship("MediaFile", back_populates="track", cascade="all, delete-orphan")
    embedding = relationship("Embedding", back_populates="track", uselist=False, cascade="all, delete-orphan")
    text_embedding = relationship("TextEmbedding", back_populates="track", uselist=False, cascade="all, delete-orphan")
    audio_feature = relationship("AudioFeature", back_populates="track", uselist=False, cascade="all, delete-orphan")
    stats = relationship("TrackStats", back_populates="track", cascade="all, delete-orphan")
    lyrics = relationship("TrackLyrics", back_populates="track", cascade="all, delete-orphan")
    lyrics_embeddings = relationship("LyricsEmbedding", back_populates="track", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_tracks_title", "title"),
    )

    def __repr__(self):
        return f"<Track(id={self.id}, title='{self.title}')>"


class TrackArtist(Base):
    """Track-Artist association (many-to-many with role)."""
    __tablename__ = "track_artists"

    track_id = Column(UUID(as_uuid=True), ForeignKey("tracks.id", ondelete="CASCADE"), primary_key=True)
    artist_id = Column(UUID(as_uuid=True), ForeignKey("artists.id", ondelete="CASCADE"), primary_key=True)
    role = Column(String(50), primary_key=True, default="primary")

    track = relationship("Track", back_populates="artist_associations")
    artist = relationship("Artist", back_populates="track_associations")

    __table_args__ = (
        Index("idx_track_artists_track_id", "track_id"),
        Index("idx_track_artists_artist_id", "artist_id"),
    )

    def __repr__(self):
        return f"<TrackArtist(track_id={self.track_id}, artist_id={self.artist_id}, role='{self.role}')>"


class TrackGenre(Base):
    """Track-Genre association (many-to-many)."""
    __tablename__ = "track_genres"

    track_id = Column(UUID(as_uuid=True), ForeignKey("tracks.id", ondelete="CASCADE"), primary_key=True)
    genre_id = Column(Integer, ForeignKey("genres.id", ondelete="CASCADE"), primary_key=True)

    track = relationship("Track", back_populates="genre_associations")
    genre = relationship("Genre", back_populates="track_associations")

    __table_args__ = (
        Index("idx_track_genres_track_id", "track_id"),
        Index("idx_track_genres_genre_id", "genre_id"),
    )

    def __repr__(self):
        return f"<TrackGenre(track_id={self.track_id}, genre_id={self.genre_id})>"


class AlbumArtist(Base):
    """Album-Artist association (many-to-many with role)."""
    __tablename__ = "album_artists"

    album_id = Column(UUID(as_uuid=True), ForeignKey("albums.id", ondelete="CASCADE"), primary_key=True)
    artist_id = Column(UUID(as_uuid=True), ForeignKey("artists.id", ondelete="CASCADE"), primary_key=True)
    role = Column(String(50), primary_key=True, default="primary")

    album = relationship("Album", back_populates="artist_associations")
    artist = relationship("Artist", back_populates="album_associations")

    __table_args__ = (
        Index("idx_album_artists_album_id", "album_id"),
        Index("idx_album_artists_artist_id", "artist_id"),
    )

    def __repr__(self):
        return f"<AlbumArtist(album_id={self.album_id}, artist_id={self.artist_id}, role='{self.role}')>"


# ───────────────────────────────────────────────────────────────────────────
# Physical entities (SERIAL PKs, per-user)
# ───────────────────────────────────────────────────────────────────────────

class AlbumVariant(Base):
    """A physical edition of an album (CD, Vinyl, Hi-Res, etc.)."""
    __tablename__ = "album_variants"

    id = Column(Integer, primary_key=True)
    album_id = Column(UUID(as_uuid=True), ForeignKey("albums.id", ondelete="CASCADE"), nullable=False)
    directory_path = Column(Text, nullable=False, unique=True)

    sample_rate = Column(Integer)
    bit_depth = Column(Integer)
    is_lossless = Column(Boolean, default=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    album = relationship("Album", back_populates="variants")
    media_files = relationship("MediaFile", back_populates="album_variant", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_album_variants_album_id", "album_id"),
        Index("idx_album_variants_directory", "directory_path"),
    )

    def __repr__(self):
        return f"<AlbumVariant(id={self.id}, album_id={self.album_id})>"


class MediaFile(Base):
    """A physical audio file on disk."""
    __tablename__ = "media_files"

    id = Column(Integer, primary_key=True)
    track_id = Column(UUID(as_uuid=True), ForeignKey("tracks.id", ondelete="CASCADE"), nullable=False)
    album_variant_id = Column(Integer, ForeignKey("album_variants.id", ondelete="CASCADE"), nullable=False)

    # File information
    file_path = Column(Text, nullable=False, unique=True)
    file_format = Column(String(10), default="FLAC")
    is_lossless = Column(Boolean, default=True)
    file_size_bytes = Column(BigInteger)
    file_modified_at = Column(DateTime)

    # Audio characteristics
    sample_rate = Column(Integer)
    bit_depth = Column(Integer)
    bitrate = Column(Integer)
    channels = Column(Integer)
    duration_seconds = Column(Numeric(10, 2))

    # Track position within album variant
    track_number = Column(Integer)
    disc_number = Column(Integer, default=1)

    # Analysis source flag: TRUE for the preferred file to use for embeddings/analysis
    is_analysis_source = Column(Boolean, default=False)

    # User data
    play_count = Column(Integer, default=0)
    last_played_at = Column(DateTime)

    # External IDs
    isrc = Column(String(20))

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    track = relationship("Track", back_populates="media_files")
    album_variant = relationship("AlbumVariant", back_populates="media_files")

    __table_args__ = (
        Index("idx_media_files_track_id", "track_id"),
        Index("idx_media_files_album_variant_id", "album_variant_id"),
        Index("idx_media_files_file_path", "file_path"),
        Index("idx_media_files_play_count", "play_count"),
        Index("idx_media_files_analysis_source", "track_id", "is_analysis_source",
              postgresql_where="is_analysis_source = true"),
    )

    def __repr__(self):
        return f"<MediaFile(id={self.id}, track_id={self.track_id})>"


# ───────────────────────────────────────────────────────────────────────────
# Embeddings & Analysis (linked to tracks, not files)
# ───────────────────────────────────────────────────────────────────────────

class Embedding(Base):
    """Audio embedding (512-dimensional vectors for CLAP). One per track."""
    __tablename__ = "embeddings"

    id = Column(Integer, primary_key=True)
    vector = Column(Vector(512), nullable=False)
    model_id = Column(Integer, ForeignKey("embedding_models.id"), nullable=False)
    track_id = Column(UUID(as_uuid=True), ForeignKey("tracks.id", ondelete="CASCADE"), nullable=False)

    # Source quality info (from the media_file used for analysis)
    source_bit_depth = Column(Integer)
    source_sample_rate = Column(Integer)
    source_is_lossless = Column(Boolean)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    model = relationship("EmbeddingModel", back_populates="embeddings")
    track = relationship("Track", back_populates="embedding")

    __table_args__ = (
        UniqueConstraint("track_id", "model_id", name="uq_embeddings_track_model"),
        Index("idx_embeddings_vector", "vector", postgresql_using="hnsw",
              postgresql_with={"m": 16, "ef_construction": 64},
              postgresql_ops={"vector": "vector_cosine_ops"}),
        Index("idx_embeddings_model_id", "model_id"),
        Index("idx_embeddings_track_id", "track_id"),
    )

    def __repr__(self):
        return f"<Embedding(id={self.id}, track_id={self.track_id})>"


class TextEmbedding(Base):
    """Text embeddings (384-dimensional vectors from sentence-transformers). One per track."""
    __tablename__ = "text_embeddings"

    id = Column(Integer, primary_key=True)
    vector = Column(Vector(384), nullable=False)
    model_id = Column(Integer, ForeignKey("embedding_models.id"), nullable=False)
    track_id = Column(UUID(as_uuid=True), ForeignKey("tracks.id", ondelete="CASCADE"), nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    model = relationship("EmbeddingModel", backref="text_embeddings")
    track = relationship("Track", back_populates="text_embedding")

    __table_args__ = (
        UniqueConstraint("track_id", "model_id", name="uq_text_embeddings_track_model"),
        Index("idx_text_embeddings_vector", "vector",
              postgresql_using="hnsw",
              postgresql_with={"m": 16, "ef_construction": 64},
              postgresql_ops={"vector": "vector_cosine_ops"}),
        Index("idx_text_embeddings_model_id", "model_id"),
        Index("idx_text_embeddings_track_id", "track_id"),
    )

    def __repr__(self):
        return f"<TextEmbedding(id={self.id}, track_id={self.track_id})>"


class LyricsEmbedding(Base):
    """Lyrics embeddings (384-dimensional, multiple chunks per track for long lyrics)."""
    __tablename__ = "lyrics_embeddings"

    id = Column(Integer, primary_key=True)
    track_id = Column(UUID(as_uuid=True), ForeignKey("tracks.id", ondelete="CASCADE"), nullable=False)
    model_id = Column(Integer, ForeignKey("embedding_models.id", ondelete="CASCADE"), nullable=False)
    vector = Column(Vector(384), nullable=False)
    chunk_index = Column(Integer, nullable=False, default=0)
    chunk_text = Column(Text)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    model = relationship("EmbeddingModel", backref="lyrics_embeddings")
    track = relationship("Track", back_populates="lyrics_embeddings")

    __table_args__ = (
        UniqueConstraint("track_id", "model_id", "chunk_index", name="uq_lyrics_embeddings_track_model_chunk"),
        Index("idx_lyrics_embeddings_vector", "vector",
              postgresql_using="hnsw",
              postgresql_with={"m": 16, "ef_construction": 64},
              postgresql_ops={"vector": "vector_cosine_ops"}),
        Index("idx_lyrics_embeddings_track_id", "track_id"),
        Index("idx_lyrics_embeddings_model_id", "model_id"),
    )

    def __repr__(self):
        return f"<LyricsEmbedding(id={self.id}, track_id={self.track_id}, chunk={self.chunk_index})>"


class AudioFeature(Base):
    """Audio features extracted from FLAC files using librosa DSP and CLAP zero-shot classification."""
    __tablename__ = "audio_features"

    id = Column(Integer, primary_key=True)
    track_id = Column(UUID(as_uuid=True), ForeignKey("tracks.id", ondelete="CASCADE"), unique=True, nullable=False)

    # librosa DSP features
    bpm = Column(Float)
    key = Column(String(3))
    mode = Column(String(5))
    key_confidence = Column(Float)
    energy = Column(Float)
    energy_db = Column(Float)
    brightness = Column(Float)
    dynamic_range_db = Column(Float)
    zero_crossing_rate = Column(Float)

    # CLAP zero-shot classifications
    instruments = Column(JSONB)
    moods = Column(JSONB)
    vocal_instrumental = Column(String(20))
    vocal_score = Column(Float)
    danceability = Column(Float)

    # Source quality info
    source_bit_depth = Column(Integer)
    source_sample_rate = Column(Integer)
    source_is_lossless = Column(Boolean)

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    track = relationship("Track", back_populates="audio_feature")

    __table_args__ = (
        Index("idx_audio_features_track_id", "track_id"),
        Index("idx_audio_features_bpm", "bpm"),
        Index("idx_audio_features_key", "key", "mode"),
        Index("idx_audio_features_energy", "energy_db"),
        Index("idx_audio_features_danceability", "danceability"),
        Index("idx_audio_features_vocal", "vocal_instrumental"),
    )

    def __repr__(self):
        return f"<AudioFeature(track_id={self.track_id}, bpm={self.bpm}, key={self.key} {self.mode})>"


# ───────────────────────────────────────────────────────────────────────────
# Genres
# ───────────────────────────────────────────────────────────────────────────

class Genre(Base):
    """Genre model (normalized)."""
    __tablename__ = "genres"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False, unique=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    track_associations = relationship("TrackGenre", back_populates="genre", cascade="all, delete-orphan")
    descriptions = relationship("GenreDescription", back_populates="genre", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_genres_name", "name"),
    )

    def __repr__(self):
        return f"<Genre(id={self.id}, name='{self.name}')>"


class GenreDescription(Base):
    """Normalized genre/tag descriptions from multiple sources."""
    __tablename__ = "genre_descriptions"

    id = Column(Integer, primary_key=True)
    genre_id = Column(Integer, ForeignKey("genres.id", ondelete="CASCADE"), nullable=False)
    source = Column(String(50), nullable=False)

    summary = Column(Text)
    content = Column(Text)
    url = Column(String(500))
    reach = Column(Integer)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    genre = relationship("Genre", back_populates="descriptions")

    __table_args__ = (
        Index("idx_genre_descriptions_genre", "genre_id"),
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
    """Universal tag system for artists, albums."""
    __tablename__ = "tags"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False, unique=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    artist_associations = relationship("ArtistTag", back_populates="tag", cascade="all, delete-orphan")
    album_associations = relationship("AlbumTag", back_populates="tag", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_tags_name", "name"),
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
    artist_id = Column(UUID(as_uuid=True), ForeignKey("artists.id", ondelete="CASCADE"), nullable=False)
    tag_id = Column(Integer, ForeignKey("tags.id", ondelete="CASCADE"), nullable=False)
    weight = Column(Integer, nullable=False)
    source = Column(String(50), nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    artist = relationship("Artist", back_populates="tag_associations")
    tag = relationship("Tag", back_populates="artist_associations")

    __table_args__ = (
        Index("idx_artist_tags_artist", "artist_id"),
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
    artist_id = Column(UUID(as_uuid=True), ForeignKey("artists.id", ondelete="CASCADE"), nullable=False)
    source = Column(String(50), nullable=False)

    summary = Column(Text)
    content = Column(Text)
    url = Column(String(500))
    listeners = Column(Integer)
    playcount = Column(BigInteger)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    artist = relationship("Artist", back_populates="bios")

    __table_args__ = (
        Index("idx_artist_bios_artist", "artist_id"),
        Index("idx_artist_bios_source", "source"),
        Index("idx_artist_bios_listeners", "listeners", postgresql_where=(Column("listeners") != None)),
        Index("idx_artist_bios_playcount", "playcount", postgresql_where=(Column("playcount") != None)),
        UniqueConstraint("artist_id", "source", name="uq_artist_bios"),
        CheckConstraint("summary IS NOT NULL OR content IS NOT NULL", name="chk_has_bio"),
    )

    def __repr__(self):
        return f"<ArtistBio(artist_id={self.artist_id}, source='{self.source}')>"


class SimilarArtist(Base):
    """Similar artist relationships from multiple sources."""
    __tablename__ = "similar_artists"

    id = Column(Integer, primary_key=True)
    artist_id = Column(UUID(as_uuid=True), ForeignKey("artists.id", ondelete="CASCADE"), nullable=False)
    similar_artist_id = Column(UUID(as_uuid=True), ForeignKey("artists.id", ondelete="CASCADE"), nullable=False)
    match_score = Column(Numeric(5, 4), nullable=False)
    source = Column(String(50), nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    artist = relationship("Artist", foreign_keys=[artist_id], back_populates="similar_to")
    similar_artist = relationship("Artist", foreign_keys=[similar_artist_id], back_populates="similar_from")

    __table_args__ = (
        Index("idx_similar_artists_artist", "artist_id"),
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

class AlbumInfo(Base):
    """Normalized album information from multiple sources."""
    __tablename__ = "album_info"

    id = Column(Integer, primary_key=True)
    album_id = Column(UUID(as_uuid=True), ForeignKey("albums.id", ondelete="CASCADE"), nullable=False)
    source = Column(String(50), nullable=False)

    summary = Column(Text)
    content = Column(Text)
    url = Column(String(500))
    listeners = Column(Integer)
    playcount = Column(BigInteger)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    album = relationship("Album", back_populates="info_records")

    __table_args__ = (
        Index("idx_album_info_album", "album_id"),
        Index("idx_album_info_source", "source"),
        Index("idx_album_info_listeners", "listeners", postgresql_where=(Column("listeners") != None)),
        Index("idx_album_info_playcount", "playcount", postgresql_where=(Column("playcount") != None)),
        UniqueConstraint("album_id", "source", name="uq_album_info"),
        CheckConstraint("summary IS NOT NULL OR content IS NOT NULL", name="chk_has_album_info"),
    )

    def __repr__(self):
        return f"<AlbumInfo(album_id={self.album_id}, source='{self.source}')>"


class AlbumTag(Base):
    """Many-to-many relationship between albums and tags with weight."""
    __tablename__ = "album_tags"

    id = Column(Integer, primary_key=True)
    album_id = Column(UUID(as_uuid=True), ForeignKey("albums.id", ondelete="CASCADE"), nullable=False)
    tag_id = Column(Integer, ForeignKey("tags.id", ondelete="CASCADE"), nullable=False)
    weight = Column(Integer, nullable=False)
    source = Column(String(50), nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    album = relationship("Album", back_populates="tag_associations")
    tag = relationship("Tag", back_populates="album_associations")

    __table_args__ = (
        Index("idx_album_tags_album", "album_id"),
        Index("idx_album_tags_tag", "tag_id"),
        Index("idx_album_tags_source", "source"),
        Index("idx_album_tags_weight", "weight"),
        UniqueConstraint("album_id", "tag_id", "source", name="uq_album_tags"),
        CheckConstraint("weight >= 0 AND weight <= 100", name="chk_album_tag_weight_range"),
    )

    def __repr__(self):
        return f"<AlbumTag(album_id={self.album_id}, tag_id={self.tag_id}, weight={self.weight})>"


# ───────────────────────────────────────────────────────────────────────────
# Statistics & External metadata
# ───────────────────────────────────────────────────────────────────────────

class TrackStats(Base):
    """Track popularity statistics from external sources (Last.fm, etc)."""
    __tablename__ = "track_stats"

    id = Column(Integer, primary_key=True)
    track_id = Column(UUID(as_uuid=True), ForeignKey("tracks.id", ondelete="CASCADE"), nullable=False)
    source = Column(String(50), nullable=False)

    listeners = Column(Integer)
    playcount = Column(BigInteger)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    track = relationship("Track", back_populates="stats")

    __table_args__ = (
        UniqueConstraint("track_id", "source", name="uq_track_stats"),
        CheckConstraint("listeners IS NOT NULL OR playcount IS NOT NULL", name="chk_has_track_stats"),
        Index("idx_track_stats_track", "track_id"),
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
    track_id = Column(UUID(as_uuid=True), ForeignKey("tracks.id", ondelete="CASCADE"), nullable=False)
    source = Column(String(50), nullable=False)

    plain_lyrics = Column(Text)
    synced_lyrics = Column(Text)
    instrumental = Column(Boolean, default=False)
    external_id = Column(Integer)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

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

    entity_type = Column(String(50), nullable=False)  # 'artist', 'album', 'track', 'genre'
    entity_id = Column(Text, nullable=False)  # UUID as string for artist/album/track, int as string for genre

    source = Column(String(50), nullable=False)
    metadata_type = Column(String(50), nullable=False)
    data = Column(JSONB, nullable=False)

    fetched_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    fetch_status = Column(String(20), default='success')
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
