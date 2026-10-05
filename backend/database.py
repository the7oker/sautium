"""
Database connection and session management.
"""

import logging
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import sessionmaker, Session

from config import settings

logger = logging.getLogger(__name__)


def make_engine(dsn: str, **kwargs) -> Engine:
    """An engine over a libpq DSN — settings.database_url, which psycopg2 and
    pg_dump take as it is. The driver is named because the default moved
    under us: SQLAlchemy 2.1 maps a bare postgresql:// to psycopg 3, which no
    node installs (requirements carry psycopg2, and every raw connection here
    is psycopg2), so a fresh install died importing it (2026-10-05)."""
    return create_engine(make_url(dsn).set(drivername="postgresql+psycopg2"), **kwargs)


engine = make_engine(
    settings.database_url,
    echo=settings.debug,
    pool_pre_ping=True,  # Verify connections before using
    pool_size=10,
    max_overflow=20
)

# Create session factory
SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)


def get_db() -> Generator[Session, None, None]:
    """
    Get database session for dependency injection.
    Usage with FastAPI:
        def endpoint(db: Session = Depends(get_db)):
            ...
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def get_db_context() -> Generator[Session, None, None]:
    """
    Get database session as context manager.
    Usage:
        with get_db_context() as db:
            ...
    """
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Database error: {e}")
        raise
    finally:
        db.close()


def init_db():
    """Initialize database (create tables if needed)."""
    from models import Base
    Base.metadata.create_all(bind=engine)
    logger.info("Database tables created/verified")
