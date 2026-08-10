"""
Service lifecycle manager for Sautium desktop launcher.

Manages three processes:
1. PostgreSQL (pg_ctl)
2. FastAPI backend (uvicorn)
3. Playback tracker (optional, if HQPlayer enabled)
"""

import logging
import os
import platform
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional, Callable

import psutil

logger = logging.getLogger(__name__)

# A backend that survived this long crashed on its own trouble, not a boot
# loop — the restart budget resets.
_STABLE_UPTIME_SECONDS = 300
_MAX_CRASH_RESTARTS = 3


class ServiceManager:
    """Manages the lifecycle of backend services."""

    def __init__(self, config: dict):
        self.config = config
        self.backend_proc: Optional[subprocess.Popen] = None
        # Watchdog wiring: on_backend_event(state, message) fires from the
        # watchdog thread with state ∈ {"crashed", "restarted", "gave_up"}.
        self.on_backend_event: Optional[Callable[[str, str], None]] = None
        self._backend_stopping = False
        self._backend_started_at = 0.0
        self._crash_restarts = 0
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

        # PyTorch publishes no macOS x86_64 wheel past 2.2.2, so an Intel Mac
        # cannot have the pinned trio at all. Such a machine has neither CUDA
        # nor MPS, which makes it `lite` in hardware_profile — no local
        # analysis, no pre-warm, no translation — so the ML stack has nothing
        # to do there and the backend runs without it. requirements.txt
        # carries the matching environment marker; skipping here keeps the
        # launcher from reporting a failure for a package it must not install.
        no_ml_wheels = is_macos and platform.machine() == "x86_64"
        if no_ml_wheels:
            logger.info(
                "Intel macOS — no PyTorch wheels past 2.2.2; running as a "
                "torch-less lite node (analysis arrives via P2P)"
            )
            if progress_cb:
                progress_cb("CPU-only Mac — skipping PyTorch (lite node)")

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
        if (torch_missing or torch_wrong_build) and not no_ml_wheels:
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

        # Generated config goes to the launcher's own data dir, NOT into the
        # repo's backend/ — that directory is bind-mounted into the Docker
        # image as /app, and the backend's pydantic Settings reads ".env" from
        # its working directory. A file written here for the launcher was
        # therefore also read by the container, and every key compose did not
        # state explicitly leaked in: the container ran with the launcher's
        # GENA port (breaking DLNA eventing until compose was made explicit),
        # the launcher's random P2P port, and p2p_identity_dir pointing at a
        # Windows path. Two runtimes cannot share one config file.
        #
        # The launcher's backend does not need the file found — _load_env_file
        # below puts every key into the child's real environment, which
        # pydantic prefers over any file anyway.
        from desktop.config_manager import (generate_env_file, generate_mcp_config,
                                            get_data_dir)
        env_path = get_data_dir() / "backend.env"
        stale = self._backend_dir / ".env"
        if stale.exists():
            try:
                stale.unlink()
                logger.info("Removed shared backend/.env — the container read it too")
            except OSError as e:
                logger.warning("Could not remove stale backend/.env: %s", e)
        generate_env_file(self.config, env_path)
        generate_mcp_config(self.config, self._backend_dir / "mcp-windows.json")

        env = os.environ.copy()
        # backend_dir for the backend's own flat imports; project root so
        # `import desktop.*` works like it does under Docker (/app holds
        # both) — mb_discovery pulls desktop.mb_slice_client for the
        # click-to-mint slice fetch on dump-less nodes.
        env["PYTHONPATH"] = os.pathsep.join(
            [str(self._backend_dir), str(self._project_root)])
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

        # The peer surface (backend/p2p_app.py) runs as a second uvicorn
        # inside that process and mints its cert itself — point it at the
        # same directory, or it would look for the container path.
        env["SAUTIUM_TLS_DIR"] = str(tls_dir)

        # Ensure Windows Firewall allows LAN access
        self._ensure_firewall_rule(port)
        # Media surfaces (DLNA renderers fetch audio / send GENA events):
        # profile=any, or renderers on a "Public"-classified Wi-Fi can
        # control playback but never pull the bytes.
        self._ensure_firewall_rule(
            f"{self.ports.get('media', 8832)},{self.ports.get('gena', 8833)}",
            profile="any")
        # Backend peer surface, only if this install runs one (0 = off, the
        # launcher default — its own sync server serves peers instead and
        # opens its own rule).
        if self.ports.get("p2p_sync"):
            self._ensure_firewall_rule(self.ports["p2p_sync"])

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
            self._backend_stopping = False
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
        if not self._wait_for_backend(port):
            return False
        self._backend_started_at = time.time()
        threading.Thread(target=self._watch_backend, args=(self.backend_proc,),
                         daemon=True, name="backend-watchdog").start()
        return True

    def stop_backend(self) -> None:
        """Stop the backend process."""
        self._backend_stopping = True
        self._stop_process(self.backend_proc, "Backend")
        self.backend_proc = None
        if hasattr(self, "_backend_log_file") and self._backend_log_file:
            self._backend_log_file.close()
            self._backend_log_file = None

    def _watch_backend(self, proc: subprocess.Popen) -> None:
        """Event-driven death watch: blocks on the process handle, no
        polling. A deliberate stop/replace exits silently; a crash logs the
        log tail, notifies the UI and restarts — fast deaths burn a limited
        budget so a boot-loop can't spin forever."""
        proc.wait()
        if self._backend_stopping or proc is not self.backend_proc:
            return
        uptime = time.time() - self._backend_started_at
        logger.error(
            f"Backend died unexpectedly (exit {proc.returncode}, "
            f"uptime {uptime:.0f}s). Log tail:\n{self._read_backend_log_tail()}")
        if uptime >= _STABLE_UPTIME_SECONDS:
            self._crash_restarts = 0
        self._crash_restarts += 1
        if self._crash_restarts > _MAX_CRASH_RESTARTS:
            self._notify_backend_event(
                "gave_up", f"Backend crashed {self._crash_restarts} times in "
                "a row — not restarting, see backend.log")
            return
        self._notify_backend_event(
            "crashed", f"Backend crashed (exit {proc.returncode}) — restarting...")
        if self.start_backend():
            self._notify_backend_event("restarted", "Backend restarted after crash")
        else:
            self._notify_backend_event(
                "gave_up", "Backend restart failed — see backend.log")

    def _notify_backend_event(self, state: str, message: str) -> None:
        if self.on_backend_event is not None:
            self.on_backend_event(state, message)

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
                    # Safety net behind the orphan sweep: a 200 from a
                    # process that is not our child means someone else owns
                    # the port and we would report a stale backend as ours.
                    if self.backend_proc and self.backend_proc.poll() is not None:
                        logger.error(
                            "Port %d answers /health but our backend exited "
                            "(%s) — another process owns the port: %s",
                            port, self.backend_proc.returncode,
                            self._read_backend_log_tail(),
                        )
                        return False
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
    def _prune_stale_p2p_rules(current_port: int) -> None:
        """Delete firewall rules left behind by earlier P2P ports.

        The P2P port is drawn once per install and kept, so a normal user
        accumulates one rule. Reinstalling — or wiping the config, which is
        how a test node is rebuilt — draws a new one and leaves the old rule
        behind forever. Sixty-five had piled up on the development machine:
        sixty-four open inbound ports with nothing listening on them, waiting
        for some unrelated program to bind one and find itself exposed to the
        LAN.

        Only the 20000-29999 range is swept, which is exactly where the random
        draw lands. The fixed-port rules (web UI, media, GENA, peer surface)
        are named the same way and must survive."""
        if sys.platform != "win32":
            return
        import re
        try:
            out = subprocess.run(
                ["netsh", "advfirewall", "firewall", "show", "rule", "name=all"],
                capture_output=True, text=True, errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            stale = sorted({
                m.group(1) for m in re.finditer(r"Sautium \(TCP (2\d{4})\)", out.stdout)
                if m.group(1) != str(current_port)
            })
            if not stale:
                return
            batch = " & ".join(
                f'netsh advfirewall firewall delete rule name="Sautium (TCP {p})"'
                for p in stale)
            done = subprocess.run(["cmd", "/c", batch], capture_output=True,
                                  text=True, errors="replace",
                                  creationflags=subprocess.CREATE_NO_WINDOW)
            if done.returncode == 0:
                logger.info("Removed %d stale P2P firewall rule(s)", len(stale))
                return
            # One elevation for the whole batch — a prompt per rule would be
            # sixty-five prompts, which is the same as not offering it.
            import ctypes
            ret = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", "cmd", f'/c "{batch}"', None, 0)
            if ret > 32:
                logger.info("Removed %d stale P2P firewall rule(s) (elevated)",
                            len(stale))
            else:
                logger.warning("Firewall cleanup declined — %d stale rule(s) "
                               "remain", len(stale))
        except Exception as e:
            logger.debug(f"Firewall cleanup skipped: {e}")

    @staticmethod
    def _ensure_firewall_rule(port, protocol: str = "TCP",
                              profile: str = "private") -> None:
        """Add Windows Firewall rule, with UAC elevation if needed.

        `port` is an int or a netsh port list ("8832,8833"). Media-surface
        rules need profile="any": Windows misclassifies networks as Public
        often enough that a private-locked rule silently breaks renderer
        fetches (the 2026-07-12 DLNA lesson).

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
                 f"localport={port}", f"profile={profile}"],
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
                f"localport={port} profile={profile}"
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
        """Kill any process listening on the given port (orphan from previous session).

        Enumerated per process rather than through psutil.net_connections():
        that call needs root on macOS and raises AccessDenied for the WHOLE
        system table, so the orphan sweep silently did nothing there. The new
        backend then failed to bind while the stale one kept answering
        /health — a "started successfully" that ran the previous session's
        code. Per-process sockets are readable without privileges for
        processes this user owns, which the orphan always is.
        """
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                listening = any(
                    conn.status == psutil.CONN_LISTEN and conn.laddr.port == port
                    for conn in proc.net_connections(kind="tcp")
                )
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            if not listening:
                continue
            logger.warning(
                f"Killing orphan process on port {port}: "
                f"PID {proc.pid} ({proc.info.get('name')})"
            )
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except psutil.NoSuchProcess:
                pass
            except psutil.TimeoutExpired:
                try:
                    proc.kill()
                except psutil.NoSuchProcess:
                    pass

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
