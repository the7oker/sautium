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
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional, Callable

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)

# Migration tracking table
MIGRATIONS_TABLE = "_schema_migrations"


PG_DOWNLOAD_URL = (
    "https://get.enterprisedb.com/postgresql/"
    "postgresql-16.8-1-windows-x64-binaries.zip"
)

PGVECTOR_DOWNLOAD_URL = (
    "https://github.com/andreiramani/pgvector_pgsql_windows/"
    "releases/download/0.8.1_16/vector.v0.8.1-pg16.zip"
)


def get_pg_bin_dir() -> Path:
    """Get the PostgreSQL bin directory (portable install)."""
    from desktop.utils import get_project_root
    project_root = get_project_root()
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


def download_portable_postgres(progress_cb: Optional[Callable] = None) -> bool:
    """Download and extract portable PostgreSQL for Windows."""
    from desktop.utils import get_project_root
    project_root = get_project_root()
    pg_bin = project_root / "pgsql" / "bin"

    if (pg_bin / "pg_ctl.exe").exists():
        logger.info("PostgreSQL already present")
        return True

    zip_path = project_root / "_postgresql_download.zip"

    try:
        if progress_cb:
            progress_cb("Downloading PostgreSQL (~200 MB)...")

        def _reporthook(block_num, block_size, total_size):
            if total_size > 0 and progress_cb:
                downloaded = block_num * block_size
                pct = min(100, downloaded * 100 // total_size)
                mb = downloaded // (1024 * 1024)
                total_mb = total_size // (1024 * 1024)
                progress_cb(f"Downloading PostgreSQL... {mb}/{total_mb} MB ({pct}%)")

        urllib.request.urlretrieve(PG_DOWNLOAD_URL, str(zip_path), reporthook=_reporthook)

        if progress_cb:
            progress_cb("Extracting PostgreSQL...")

        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(str(project_root))

        logger.info(f"PostgreSQL extracted to {project_root / 'pgsql'}")

        # Also download pgvector extension
        _download_pgvector(project_root / "pgsql", progress_cb)

        if progress_cb:
            progress_cb("PostgreSQL installed!")

        return True

    except Exception as e:
        logger.error(f"Failed to download PostgreSQL: {e}")
        if progress_cb:
            progress_cb(f"PostgreSQL download failed: {e}")
        return False

    finally:
        zip_path.unlink(missing_ok=True)


def _download_pgvector(pgsql_dir: Path, progress_cb: Optional[Callable] = None) -> bool:
    """Download and install pgvector extension into portable PostgreSQL."""
    ext_dir = pgsql_dir / "share" / "extension"
    if (ext_dir / "vector.control").exists():
        logger.info("pgvector already installed")
        return True

    zip_path = pgsql_dir.parent / "_pgvector_download.zip"

    try:
        if progress_cb:
            progress_cb("Downloading pgvector extension...")

        urllib.request.urlretrieve(PGVECTOR_DOWNLOAD_URL, str(zip_path))

        if progress_cb:
            progress_cb("Installing pgvector...")

        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(tmp)

            # Copy files to correct locations
            lib_dir = pgsql_dir / "lib"
            ext_dir.mkdir(parents=True, exist_ok=True)

            import shutil
            for f in tmp_path.rglob("*.dll"):
                shutil.copy2(f, lib_dir / f.name)
                logger.info(f"Installed {f.name} -> {lib_dir}")
            for f in tmp_path.rglob("*.control"):
                shutil.copy2(f, ext_dir / f.name)
                logger.info(f"Installed {f.name} -> {ext_dir}")
            for f in tmp_path.rglob("*.sql"):
                shutil.copy2(f, ext_dir / f.name)
                logger.info(f"Installed {f.name} -> {ext_dir}")

        logger.info("pgvector extension installed successfully")
        return True

    except Exception as e:
        logger.warning(f"Failed to download pgvector: {e}. Embedding search will be unavailable.")
        if progress_cb:
            progress_cb(f"pgvector download failed (non-critical): {e}")
        return False

    finally:
        zip_path.unlink(missing_ok=True)


def get_pg_data_dir() -> Path:
    """Get the PostgreSQL data directory."""
    from desktop.config_manager import get_data_dir
    return get_data_dir() / "pgdata"


def _which(name: str) -> Optional[str]:
    """Find executable in PATH, with .exe suffix on Windows."""
    import shutil
    if sys.platform == "win32" and not name.endswith(".exe"):
        name += ".exe"
    return shutil.which(name)


def _get_pg_env() -> dict:
    """Get environment with PostgreSQL bin/lib in PATH."""
    env = os.environ.copy()
    try:
        pg_bin = get_pg_bin_dir()
        pg_lib = pg_bin.parent / "lib"
        # Prepend pgsql/bin and pgsql/lib to PATH so DLLs are found
        env["PATH"] = f"{pg_bin};{pg_lib};{env.get('PATH', '')}"
    except FileNotFoundError:
        pass
    return env


def _run_pg_cmd(cmd: list, env: Optional[dict] = None, timeout: int = 60) -> subprocess.CompletedProcess:
    """Run a PostgreSQL command with CREATE_NO_WINDOW on Windows."""
    if env is None:
        env = _get_pg_env()
    kwargs = {
        "capture_output": True,
        "text": True,
        "timeout": timeout,
        "env": env,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    logger.debug(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, **kwargs)
    if result.returncode != 0:
        logger.debug(f"stdout: {result.stdout}")
        logger.debug(f"stderr: {result.stderr}")
    return result


def initialize_cluster(password: str, progress_cb: Optional[Callable] = None) -> bool:
    """
    Initialize a new PostgreSQL data cluster.

    Returns True if cluster was initialized, False if already exists.
    """
    pg_bin = get_pg_bin_dir()
    data_dir = get_pg_data_dir()
    initdb = str(pg_bin / "initdb")

    # Check if already initialized
    if data_dir.exists() and (data_dir / "PG_VERSION").exists():
        logger.info(f"PostgreSQL cluster already exists at {data_dir}")
        return False

    # Clean up partially initialized pgdata (e.g. from a previous failed attempt)
    if data_dir.exists() and any(data_dir.iterdir()):
        logger.warning(f"pgdata exists but not initialized, cleaning up: {data_dir}")
        import shutil
        shutil.rmtree(data_dir)

    # Create fresh pgdata directory
    data_dir.mkdir(parents=True, exist_ok=True)

    if progress_cb:
        progress_cb("Initializing PostgreSQL database cluster...")

    logger.info(f"Initializing PostgreSQL cluster at {data_dir}")

    # Write password to temp file OUTSIDE pgdata (initdb requires empty dir)
    pw_file = data_dir.parent / ".pwfile"
    pw_file.write_text(password, encoding="utf-8")

    try:
        logger.info(f"Running: {initdb} -D {data_dir}")
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
            err_msg = result.stderr or result.stdout or "Unknown error"
            logger.error(f"initdb failed (rc={result.returncode}): {err_msg}")
            if progress_cb:
                progress_cb(f"initdb failed: {err_msg[:200]}")
            raise RuntimeError(f"initdb failed: {err_msg}")

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

    # Remove stale postmaster.pid if PostgreSQL was killed ungracefully
    pid_file = data_dir / "postmaster.pid"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().split("\n")[0].strip())
            import psutil
            if not psutil.pid_exists(pid):
                logger.warning(f"Removing stale postmaster.pid (PID {pid} not running)")
                pid_file.unlink()
        except Exception:
            pid_file.unlink(missing_ok=True)

    # On Windows, pg_ctl start -w hangs with captured output because
    # the child postgres process inherits pipe handles. Use Popen
    # without capturing output and poll for readiness instead.
    env = _get_pg_env()
    cmd = [
        pg_ctl, "start",
        "-D", str(data_dir),
        "-l", str(data_dir / "server.log"),
        "-o", f"-p {port}",
    ]
    kwargs = {"env": env, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    proc = subprocess.Popen(cmd, **kwargs)
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        logger.warning("pg_ctl start timed out, checking if server started anyway...")
        proc.kill()

    # Poll for readiness — try connecting as postgres superuser
    for i in range(30):
        try:
            conn = psycopg2.connect(
                host="localhost", port=port,
                user="postgres", password="dummy",
                dbname="postgres", connect_timeout=1,
            )
            conn.close()
            logger.info("PostgreSQL started successfully")
            return True
        except psycopg2.OperationalError as e:
            err = str(e)
            # Auth failure means server is up (just wrong password)
            if "password authentication failed" in err or "authentication failed" in err:
                logger.info("PostgreSQL started successfully")
                return True
            time.sleep(1)

    logger.error("PostgreSQL did not become ready in time")
    return False


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
    try:
        pg_bin = get_pg_bin_dir()
    except FileNotFoundError:
        return False

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

    # Connect to music_ai as postgres to install extension and grant privileges
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
            # Try to install pgvector (optional — not available in portable PG)
            try:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
                logger.info("pgvector extension installed")
            except Exception as e:
                logger.warning(
                    f"pgvector not available: {e}. "
                    "Embedding search will be disabled until pgvector is installed."
                )
                if progress_cb:
                    progress_cb("pgvector not available (embedding search disabled)")

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
    0. Download PostgreSQL if not present (Windows)
    1. Initialize cluster (if needed)
    2. Start PostgreSQL
    3. Create database/role/pgvector (if needed)
    4. Run migrations
    """
    # Auto-download PostgreSQL if missing on Windows
    if sys.platform == "win32":
        try:
            get_pg_bin_dir()
        except FileNotFoundError:
            logger.info("PostgreSQL not found, downloading portable version...")
            if not download_portable_postgres(progress_cb):
                raise RuntimeError(
                    "Failed to download PostgreSQL. "
                    "Check your internet connection and try again."
                )

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
