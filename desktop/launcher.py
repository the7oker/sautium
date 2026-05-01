"""
Main launcher window for Sautium.

Shows service status, QR code for mobile access, and controls.
Minimizes to system tray on close.
"""

import logging
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path
from tkinter import filedialog
from typing import Optional

import customtkinter as ctk

from desktop.api_client import BackendAPIClient
from desktop.config_manager import load_config, save_config, update_config
from desktop.service_manager import ServiceManager
from desktop.utils import get_local_ip, generate_qr_ctk

logger = logging.getLogger(__name__)

# Appearance
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class LauncherApp(ctk.CTk):
    """Main Sautium launcher window."""

    def __init__(self):
        super().__init__()

        self.title("Sautium")
        self.geometry("480x870")
        self.resizable(False, False)

        self.config = load_config()
        self.service_manager = ServiceManager(self.config)
        self.api_client = BackendAPIClient()
        self.p2p_manager = None
        # Set while _start_p2p_if_enabled's background thread is between
        # "thread launched" and "self.p2p_manager assigned". Prevents a
        # second _on_services_ready call (from a Scan-driven backend
        # restart) from launching a parallel P2PManager that races on the
        # same listen port.
        self._p2p_starting = False
        self.tray = None
        self._update_thread = None
        self._stats_timer = None

        # Check first run
        if not self.config.get("first_run_complete"):
            # On macOS, move offscreen instead of withdraw — withdrawn parent hides transient children
            if sys.platform == "darwin":
                self.geometry("1x1+9999+9999")
                self.overrideredirect(True)
            else:
                self.withdraw()
            self.after(200, self._run_wizard)
        else:
            self._build_ui()
            self.after(100, self._startup_sequence)

        # Close → minimize to tray
        self.protocol("WM_DELETE_WINDOW", self._minimize_to_tray)

    def _run_wizard(self):
        from desktop.wizard import SetupWizard

        def on_wizard_complete(config):
            self.config = config
            self.service_manager = ServiceManager(self.config)
            if sys.platform == "darwin":
                self.overrideredirect(False)
            self.deiconify()
            self.geometry("480x870")
            self._build_ui()
            self.lift()
            self.focus_force()
            self.after(100, self._startup_sequence)

        self.update_idletasks()
        wiz = SetupWizard(self, on_complete=on_wizard_complete)
        wiz.after(50, wiz.lift)
        wiz.after(50, wiz.focus_force)

    def _build_ui(self):
        """Build the main launcher UI."""
        # Title
        ctk.CTkLabel(
            self, text="Sautium",
            font=ctk.CTkFont(size=24, weight="bold"),
        ).pack(pady=(15, 5))

        # Status section
        status_frame = ctk.CTkFrame(self)
        status_frame.pack(fill="x", padx=20, pady=10)

        self._status_dot = ctk.CTkLabel(
            status_frame, text="", width=16, height=16,
            fg_color="gray", corner_radius=8,
        )
        self._status_dot.pack(side="left", padx=(10, 5), pady=10)

        self._status_text = ctk.CTkLabel(
            status_frame, text="Starting...",
            font=ctk.CTkFont(size=14),
        )
        self._status_text.pack(side="left", padx=5, pady=10)

        # QR Code
        self._qr_frame = ctk.CTkFrame(self, fg_color="transparent")
        self._qr_frame.pack(pady=5)

        self._qr_label = ctk.CTkLabel(self._qr_frame, text="")
        self._qr_label.pack()

        # URL label
        self._url_label = ctk.CTkLabel(
            self, text="", text_color="gray",
            font=ctk.CTkFont(size=12),
        )
        self._url_label.pack(pady=(0, 5))

        # Library stats section
        stats_outer = ctk.CTkFrame(self)
        stats_outer.pack(fill="x", padx=20, pady=5)

        ctk.CTkLabel(
            stats_outer, text="Library",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(anchor="w", padx=10, pady=(5, 2))

        stats_grid = ctk.CTkFrame(stats_outer, fg_color="transparent")
        stats_grid.pack(fill="x", padx=10, pady=(0, 5))
        stats_grid.columnconfigure((0, 1), weight=1)

        self._stat_labels = {}
        for i, (key, label) in enumerate([
            ("tracks", "Tracks"), ("artists", "Artists"),
            ("albums", "Albums"), ("genres", "Genres"),
        ]):
            lbl = ctk.CTkLabel(
                stats_grid, text=f"{label}: —",
                font=ctk.CTkFont(size=12), text_color="gray",
            )
            lbl.grid(row=i // 2, column=i % 2, sticky="w", padx=5, pady=1)
            self._stat_labels[key] = lbl

        # Enrichment stats section
        ctk.CTkLabel(
            stats_outer, text="Enrichment",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(anchor="w", padx=10, pady=(5, 2))

        enrich_grid = ctk.CTkFrame(stats_outer, fg_color="transparent")
        enrich_grid.pack(fill="x", padx=10, pady=(0, 5))
        enrich_grid.columnconfigure((0, 1), weight=1)

        for i, (key, label) in enumerate([
            ("embeddings", "Embeddings"), ("features", "Features"),
            ("lastfm", "Last.fm"), ("lyrics", "Lyrics"),
        ]):
            lbl = ctk.CTkLabel(
                enrich_grid, text=f"{label}: —",
                font=ctk.CTkFont(size=12), text_color="gray",
            )
            lbl.grid(row=i // 2, column=i % 2, sticky="w", padx=5, pady=1)
            self._stat_labels[key] = lbl

        # Buttons
        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(fill="x", padx=20, pady=5)

        self._btn_open = ctk.CTkButton(
            btn_frame, text="Open Web UI", width=200,
            command=self._open_web_ui, state="disabled",
        )
        self._btn_open.pack(pady=3)

        self._btn_scan = ctk.CTkButton(
            btn_frame, text="Scan Library", width=200,
            command=self._scan_library, state="disabled",
            fg_color="transparent", border_width=1,
        )
        self._btn_scan.pack(pady=3)

        self._btn_enrich = ctk.CTkButton(
            btn_frame, text="Enrich Library", width=200,
            command=self._enrich_library, state="disabled",
            fg_color="transparent", border_width=1,
        )
        self._btn_enrich.pack(pady=3)

        self._btn_sync = ctk.CTkButton(
            btn_frame, text="Sync Library", width=200,
            command=self._sync_library, state="disabled",
            fg_color="transparent", border_width=1,
        )
        self._btn_sync.pack(pady=3)

        self._btn_settings = ctk.CTkButton(
            btn_frame, text="Settings", width=200,
            command=self._open_settings,
            fg_color="transparent", border_width=1,
        )
        self._btn_settings.pack(pady=3)

        self._btn_update = ctk.CTkButton(
            btn_frame, text="Check for Updates", width=200,
            command=self._check_updates,
            fg_color="transparent", border_width=1,
        )
        self._btn_update.pack(pady=3)

        # Progress / status message
        self._progress_text = ctk.CTkLabel(
            self, text="", text_color="gray",
            font=ctk.CTkFont(size=11),
        )
        self._progress_text.pack(pady=(5, 0))

        # Quit button
        ctk.CTkButton(
            self, text="Quit", width=100,
            command=self._quit,
            fg_color="#8B0000", hover_color="#A52A2A",
        ).pack(pady=(5, 15))

    def _startup_sequence(self):
        """Start all services in a background thread."""
        def _start():
            self._set_status("starting", "Starting services...")

            def progress(msg):
                self.after(0, lambda: self._progress_text.configure(text=msg))

            try:
                success = self.service_manager.start_all(progress_cb=progress)
            except Exception as e:
                logger.error(f"Startup failed: {e}", exc_info=True)
                err_msg = str(e)[:200]
                self.after(0, lambda: self._set_status("error", "Startup failed"))
                self.after(0, lambda: self._progress_text.configure(text=err_msg))
                return

            if success:
                self.after(0, self._on_services_ready)
            else:
                self.after(0, lambda: self._set_status("error", "Failed to start services"))

        threading.Thread(target=_start, daemon=True).start()

        # Check for updates in background (non-blocking)
        self._check_updates_background()

    def _on_services_ready(self):
        """Called when all services are running."""
        port = self.config.get("ports", {}).get("web", 8000)
        local_ip = get_local_ip()
        local_url = f"http://localhost:{port}"
        lan_url = f"http://{local_ip}:{port}"

        # Check GPU status via backend Python (torch is in python312, not launcher)
        gpu_text = ""
        try:
            backend_python = self.service_manager._get_backend_python()
            gpu_check = subprocess.run(
                [backend_python, "-c",
                 "import torch; print(f'GPU: {torch.cuda.get_device_name(0)}') "
                 "if torch.cuda.is_available() else print('GPU: not available (CPU mode)')"],
                capture_output=True, text=True, timeout=15,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            gpu_text = gpu_check.stdout.strip() if gpu_check.returncode == 0 else "GPU: torch not available"
        except Exception:
            gpu_text = "GPU: check failed"

        self._set_status("running", "All services running")
        self._url_label.configure(text=f"Local: {local_url}  |  LAN: {lan_url}")
        self._btn_open.configure(state="normal")
        self._btn_scan.configure(state="normal")
        self._btn_enrich.configure(state="normal")
        # Sync button stays disabled until P2P is ready
        self._btn_sync.configure(state="disabled")
        self._progress_text.configure(text=gpu_text)

        # Connect API client to the right port
        self.api_client.set_port(port)

        # Fetch and display library stats
        self._fetch_and_display_stats()

        # Generate QR code for LAN access
        qr_img = generate_qr_ctk(lan_url, size=180)
        if qr_img:
            self._qr_label.configure(image=qr_img, text="")
        else:
            self._qr_label.configure(text=f"Scan: {lan_url}")

        # Silently generate node identity if not present
        self._ensure_node_identity()

        # Start P2P services only if not already running. _on_services_ready
        # is also called from the Scan flow's `restart_backend_and_tracker`,
        # but P2P is independent of the backend process — its only DB-side
        # dependency (postgres password / port) doesn't change on a backend
        # restart, so there's nothing to rebuild. Re-running stop+start here
        # used to race with the still-mounting first P2PManager (its
        # background thread hadn't yet assigned self.p2p_manager) and bound
        # two managers to the same port. Update path explicitly nulls
        # p2p_manager itself before calling _on_services_ready.
        if self.p2p_manager is None and not self._p2p_starting:
            self._start_p2p_if_enabled()

        # Auto-trigger Last.fm auth if pending from wizard
        self._check_lastfm_pending_auth()

    def _ensure_node_identity(self):
        """Generate node identity on first run (non-blocking)."""
        def _gen():
            try:
                from desktop.node_identity import has_identity, generate_identity
                if not has_identity():
                    generate_identity()
                    logger.info("Node identity generated")
            except Exception as e:
                logger.debug(f"Node identity generation skipped: {e}")

        threading.Thread(target=_gen, daemon=True).start()

    @staticmethod
    def _ensure_p2p_deps(progress_cb=None):
        """Install P2P dependencies if missing."""
        missing = []
        try:
            import aiohttp  # noqa: F401
        except ImportError:
            missing.append("aiohttp>=3.9.1")

        need_libtorrent = False
        try:
            import libtorrent  # noqa: F401
        except ImportError:
            missing.append("libtorrent>=2.0.0")
            need_libtorrent = True

        try:
            import miniupnpc  # noqa: F401
        except ImportError:
            missing.append("miniupnpc")

        if missing:
            if progress_cb:
                progress_cb(f"Installing P2P deps: {', '.join(missing)}...")
            logger.info(f"Installing P2P deps: {missing}")

            cmd = [sys.executable, "-m", "pip", "install", "--quiet"] + missing
            kwargs = {"capture_output": True, "text": True, "timeout": 300}
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

            result = subprocess.run(cmd, **kwargs)
            if result.returncode != 0:
                logger.warning(
                    f"P2P deps install failed: {result.stderr[:200]}")
                if progress_cb:
                    progress_cb("P2P deps install failed (non-critical)")
                return False

            logger.info("P2P dependencies installed")

        # On Windows, libtorrent needs OpenSSL 1.1 DLLs that aren't
        # bundled in the pip wheel.
        if sys.platform == "win32" and need_libtorrent:
            LauncherApp._fix_libtorrent_openssl(progress_cb)

        return True

    @staticmethod
    def _fix_libtorrent_openssl(progress_cb=None):
        """Copy OpenSSL 1.1 DLLs into the libtorrent package on Windows.

        The libtorrent pip wheel links against libssl-1_1-x64.dll and
        libcrypto-1_1-x64.dll but doesn't include them.
        """
        try:
            import importlib.util
            spec = importlib.util.find_spec("libtorrent")
            if not spec or not spec.origin:
                return
            lt_dir = Path(spec.origin).parent

            needed = ["libssl-1_1-x64.dll", "libcrypto-1_1-x64.dll"]
            if all((lt_dir / dll).exists() for dll in needed):
                return  # already fixed

            if progress_cb:
                progress_cb("Fixing libtorrent OpenSSL DLLs...")
            logger.info("libtorrent needs OpenSSL 1.1 DLLs")

            # Strategy 1: search system for existing DLLs
            import glob
            search_paths = [
                r"C:\Program Files\**",
                r"C:\Program Files (x86)\**",
                r"C:\Windows\System32",
            ]
            for dll_name in needed:
                if (lt_dir / dll_name).exists():
                    continue
                found = False
                for pattern in search_paths:
                    matches = glob.glob(
                        f"{pattern}\\{dll_name}", recursive=True
                    )
                    if matches:
                        import shutil
                        shutil.copy2(matches[0], lt_dir / dll_name)
                        logger.info(f"Copied {dll_name} from {matches[0]}")
                        found = True
                        break
                if not found:
                    # Strategy 2: download from Python embedded package
                    logger.warning(
                        f"{dll_name} not found on system. "
                        "libtorrent may not work (DHT disabled)."
                    )

            # Verify it works
            try:
                import libtorrent  # noqa: F811
                logger.info(f"libtorrent {libtorrent.version} loaded OK")
            except ImportError as e:
                logger.warning(f"libtorrent still can't load: {e}")

        except Exception as e:
            logger.warning(f"OpenSSL DLL fix failed: {e}")

    @staticmethod
    def _reload_p2p_modules():
        """Reload P2P modules after git pull so new code takes effect.

        The backend runs as a subprocess (gets new code on restart),
        but P2P modules run in the launcher process — Python's import
        cache keeps the old code in memory after git pull.
        """
        import importlib
        module_names = [
            "desktop.p2p.sync_queries",
            "desktop.p2p.chat_service",
            "desktop.p2p.lan_discovery",
            "desktop.p2p.dht_service",
            "desktop.p2p.upnp_service",
            "desktop.p2p.sync_server",
            "desktop.p2p.p2p_manager",
            "desktop.node_identity",
            "desktop.sync_client",
        ]
        for name in module_names:
            if name in sys.modules:
                try:
                    importlib.reload(sys.modules[name])
                except Exception as e:
                    logger.debug(f"Reload {name}: {e}")

    def _start_p2p_if_enabled(self):
        """Start P2P services (DHT always starts for peer discovery).

        When p2p.enabled=True, the node also announces its artists so
        other peers can discover and sync from it.
        When p2p.enabled=False, DHT still starts in discovery-only mode
        so "Sync Library" can find peers automatically.
        """
        if self._p2p_starting or self.p2p_manager is not None:
            return
        self._p2p_starting = True

        def _start():
            try:
                def progress(msg):
                    self.after(
                        0,
                        lambda: self._progress_text.configure(text=msg),
                    )

                # Auto-install P2P deps if missing
                self._ensure_p2p_deps(progress_cb=progress)

                from desktop.p2p.p2p_manager import P2PManager
                from desktop.node_identity import get_node_id
                from desktop.p2p.lan_discovery import LAN_DISCOVERY_PORT

                db_dsn = self._get_local_db_dsn()
                node_id = get_node_id() or ""

                p2p_cfg = self.config.get("p2p", {})
                sync_port = p2p_cfg.get("listen_port", 20000)

                # Open firewall for P2P ports (Windows)
                self.service_manager._ensure_firewall_rule(
                    LAN_DISCOVERY_PORT, "UDP")
                self.service_manager._ensure_firewall_rule(
                    sync_port, "TCP")

                self.p2p_manager = P2PManager(db_dsn, self.config)
                self.p2p_manager.start(
                    node_id=node_id, progress_cb=progress,
                )
                # Enable Sync button now that P2P is ready
                self.after(0, lambda: self._btn_sync.configure(
                    state="normal"))
            except Exception as e:
                logger.error(f"P2P startup failed: {e}", exc_info=True)
                # Enable Sync anyway — it will show a clear error
                self.after(0, lambda: self._btn_sync.configure(
                    state="normal"))
            finally:
                self._p2p_starting = False

        threading.Thread(target=_start, daemon=True).start()

    def _check_lastfm_pending_auth(self):
        """If user enabled Last.fm in wizard, trigger authorization."""
        lastfm = self.config.get("lastfm", {})
        if not lastfm.get("pending_auth") or not lastfm.get("username"):
            return
        if lastfm.get("session_key"):
            return  # Already authorized

        # Clear pending flag
        self.config["lastfm"]["pending_auth"] = False
        save_config(self.config)

        def _auth():
            try:
                result = self.api_client.lastfm_auth_start()
                if not result or not result.get("auth_url"):
                    logger.warning("Last.fm auth start failed")
                    return

                webbrowser.open(result["auth_url"])

                # Show dialog asking user to complete auth
                self.after(0, lambda: self._show_lastfm_auth_dialog())
            except Exception as e:
                logger.warning(f"Last.fm auth failed: {e}")

        threading.Thread(target=_auth, daemon=True).start()

    def _show_lastfm_auth_dialog(self):
        """Show dialog to complete Last.fm authorization after browser step."""
        dialog = ctk.CTkToplevel(self)
        dialog.title("Last.fm Authorization")
        dialog.geometry("420x200")
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()

        ctk.CTkLabel(
            dialog, text="Last.fm Authorization",
            font=ctk.CTkFont(size=18, weight="bold"),
        ).pack(pady=(20, 10))

        ctk.CTkLabel(
            dialog,
            text="A browser window has opened.\nAllow access on Last.fm, then click 'Complete'.",
            justify="center",
        ).pack(pady=5)

        self._lastfm_dialog_msg = ctk.CTkLabel(
            dialog, text="", text_color="gray",
        )
        self._lastfm_dialog_msg.pack(pady=5)

        btn_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_frame.pack(pady=10)

        ctk.CTkButton(
            btn_frame, text="Complete", width=120,
            command=lambda: self._complete_lastfm_auth(dialog),
        ).pack(side="left", padx=5)

        ctk.CTkButton(
            btn_frame, text="Skip", width=100,
            fg_color="transparent", border_width=1,
            command=dialog.destroy,
        ).pack(side="left", padx=5)

    def _complete_lastfm_auth(self, dialog):
        """Complete Last.fm auth after user allowed in browser."""
        self._lastfm_dialog_msg.configure(text="Checking...", text_color="gray")

        def _complete():
            result = self.api_client.lastfm_auth_complete()
            if result and result.get("success"):
                session_key = result["session_key"]
                self.config.setdefault("lastfm", {})["session_key"] = session_key
                save_config(self.config)
                self.after(0, lambda: self._lastfm_dialog_msg.configure(
                    text="Authorized successfully!", text_color="#22c55e"))
                self.after(1500, dialog.destroy)
            else:
                detail = ""
                if result and result.get("detail"):
                    detail = f"\n{result['detail']}"
                self.after(0, lambda: self._lastfm_dialog_msg.configure(
                    text=f"Authorization failed.{detail}\nYou can try again in Settings.",
                    text_color="#ef4444"))

        threading.Thread(target=_complete, daemon=True).start()

    def _fetch_and_display_stats(self, schedule_next: bool = True):
        """Fetch library stats from backend and update UI labels."""
        # Cancel any pending refresh to avoid stacking timers
        if self._stats_timer is not None:
            self.after_cancel(self._stats_timer)
            self._stats_timer = None

        def _fetch():
            data = self.api_client.get_stats()
            if data:
                self.after(0, lambda: self._update_stats_labels(data))
            # Schedule next refresh in 60 seconds (only if not called from polling)
            if schedule_next:
                self._stats_timer = self.after(60_000, self._fetch_and_display_stats)

        threading.Thread(target=_fetch, daemon=True).start()

    def _update_stats_labels(self, data: dict):
        """Update stats labels from API response."""
        total_tracks = int(data.get("total_tracks", 0))

        # Library stats (simple counts)
        simple = {
            "tracks": ("Tracks", "total_tracks"),
            "artists": ("Artists", "total_artists"),
            "albums": ("Albums", "total_albums"),
            "genres": ("Genres", "unique_genres"),
        }
        for key, (label, api_key) in simple.items():
            value = data.get(api_key, 0)
            if isinstance(value, (int, float)):
                value = f"{int(value):,}"
            self._stat_labels[key].configure(text=f"{label}: {value}")

        # Enrichment stats (coverage format: done / total)
        embeddings = int(data.get("tracks_with_embeddings", 0))
        features = int(data.get("tracks_with_features", 0))
        lib_artists = int(data.get("library_artists", 0))
        lastfm_artists = int(data.get("artists_with_lastfm", 0))
        lib_albums = int(data.get("library_albums", 0))
        lastfm_albums = int(data.get("albums_with_lastfm", 0))
        lyrics = int(data.get("tracks_with_lyrics", 0))

        lastfm_total = lib_artists + lib_albums
        lastfm_done = lastfm_artists + lastfm_albums

        enrichment = {
            "embeddings": ("Embeddings", embeddings, total_tracks),
            "features": ("Features", features, total_tracks),
            "lastfm": ("Last.fm", lastfm_done, lastfm_total),
            "lyrics": ("Lyrics", lyrics, total_tracks),
        }
        for key, (label, done, total) in enrichment.items():
            if total > 0:
                pct = done * 100 // total
                txt = f"{label}: {done:,} / {total:,}  ({pct}%)"
                color = "#22c55e" if pct == 100 else "#f59e0b" if pct >= 80 else "gray"
            else:
                txt = f"{label}: —"
                color = "gray"
            self._stat_labels[key].configure(text=txt, text_color=color)

    def _set_status(self, state: str, text: str):
        """Update status indicator."""
        colors = {
            "running": "#22c55e",
            "starting": "#f59e0b",
            "error": "#ef4444",
            "updating": "#3b82f6",
        }
        self._status_dot.configure(fg_color=colors.get(state, "gray"))
        self._status_text.configure(text=text)

    def _open_web_ui(self):
        port = self.config.get("ports", {}).get("web", 8000)
        webbrowser.open(f"http://localhost:{port}")

    def _scan_library(self):
        """Open folder picker and scan selected folder."""
        music_path = self.config.get("music_path", "")
        selected = filedialog.askdirectory(
            title="Select Folder to Scan",
            initialdir=music_path,
        )
        if not selected:
            return

        # Normalize paths for comparison
        selected_p = Path(selected).resolve()
        music_p = Path(music_path).resolve() if music_path else None

        subpath = None
        need_restart = False

        if music_p and selected_p == music_p:
            subpath = None
        elif music_p and self._is_subpath(selected_p, music_p):
            subpath = str(selected_p.relative_to(music_p))
        else:
            self.config = update_config({"music_path": str(selected_p)})
            self.service_manager.config = self.config
            need_restart = True

        self._btn_scan.configure(text="Cancel Scan",
                                 command=self._cancel_scan,
                                 fg_color="#8B0000", hover_color="#A52A2A")
        self._btn_enrich.configure(state="disabled")
        self._btn_sync.configure(state="disabled")
        self._progress_text.configure(text="Starting scan...")

        def _do_start():
            if need_restart:
                def progress(msg):
                    self.after(0, lambda: self._progress_text.configure(text=msg))

                msg = "Setting up music library..." if not music_p else "Restarting with new music path..."
                self._set_status_safe("starting", msg)
                ok = self.service_manager.restart_backend_and_tracker(progress_cb=progress)
                if not ok:
                    self.after(0, lambda: self._progress_text.configure(
                        text="Failed to restart backend"))
                    self.after(0, self._scan_done)
                    return
                port = self.config.get("ports", {}).get("web", 8000)
                self.api_client.set_port(port)
                self.after(0, lambda: self._on_services_ready())

            result = self.api_client.start_scan(subpath)
            if result and result.get("success"):
                self.after(500, self._poll_scan)
            else:
                self.after(0, lambda: self._progress_text.configure(
                    text="Failed to start scan"))
                self.after(0, self._scan_done)

        threading.Thread(target=_do_start, daemon=True).start()

    def _poll_scan(self):
        """Poll scan status every 1 second."""
        def _check():
            status = self.api_client.scan_status()
            if not status:
                self.after(0, self._scan_done)
                return

            progress = status.get("progress", "")
            running = status.get("running", False)

            self.after(0, lambda: self._progress_text.configure(text=progress))

            # Update live stats from scan progress
            scan_stats = status.get("stats")
            if scan_stats:
                self.after(0, lambda: self._update_scan_live_stats(scan_stats))

            if running:
                self.after(1000, self._poll_scan)
            else:
                self.after(0, self._scan_done)

        threading.Thread(target=_check, daemon=True).start()

    def _update_scan_live_stats(self, scan_stats: dict):
        """Update Library stats labels with live scan data during scanning."""
        # Refresh full stats from backend (without scheduling next timer)
        self._fetch_and_display_stats(schedule_next=False)

    def _cancel_scan(self):
        """Request cancellation of running scan."""
        self.api_client.scan_cancel()
        self._progress_text.configure(text="Cancelling...")
        self._btn_scan.configure(state="disabled")

    def _scan_done(self):
        """Restore UI after scan completes."""
        self._btn_scan.configure(
            text="Scan Library", command=self._scan_library,
            state="normal", fg_color="transparent",
            hover_color=("gray75", "gray25"),
        )
        self._btn_enrich.configure(state="normal")
        self._btn_sync.configure(state="normal")
        self._fetch_and_display_stats()

    def _enrich_library(self):
        """Start background enrichment and poll for progress."""
        result = self.api_client.enrich_start()
        if not result or not result.get("success"):
            detail = result.get("detail", "no response") if result else "no response"
            self._progress_text.configure(text=f"Enrich failed: {detail[:80]}")
            return

        self._btn_enrich.configure(text="Cancel Enrichment",
                                   command=self._cancel_enrich,
                                   fg_color="#8B0000", hover_color="#A52A2A")
        self._btn_scan.configure(state="disabled")
        self._progress_text.configure(text="Starting enrichment...")
        self._poll_enrich()

    def _poll_enrich(self):
        """Poll enrichment status every 2 seconds."""
        def _check():
            status = self.api_client.enrich_status()
            if not status:
                self.after(0, self._enrich_done)
                return

            progress = status.get("progress", "")
            running = status.get("running", False)

            self.after(0, lambda: self._progress_text.configure(text=progress))
            self.after(0, self._fetch_and_display_stats)

            if running:
                self.after(1000, self._poll_enrich)
            else:
                self.after(0, self._enrich_done)

        threading.Thread(target=_check, daemon=True).start()

    def _cancel_enrich(self):
        """Request cancellation of running enrichment."""
        self.api_client.enrich_cancel()
        self._progress_text.configure(text="Cancelling...")
        self._btn_enrich.configure(state="disabled")

    def _enrich_done(self):
        """Restore UI after enrichment completes."""
        self._btn_enrich.configure(
            text="Enrich Library", command=self._enrich_library,
            state="normal", fg_color="transparent",
            hover_color=("gray75", "gray25"),
        )
        self._btn_scan.configure(state="normal")
        self._fetch_and_display_stats()

    def _get_local_db_dsn(self) -> str:
        """Build DSN for the launcher's local PostgreSQL."""
        ports = self.config.get("ports", {})
        pw = self.config.get("postgres_password", "changeme")
        port = ports.get("postgres", 15432)
        return f"postgresql://sautium:{pw}@localhost:{port}/sautium"

    def _sync_library(self):
        """Sync enrichment data purely via P2P (LAN discovery + DHT)."""
        self._btn_sync.configure(state="disabled", text="Syncing...")
        self._btn_scan.configure(state="disabled")
        self._btn_enrich.configure(state="disabled")

        if not self.p2p_manager or not self.p2p_manager.is_running:
            self._progress_text.configure(
                text="P2P not running — restart the app")
            self._sync_done()
            return

        self._progress_text.configure(text="Syncing: searching peers...")

        def _do_sync():
            # Normalize local artists before sync
            self.after(0, lambda: self._progress_text.configure(
                text="Normalizing artists..."))
            norm_result = self.api_client.normalize_artists(pass2=True)
            if norm_result:
                p1 = norm_result.get("statistics", {}).get("pass1", {})
                if p1.get("split", 0) > 0:
                    logger.info(f"Pre-sync normalization: {p1}")

            def progress(msg):
                self.after(
                    0, lambda: self._progress_text.configure(text=msg)
                )

            try:
                stats = self.p2p_manager.sync_from_peers(
                    progress_cb=progress
                )
            except Exception as e:
                logger.error(f"P2P sync failed: {e}", exc_info=True)
                self.after(0, lambda: self._progress_text.configure(
                    text=f"Sync failed: {str(e)[:100]}"))
                self.after(0, self._sync_done)
                return

            total = sum(
                v for v in stats.values() if isinstance(v, int)
            )

            if total == 0:
                self.after(0, lambda: self._progress_text.configure(
                    text="No peers found with enrichment data"))
            else:
                msg = f"Sync complete — {total} items imported"
                details = ", ".join(
                    f"{k}: {v}" for k, v in stats.items()
                    if isinstance(v, int) and v > 0
                )
                if details:
                    msg += f" ({details})"
                self.after(0, lambda: self._progress_text.configure(text=msg))

            self.after(0, self._sync_done)

        threading.Thread(target=_do_sync, daemon=True).start()

    def _sync_done(self):
        """Restore UI after sync completes."""
        self._btn_sync.configure(state="normal", text="Sync Library")
        self._btn_scan.configure(state="normal")
        self._btn_enrich.configure(state="normal")
        self._fetch_and_display_stats()

    @staticmethod
    def _is_subpath(child: Path, parent: Path) -> bool:
        """Check if child is inside parent directory."""
        try:
            child.relative_to(parent)
            return True
        except ValueError:
            return False

    def _set_status_safe(self, state: str, text: str):
        """Thread-safe version of _set_status."""
        self.after(0, lambda: self._set_status(state, text))

    def _open_settings(self):
        from desktop.settings import SettingsDialog
        SettingsDialog(self, self.config, on_save=self._on_settings_saved,
                       api_client=self.api_client)

    def _on_settings_saved(self, new_config):
        self.config = new_config
        self.service_manager.config = new_config
        port = new_config.get("ports", {}).get("web", 8000)
        self.api_client.set_port(port)
        self._set_status("starting", "Applying settings...")

        def _restart():
            def progress(msg):
                self.after(0, lambda: self._progress_text.configure(text=msg))

            self.service_manager.restart_backend_and_tracker(progress_cb=progress)
            self.after(0, self._on_services_ready)

        threading.Thread(target=_restart, daemon=True).start()

    def _check_updates(self):
        """Manual update check."""
        self._btn_update.configure(state="disabled", text="Checking...")

        def _check():
            from desktop.updater import check_for_updates, perform_update

            has_updates, count, old_hash = check_for_updates()

            if not has_updates:
                self.after(0, lambda: self._show_update_result(False, 0))
            else:
                self.after(0, lambda: self._show_update_dialog(count, old_hash))

        threading.Thread(target=_check, daemon=True).start()

    def _check_updates_background(self):
        """Background update check at startup."""
        def _check():
            try:
                from desktop.updater import check_for_updates
                has_updates, count, _ = check_for_updates()
                if has_updates:
                    self.after(0, lambda: self._btn_update.configure(
                        text=f"Update Available ({count} commits)",
                        fg_color="#3b82f6",
                    ))
            except Exception as e:
                logger.debug(f"Background update check failed: {e}")

        self._update_thread = threading.Thread(target=_check, daemon=True)
        self._update_thread.start()

    def _show_update_result(self, has_updates: bool, count: int):
        self._btn_update.configure(state="normal", text="Check for Updates",
                                   fg_color="transparent")
        if not has_updates:
            self._progress_text.configure(text="You're up to date!")
            self.after(3000, lambda: self._progress_text.configure(text=""))

    def _show_update_dialog(self, count: int, old_hash: str):
        """Show update confirmation dialog."""
        self._btn_update.configure(state="normal", text="Check for Updates",
                                   fg_color="transparent")

        dialog = ctk.CTkToplevel(self)
        dialog.title("Update Available")
        dialog.geometry("400x200")
        dialog.transient(self)
        dialog.grab_set()

        ctk.CTkLabel(
            dialog,
            text=f"Update available: {count} new commit(s)",
            font=ctk.CTkFont(size=16, weight="bold"),
        ).pack(pady=(20, 10))

        ctk.CTkLabel(
            dialog,
            text="Services will be briefly restarted during the update.",
            text_color="gray",
        ).pack(pady=5)

        btn_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_frame.pack(pady=20)

        def _do_update():
            dialog.destroy()
            self._perform_update()

        ctk.CTkButton(
            btn_frame, text="Update Now", width=120,
            command=_do_update,
        ).pack(side="left", padx=10)

        ctk.CTkButton(
            btn_frame, text="Later", width=120,
            command=dialog.destroy,
            fg_color="transparent", border_width=1,
        ).pack(side="left", padx=10)

    def _perform_update(self):
        """Execute the update."""
        self._set_status("updating", "Updating...")

        def _update():
            from desktop.updater import perform_update

            def progress(msg):
                self.after(0, lambda: self._progress_text.configure(text=msg))

            success, changelog = perform_update(
                self.service_manager, self.config, progress_cb=progress,
                p2p_manager=self.p2p_manager,
            )
            self.p2p_manager = None

            if success:
                # Reload P2P modules so new code takes effect
                # (backend is a separate process, but P2P runs in-process)
                self._reload_p2p_modules()
                self.after(0, lambda: self._show_changelog(changelog))
                self.after(0, self._on_services_ready)
            else:
                self.after(0, lambda: self._set_status("error", "Update failed"))

        threading.Thread(target=_update, daemon=True).start()

    def _show_changelog(self, changelog: list):
        """Show changelog after update."""
        if not changelog:
            return

        dialog = ctk.CTkToplevel(self)
        dialog.title("Update Complete")
        dialog.geometry("500x350")
        dialog.transient(self)

        ctk.CTkLabel(
            dialog, text="Update Complete!",
            font=ctk.CTkFont(size=16, weight="bold"),
        ).pack(pady=(15, 5))

        ctk.CTkLabel(dialog, text="Changes:").pack(anchor="w", padx=20)

        textbox = ctk.CTkTextbox(dialog, width=450, height=180)
        textbox.pack(padx=20, pady=5)
        textbox.insert("1.0", "\n".join(changelog))
        textbox.configure(state="disabled")

        ctk.CTkButton(
            dialog, text="OK", width=100,
            command=dialog.destroy,
        ).pack(pady=10)

    def _minimize_to_tray(self):
        """Minimize to system tray instead of closing."""
        self.withdraw()
        if self.tray is None:
            from desktop.tray import create_tray
            self.tray = create_tray(
                on_show=self._show_from_tray,
                on_open_ui=self._open_web_ui,
                on_check_updates=self._check_updates,
                on_quit=self._quit,
            )

    def _show_from_tray(self):
        """Restore window from tray."""
        self.deiconify()
        self.lift()
        self.focus_force()

    def _quit(self):
        """Full quit: stop services and exit."""
        self._set_status("starting", "Shutting down...")
        self._progress_text.configure(text="Stopping services...")
        self.update()

        def _shutdown():
            if self.p2p_manager:
                try:
                    self.p2p_manager.stop()
                except Exception as e:
                    logger.debug(f"P2P stop error: {e}")
            self.service_manager.stop_all()
            self.after(0, self._final_quit)

        threading.Thread(target=_shutdown, daemon=True).start()

    def _final_quit(self):
        # Cancel periodic stats timer
        if self._stats_timer:
            try:
                self.after_cancel(self._stats_timer)
            except Exception:
                pass
            self._stats_timer = None
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        # Cancel ALL pending Tcl after-events to avoid
        # "invalid command name" errors from CustomTkinter internals
        try:
            self.tk.eval('foreach id [after info] {after cancel $id}')
        except Exception:
            pass
        self.destroy()


def main():
    """Entry point for the launcher."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    app = LauncherApp()
    app.mainloop()


if __name__ == "__main__":
    main()
