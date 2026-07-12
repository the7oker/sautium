"""
Service lifecycle manager for Sautium desktop launcher.

Manages three processes:
1. PostgreSQL (pg_ctl)
2. FastAPI backend (uvicorn)
3. Playback tracker (optional, if HQPlayer enabled)
"""

import logging
import os
import shutil
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

        # Ensure the correct PostgreSQL major (idempotent; upgrades a stale major
        # in place). Bare-metal Linux is expected to provide PostgreSQL itself.
        if sys.platform in ("win32", "darwin"):
            if not download_portable_postgres(progress_cb):
                return False
        else:
            try:
                get_pg_bin_dir()
            except FileNotFoundError:
                logger.error("PostgreSQL not found. Install it manually.")
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
                    user="sautium", password=password,
                    dbname="sautium",
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
        """Install backend dependencies if missing or out-of-date.

        Skip-fast condition: marker file at <data_dir>/.backend_deps_hash holds
        the SHA-256 of requirements.txt from the last successful install. If it
        matches the current file, the venv is up to date and we return immediately.
        Any change to requirements.txt (added/removed/version-bumped package)
        invalidates the marker and forces a `pip install -r requirements.txt`,
        which top-ups whatever is missing.
        """
        # Ensure embedded Python 3.12 is available
        if not self._ensure_backend_python(progress_cb):
            return False

        python = self._get_backend_python()
        logger.info(f"Backend Python for deps: {python}")

        req_file = self._backend_dir / "requirements.txt"
        if not req_file.exists():
            logger.error(f"requirements.txt not found: {req_file}")
            return False

        import hashlib
        from desktop.config_manager import get_data_dir
        req_hash = hashlib.sha256(req_file.read_bytes()).hexdigest()
        marker = get_data_dir() / ".backend_deps_hash"
        if marker.exists() and marker.read_text().strip() == req_hash:
            logger.info("Backend dependencies already up to date")
            return True

        _cflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        is_macos = sys.platform == "darwin"

        # Install PyTorch only if absent or built for the wrong accelerator —
        # once a correct build is in the venv, we leave it alone (re-installing
        # on every requirements.txt edit would cost minutes and re-download
        # multi-GB wheels for no reason).
        #
        # Versions are pinned to the same trio as backend/requirements.txt
        # (torch._dynamo is sensitive to the torch×stdlib pair — see the
        # comment there). The CUDA index must carry those exact versions:
        # cu124 stopped at torch 2.6, which used to make a fresh install grab
        # 2.6+cu124 and then let requirements.txt replace it with the CPU-only
        # 2.12.0 wheel from PyPI — silently killing GPU on new nodes. cu126
        # has the full trio and needs only driver R525+.
        _TORCH_PINS = ["torch==2.12.0", "torchvision==0.27.0", "torchaudio==2.11.0"]
        _CUDA_INDEX = "https://download.pytorch.org/whl/cu126"
        _CPU_INDEX = "https://download.pytorch.org/whl/cpu"

        from desktop.utils import detect_gpu
        has_nvidia = (not is_macos) and shutil.which("nvidia-smi") is not None \
            and detect_gpu()[0]

        # A CPU-only build on an NVIDIA machine is the wrong-build failure a
        # plain `import torch` check can't see — torch.version.cuda is a build
        # property (None on +cpu wheels), so it detects it deterministically.
        torch_check = subprocess.run(
            [python, "-c", "import torch; print(torch.version.cuda or 'cpu')"],
            capture_output=True, text=True, timeout=30,
            creationflags=_cflags,
        )
        torch_missing = torch_check.returncode != 0
        torch_wrong_build = (
            not torch_missing and has_nvidia
            and torch_check.stdout.strip() == "cpu"
        )
        if torch_missing or torch_wrong_build:
            if progress_cb:
                progress_cb("Installing PyTorch (first run, may take a few minutes)...")
            logger.info(
                "PyTorch %s, installing...",
                "not found" if torch_missing else "is a CPU build on a CUDA machine",
            )

            torch_cmd = [python, "-m", "pip", "install", *_TORCH_PINS, "--quiet"]
            if not is_macos:
                index = _CUDA_INDEX if has_nvidia else _CPU_INDEX
                torch_cmd += ["--index-url", index]

            torch_kwargs = {"capture_output": True, "text": True, "timeout": 600, "env": os.environ.copy()}
            if sys.platform == "win32":
                torch_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            torch_result = subprocess.run(torch_cmd, **torch_kwargs)
            if torch_result.returncode != 0:
                logger.warning(f"PyTorch install failed: {torch_result.stderr[:200]}")
                if progress_cb:
                    progress_cb("PyTorch install failed, continuing anyway...")

            if is_macos:
                verify_code = (
                    "import torch; mps = torch.backends.mps.is_available() if hasattr(torch.backends, 'mps') else False; "
                    "print(f'torch {torch.__version__}, MPS: {mps}')"
                )
            else:
                verify_code = (
                    "import torch; print(f'torch {torch.__version__}, CUDA: {torch.cuda.is_available()}',"
                    "f'GPU: {torch.cuda.get_device_name(0)}' if torch.cuda.is_available() else '')"
                )
            verify_kwargs = {"capture_output": True, "text": True, "timeout": 30}
            if sys.platform == "win32":
                verify_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            verify = subprocess.run([python, "-c", verify_code], **verify_kwargs)
            torch_info = verify.stdout.strip() if verify.returncode == 0 else "torch not available"
            logger.info(f"PyTorch status: {torch_info}")
            if progress_cb:
                progress_cb(f"PyTorch: {torch_info}")

        if progress_cb:
            progress_cb("Installing/updating backend dependencies...")
        logger.info("Installing backend dependencies from requirements.txt")

        cmd = [
            python, "-m", "pip", "install",
            "-r", str(req_file),
            "--quiet",
        ]
        # On Windows, force binary-only to avoid build issues
        if sys.platform == "win32":
            cmd.insert(-1, "--only-binary=:all:")

        # Add pgsql/bin to PATH so pg_config is found for psycopg2
        # Force English locale to avoid encoding issues with pg_config
        env = os.environ.copy()
        sep = os.pathsep
        pg_bin = self._project_root / "pgsql" / "bin"
        if pg_bin.exists():
            env["PATH"] = f"{pg_bin}{sep}{env.get('PATH', '')}"
        # On macOS, also add Homebrew pg_config to PATH
        if is_macos:
            from desktop.db_init import _get_homebrew_pg_bin
            brew_pg = _get_homebrew_pg_bin()
            if brew_pg:
                env["PATH"] = f"{brew_pg}{sep}{env.get('PATH', '')}"
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

        marker.write_text(req_hash)
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
        # Ensure the media binaries (ffmpeg/fpcalc/flac) resolve for the
        # backend even when the launcher was started as a GUI .app (minimal
        # PATH without Homebrew/bundled bins). The audio pipeline shells out
        # to them (audio_analysis, provenance, streaming).
        #
        # ensure_media_tools first: it's idempotent-fast when the binaries
        # exist, and it's the only retry path if the wizard's media step
        # failed or the tools were deleted — without it a missing ffmpeg
        # degrades to a silent "0/N enriched" with no recovery on relaunch.
        from desktop.db_init import ensure_media_tools, media_tool_dirs
        tools = ensure_media_tools(progress_cb)
        missing = [name for name, ok in tools.items() if not ok]
        if missing:
            logger.warning(
                "Media tools missing after ensure: %s — audio analysis/"
                "fingerprinting will be degraded until installed",
                ", ".join(missing),
            )
        tool_dirs = media_tool_dirs()
        if tool_dirs:
            env["PATH"] = os.pathsep.join(tool_dirs + [env.get("PATH", "")])
        # Load .env vars into environment
        self._load_env_file(env_path, env)
        # CUDA caching-allocator: expandable segments curb the VRAM
        # fragmentation that otherwise balloons reserved memory over a
        # long-lived backend (measured −71% frag gap). setdefault so an
        # explicit .env / shell override still wins.
        env.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

        backend_python = self._get_backend_python()
        logger.info(f"Backend Python: {backend_python}")

        # Generate (or reuse) self-signed TLS cert. Browsers gate
        # crypto.subtle behind a secure context, so HMAC request signing
        # in the Web UI doesn't work over plain HTTP from a phone on LAN.
        # Backend listens HTTPS-only.
        #
        # Cert lives under ~/.sautium/tls/<account-pubkey-prefix>/ rather
        # than the per-install %APPDATA%\Sautium dir so a typical
        # reinstall (which scrubs APPDATA + LOCALAPPDATA + the bundled
        # Python) leaves the cert in place — the browser keeps the
        # one-time trust decision. The pubkey-prefix subdir keeps
        # different accounts on the same machine isolated. Path.home()
        # resolves to %USERPROFILE% on Windows, $HOME on macOS / Linux,
        # so the layout is identical cross-platform. Falls back to the
        # legacy data-dir location if no account is set up yet (first
        # launch of the wizard).
        tls_dir = None
        try:
            from desktop.node_identity import has_account, get_account_info
            if has_account():
                acct = get_account_info() or {}
                pub = (acct.get("public_key_hex") or "").lower()
                if pub:
                    tls_dir = Path.home() / ".sautium" / "tls" / pub[:16]
        except Exception as e:
            logger.debug(f"Account lookup for TLS dir failed: {e}")
        if tls_dir is None:
            from desktop.config_manager import get_data_dir
            tls_dir = get_data_dir() / "tls"
        tls_dir.mkdir(parents=True, exist_ok=True)
        cert_path = tls_dir / "cert.pem"
        key_path = tls_dir / "key.pem"

        try:
            subprocess.run(
                [backend_python, str(self._backend_dir / "tls_gen.py"),
                 "--data-dir", str(tls_dir)],
                cwd=str(self._backend_dir),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            logger.info(f"TLS cert ready at {cert_path}")
        except subprocess.CalledProcessError as e:
            logger.error(f"TLS cert generation failed: {e.stderr or e.stdout}")
            return False

        cmd = [
            backend_python, "-m", "uvicorn",
            "main:app",
            "--host", "0.0.0.0",
            "--port", str(port),
            "--ssl-keyfile", str(key_path),
            "--ssl-certfile", str(cert_path),
            "--timeout-graceful-shutdown", "5",
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
        if hasattr(self, "_backend_log_file") and self._backend_log_file:
            self._backend_log_file.close()
            self._backend_log_file = None

    def _wait_for_backend(self, port: int, timeout: int = 120) -> bool:
        """Wait for the backend /health endpoint to respond."""
        import ssl
        import urllib.request
        import urllib.error

        # Backend serves HTTPS with a self-signed cert (see start_backend).
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        url = f"https://127.0.0.1:{port}/health"
        for i in range(timeout):
            # Check if process died
            if self.backend_proc and self.backend_proc.poll() is not None:
                err_msg = self._read_backend_log_tail()
                logger.error(f"Backend exited early: {err_msg}")
                return False
            try:
                req = urllib.request.urlopen(url, timeout=5, context=ssl_ctx)
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
        """No-op. Play tracking (listening_history + local_play_stats + Last.fm
        scrobbling) now runs inside the backend's own status poller
        (backend/routers/player.py), source-agnostic over owned and streamed
        phantom tracks — there is no separate tracker process anymore. Kept as a
        lifecycle hook so the launcher/updater start sequence is unchanged."""
        return True

    def stop_tracker(self) -> None:
        """No-op — tracking lives in the backend process (see start_tracker)."""

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

        backend_up = self.backend_proc is not None and self.backend_proc.poll() is None
        return {
            "postgres": is_postgres_running(),
            "backend": backend_up,
            # Tracking is part of the backend poller now — it's up iff backend is.
            "tracker": backend_up,
        }

    # ================================================================
    # Helpers
    # ================================================================

    def _stop_process(self, proc: Optional[subprocess.Popen], name: str) -> None:
        """Gracefully stop a subprocess (process tree)."""
        if proc is None or proc.poll() is not None:
            return

        logger.info(f"Stopping {name} (PID {proc.pid})...")

        try:
            parent = psutil.Process(proc.pid)
            children = parent.children(recursive=True)

            # On Unix, send SIGINT (Ctrl+C) to parent — uvicorn handles it cleanly.
            # On Windows, terminate the whole tree (SIGINT is unreliable).
            if sys.platform != "win32":
                parent.send_signal(signal.SIGINT)
            else:
                for child in children:
                    child.terminate()
                parent.terminate()

            # Wait for the whole process tree
            gone, alive = psutil.wait_procs([parent] + children, timeout=10)

            if alive:
                logger.warning(f"{name} didn't stop gracefully, killing {len(alive)} process(es)...")
                for p in alive:
                    try:
                        p.kill()
                    except psutil.NoSuchProcess:
                        pass
                psutil.wait_procs(alive, timeout=5)
        except psutil.NoSuchProcess:
            pass
        except Exception as e:
            logger.error(f"Error stopping {name}: {e}")

        logger.info(f"{name} stopped")

    @staticmethod
    def _ensure_firewall_rule(port: int, protocol: str = "TCP") -> None:
        """Add Windows Firewall rule, with UAC elevation if needed.

        Skips silently if the rule already exists.  Tries without elevation
        first; on failure requests admin via ShellExecuteW (one-time UAC
        prompt per missing rule).
        """
        if sys.platform != "win32":
            return

        rule_name = f"Sautium ({protocol} {port})"
        try:
            check = subprocess.run(
                ["netsh", "advfirewall", "firewall", "show", "rule",
                 f"name={rule_name}"],
                capture_output=True, text=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if check.returncode == 0 and rule_name in check.stdout:
                return  # rule already exists

            # Try without elevation (works if already admin)
            add = subprocess.run(
                ["netsh", "advfirewall", "firewall", "add", "rule",
                 f"name={rule_name}",
                 "dir=in", "action=allow", f"protocol={protocol}",
                 f"localport={port}", "profile=private"],
                capture_output=True, text=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if add.returncode == 0:
                logger.info(f"Firewall rule created: {rule_name}")
                return

            # Elevate via UAC
            import ctypes
            args = (
                f'advfirewall firewall add rule name="{rule_name}" '
                f"dir=in action=allow protocol={protocol} "
                f"localport={port} profile=private"
            )
            ret = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", "netsh", args, None, 0,
            )
            if ret > 32:
                logger.info(f"Firewall rule created (elevated): {rule_name}")
            else:
                logger.warning(
                    f"Firewall elevation declined: {rule_name}"
                )
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
