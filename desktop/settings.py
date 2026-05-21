"""
Settings dialog for Sautium.

A tabbed CTkToplevel dialog for modifying application settings.
"""

import logging
from typing import Callable, Optional

import customtkinter as ctk

from desktop.api_client import BackendAPIClient
from desktop.config_manager import load_config, save_config

logger = logging.getLogger(__name__)


class SettingsDialog(ctk.CTkToplevel):
    """Settings dialog for launcher-only options (ports). AI provider,
    HQPlayer connection and Last.fm scrobbling now live in the Web UI —
    keeping a single source of truth instead of two parallel configs."""

    def __init__(self, parent, config: dict, on_save: Optional[Callable] = None,
                 api_client: Optional[BackendAPIClient] = None):
        super().__init__(parent)

        self.title("Settings")
        self.geometry("550x500")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self.config = config.copy()
        self.on_save = on_save
        self.api_client = api_client

        # Single-tab tabview kept so the UI's vertical rhythm matches
        # the wizard. If more launcher-only sections appear later
        # (proxy, GPU/CPU mode override) they slot in here.
        self.tabview = ctk.CTkTabview(self, width=510, height=400)
        self.tabview.pack(padx=20, pady=(10, 0))

        self.tabview.add("General")
        self._build_general_tab()

        # Buttons
        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(fill="x", padx=20, pady=10)

        ctk.CTkButton(
            btn_frame, text="Save", width=100,
            command=self._save,
        ).pack(side="right", padx=5)

        ctk.CTkButton(
            btn_frame, text="Cancel", width=100,
            command=self.destroy,
            fg_color="transparent", border_width=1,
        ).pack(side="right", padx=5)

        self._restart_warning = ctk.CTkLabel(
            btn_frame, text="", text_color="#f59e0b",
            font=ctk.CTkFont(size=11),
        )
        self._restart_warning.pack(side="left")

    # ================================================================
    # General tab
    # ================================================================

    def _build_general_tab(self):
        tab = self.tabview.tab("General")
        ports = self.config.get("ports", {})

        ctk.CTkLabel(tab, text="Ports", font=ctk.CTkFont(weight="bold")).pack(
            anchor="w", pady=(5, 3)
        )

        port_frame = ctk.CTkFrame(tab, fg_color="transparent")
        port_frame.pack(fill="x", padx=10)

        self._pg_port_var = ctk.StringVar(value=str(ports.get("postgres", 5432)))
        self._web_port_var = ctk.StringVar(value=str(ports.get("web", 8000)))
        self._tracker_port_var = ctk.StringVar(value=str(ports.get("tracker", 8765)))

        for label, var in [
            ("PostgreSQL:", self._pg_port_var),
            ("Web Server:", self._web_port_var),
            ("Tracker:", self._tracker_port_var),
        ]:
            row = ctk.CTkFrame(port_frame, fg_color="transparent")
            row.pack(fill="x", pady=2)
            ctk.CTkLabel(row, text=label, width=100, anchor="w").pack(side="left")
            ctk.CTkEntry(row, textvariable=var, width=80).pack(side="left")

        ctk.CTkLabel(
            tab,
            text="Changing ports requires a restart.",
            text_color="gray", font=ctk.CTkFont(size=11),
        ).pack(anchor="w", padx=10, pady=5)

    # ================================================================
    # Save
    # ================================================================

    def _save(self):
        """Persist port changes and notify the parent launcher."""
        try:
            self.config["ports"] = {
                "postgres": int(self._pg_port_var.get()),
                "web": int(self._web_port_var.get()),
                "tracker": int(self._tracker_port_var.get()),
            }
        except ValueError:
            pass

        save_config(self.config)
        logger.info("Settings saved")

        if self.on_save:
            self.on_save(self.config)

        self.destroy()
