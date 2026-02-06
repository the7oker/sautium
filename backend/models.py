"""
Database models for Music AI DJ.
SQLAlchemy ORM models matching the PostgreSQL schema.
"""

import enum
from datetime import datetime
from typing import Optional, List

from sqlalchemy import (
    Column, Integer, String, Text, DateTime, Numeric, BigInteger,
    ForeignKey, CheckConstraint, Index, Enum as SQLEnum, ARRAY
)
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


class Genre(Base):
    """Genre model (normalized)."""
    __tablename__ = "genres"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False, unique=True)
    description = Column(Text)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    track_associations = relationship("TrackGenre", back_populates="genre", cascade="all, delete-orphan")

    # Indexes
    __table_args__ = (
        Index("idx_genres_name", "name"),
    )

    def __repr__(self):
        return f"<Genre(id={self.id}, name='{self.name}')>"


class Artist(Base):
    """Artist model."""
    __tablename__ = "artists"

    id = Column(Integer, primary_key=True)
    name = Column(String(500), nullable=False, unique=True)

    # External service IDs (Phase 2)
    spotify_id = Column(String(100))
    lastfm_id = Column(String(100))
    musicbrainz_id = Column(String(100))

    # Metadata
    bio = Column(Text)
    country = Column(String(100))

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    track_associations = relationship("TrackArtist", back_populates="artist", cascade="all, delete-orphan")

    # Indexes
    __table_args__ = (
        Index("idx_artists_name", "name"),
        Index("idx_artists_spotify_id", "spotify_id"),
    )

    def __repr__(self):
        return f"<Artist(id={self.id}, name='{self.name}')>"


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
    quality_source = Column(SQLEnum(QualitySource, name="quality_source_type"), default=QualitySource.CD)
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

    # Audio embedding (512-dimensional for CLAP)
    embedding = Column(Vector(512))
    embedding_model = Column(String(100))
    embedding_generated_at = Column(DateTime)

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
