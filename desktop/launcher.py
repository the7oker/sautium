"""
Main launcher window for Music AI DJ.

Shows service status, QR code for mobile access, and controls.
Minimizes to system tray on close.
"""

import logging
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
    """Main Music AI DJ launcher window."""

    def __init__(self):
        super().__init__()

        self.title("Music AI DJ")
        self.geometry("480x700")
        self.resizable(False, False)

        self.config = load_config()
        self.service_manager = ServiceManager(self.config)
        self.api_client = BackendAPIClient()
        self.tray = None
        self._update_thread = None
        self._stats_timer = None

        # Check first run
        if not self.config.get("first_run_complete"):
            self.withdraw()
            self.after(100, self._run_wizard)
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
            self.deiconify()
            self._build_ui()
            self.after(100, self._startup_sequence)

        SetupWizard(self, on_complete=on_wizard_complete)

    def _build_ui(self):
        """Build the main launcher UI."""
        # Title
        ctk.CTkLabel(
            self, text="Music AI DJ",
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
            ("albums", "Albums"), ("embeddings", "Embeddings"),
        ]):
            lbl = ctk.CTkLabel(
                stats_grid, text=f"{label}: —",
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

        self._set_status("running", "All services running")
        self._url_label.configure(text=f"Local: {local_url}  |  LAN: {lan_url}")
        self._btn_open.configure(state="normal")
        self._btn_scan.configure(state="normal")
        self._progress_text.configure(text="")

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

    def _fetch_and_display_stats(self):
        """Fetch library stats from backend and update UI labels."""
        def _fetch():
            data = self.api_client.get_stats()
            if data:
                self.after(0, lambda: self._update_stats_labels(data))
            # Schedule next refresh in 60 seconds
            self._stats_timer = self.after(60_000, self._fetch_and_display_stats)

        threading.Thread(target=_fetch, daemon=True).start()

    def _update_stats_labels(self, data: dict):
        """Update stats labels from API response."""
        mapping = {
            "tracks": "total_tracks",
            "artists": "total_artists",
            "albums": "total_albums",
            "embeddings": "tracks_with_embeddings",
        }
        display = {
            "tracks": "Tracks",
            "artists": "Artists",
            "albums": "Albums",
            "embeddings": "Embeddings",
        }
        for key, api_key in mapping.items():
            value = data.get(api_key, "—")
            if isinstance(value, (int, float)):
                value = f"{int(value):,}"
            self._stat_labels[key].configure(text=f"{display[key]}: {value}")

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
            # Scan entire library
            subpath = None
        elif music_p and self._is_subpath(selected_p, music_p):
            # Scan subfolder
            subpath = str(selected_p.relative_to(music_p))
        else:
            # Selected folder is outside music_path — update config and restart
            self.config = update_config({"music_path": str(selected_p)})
            self.service_manager.config = self.config
            need_restart = True

        self._btn_scan.configure(state="disabled", text="Scanning...")
        self._progress_text.configure(text="Starting scan...")

        def _do_scan():
            if need_restart:
                def progress(msg):
                    self.after(0, lambda: self._progress_text.configure(text=msg))

                self._set_status_safe("starting", "Restarting with new music path...")
                ok = self.service_manager.restart_backend_and_tracker(progress_cb=progress)
                if not ok:
                    self.after(0, lambda: self._progress_text.configure(
                        text="Failed to restart backend"))
                    self.after(0, lambda: self._btn_scan.configure(
                        state="normal", text="Scan Library"))
                    return
                # Update API client port after restart
                port = self.config.get("ports", {}).get("web", 8000)
                self.api_client.set_port(port)
                self.after(0, lambda: self._on_services_ready())

            self.after(0, lambda: self._progress_text.configure(text="Scanning..."))
            result = self.api_client.start_scan(subpath)

            if result and result.get("success"):
                stats = result.get("statistics", {})
                added = stats.get("added", 0)
                skipped = stats.get("skipped", 0)
                errors = stats.get("errors", 0)
                msg = f"Scan complete — Added: {added}, Skipped: {skipped}"
                if errors:
                    msg += f", Errors: {errors}"
            elif result:
                msg = f"Scan failed — {result}"
            else:
                msg = "Scan failed — no response from backend"

            self.after(0, lambda: self._progress_text.configure(text=msg))
            self.after(0, lambda: self._btn_scan.configure(state="normal", text="Scan Library"))
            self.after(0, self._fetch_and_display_stats)

        threading.Thread(target=_do_scan, daemon=True).start()

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
        SettingsDialog(self, self.config, on_save=self._on_settings_saved)

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
        self._btn_update.configure(state="normal", text="Check for Updates")
        if not has_updates:
            self._progress_text.configure(text="You're up to date!")
            self.after(3000, lambda: self._progress_text.configure(text=""))

    def _show_update_dialog(self, count: int, old_hash: str):
        """Show update confirmation dialog."""
        self._btn_update.configure(state="normal", text="Check for Updates")

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
            )

            if success:
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
        dialog.geometry("500x300")
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
            self.service_manager.stop_all()
            self.after(0, self._final_quit)

        threading.Thread(target=_shutdown, daemon=True).start()

    def _final_quit(self):
        if self.tray:
            try:
                self.tray.stop()
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
