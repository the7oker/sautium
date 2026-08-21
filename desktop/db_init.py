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
import platform
import shutil
import subprocess
import sys
import tarfile
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

# Portable PostgreSQL (Windows), Homebrew formula (macOS) and the Docker image
# tag (docker-compose*.yml) all track the SAME major — bump them together.
# A datadir is bound to its major: new binaries refuse a mismatched cluster.
EXPECTED_PG_MAJOR = 18

# Windows portable PostgreSQL
PG_DOWNLOAD_URL = (
    "https://get.enterprisedb.com/postgresql/"
    "postgresql-18.4-1-windows-x64-binaries.zip"
)

PGVECTOR_DOWNLOAD_URL = (
    "https://github.com/andreiramani/pgvector_pgsql_windows/"
    "releases/download/0.8.3_18.4/vector.v0.8.3-pg18.zip"
)

IS_MACOS = sys.platform == "darwin"
IS_WINDOWS = sys.platform == "win32"


def _binary_pg_major(pg_bin: Path) -> Optional[int]:
    """PG major of an installed binary set, via `pg_ctl --version`
    ('pg_ctl (PostgreSQL) 18.4' → 18). None if it can't be determined."""
    import re
    pg_ctl = pg_bin / ("pg_ctl.exe" if IS_WINDOWS else "pg_ctl")
    if not pg_ctl.exists():
        return None
    kwargs = {"capture_output": True, "text": True, "timeout": 10}
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        out = subprocess.run([str(pg_ctl), "--version"], **kwargs).stdout or ""
    except Exception:
        return None
    m = re.search(r"\)\s+(\d+)", out)
    return int(m.group(1)) if m else None


def get_pg_bin_dir() -> Path:
    """Get the PostgreSQL bin directory (portable install or Homebrew)."""
    from desktop.utils import get_project_root
    project_root = get_project_root()
    pg_dir = project_root / "pgsql" / "bin"
    if pg_dir.exists():
        return pg_dir

    # macOS: check Homebrew paths
    if IS_MACOS:
        brew_pg = _get_homebrew_pg_bin()
        if brew_pg:
            return brew_pg

    # Fallback: check PATH
    pg_ctl = _which("pg_ctl")
    if pg_ctl:
        return Path(pg_ctl).parent

    raise FileNotFoundError(
        "PostgreSQL not found. Expected at pgsql/bin/ or in PATH."
    )


def download_portable_postgres(progress_cb: Optional[Callable] = None) -> bool:
    """Download/install PostgreSQL (Windows: portable zip, macOS: Homebrew)."""
    if IS_MACOS:
        return _install_postgres_macos(progress_cb)

    # Windows: download portable binaries
    from desktop.utils import get_project_root
    project_root = get_project_root()
    pg_bin = project_root / "pgsql" / "bin"

    if (pg_bin / "pg_ctl.exe").exists():
        major = _binary_pg_major(pg_bin)
        if major == EXPECTED_PG_MAJOR or major is None:
            logger.info("PostgreSQL already present (PG%s)", major)
            return True
        # Stale major from an earlier release — swap the binaries. The datadir is
        # wiped separately by initialize_cluster (embedded PG is disposable).
        logger.warning("Portable PostgreSQL is PG%s, need PG%s — replacing binaries",
                       major, EXPECTED_PG_MAJOR)
        if progress_cb:
            progress_cb(f"Upgrading PostgreSQL {major} → {EXPECTED_PG_MAJOR}...")
        try:
            stop_postgres()
        except Exception:
            pass
        shutil.rmtree(project_root / "pgsql", ignore_errors=True)

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
    # On macOS pgvector is installed via Homebrew alongside PostgreSQL
    if IS_MACOS:
        return True

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


# ================================================================
# macOS (Homebrew) support
# ================================================================

