"""
Database initialization for desktop (standalone) mode.

Handles:
- PostgreSQL cluster initialization (initdb)
- Database and role creation
- pgvector extension
- Schema migrations (numbered SQL files)
"""

import glob
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Callable

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)

# Migration tracking table
MIGRATIONS_TABLE = "_schema_migrations"


def get_pg_bin_dir() -> Path:
    """Get the PostgreSQL bin directory (portable install)."""
    # Look relative to project root: pgsql/bin/
    project_root = Path(__file__).parent.parent
    pg_dir = project_root / "pgsql" / "bin"
    if pg_dir.exists():
        return pg_dir

    # Fallback: check PATH
    pg_ctl = _which("pg_ctl")
    if pg_ctl:
        return Path(pg_ctl).parent

    raise FileNotFoundError(
        "PostgreSQL not found. Expected at pgsql/bin/ or in PATH."
    )


def get_pg_data_dir() -> Path:
    """Get the PostgreSQL data directory."""
    from desktop.config_manager import get_data_dir
    data_dir = get_data_dir() / "pgdata"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def _which(name: str) -> Optional[str]:
    """Find executable in PATH, with .exe suffix on Windows."""
    import shutil
    if sys.platform == "win32" and not name.endswith(".exe"):
        name += ".exe"
    return shutil.which(name)


def _run_pg_cmd(cmd: list, env: Optional[dict] = None, timeout: int = 60) -> subprocess.CompletedProcess:
    """Run a PostgreSQL command with CREATE_NO_WINDOW on Windows."""
    kwargs = {
        "capture_output": True,
        "text": True,
        "timeout": timeout,
        "env": env or os.environ.copy(),
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.run(cmd, **kwargs)


def initialize_cluster(password: str, progress_cb: Optional[Callable] = None) -> bool:
    """
    Initialize a new PostgreSQL data cluster.

    Returns True if cluster was initialized, False if already exists.
    """
    pg_bin = get_pg_bin_dir()
    data_dir = get_pg_data_dir()
    initdb = str(pg_bin / "initdb")

    # Check if already initialized
    if (data_dir / "PG_VERSION").exists():
        logger.info(f"PostgreSQL cluster already exists at {data_dir}")
        return False

    if progress_cb:
        progress_cb("Initializing PostgreSQL database cluster...")

    logger.info(f"Initializing PostgreSQL cluster at {data_dir}")

    # Write password to temp file for initdb
    pw_file = data_dir / ".pwfile"
    pw_file.write_text(password)

    try:
        result = _run_pg_cmd(
            [
                initdb,
                "-D", str(data_dir),
                "-U", "postgres",
                "-E", "UTF8",
                "--locale=C",
                f"--pwfile={pw_file}",
                "-A", "md5",
            ],
            timeout=120,
        )

        if result.returncode != 0:
            logger.error(f"initdb failed: {result.stderr}")
            raise RuntimeError(f"initdb failed: {result.stderr}")

        logger.info("PostgreSQL cluster initialized successfully")

        # Configure for local connections
        _configure_pg_hba(data_dir)
        _configure_postgresql_conf(data_dir)

        return True
    finally:
        pw_file.unlink(missing_ok=True)


def _configure_pg_hba(data_dir: Path) -> None:
    """Configure pg_hba.conf for localhost md5 auth."""
    hba_path = data_dir / "pg_hba.conf"
    hba_content = """# Music AI DJ - PostgreSQL HBA Configuration
# TYPE  DATABASE  USER  ADDRESS  METHOD
local   all       all            md5
host    all       all   127.0.0.1/32  md5
host    all       all   ::1/128       md5
"""
    hba_path.write_text(hba_content)


def _configure_postgresql_conf(data_dir: Path) -> None:
    """Configure postgresql.conf for local-only connections."""
    conf_path = data_dir / "postgresql.conf"

    # Read existing and append our settings
    existing = conf_path.read_text() if conf_path.exists() else ""

    additions = """
# Music AI DJ settings
listen_addresses = '127.0.0.1'
shared_preload_libraries = ''
log_destination = 'stderr'
logging_collector = off
"""
    conf_path.write_text(existing + additions)


def start_postgres(port: int = 5432) -> bool:
    """Start PostgreSQL server using pg_ctl."""
    pg_bin = get_pg_bin_dir()
    data_dir = get_pg_data_dir()
    pg_ctl = str(pg_bin / "pg_ctl")

    # Check if already running
    result = _run_pg_cmd([pg_ctl, "status", "-D", str(data_dir)])
    if result.returncode == 0:
        logger.info("PostgreSQL is already running")
        return True

    logger.info(f"Starting PostgreSQL on port {port}...")
    result = _run_pg_cmd(
        [
            pg_ctl, "start",
            "-D", str(data_dir),
            "-l", str(data_dir / "server.log"),
            "-o", f"-p {port}",
            "-w",  # wait for startup
        ],
        timeout=30,
    )

    if result.returncode != 0:
        logger.error(f"Failed to start PostgreSQL: {result.stderr}")
        return False

    logger.info("PostgreSQL started successfully")
    return True


def stop_postgres() -> bool:
    """Stop PostgreSQL server."""
    pg_bin = get_pg_bin_dir()
    data_dir = get_pg_data_dir()
    pg_ctl = str(pg_bin / "pg_ctl")

    result = _run_pg_cmd(
        [pg_ctl, "stop", "-D", str(data_dir), "-m", "fast", "-w"],
        timeout=30,
    )

    if result.returncode != 0:
        logger.warning(f"pg_ctl stop: {result.stderr}")
        return False

    logger.info("PostgreSQL stopped")
    return True


def is_postgres_running() -> bool:
    """Check if PostgreSQL is running."""
    pg_bin = get_pg_bin_dir()
    data_dir = get_pg_data_dir()
    pg_ctl = str(pg_bin / "pg_ctl")

    result = _run_pg_cmd([pg_ctl, "status", "-D", str(data_dir)])
    return result.returncode == 0


def create_database(
    password: str,
    port: int = 5432,
    progress_cb: Optional[Callable] = None,
) -> None:
    """Create the musicai role, music_ai database, and install pgvector."""
    if progress_cb:
        progress_cb("Creating database and user...")

    # Connect as postgres superuser
    conn = psycopg2.connect(
        host="localhost",
        port=port,
        user="postgres",
        password=password,
        dbname="postgres",
    )
    conn.autocommit = True

    try:
        with conn.cursor() as cur:
            # Create role if not exists
            cur.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = 'musicai'"
            )
            if not cur.fetchone():
                cur.execute(
                    f"CREATE ROLE musicai WITH LOGIN PASSWORD %s",
                    (password,),
                )
                logger.info("Created role 'musicai'")

            # Create database if not exists
            cur.execute(
                "SELECT 1 FROM pg_database WHERE datname = 'music_ai'"
            )
            if not cur.fetchone():
                cur.execute("CREATE DATABASE music_ai OWNER musicai")
                logger.info("Created database 'music_ai'")

            # Grant privileges
            cur.execute("GRANT ALL PRIVILEGES ON DATABASE music_ai TO musicai")
    finally:
        conn.close()

    if progress_cb:
        progress_cb("Installing pgvector extension...")

    # Connect to music_ai as postgres to install extension
    conn = psycopg2.connect(
        host="localhost",
        port=port,
        user="postgres",
        password=password,
        dbname="music_ai",
    )
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            # Grant schema privileges to musicai
            cur.execute("GRANT ALL ON SCHEMA public TO musicai")
            cur.execute(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                "GRANT ALL ON TABLES TO musicai"
            )
            cur.execute(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                "GRANT ALL ON SEQUENCES TO musicai"
            )
            logger.info("pgvector extension installed")
    finally:
        conn.close()


