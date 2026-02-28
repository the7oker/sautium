"""
Service lifecycle manager for Music AI DJ desktop launcher.

Manages three processes:
1. PostgreSQL (pg_ctl)
2. FastAPI backend (uvicorn)
3. Playback tracker (optional, if HQPlayer enabled)
"""

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Callable

import psutil

logger = logging.getLogger(__name__)


class ServiceManager:
    """Manages the lifecycle of backend services."""

    def __init__(self, config: dict):
        self.config = config
        self.backend_proc: Optional[subprocess.Popen] = None
        self.tracker_proc: Optional[subprocess.Popen] = None
        self._project_root = Path(__file__).parent.parent
        self._backend_dir = self._project_root / "backend"

    @property
    def ports(self) -> dict:
        return self.config.get("ports", {"postgres": 5432, "web": 8000, "tracker": 8765})

    # ================================================================
    # PostgreSQL
    # ================================================================

    def start_postgres(self, progress_cb: Optional[Callable] = None) -> bool:
        """Start PostgreSQL and run migrations."""
        from desktop.db_init import start_postgres, is_postgres_running, run_migrations

        port = self.ports.get("postgres", 5432)
        password = self.config.get("postgres_password", "changeme")

        if is_postgres_running():
            logger.info("PostgreSQL already running")
        else:
            if progress_cb:
                progress_cb("Starting PostgreSQL...")
            if not start_postgres(port=port):
                return False

        # Wait for connection
        if progress_cb:
            progress_cb("Connecting to database...")
        if not self._wait_for_postgres(password, port):
            return False

        # Run pending migrations
        if progress_cb:
            progress_cb("Checking migrations...")
        try:
            run_migrations(password, port=port, progress_cb=progress_cb)
        except Exception as e:
            logger.error(f"Migration failed: {e}")
            return False

        return True

    def stop_postgres(self) -> bool:
        """Stop PostgreSQL."""
        from desktop.db_init import stop_postgres
        return stop_postgres()

    def _wait_for_postgres(self, password: str, port: int, timeout: int = 20) -> bool:
        """Wait for PostgreSQL to accept connections."""
        import psycopg2

        for _ in range(timeout):
            try:
                conn = psycopg2.connect(
                    host="localhost", port=port,
                    user="musicai", password=password,
                    dbname="music_ai",
                    connect_timeout=2,
                )
                conn.close()
                return True
            except psycopg2.OperationalError:
                time.sleep(1)

        logger.error("PostgreSQL connection timeout")
        return False

    # ================================================================
    # Backend (FastAPI / uvicorn)
    # ================================================================

    def start_backend(self, progress_cb: Optional[Callable] = None) -> bool:
        """Start the FastAPI backend."""
        if self.backend_proc and self.backend_proc.poll() is None:
            logger.info("Backend already running")
            return True

        if progress_cb:
            progress_cb("Starting backend server...")

        port = self.ports.get("web", 8000)
        env_path = self._backend_dir / ".env"

        # Generate .env
        from desktop.config_manager import generate_env_file, generate_mcp_config
        generate_env_file(self.config, env_path)
        generate_mcp_config(self.config, self._backend_dir / "mcp-windows.json")

        env = os.environ.copy()
        env["PYTHONPATH"] = str(self._backend_dir)
        # Load .env vars into environment
        self._load_env_file(env_path, env)

        python = sys.executable
        cmd = [
            python, "-m", "uvicorn",
            "main:app",
            "--host", "127.0.0.1",
            "--port", str(port),
        ]

        kwargs = {
            "cwd": str(self._backend_dir),
            "env": env,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        try:
            self.backend_proc = subprocess.Popen(cmd, **kwargs)
            logger.info(f"Backend started (PID {self.backend_proc.pid}) on port {port}")
        except Exception as e:
            logger.error(f"Failed to start backend: {e}")
            return False

        # Wait for /health endpoint
        if progress_cb:
            progress_cb("Waiting for backend to be ready...")
        return self._wait_for_backend(port)

    def stop_backend(self) -> None:
        """Stop the backend process."""
        self._stop_process(self.backend_proc, "Backend")
        self.backend_proc = None

    def _wait_for_backend(self, port: int, timeout: int = 30) -> bool:
        """Wait for the backend /health endpoint to respond."""
        import urllib.request
        import urllib.error

        url = f"http://127.0.0.1:{port}/health"
        for _ in range(timeout):
            # Check if process died
            if self.backend_proc and self.backend_proc.poll() is not None:
                stderr = self.backend_proc.stderr.read().decode() if self.backend_proc.stderr else ""
                logger.error(f"Backend exited early: {stderr[:500]}")
                return False
            try:
                req = urllib.request.urlopen(url, timeout=2)
                if req.status == 200:
                    logger.info("Backend is ready")
                    return True
            except (urllib.error.URLError, ConnectionError, OSError):
                time.sleep(1)

        logger.error("Backend did not become ready")
        return False

    # ================================================================
    # Playback Tracker
    # ================================================================

    def start_tracker(self, progress_cb: Optional[Callable] = None) -> bool:
        """Start the playback tracker (if HQPlayer enabled)."""
        hqp = self.config.get("hqplayer", {})
        if not hqp.get("enabled"):
            logger.info("HQPlayer disabled — tracker not started")
            return True

        if self.tracker_proc and self.tracker_proc.poll() is None:
            logger.info("Tracker already running")
            return True

        if progress_cb:
            progress_cb("Starting playback tracker...")

        ports = self.ports
        password = self.config.get("postgres_password", "changeme")
        lastfm = self.config.get("lastfm", {})

        python = sys.executable
        cmd = [
            python, str(self._backend_dir / "playback_tracker.py"),
            "--hqplayer-host", hqp.get("host", "localhost"),
            "--hqplayer-port", str(hqp.get("port", 4321)),
            "--db-host", "localhost",
            "--db-port", str(ports.get("postgres", 5432)),
            "--db-user", "musicai",
            "--db-password", password,
            "--db-name", "music_ai",
            "--http-port", str(ports.get("tracker", 8765)),
        ]

        env = os.environ.copy()
        env["PYTHONPATH"] = str(self._backend_dir)
        # Set Last.fm env vars for tracker
        if lastfm.get("api_key"):
            env["LASTFM_API_KEY"] = lastfm["api_key"]
        if lastfm.get("api_secret"):
            env["LASTFM_API_SECRET"] = lastfm["api_secret"]
        if lastfm.get("session_key"):
            env["LASTFM_SESSION_KEY"] = lastfm["session_key"]
        if lastfm.get("username"):
            env["LASTFM_USERNAME"] = lastfm["username"]

        kwargs = {
            "cwd": str(self._backend_dir),
            "env": env,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        try:
            self.tracker_proc = subprocess.Popen(cmd, **kwargs)
            logger.info(f"Tracker started (PID {self.tracker_proc.pid})")
            return True
        except Exception as e:
            logger.error(f"Failed to start tracker: {e}")
            return False

    def stop_tracker(self) -> None:
        """Stop the playback tracker."""
        self._stop_process(self.tracker_proc, "Tracker")
        self.tracker_proc = None

    # ================================================================
    # Aggregate operations
    # ================================================================

    def start_all(self, progress_cb: Optional[Callable] = None) -> bool:
        """Start all services in order: PostgreSQL -> backend -> tracker."""
        if not self.start_postgres(progress_cb):
            return False
        if not self.start_backend(progress_cb):
            return False
        if not self.start_tracker(progress_cb):
            return False
        if progress_cb:
            progress_cb("All services running!")
        return True

    def stop_all(self) -> None:
        """Stop all services in reverse order."""
        self.stop_tracker()
        self.stop_backend()
        self.stop_postgres()

    def restart_backend_and_tracker(self, progress_cb: Optional[Callable] = None) -> bool:
        """Restart backend and tracker (keep PostgreSQL running)."""
        self.stop_tracker()
        self.stop_backend()
        if not self.start_backend(progress_cb):
            return False
        if not self.start_tracker(progress_cb):
            return False
        return True

    def get_status(self) -> dict:
        """Get status of all services."""
        from desktop.db_init import is_postgres_running

        return {
            "postgres": is_postgres_running(),
            "backend": self.backend_proc is not None and self.backend_proc.poll() is None,
            "tracker": self.tracker_proc is not None and self.tracker_proc.poll() is None,
        }

    # ================================================================
    # Helpers
    # ================================================================

    def _stop_process(self, proc: Optional[subprocess.Popen], name: str) -> None:
        """Gracefully stop a subprocess."""
        if proc is None or proc.poll() is not None:
            return

        logger.info(f"Stopping {name} (PID {proc.pid})...")

        try:
            if sys.platform == "win32":
                # On Windows, terminate the process tree
                parent = psutil.Process(proc.pid)
                for child in parent.children(recursive=True):
                    child.terminate()
                parent.terminate()
                proc.wait(timeout=10)
            else:
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=10)
        except psutil.NoSuchProcess:
            pass
        except subprocess.TimeoutExpired:
            logger.warning(f"{name} didn't stop gracefully, killing...")
            proc.kill()
            proc.wait(timeout=5)
        except Exception as e:
            logger.error(f"Error stopping {name}: {e}")

        logger.info(f"{name} stopped")

    @staticmethod
    def _load_env_file(env_path: Path, env: dict) -> None:
        """Load a .env file into an environment dict."""
        if not env_path.exists():
            return
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, _, value = line.partition("=")
                    env[key.strip()] = value.strip()
