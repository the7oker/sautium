"""
Configuration management for Music AI DJ.
Loads settings from environment variables with sensible defaults.
"""

import os
from pathlib import Path
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Application Info
    app_name: str = "Music AI DJ"
    app_version: str = "0.1.0"
    debug: bool = False

    # Database Configuration
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "music_ai"
    postgres_user: str = "musicai"
    postgres_password: str = "changeme"

    # Music Library
    music_library_path: str = "/music"

    # API Keys
    anthropic_api_key: Optional[str] = None

    # GPU Configuration
    cuda_visible_devices: str = "0"

    # Application Settings
    log_level: str = "INFO"

    # Embedding Configuration
    embedding_model: str = "laion/clap-htsat-unfused"
    embedding_dimension: int = 512
    audio_sample_duration: int = 30  # seconds
    embedding_batch_size: int = 16

    # Text Embedding Configuration (sentence-transformers)
    text_embedding_model: str = "paraphrase-multilingual-MiniLM-L12-v2"
    text_embedding_dimension: int = 384
    text_embedding_batch_size: int = 64

    # Audio Analysis Configuration
    audio_analysis_sample_rate: int = 22050     # librosa features (lower = faster)
    audio_analysis_duration: int = 30           # seconds (middle segment)
    audio_analysis_batch_size: int = 8          # CLAP batch size for zero-shot

    # Search Configuration
    default_search_limit: int = 20
    min_similarity_threshold: float = 0.5

    # External APIs (Phase 2)
    # Last.fm keys default to built-in app keys (semi-public, standard for desktop apps)
    lastfm_api_key: Optional[str] = None
    lastfm_api_secret: Optional[str] = None
    lastfm_username: Optional[str] = None
    lastfm_session_key: Optional[str] = None

    # Genius API (lyrics source)
    genius_access_token: Optional[str] = None

    # HQPlayer Integration (Phase 3.2)
    hqplayer_host: str = "localhost"
    hqplayer_port: int = 4321
    hqplayer_enabled: bool = False

    # Native OS path prefix for stored DB paths (used when scanner runs inside Docker)
    # Docker: scanner sees /music/... → DB stores E:/Music/...
    # Launcher: not set or same as MUSIC_LIBRARY_PATH → DB stores native path as-is
    music_host_path: Optional[str] = None

    # Playback tracker daemon URL
    tracker_url: str = "http://localhost:8765"

    # Claude Code integration (agent-based AI DJ)
    claude_code_enabled: bool = False

    # Multi-provider LLM support
    openai_api_key: Optional[str] = None
    groq_api_key: Optional[str] = None
    openai_compat_base_url: Optional[str] = None
    openai_compat_api_key: Optional[str] = None
    openai_compat_model: Optional[str] = None
    openai_compat_name: Optional[str] = None
    default_provider: str = "claude_code"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore"
    )

    def model_post_init(self, __context) -> None:
        """Fill in built-in app keys when not provided via env."""
        from app_keys import LASTFM_API_KEY, LASTFM_API_SECRET, GENIUS_ACCESS_TOKEN
        if not self.lastfm_api_key:
            self.lastfm_api_key = LASTFM_API_KEY
        if not self.lastfm_api_secret:
            self.lastfm_api_secret = LASTFM_API_SECRET
        if not self.genius_access_token:
            self.genius_access_token = GENIUS_ACCESS_TOKEN

    @property
    def database_url(self) -> str:
        """Construct PostgreSQL connection URL."""
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def music_library_exists(self) -> bool:
        """Check if music library path exists."""
        return Path(self.music_library_path).exists()

    def translate_to_host_path(self, scanner_path: str) -> str:
        """Translate a scanner-visible path to a native OS path for DB storage.

        Docker: /music/Blues/track.flac → E:/Music/Blues/track.flac
        Launcher: E:/Music/Blues/track.flac → E:/Music/Blues/track.flac (no-op)
        """
        host = self.music_host_path
        if not host or host == self.music_library_path:
            return scanner_path
        # Replace the scanner prefix with the host prefix
        lib = self.music_library_path.rstrip("/\\")
        host = host.rstrip("/\\")
        if scanner_path.startswith(lib):
            return host + scanner_path[len(lib):]
        return scanner_path

    def translate_to_local_path(self, db_path: str) -> str:
        """Translate a DB-stored native path back to a local path for file access.

        Docker: E:/Music/Blues/track.flac → /music/Blues/track.flac
        Launcher: E:/Music/Blues/track.flac → E:/Music/Blues/track.flac (no-op)
        """
        host = self.music_host_path
        if not host or host == self.music_library_path:
            return db_path
        host = host.rstrip("/\\")
        lib = self.music_library_path.rstrip("/\\")
        if db_path.startswith(host):
            return lib + db_path[len(host):]
        return db_path

    def validate_required_settings(self) -> list[str]:
        """
        Validate that required settings are configured.
        Returns list of missing required settings.
        """
        missing = []

        # Check that at least one AI provider is configured
        has_provider = any([
            self.anthropic_api_key,
            self.openai_api_key,
            self.claude_code_enabled,
            self.openai_compat_base_url and self.openai_compat_model,
        ])
        if not has_provider:
            missing.append("AI provider (set ANTHROPIC_API_KEY, OPENAI_API_KEY, or CLAUDE_CODE_ENABLED)")

        if not self.music_library_exists:
            missing.append(f"MUSIC_LIBRARY_PATH (current: {self.music_library_path})")

        return missing


# Global settings instance
settings = Settings()


def get_settings() -> Settings:
    """Get application settings instance."""
    return settings


# Logging configuration
LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
        "json": {
            "()": "pythonjsonlogger.jsonlogger.JsonFormatter",
            "format": "%(asctime)s %(name)s %(levelname)s %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "default",
            "stream": "ext://sys.stdout",
        },
    },
    "root": {
        "level": settings.log_level,
        "handlers": ["console"],
    },
    "loggers": {
        "uvicorn": {
            "level": "INFO",
            "handlers": ["console"],
            "propagate": False,
        },
        "uvicorn.access": {
            "level": "INFO",
            "handlers": ["console"],
            "propagate": False,
        },
    },
}
