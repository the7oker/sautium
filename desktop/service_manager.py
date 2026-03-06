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
        from desktop.utils import get_project_root
        self._project_root = get_project_root()
        self._backend_dir = self._project_root / "backend"

    @property
    def ports(self) -> dict:
        return self.config.get("ports", {"postgres": 5432, "web": 8000, "tracker": 8765})

    # ================================================================
    # PostgreSQL
    # ================================================================

    def start_postgres(self, progress_cb: Optional[Callable] = None) -> bool:
        """Start PostgreSQL and run migrations."""
        from desktop.db_init import (
            start_postgres, is_postgres_running, run_migrations,
            get_pg_bin_dir, download_portable_postgres, initialize_cluster,
            create_database,
        )

        port = self.ports.get("postgres", 5432)
        password = self.config.get("postgres_password", "changeme")

        # Auto-download PostgreSQL if not found
        try:
            get_pg_bin_dir()
        except FileNotFoundError:
            if sys.platform == "win32":
                if not download_portable_postgres(progress_cb):
                    return False
            else:
                logger.error("PostgreSQL not found")
                return False

        # Initialize cluster if needed
        initialize_cluster(password, progress_cb=progress_cb)

        if is_postgres_running():
            logger.info("PostgreSQL already running")
        else:
            if progress_cb:
                progress_cb("Starting PostgreSQL...")
            if not start_postgres(port=port):
                return False

        # Create database/role if needed (first run)
        if progress_cb:
            progress_cb("Checking database...")
        try:
            create_database(password, port=port, progress_cb=progress_cb)
        except Exception as e:
            logger.warning(f"create_database: {e}")

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

    def _get_backend_python(self) -> str:
        """Return path to Python for the backend (embedded 3.12 on Windows)."""
        from desktop.python_env import get_backend_python
        return get_backend_python()

    def _ensure_backend_python(self, progress_cb: Optional[Callable] = None) -> bool:
        """Ensure embedded Python 3.12 is available (Windows only)."""
        if sys.platform != "win32":
            return True

        from desktop.python_env import is_python_ready, download_embedded_python
        if is_python_ready():
            return True

        return download_embedded_python(progress_cb)

    def _ensure_backend_deps(self, progress_cb: Optional[Callable] = None) -> bool:
        """Install backend dependencies if missing."""
        # Ensure embedded Python 3.12 is available
        if not self._ensure_backend_python(progress_cb):
            return False

        python = self._get_backend_python()
        logger.info(f"Backend Python for deps: {python}")

        # Check if uvicorn already installed in backend Python
        check = subprocess.run(
            [python, "-c", "import uvicorn"],
            capture_output=True, text=True, timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        if check.returncode == 0:
            logger.info("Backend dependencies already installed")
            return True

        req_file = self._backend_dir / "requirements.txt"
        if not req_file.exists():
            logger.error(f"requirements.txt not found: {req_file}")
            return False

        if progress_cb:
            progress_cb("Installing backend dependencies (first run)...")

        logger.info("Installing backend dependencies...")

        # Install torch with CUDA first (PyPI torch is CPU-only on Windows)
        if progress_cb:
            progress_cb("Installing PyTorch with CUDA (may take a few minutes)...")
        torch_cmd = [
            python, "-m", "pip", "install",
            "torch", "torchvision", "torchaudio",
            "--index-url", "https://download.pytorch.org/whl/cu124",
            "--quiet",
        ]
        torch_env = os.environ.copy()
        torch_kwargs = {"capture_output": True, "text": True, "timeout": 600, "env": torch_env}
        if sys.platform == "win32":
            torch_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        torch_result = subprocess.run(torch_cmd, **torch_kwargs)
        if torch_result.returncode != 0:
            logger.warning(f"CUDA torch install failed, falling back to default: {torch_result.stderr[:200]}")
            if progress_cb:
                progress_cb("CUDA torch failed, using CPU fallback...")

        # Verify torch + CUDA
        verify_cmd = [python, "-c",
                      "import torch; print(f'torch {torch.__version__}, CUDA: {torch.cuda.is_available()}',"
                      "f'GPU: {torch.cuda.get_device_name(0)}' if torch.cuda.is_available() else '')"]
        verify_kwargs = {"capture_output": True, "text": True, "timeout": 30}
        if sys.platform == "win32":
            verify_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        verify = subprocess.run(verify_cmd, **verify_kwargs)
        torch_info = verify.stdout.strip() if verify.returncode == 0 else "torch not available"
        logger.info(f"PyTorch status: {torch_info}")
        if progress_cb:
            progress_cb(f"PyTorch: {torch_info}")

        if progress_cb:
            progress_cb("Installing backend dependencies...")

        cmd = [
            python, "-m", "pip", "install",
            "-r", str(req_file),
            "--only-binary=:all:",
            "--quiet",
        ]

        # Add pgsql/bin to PATH so pg_config is found for psycopg2
        # Force English locale to avoid encoding issues with pg_config
        env = os.environ.copy()
        pg_bin = self._project_root / "pgsql" / "bin"
        if pg_bin.exists():
            env["PATH"] = f"{pg_bin};{env.get('PATH', '')}"
        env["LANG"] = "C"
        env["LC_ALL"] = "C"
        env["PGCLIENTENCODING"] = "UTF8"

        kwargs = {"capture_output": True, "text": True, "timeout": 600, "env": env}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        result = subprocess.run(cmd, **kwargs)
        if result.returncode != 0:
            logger.error(f"pip install failed: {result.stderr}")
            if progress_cb:
                progress_cb(f"Failed to install dependencies: {result.stderr[:200]}")
            return False

        logger.info("Backend dependencies installed")
        return True

    def start_backend(self, progress_cb: Optional[Callable] = None) -> bool:
        """Start the FastAPI backend."""
        if self.backend_proc and self.backend_proc.poll() is None:
            logger.info("Backend already running")
            return True

        # Auto-install dependencies if missing
        if not self._ensure_backend_deps(progress_cb):
            return False

        if progress_cb:
            progress_cb("Starting backend server...")

        port = self.ports.get("web", 8000)

        # Kill orphan backend from a previous launcher session
        self._kill_orphan_on_port(port)

        env_path = self._backend_dir / ".env"

        # Generate .env
        from desktop.config_manager import generate_env_file, generate_mcp_config
        generate_env_file(self.config, env_path)
        generate_mcp_config(self.config, self._backend_dir / "mcp-windows.json")

        env = os.environ.copy()
        env["PYTHONPATH"] = str(self._backend_dir)
        # Load .env vars into environment
        self._load_env_file(env_path, env)

        backend_python = self._get_backend_python()
        logger.info(f"Backend Python: {backend_python}")
        cmd = [
            backend_python, "-m", "uvicorn",
            "main:app",
            "--host", "0.0.0.0",
            "--port", str(port),
        ]

        # Ensure Windows Firewall allows LAN access
        self._ensure_firewall_rule(port)

        # Log backend output to file instead of PIPE (PIPE can block on Windows)
        from desktop.config_manager import get_data_dir
        log_dir = get_data_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        backend_log = log_dir / "backend.log"
        self._backend_log_file = open(backend_log, "w", encoding="utf-8")

        kwargs = {
            "cwd": str(self._backend_dir),
            "env": env,
            "stdout": self._backend_log_file,
            "stderr": self._backend_log_file,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        try:
            self.backend_proc = subprocess.Popen(cmd, **kwargs)
            logger.info(f"Backend started (PID {self.backend_proc.pid}) on port {port}")
            logger.info(f"Backend log: {backend_log}")
        except Exception as e:
            logger.error(f"Failed to start backend: {e}")
            self._backend_log_file.close()
            return False

        # Wait for /health endpoint
        if progress_cb:
            progress_cb("Waiting for backend to be ready...")
        return self._wait_for_backend(port)

    def stop_backend(self) -> None:
        """Stop the backend process."""
        self._stop_process(self.backend_proc, "Backend")
        self.backend_proc = None

    def _wait_for_backend(self, port: int, timeout: int = 120) -> bool:
        """Wait for the backend /health endpoint to respond."""
        import urllib.request
        import urllib.error

        url = f"http://127.0.0.1:{port}/health"
        for i in range(timeout):
            # Check if process died
            if self.backend_proc and self.backend_proc.poll() is not None:
                err_msg = self._read_backend_log_tail()
                logger.error(f"Backend exited early: {err_msg}")
                return False
            try:
                req = urllib.request.urlopen(url, timeout=5)
                if req.status == 200:
                    logger.info("Backend is ready")
                    return True
            except Exception as e:
                if i % 10 == 0:
                    logger.debug(f"Health check attempt {i}: {type(e).__name__}: {e}")
                time.sleep(1)

        err_msg = self._read_backend_log_tail()
        logger.error(f"Backend did not become ready within {timeout}s. Log: {err_msg}")
        return False

    def _read_backend_log_tail(self) -> str:
        """Read last lines from backend log file."""
        try:
            from desktop.config_manager import get_data_dir
            log_file = get_data_dir() / "backend.log"
            if log_file.exists():
                lines = log_file.read_text(encoding="utf-8", errors="replace").strip().split("\n")
                return "\n".join(lines[-20:])
        except Exception:
            pass
        return "(no log available)"

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

        # Kill orphan tracker from a previous launcher session
        tracker_port = self.ports.get("tracker", 8765)
        self._kill_orphan_on_port(tracker_port)

        ports = self.ports
        password = self.config.get("postgres_password", "changeme")
        lastfm = self.config.get("lastfm", {})

        backend_python = self._get_backend_python()
        cmd = [
            backend_python, str(self._backend_dir / "playback_tracker.py"),
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
    def _ensure_firewall_rule(port: int) -> None:
        """Add Windows Firewall rule to allow LAN access on the given port."""
        if sys.platform != "win32":
            return

        rule_name = f"Music AI DJ (port {port})"
        try:
            # Check if rule already exists
            check = subprocess.run(
                ["netsh", "advfirewall", "firewall", "show", "rule",
                 f"name={rule_name}"],
                capture_output=True, text=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if check.returncode == 0 and rule_name in check.stdout:
                return  # Rule already exists

            # Create inbound rule
            subprocess.run(
                ["netsh", "advfirewall", "firewall", "add", "rule",
                 f"name={rule_name}",
                 "dir=in", "action=allow", "protocol=TCP",
                 f"localport={port}", "profile=private"],
                capture_output=True, text=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            logger.info(f"Firewall rule created: {rule_name}")
        except Exception as e:
            logger.debug(f"Could not create firewall rule: {e}")

    @staticmethod
    def _kill_orphan_on_port(port: int) -> None:
        """Kill any process listening on the given port (orphan from previous session)."""
        try:
            for conn in psutil.net_connections(kind="tcp"):
                if conn.laddr.port == port and conn.status == "LISTEN":
                    try:
                        proc = psutil.Process(conn.pid)
                        logger.warning(
                            f"Killing orphan process on port {port}: "
                            f"PID {conn.pid} ({proc.name()})"
                        )
                        proc.terminate()
                        proc.wait(timeout=5)
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                    except psutil.TimeoutExpired:
                        try:
                            psutil.Process(conn.pid).kill()
                        except psutil.NoSuchProcess:
                            pass
        except (psutil.AccessDenied, OSError) as e:
            logger.debug(f"Could not check for orphan processes: {e}")

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