def run_migrations(
    password: str,
    port: int = 5432,
    progress_cb: Optional[Callable] = None,
) -> int:
    """
    Run pending SQL migrations from desktop/migrations/.

    Migrations are numbered files: 001_initial.sql, 002_add_feature.sql, etc.
    Tracks applied migrations in _schema_migrations table.

    Returns the number of migrations applied.
    """
    migrations_dir = Path(__file__).parent / "migrations"
    if not migrations_dir.exists():
        logger.warning(f"Migrations directory not found: {migrations_dir}")
        return 0

    # Find all migration files
    migration_files = sorted(migrations_dir.glob("*.sql"))
    if not migration_files:
        logger.info("No migration files found")
        return 0

    conn = psycopg2.connect(
        host="localhost",
        port=port,
        user="musicai",
        password=password,
        dbname="music_ai",
    )
    conn.autocommit = False

    try:
        with conn.cursor() as cur:
            # Ensure migrations tracking table exists
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {MIGRATIONS_TABLE} (
                    id SERIAL PRIMARY KEY,
                    filename VARCHAR(255) NOT NULL UNIQUE,
                    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()

            # Get already applied migrations
            cur.execute(f"SELECT filename FROM {MIGRATIONS_TABLE}")
            applied = {row[0] for row in cur.fetchall()}

            applied_count = 0
            for migration_file in migration_files:
                filename = migration_file.name
                if filename in applied:
                    continue

                logger.info(f"Applying migration: {filename}")
                if progress_cb:
                    progress_cb(f"Running migration: {filename}")

                sql = migration_file.read_text(encoding="utf-8")

                try:
                    cur.execute(sql)
                    cur.execute(
                        f"INSERT INTO {MIGRATIONS_TABLE} (filename) VALUES (%s)",
                        (filename,),
                    )
                    conn.commit()
                    applied_count += 1
                    logger.info(f"Migration {filename} applied successfully")
                except Exception as e:
                    conn.rollback()
                    logger.error(f"Migration {filename} failed: {e}")
                    raise

            if applied_count == 0:
                logger.info("All migrations are up to date")
            else:
                logger.info(f"Applied {applied_count} migration(s)")

            return applied_count
    finally:
        conn.close()


def full_init(
    password: str,
    port: int = 5432,
    progress_cb: Optional[Callable] = None,
) -> None:
    """
    Full initialization sequence:
    1. Initialize cluster (if needed)
    2. Start PostgreSQL
    3. Create database/role/pgvector (if needed)
    4. Run migrations
    """
    initialize_cluster(password, progress_cb=progress_cb)
    start_postgres(port=port)

    # Wait for PostgreSQL to be ready
    if progress_cb:
        progress_cb("Waiting for PostgreSQL to be ready...")

    for i in range(30):
        try:
            conn = psycopg2.connect(
                host="localhost",
                port=port,
                user="postgres",
                password=password,
                dbname="postgres",
            )
            conn.close()
            break
        except psycopg2.OperationalError:
            time.sleep(1)
    else:
        raise RuntimeError("PostgreSQL did not start within 30 seconds")

    create_database(password, port=port, progress_cb=progress_cb)
    run_migrations(password, port=port, progress_cb=progress_cb)

    if progress_cb:
        progress_cb("Database ready!")
