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
    postgres_host: str = "postgres"
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

    # Text Embedding Configuration
    text_embedding_model: str = "all-MiniLM-L6-v2"
    text_embedding_dimension: int = 384
    text_embedding_batch_size: int = 64

    # Search Configuration
    default_search_limit: int = 20
    min_similarity_threshold: float = 0.5

    # External APIs (Phase 2)
    spotify_client_id: Optional[str] = None
    spotify_client_secret: Optional[str] = None
    lastfm_api_key: Optional[str] = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore"
    )

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

    def validate_required_settings(self) -> list[str]:
        """
        Validate that required settings are configured.
        Returns list of missing required settings.
        """
        missing = []

        if not self.anthropic_api_key:
            missing.append("ANTHROPIC_API_KEY")

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
