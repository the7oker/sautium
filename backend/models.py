"""
Database models for Music AI DJ.
SQLAlchemy ORM models matching the PostgreSQL schema.
"""

import enum
from datetime import datetime
from typing import Optional, List

from sqlalchemy import (
    Column, Integer, String, Text, DateTime, Numeric, BigInteger,
    ForeignKey, CheckConstraint, Index, Enum as SQLEnum, ARRAY, UniqueConstraint
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
from pgvector.sqlalchemy import Vector

Base = declarative_base()


class QualitySource(str, enum.Enum):
    """Quality source enumeration matching database enum."""
    CD = "CD"
    VINYL = "Vinyl"
    HI_RES = "Hi-Res"
    MP3 = "MP3"


class EmbeddingModel(Base):
    """Embedding model metadata (CLAP, future models, etc)."""
    __tablename__ = "embedding_models"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False, unique=True)  # e.g., "laion/clap-htsat-unfused"
    description = Column(Text)
    dimension = Column(Integer, nullable=False)  # 512 for CLAP

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    embeddings = relationship("Embedding", back_populates="model")

    # Indexes
    __table_args__ = (
        Index("idx_embedding_models_name", "name"),
    )

    def __repr__(self):
        return f"<EmbeddingModel(id={self.id}, name='{self.name}')>"


class Embedding(Base):
    """Audio embedding model (512-dimensional vectors for CLAP)."""
    __tablename__ = "embeddings"

    id = Column(Integer, primary_key=True)
    vector = Column(Vector(512), nullable=False)
    model_id = Column(Integer, ForeignKey("embedding_models.id"), nullable=False)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    model = relationship("EmbeddingModel", back_populates="embeddings")
    tracks = relationship("Track", back_populates="embedding_obj")

    # Indexes
    __table_args__ = (
        Index("idx_embeddings_vector", "vector", postgresql_using="hnsw",
              postgresql_with={"m": 16, "ef_construction": 64},
              postgresql_ops={"vector": "vector_cosine_ops"}),
        Index("idx_embeddings_model_id", "model_id"),
    )

    def __repr__(self):
        return f"<Embedding(id={self.id}, model_id={self.model_id})>"


class ExternalMetadata(Base):
    """External metadata from various sources (Last.fm, Spotify, MusicBrainz, etc.)."""
    __tablename__ = "external_metadata"

    id = Column(Integer, primary_key=True)

    # What entity we're describing
    entity_type = Column(String(50), nullable=False)  # 'artist', 'album', 'track', 'genre'
    entity_id = Column(Integer, nullable=False)

    # Source of the data
    source = Column(String(50), nullable=False)  # 'lastfm', 'spotify', 'musicbrainz'

    # Type of metadata
    metadata_type = Column(String(50), nullable=False)  # 'bio', 'tags', 'genres', 'audio_features', 'similar_artists'

    # The actual data (flexible JSON structure)
    data = Column(JSONB, nullable=False)

    # Fetch metadata
    fetched_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    fetch_status = Column(String(20), default='success')  # 'success', 'not_found', 'error'
    error_message = Column(Text)

    # Indexes and constraints
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
        return f"<ExternalMetadata(id={self.id}, entity={self.entity_type}/{self.entity_id}, source={self.source}, type={self.metadata_type})>"


class Genre(Base):
    """Genre model (normalized). Descriptions stored in genre_descriptions table."""
    __tablename__ = "genres"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False, unique=True)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    track_associations = relationship("TrackGenre", back_populates="genre", cascade="all, delete-orphan")
    descriptions = relationship("GenreDescription", back_populates="genre", cascade="all, delete-orphan")

    # Indexes
    __table_args__ = (
        Index("idx_genres_name", "name"),
    )

    def __repr__(self):
        return f"<Genre(id={self.id}, name='{self.name}')>"


class GenreDescription(Base):
    """Normalized genre/tag descriptions from multiple sources (Last.fm, Wikipedia, etc.)."""
    __tablename__ = "genre_descriptions"

    id = Column(Integer, primary_key=True)
    genre_id = Column(Integer, ForeignKey("genres.id", ondelete="CASCADE"), nullable=False)
    source = Column(String(50), nullable=False)  # 'lastfm', 'wikipedia', 'spotify', etc.

    # Description fields
    summary = Column(Text)  # Short description (1-2 paragraphs)
    content = Column(Text)  # Full description (detailed)
    url = Column(String(500))  # Link to source page

    # Source-specific metadata
    reach = Column(Integer)  # Last.fm: tag popularity

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    genre = relationship("Genre", back_populates="descriptions")

    # Indexes and constraints
    __table_args__ = (
        Index("idx_genre_descriptions_genre", "genre_id"),
        Index("idx_genre_descriptions_source", "source"),
        UniqueConstraint("genre_id", "source", name="uq_genre_descriptions"),
        CheckConstraint("summary IS NOT NULL OR content IS NOT NULL", name="chk_has_description"),
    )

    def __repr__(self):
        return f"<GenreDescription(genre_id={self.genre_id}, source='{self.source}', summary_len={len(self.summary or '')})>"