def _get_homebrew_prefix() -> Optional[str]:
    """Get Homebrew prefix path."""
    brew = shutil.which("brew")
    if not brew:
        # Check well-known locations
        for path in ["/opt/homebrew/bin/brew", "/usr/local/bin/brew"]:
            if os.path.isfile(path):
                brew = path
                break
    if not brew:
        return None
    try:
        result = subprocess.run(
            [brew, "--prefix"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def _get_homebrew_pg_bin() -> Optional[Path]:
    """Find PostgreSQL installed via Homebrew."""
    brew = shutil.which("brew")
    if not brew:
        for path in ["/opt/homebrew/bin/brew", "/usr/local/bin/brew"]:
            if os.path.isfile(path):
                brew = path
                break
    if not brew:
        return None

    # Try versioned formulae first, then unversioned
    for formula in ["postgresql@18", "postgresql@17", "postgresql@16", "postgresql@15", "postgresql"]:
        try:
            result = subprocess.run(
                [brew, "--prefix", formula],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                bin_dir = Path(result.stdout.strip()) / "bin"
                if (bin_dir / "pg_ctl").exists():
                    logger.info(f"Found Homebrew PostgreSQL: {bin_dir}")
                    return bin_dir
        except Exception:
            continue
    return None


def _find_brew() -> Optional[str]:
    """Find the brew executable."""
    brew = shutil.which("brew")
    if brew:
        return brew
    for path in ["/opt/homebrew/bin/brew", "/usr/local/bin/brew"]:
        if os.path.isfile(path):
            return path
    return None


def _install_postgres_macos(progress_cb: Optional[Callable] = None) -> bool:
    """Install PostgreSQL + pgvector via Homebrew on macOS."""
    # Skip install only if the EXPECTED major is already present. An older major
    # left over (e.g. @17) must NOT short-circuit — otherwise the bump never
    # installs @18 and initialize_cluster's wipe would just recreate the old one.
    brew_pg = _get_homebrew_pg_bin()
    if brew_pg and _binary_pg_major(brew_pg) == EXPECTED_PG_MAJOR:
        logger.info("PostgreSQL %s already installed via Homebrew", EXPECTED_PG_MAJOR)
        _ensure_pgvector_homebrew(progress_cb)
        return True

    brew = _find_brew()
    if not brew:
        logger.error(
            "Homebrew not found. Please install Homebrew first: "
            "https://brew.sh"
        )
        if progress_cb:
            progress_cb(
                "Homebrew not found! Install it from https://brew.sh "
                "then restart Sautium."
            )
        return False

    try:
        if progress_cb:
            progress_cb("Installing PostgreSQL via Homebrew (may take a few minutes)...")

        logger.info("Installing postgresql@18 via Homebrew...")
        result = subprocess.run(
            [brew, "install", "postgresql@18"],
            capture_output=True, text=True, timeout=600,
        )
        if result.returncode != 0:
            logger.error(f"brew install postgresql@18 failed: {result.stderr}")
            if progress_cb:
                progress_cb(f"PostgreSQL install failed: {result.stderr[:200]}")
            return False

        logger.info("PostgreSQL installed via Homebrew")

        # Install pgvector
        _ensure_pgvector_homebrew(progress_cb)

        if progress_cb:
            progress_cb("PostgreSQL installed!")

        return True

    except Exception as e:
        logger.error(f"Failed to install PostgreSQL via Homebrew: {e}")
        if progress_cb:
            progress_cb(f"PostgreSQL install failed: {e}")
        return False


def _ensure_pgvector_homebrew(progress_cb: Optional[Callable] = None) -> bool:
    """Install pgvector via Homebrew if not already present, and link into PG."""
    brew = _find_brew()
    if not brew:
        return False

    # Check if pgvector is already installed
    installed = False
    try:
        result = subprocess.run(
            [brew, "list", "pgvector"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            logger.info("pgvector already installed via Homebrew")
            installed = True
    except Exception:
        pass

    if not installed:
        try:
            if progress_cb:
                progress_cb("Installing pgvector extension...")

            logger.info("Installing pgvector via Homebrew...")
            result = subprocess.run(
                [brew, "install", "pgvector"],
                capture_output=True, text=True, timeout=300,
            )
            if result.returncode != 0:
                logger.warning(f"brew install pgvector failed: {result.stderr}")
                if progress_cb:
                    progress_cb("pgvector install failed (non-critical)")
                return False

            logger.info("pgvector installed via Homebrew")
            installed = True

        except Exception as e:
            logger.warning(f"Failed to install pgvector: {e}")
            return False

    # Homebrew pgvector puts files under share/postgresql@NN/ but keg-only
    # PostgreSQL uses share/postgresql/. Symlink if needed.
    if installed:
        _link_pgvector_to_pg(brew)

    return installed


def _link_pgvector_to_pg(brew: str) -> None:
    """Symlink pgvector extension files into the active PostgreSQL prefix."""
    pg_bin = _get_homebrew_pg_bin()
    if not pg_bin:
        return

    pg_prefix = pg_bin.parent  # e.g. /opt/homebrew/opt/postgresql@17
    pg_ext_dir = pg_prefix / "share" / "postgresql" / "extension"
    pg_lib_dir = pg_prefix / "lib" / "postgresql"

    # Already linked?
    if (pg_ext_dir / "vector.control").exists():
        return

    # Find pgvector cellar path
    try:
        result = subprocess.run(
            [brew, "--prefix", "pgvector"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return
        pv_prefix = Path(result.stdout.strip())
    except Exception:
        return

    # Try versioned subdirs (postgresql@17, postgresql@16, etc.)
    for subdir in sorted(pv_prefix.glob("share/postgresql@*/extension"), reverse=True):
        if (subdir / "vector.control").exists():
            logger.info(f"Linking pgvector from {subdir} into {pg_ext_dir}")
            pg_ext_dir.mkdir(parents=True, exist_ok=True)
            for f in subdir.iterdir():
                dest = pg_ext_dir / f.name
                if not dest.exists():
                    dest.symlink_to(f)
            break

    for subdir in sorted(pv_prefix.glob("lib/postgresql@*"), reverse=True):
        dylib = subdir / "vector.dylib"
        if dylib.exists():
            pg_lib_dir.mkdir(parents=True, exist_ok=True)
            dest = pg_lib_dir / "vector.dylib"
            if not dest.exists():
                dest.symlink_to(dylib)
            break


# ================================================================
# Media CLI tools (audio decode / fingerprint / transcode)
# ================================================================
# The audio pipeline shells out to external binaries: ffmpeg (+ ffprobe) for
# the shared 48kHz decode path (embeddings + analysis), fpcalc (Chromaprint)
# for the content-address fingerprint, flac for stream->FLAC transcode, and
# deno, the sandboxed JavaScript runtime yt-dlp solves YouTube's player
# challenges in. A fresh node missing any fails those steps silently (ffmpeg:
# 0 embeddings; fpcalc: no fingerprint provenance; deno: yt-dlp limps along on
# a deprecated runtime-less path that breaks whenever YouTube moves). The
# launcher installs them all at first run so the app is self-contained.
#
# yt-dlp itself is NOT here: it is a dependency of the backend interpreter
# (requirements.txt) which streaming/youtube.py runs as `-m yt_dlp`, and
# refresh_media_tools keeps it on the nightly channel.

# (binary, macOS brew formula, Windows static-build zip URL). The ffmpeg zip
# also carries ffprobe; the Windows extractor copies every .exe beside it.
_MEDIA_TOOLS = [
    ("ffmpeg", "ffmpeg",
     "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"),
    ("fpcalc", "chromaprint",
     "https://github.com/acoustid/chromaprint/releases/download/v1.5.1/"
     "chromaprint-fpcalc-1.5.1-windows-x86_64.zip"),
    ("flac", "flac",
     "https://ftp.osuosl.org/pub/xiph/releases/flac/flac-1.4.3-win.zip"),
    ("deno", "deno",
     "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip"),
]

# brew installs into these on macOS; a GUI-launched .app has a minimal PATH
# omitting them, so the backend must add them explicitly (service_manager).
_MACOS_BREW_BIN_DIRS = ["/opt/homebrew/bin", "/usr/local/bin"]


def media_tool_dirs() -> list:
    """Dirs to prepend to a child-process PATH so the media binaries resolve,
    independent of how the launcher itself was started."""
    if IS_MACOS:
        return [d for d in _MACOS_BREW_BIN_DIRS if os.path.isdir(d)]
    if IS_WINDOWS:
        from desktop.utils import get_project_root
        root = get_project_root()
        return [str(root / b / "bin") for b, _, _ in _MEDIA_TOOLS
                if (root / b / "bin").exists()]
    return []


def _which_tool(binary: str) -> Optional[str]:
    """A media binary on PATH, or in the platform's known install dirs."""
    found = shutil.which(binary)
    if found:
        return found
    exe = binary + ".exe" if IS_WINDOWS else binary
    for d in media_tool_dirs():
        cand = Path(d) / exe
        if cand.exists():
            return str(cand)
    return None


def ensure_media_tools(progress_cb: Optional[Callable] = None) -> dict:
    """Ensure every media CLI binary (ffmpeg, fpcalc, flac, deno) is present.
    Idempotent per tool. Non-fatal — a failure leaves that one step degraded,
    never blocks setup. Returns {binary: present?}."""
    result = {}
    for binary, formula, win_url in _MEDIA_TOOLS:
        if _which_tool(binary):
            result[binary] = True
            continue
        if IS_MACOS:
            result[binary] = _brew_install(binary, formula, progress_cb)
        elif IS_WINDOWS:
            result[binary] = _download_win_tool(binary, win_url, progress_cb)
        else:
            logger.warning("%s not found on PATH — install it via your package manager", binary)
            result[binary] = False
    return result


def refresh_media_tools(backend_python: str) -> None:
    """Move the backend interpreter's yt-dlp to the latest nightly. YouTube
    changes its side every few weeks and a frozen yt-dlp silently loses phantom
    streaming — upstream calls stable "prone to external breakage" and points
    regular users at nightly. Same call the Docker node makes at container
    start (backend/entrypoint.py), against a different interpreter. Offline or
    rate-limited → whatever is installed stays; non-fatal either way."""
    cmd = [backend_python, "-m", "pip", "install", "-q", "-U", "--pre",
           "--no-cache-dir", "--retries", "2", "--timeout", "15",
           "yt-dlp[default]"]
    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WINDOWS else {}
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=300, **kwargs)
    except subprocess.TimeoutExpired:
        logger.warning("yt-dlp refresh timed out; keeping the installed build")
        return
    if result.returncode != 0:
        tail = (result.stderr or result.stdout).strip().splitlines()
        logger.warning("yt-dlp refresh skipped: %s",
                       tail[-1] if tail else f"rc={result.returncode}")
    ver = subprocess.run([backend_python, "-m", "yt_dlp", "--version"],
                         capture_output=True, text=True, **kwargs).stdout.strip()
    logger.info("yt-dlp %s", ver or "not installed")


def _brew_install(binary: str, formula: str, progress_cb: Optional[Callable] = None) -> bool:
    brew = _find_brew()
    if not brew:
        logger.error("Homebrew not found — cannot auto-install %s", formula)
        if progress_cb:
            progress_cb("Homebrew not found — install it from https://brew.sh, then reopen Sautium")
        return False
    if progress_cb:
        progress_cb(f"Installing {binary}...")
    logger.info("Installing %s via Homebrew...", formula)
    env = os.environ.copy()
    env["HOMEBREW_NO_AUTO_UPDATE"] = "1"

    def _run() -> Optional[subprocess.CompletedProcess]:
        try:
            return subprocess.run(
                [brew, "install", formula],
                capture_output=True, text=True, timeout=1800, env=env,
            )
        except subprocess.TimeoutExpired:
            logger.error("brew install %s timed out", formula)
            return None

    result = _run()
    # HOMEBREW_NO_AUTO_UPDATE keeps a routine launch fast, but it also means
    # a stale Homebrew never repairs itself: a vendored-ruby or formula-index
    # mismatch then fails every install with a Ruby `require` traceback that
    # says nothing about the formula. `brew update` is the repair, so spend it
    # once on failure rather than on every launch. (Observed 2026-08-02: the
    # media step failed for all three tools while the PostgreSQL step — which
    # does not suppress auto-update — succeeded minutes later on the same box.)
    if result is not None and result.returncode != 0:
        logger.warning(
            "brew install %s failed, retrying after `brew update`: %s",
            formula, result.stderr[-300:],
        )
        if progress_cb:
            progress_cb("Updating Homebrew...")
        try:
            subprocess.run([brew, "update"], capture_output=True, text=True,
                           timeout=900)
        except subprocess.TimeoutExpired:
            logger.error("brew update timed out")
        result = _run()

    if result is None:
        return False
    if result.returncode != 0:
        logger.error("brew install %s failed: %s", formula, result.stderr[-500:])
        return False
    logger.info("%s installed via Homebrew", formula)
    return True


def _download_win_tool(binary: str, url: str, progress_cb: Optional[Callable] = None) -> bool:
    import tempfile
    from desktop.utils import get_project_root
    bin_dir = get_project_root() / binary / "bin"
    exe = binary + ".exe"
    if (bin_dir / exe).exists():
        return True
    zip_path = get_project_root() / f"_{binary}_download.zip"
    try:
        if progress_cb:
            progress_cb(f"Downloading {binary}...")
        urllib.request.urlretrieve(url, str(zip_path))
        bin_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(tmp)
            hits = list(Path(tmp).rglob(exe))
            if not hits:
                logger.error("%s not found in downloaded archive", exe)
                return False
            for f in hits[0].parent.glob("*.exe"):   # ffmpeg ships ffprobe alongside
                shutil.copy2(f, bin_dir / f.name)
        logger.info("%s installed to %s", binary, bin_dir)
        return (bin_dir / exe).exists()
    except Exception as e:
        logger.error("Failed to download %s: %s", binary, e)
        if progress_cb:
            progress_cb(f"{binary} download failed: {e}")
        return False
    finally:
        zip_path.unlink(missing_ok=True)


def get_pg_data_dir() -> Path:
    """Get the PostgreSQL data directory."""
    from desktop.config_manager import get_data_dir
    return get_data_dir() / "pgdata"


def _which(name: str) -> Optional[str]:
    """Find executable in PATH, with .exe suffix on Windows."""
    if IS_WINDOWS and not name.endswith(".exe"):
        name += ".exe"
    return shutil.which(name)


def _get_pg_env() -> dict:
    """Get environment with PostgreSQL bin/lib in PATH."""
    env = os.environ.copy()
    sep = os.pathsep
    try:
        pg_bin = get_pg_bin_dir()
        pg_lib = pg_bin.parent / "lib"
        # Prepend pgsql/bin and pgsql/lib to PATH so DLLs/dylibs are found
        env["PATH"] = f"{pg_bin}{sep}{pg_lib}{sep}{env.get('PATH', '')}"
        if IS_MACOS:
            env["DYLD_LIBRARY_PATH"] = f"{pg_lib}{sep}{env.get('DYLD_LIBRARY_PATH', '')}"
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
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    logger.debug(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, **kwargs)
    if result.returncode != 0:
        logger.debug(f"stdout: {result.stdout}")
        logger.debug(f"stderr: {result.stderr}")
    return result


def _datadir_pg_major(data_dir: Path) -> Optional[int]:
    """Major version of an existing pgdata, or None if absent/unreadable.
    A datadir is bound to its major — the new binaries refuse to start on a
    mismatched one, so a version bump must wipe & reinit. Embedded PG holds no
    irreplaceable data (it re-derives from a fresh scan + P2P sync)."""
    pv = data_dir / "PG_VERSION"
    if not pv.exists():
        return None
    try:
        return int(pv.read_text().strip().split(".")[0])
    except ValueError:
        return None


def initialize_cluster(password: str, progress_cb: Optional[Callable] = None) -> bool:
    """
    Initialize a new PostgreSQL data cluster.

    Returns True if cluster was initialized, False if already exists.
    """
    pg_bin = get_pg_bin_dir()
    data_dir = get_pg_data_dir()
    initdb = str(pg_bin / "initdb")

    # A pgdata from an older PG major won't start on the new binaries. Embedded
    # PG is a disposable test/replica store, so wipe & reinit rather than a
    # dump/restore upgrade (which would need the old binaries shipped alongside).
    existing_major = _datadir_pg_major(data_dir)
    if existing_major is not None and existing_major != EXPECTED_PG_MAJOR:
        logger.warning(
            "pgdata is PG%s but binaries are PG%s — wiping & reinitializing",
            existing_major, EXPECTED_PG_MAJOR,
        )
        if progress_cb:
            progress_cb(f"Upgrading PostgreSQL {existing_major} → {EXPECTED_PG_MAJOR} (reinitializing)...")
        try:
            stop_postgres()
        except Exception:
            pass
        shutil.rmtree(data_dir)

    # Check if already initialized
    if data_dir.exists() and (data_dir / "PG_VERSION").exists():
        logger.info(f"PostgreSQL cluster already exists at {data_dir}")
        return False

    # Clean up partially initialized pgdata (e.g. from a previous failed attempt)
    if data_dir.exists() and any(data_dir.iterdir()):
        logger.warning(f"pgdata exists but not initialized, cleaning up: {data_dir}")
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
    hba_content = """# Sautium - PostgreSQL HBA Configuration
# TYPE  DATABASE  USER  ADDRESS  METHOD
local   all       all            md5
host    all       all   127.0.0.1/32  md5
host    all       all   ::1/128       md5
"""
    hba_path.write_text(hba_content)


def _pg_memory_tier() -> str:
    """RAM-tiered memory settings for the embedded PG (HARDWARE-TIERS §6.13).
    Stock initdb values (128MB/64MB) starve a big-library node — MB-slice
    ANALYZE and HNSW scans thrash the buffer pool — while a low-RAM machine
    must keep them. Tier by total RAM, mirroring the hardware-profile
    thresholds; psutil is a desktop-venv dependency so it's always present
    here."""
    import psutil
    ram_gb = psutil.virtual_memory().total / 1e9
    if ram_gb >= 24:
        return ("shared_buffers = '1GB'\n"
                "effective_cache_size = '6GB'\n"
                "maintenance_work_mem = '512MB'\n"
                "work_mem = '16MB'\n")
    if ram_gb >= 15:
        return ("shared_buffers = '512MB'\n"
                "effective_cache_size = '4GB'\n"
                "maintenance_work_mem = '256MB'\n"
                "work_mem = '8MB'\n")
    return ""   # <15GB: stock defaults are the safe choice


def _configure_postgresql_conf(data_dir: Path) -> None:
    """Configure postgresql.conf for local-only connections + tiered memory."""
    conf_path = data_dir / "postgresql.conf"

    # Read existing and append our settings
    existing = conf_path.read_text() if conf_path.exists() else ""

    additions = f"""
# Sautium settings
listen_addresses = '127.0.0.1'
timezone = 'UTC'
shared_preload_libraries = ''
log_destination = 'stderr'
logging_collector = off
# Single-node appliance, no replication/PITR: minimal WAL lets the MB-dump
# loader's TRUNCATE+COPY-in-one-txn skip WAL; 4GB keeps a 21GB dump load from
# checkpointing every ~1GB written (mirrors docker-compose.yml).
wal_level = minimal
max_wal_senders = 0
max_wal_size = '4GB'
{_pg_memory_tier()}"""
    conf_path.write_text(existing + additions)


def _ensure_bulk_load_conf(data_dir: Path) -> None:
    """Idempotently bring an EXISTING cluster up to the bulk-load settings
    that _configure_postgresql_conf writes for new ones — clusters created
    before those settings existed never re-run initdb, so without this only
    fresh installs would get the WAL-skipping dump-load path."""
    conf_path = data_dir / "postgresql.conf"
    if not conf_path.exists():
        return
    existing = conf_path.read_text()
    if "wal_level = minimal" in existing:
        return
    conf_path.write_text(existing + """
# Sautium bulk-load settings (mirrors _configure_postgresql_conf for new clusters)
wal_level = minimal
max_wal_senders = 0
max_wal_size = '4GB'
""")
    logger.info("postgresql.conf: appended bulk-load settings (wal_level=minimal)")


def _log_server_log_tail(data_dir: Path, lines: int = 15) -> None:
    """Surface why a start failed. The real cause (a port bind conflict, a bad
    config line) is written to server.log, not to the connection timeout —
    without this the launcher only ever says 'did not become ready', hiding
    e.g. 'could not bind ... Address already in use'."""
    try:
        content = (data_dir / "server.log").read_text(errors="replace").splitlines()
    except OSError:
        return
    tail = [ln for ln in content[-lines:] if ln.strip()]
    if tail:
        logger.error("PostgreSQL server.log (tail):\n%s", "\n".join(tail))


def start_postgres(port: int = 5432) -> bool:
    """Start PostgreSQL server using pg_ctl."""
    pg_bin = get_pg_bin_dir()
    data_dir = get_pg_data_dir()
    pg_ctl = str(pg_bin / "pg_ctl")
    _ensure_bulk_load_conf(data_dir)

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
    if IS_WINDOWS:
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
    _log_server_log_tail(data_dir)
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
    """Create the sautium role, sautium database, and install pgvector."""
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
                "SELECT 1 FROM pg_roles WHERE rolname = 'sautium'"
            )
            if not cur.fetchone():
                cur.execute(
                    f"CREATE ROLE sautium WITH LOGIN PASSWORD %s",
                    (password,),
                )
                logger.info("Created role 'sautium'")

            # Create database if not exists
            cur.execute(
                "SELECT 1 FROM pg_database WHERE datname = 'sautium'"
            )
            if not cur.fetchone():
                # ICU collation (not the cluster's C locale) so lower()/sort fold
                # Unicode. Under LC_CTYPE=C, lower() only touches ASCII — Cyrillic/
                # diacritic/CJK artist names then fail MB name-matching (search_artist
                # does `lower(name) = lower(query)`), so non-ASCII artists never
                # canonicalize. ICU is per-database, so the C cluster is left as-is.
                cur.execute(
                    "CREATE DATABASE sautium OWNER sautium "
                    "LOCALE_PROVIDER icu ICU_LOCALE 'und' LOCALE 'C' "
                    "TEMPLATE template0 ENCODING UTF8"
                )
                logger.info("Created database 'sautium' (ICU collation)")

            # Grant privileges
            cur.execute("GRANT ALL PRIVILEGES ON DATABASE sautium TO sautium")
    finally:
        conn.close()

    # Connect to sautium as postgres to install extension and grant privileges
    conn = psycopg2.connect(
        host="localhost",
        port=port,
        user="postgres",
        password=password,
        dbname="sautium",
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

            # Grant schema privileges to sautium
            cur.execute("GRANT ALL ON SCHEMA public TO sautium")
            cur.execute(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                "GRANT ALL ON TABLES TO sautium"
            )
            cur.execute(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                "GRANT ALL ON SEQUENCES TO sautium"
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
        user="sautium",
        password=password,
        dbname="sautium",
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
    # Ensure the correct PostgreSQL major is installed. Idempotent-fast when
    # already correct; upgrades a stale major in place (download_portable_postgres
    # version-gates the binaries, initialize_cluster the datadir).
    if IS_WINDOWS or IS_MACOS:
        if not download_portable_postgres(progress_cb):
            raise RuntimeError(
                "Failed to install PostgreSQL. "
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