class Artist(Base):
    """Artist model. Bio and metadata stored in normalized tables (artist_bios, artist_tags, etc.)."""
    __tablename__ = "artists"

    id = Column(Integer, primary_key=True)
    name = Column(String(500), nullable=False, unique=True)

    # External service IDs (Phase 2)
    spotify_id = Column(String(100))
    lastfm_id = Column(String(100))
    musicbrainz_id = Column(String(100))

    # Basic metadata
    country = Column(String(100))

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    track_associations = relationship("TrackArtist", back_populates="artist", cascade="all, delete-orphan")
    similar_to = relationship("SimilarArtist", foreign_keys="SimilarArtist.artist_id", back_populates="artist", cascade="all, delete-orphan")
    similar_from = relationship("SimilarArtist", foreign_keys="SimilarArtist.similar_artist_id", back_populates="similar_artist", cascade="all, delete-orphan")
    bios = relationship("ArtistBio", back_populates="artist", cascade="all, delete-orphan")

    # Indexes
    __table_args__ = (
        Index("idx_artists_name", "name"),
        Index("idx_artists_spotify_id", "spotify_id"),
    )

    def __repr__(self):
        return f"<Artist(id={self.id}, name='{self.name}')>"


class ArtistBio(Base):
    """Normalized artist biographies from multiple sources (Last.fm, MusicBrainz, Wikipedia, etc.)."""
    __tablename__ = "artist_bios"

    id = Column(Integer, primary_key=True)
    artist_id = Column(Integer, ForeignKey("artists.id", ondelete="CASCADE"), nullable=False)
    source = Column(String(50), nullable=False)  # 'lastfm', 'musicbrainz', 'wikipedia', etc.

    # Bio fields
    summary = Column(Text)  # Short bio (1-2 paragraphs)
    content = Column(Text)  # Full biography (detailed)
    url = Column(String(500))  # Link to source page

    # Source-specific metadata (Last.fm stats)
    listeners = Column(Integer)  # Last.fm: total unique listeners
    playcount = Column(BigInteger)  # Last.fm: total play count

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    artist = relationship("Artist", back_populates="bios")

    # Indexes and constraints
    __table_args__ = (
        Index("idx_artist_bios_artist", "artist_id"),
        Index("idx_artist_bios_source", "source"),
        Index("idx_artist_bios_listeners", "listeners", postgresql_where=(Column("listeners") != None)),
        Index("idx_artist_bios_playcount", "playcount", postgresql_where=(Column("playcount") != None)),
        UniqueConstraint("artist_id", "source", name="uq_artist_bios"),
        CheckConstraint("summary IS NOT NULL OR content IS NOT NULL", name="chk_has_bio"),
    )

    def __repr__(self):
        return f"<ArtistBio(artist_id={self.artist_id}, source='{self.source}', listeners={self.listeners})>"


class SimilarArtist(Base):
    """Normalized similar artist relationships from multiple sources (Last.fm, Spotify, etc.)."""
    __tablename__ = "similar_artists"

    id = Column(Integer, primary_key=True)
    artist_id = Column(Integer, ForeignKey("artists.id", ondelete="CASCADE"), nullable=False)
    similar_artist_id = Column(Integer, ForeignKey("artists.id", ondelete="CASCADE"), nullable=False)
    match_score = Column(Numeric(5, 4), nullable=False)  # 0.0000 to 1.0000
    source = Column(String(50), nullable=False)  # 'lastfm', 'spotify', 'musicbrainz', etc.

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    artist = relationship("Artist", foreign_keys=[artist_id], back_populates="similar_to")
    similar_artist = relationship("Artist", foreign_keys=[similar_artist_id], back_populates="similar_from")

    # Indexes and constraints
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
        return f"<SimilarArtist(artist_id={self.artist_id}, similar_artist_id={self.similar_artist_id}, match={self.match_score}, source='{self.source}')>"


class Album(Base):
    """Album model."""
    __tablename__ = "albums"

    id = Column(Integer, primary_key=True)
    title = Column(String(500), nullable=False)

    # Album details
    release_year = Column(Integer)
    label = Column(String(200))
    catalog_number = Column(String(100))
    total_tracks = Column(Integer)

    # Quality information
    quality_source = Column(
        SQLEnum(
            QualitySource,
            name="quality_source_type",
            values_callable=lambda x: [e.value for e in x],
            create_constraint=False
        ),
        default=QualitySource.CD
    )
    sample_rate = Column(Integer)
    bit_depth = Column(Integer)

    # External service IDs (Phase 2)
    spotify_id = Column(String(100))
    musicbrainz_id = Column(String(100))

    # File system information
    directory_path = Column(Text, nullable=False, unique=True)

    # User data (Phase 4)
    user_rating = Column(Numeric(3, 2))
    user_notes = Column(Text)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    tracks = relationship("Track", back_populates="album", cascade="all, delete-orphan")

    # Constraints and indexes
    __table_args__ = (
        CheckConstraint("user_rating >= 0 AND user_rating <= 5", name="check_album_rating"),
        Index("idx_albums_title", "title"),
        Index("idx_albums_release_year", "release_year"),
        Index("idx_albums_quality_source", "quality_source"),
    )

    def __repr__(self):
        return f"<Album(id={self.id}, title='{self.title}')>"


class Track(Base):
    """Track model."""
    __tablename__ = "tracks"

    id = Column(Integer, primary_key=True)
    title = Column(String(500), nullable=False)
    album_id = Column(Integer, ForeignKey("albums.id"), nullable=False)

    # Track details
    track_number = Column(Integer)
    disc_number = Column(Integer, default=1)
    duration_seconds = Column(Numeric(10, 2))

    # Audio characteristics
    sample_rate = Column(Integer)
    bit_depth = Column(Integer)
    bitrate = Column(Integer)
    channels = Column(Integer)

    # File information
    file_path = Column(Text, nullable=False, unique=True)
    file_size_bytes = Column(BigInteger)
    file_format = Column(String(10), default="FLAC")

    # Audio embedding reference
    embedding_id = Column(Integer, ForeignKey("embeddings.id"))

    # External service IDs (Phase 2)
    spotify_id = Column(String(100))
    isrc = Column(String(20))
    musicbrainz_id = Column(String(100))

    # Spotify audio features (Phase 2)
    spotify_tempo = Column(Numeric(6, 2))
    spotify_energy = Column(Numeric(3, 2))
    spotify_danceability = Column(Numeric(3, 2))
    spotify_valence = Column(Numeric(3, 2))
    spotify_acousticness = Column(Numeric(3, 2))
    spotify_instrumentalness = Column(Numeric(3, 2))
    spotify_liveness = Column(Numeric(3, 2))
    spotify_speechiness = Column(Numeric(3, 2))
    spotify_loudness = Column(Numeric(6, 2))
    spotify_key = Column(Integer)
    spotify_mode = Column(Integer)
    spotify_time_signature = Column(Integer)

    # User data (Phase 4)
    play_count = Column(Integer, default=0)
    last_played_at = Column(DateTime)
    user_rating = Column(Numeric(3, 2))
    user_notes = Column(Text)
    user_tags = Column(ARRAY(Text))

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    album = relationship("Album", back_populates="tracks")
    embedding_obj = relationship("Embedding", back_populates="tracks")
    artist_associations = relationship("TrackArtist", back_populates="track", cascade="all, delete-orphan")
    genre_associations = relationship("TrackGenre", back_populates="track", cascade="all, delete-orphan")

    # Constraints and indexes
    __table_args__ = (
        CheckConstraint("user_rating >= 0 AND user_rating <= 5", name="check_track_rating"),
        CheckConstraint("spotify_energy >= 0 AND spotify_energy <= 1", name="check_energy"),
        CheckConstraint("spotify_danceability >= 0 AND spotify_danceability <= 1", name="check_danceability"),
        CheckConstraint("spotify_valence >= 0 AND spotify_valence <= 1", name="check_valence"),
        CheckConstraint("spotify_acousticness >= 0 AND spotify_acousticness <= 1", name="check_acousticness"),
        CheckConstraint("spotify_instrumentalness >= 0 AND spotify_instrumentalness <= 1", name="check_instrumentalness"),
        CheckConstraint("spotify_liveness >= 0 AND spotify_liveness <= 1", name="check_liveness"),
        CheckConstraint("spotify_speechiness >= 0 AND spotify_speechiness <= 1", name="check_speechiness"),
        Index("idx_tracks_title", "title"),
        Index("idx_tracks_album_id", "album_id"),
        Index("idx_tracks_file_path", "file_path"),
        Index("idx_tracks_play_count", "play_count"),
    )

    def __repr__(self):
        return f"<Track(id={self.id}, title='{self.title}')>"


class TrackGenre(Base):
    """Track-Genre association (many-to-many)."""
    __tablename__ = "track_genres"

    track_id = Column(Integer, ForeignKey("tracks.id"), primary_key=True)
    genre_id = Column(Integer, ForeignKey("genres.id"), primary_key=True)

    # Relationships
    track = relationship("Track", back_populates="genre_associations")
    genre = relationship("Genre", back_populates="track_associations")

    # Indexes
    __table_args__ = (
        Index("idx_track_genres_track_id", "track_id"),
        Index("idx_track_genres_genre_id", "genre_id"),
    )

    def __repr__(self):
        return f"<TrackGenre(track_id={self.track_id}, genre_id={self.genre_id})>"


class TrackArtist(Base):
    """Track-Artist association for multiple artists per track."""
    __tablename__ = "track_artists"

    track_id = Column(Integer, ForeignKey("tracks.id"), primary_key=True)
    artist_id = Column(Integer, ForeignKey("artists.id"), primary_key=True)
    role = Column(String(50), primary_key=True, default="primary")

    # Relationships
    track = relationship("Track", back_populates="artist_associations")
    artist = relationship("Artist", back_populates="track_associations")

    # Indexes
    __table_args__ = (
        Index("idx_track_artists_track_id", "track_id"),
        Index("idx_track_artists_artist_id", "artist_id"),
    )

    def __repr__(self):
        return f"<TrackArtist(track_id={self.track_id}, artist_id={self.artist_id}, role='{self.role}')>"
