"""
Settings dialog for Music AI DJ.

A tabbed CTkToplevel dialog for modifying application settings.
"""

import logging
from typing import Callable, Optional

import customtkinter as ctk

from desktop.config_manager import load_config, save_config
from desktop.utils import detect_claude_cli

logger = logging.getLogger(__name__)


class SettingsDialog(ctk.CTkToplevel):
    """Settings dialog with tabs: General, AI Provider, HQPlayer, Last.fm."""

    def __init__(self, parent, config: dict, on_save: Optional[Callable] = None):
        super().__init__(parent)

        self.title("Settings")
        self.geometry("550x500")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self.config = config.copy()
        self.on_save = on_save

        # Tabview
        self.tabview = ctk.CTkTabview(self, width=510, height=400)
        self.tabview.pack(padx=20, pady=(10, 0))

        self.tabview.add("General")
        self.tabview.add("AI Provider")
        self.tabview.add("HQPlayer")
        self.tabview.add("Last.fm")

        self._build_general_tab()
        self._build_provider_tab()
        self._build_hqplayer_tab()
        self._build_lastfm_tab()

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
    # Provider tab
    # ================================================================

    def _build_provider_tab(self):
        tab = self.tabview.tab("AI Provider")

        self._provider_var = ctk.StringVar(
            value=self.config.get("provider", "anthropic")
        )

        ctk.CTkLabel(tab, text="Default Provider",
                      font=ctk.CTkFont(weight="bold")).pack(anchor="w", pady=(5, 3))

        providers_frame = ctk.CTkFrame(tab, fg_color="transparent")
        providers_frame.pack(fill="x", padx=10)

        if detect_claude_cli():
            ctk.CTkRadioButton(
                providers_frame, text="Claude Code (subscription)",
                variable=self._provider_var, value="claude_code",
            ).pack(anchor="w", pady=2)

        ctk.CTkRadioButton(
            providers_frame, text="Anthropic API",
            variable=self._provider_var, value="anthropic",
        ).pack(anchor="w", pady=2)

        ctk.CTkRadioButton(
            providers_frame, text="OpenAI API",
            variable=self._provider_var, value="openai",
        ).pack(anchor="w", pady=2)

        ctk.CTkRadioButton(
            providers_frame, text="OpenAI-compatible",
            variable=self._provider_var, value="openai_compat",
        ).pack(anchor="w", pady=2)

        # API keys
        ctk.CTkLabel(tab, text="API Keys",
                      font=ctk.CTkFont(weight="bold")).pack(anchor="w", pady=(10, 3))

        keys_frame = ctk.CTkFrame(tab, fg_color="transparent")
        keys_frame.pack(fill="x", padx=10)

        api_keys = self.config.get("api_keys", {})
        self._anthropic_key_var = ctk.StringVar(value=api_keys.get("anthropic") or "")
        self._openai_key_var = ctk.StringVar(value=api_keys.get("openai") or "")

        for label, var in [
            ("Anthropic:", self._anthropic_key_var),
            ("OpenAI:", self._openai_key_var),
        ]:
            row = ctk.CTkFrame(keys_frame, fg_color="transparent")
            row.pack(fill="x", pady=2)
            ctk.CTkLabel(row, text=label, width=80, anchor="w").pack(side="left")
            ctk.CTkEntry(row, textvariable=var, width=350, show="*").pack(side="left")

    # ================================================================
    # HQPlayer tab
    # ================================================================

    def _build_hqplayer_tab(self):
        tab = self.tabview.tab("HQPlayer")
        hqp = self.config.get("hqplayer", {})

        self._hqp_enabled_var = ctk.BooleanVar(value=hqp.get("enabled", True))
        self._hqp_host_var = ctk.StringVar(value=hqp.get("host", "localhost"))
        self._hqp_port_var = ctk.StringVar(value=str(hqp.get("port", 4321)))

        ctk.CTkCheckBox(
            tab, text="Enable HQPlayer integration",
            variable=self._hqp_enabled_var,
        ).pack(anchor="w", pady=(10, 5))

        fields_frame = ctk.CTkFrame(tab, fg_color="transparent")
        fields_frame.pack(fill="x", padx=10)

        for label, var in [
            ("Host:", self._hqp_host_var),
            ("Port:", self._hqp_port_var),
        ]:
            row = ctk.CTkFrame(fields_frame, fg_color="transparent")
            row.pack(fill="x", pady=2)
            ctk.CTkLabel(row, text=label, width=60, anchor="w").pack(side="left")
            ctk.CTkEntry(row, textvariable=var, width=200).pack(side="left")

    # ================================================================
    # Last.fm tab
    # ================================================================

    def _build_lastfm_tab(self):
        tab = self.tabview.tab("Last.fm")
        lastfm = self.config.get("lastfm", {})

        ctk.CTkLabel(tab, text="Last.fm Scrobbling",
                      font=ctk.CTkFont(weight="bold")).pack(anchor="w", pady=(5, 3))

        self._lastfm_key_var = ctk.StringVar(value=lastfm.get("api_key") or "")
        self._lastfm_secret_var = ctk.StringVar(value=lastfm.get("api_secret") or "")
        self._lastfm_user_var = ctk.StringVar(value=lastfm.get("username") or "")
        self._lastfm_session_var = ctk.StringVar(value=lastfm.get("session_key") or "")

        fields_frame = ctk.CTkFrame(tab, fg_color="transparent")
        fields_frame.pack(fill="x", padx=10)

        for label, var, show in [
            ("API Key:", self._lastfm_key_var, "*"),
            ("API Secret:", self._lastfm_secret_var, "*"),
            ("Username:", self._lastfm_user_var, ""),
            ("Session Key:", self._lastfm_session_var, "*"),
        ]:
            row = ctk.CTkFrame(fields_frame, fg_color="transparent")
            row.pack(fill="x", pady=2)
            ctk.CTkLabel(row, text=label, width=100, anchor="w").pack(side="left")
            entry = ctk.CTkEntry(row, textvariable=var, width=350)
            if show:
                entry.configure(show=show)
            entry.pack(side="left")

    # ================================================================
    # Save
    # ================================================================

    def _save(self):
        """Collect all settings and save."""
        # Ports
        try:
            self.config["ports"] = {
                "postgres": int(self._pg_port_var.get()),
                "web": int(self._web_port_var.get()),
                "tracker": int(self._tracker_port_var.get()),
            }
        except ValueError:
            pass

        # Provider
        self.config["provider"] = self._provider_var.get()
        self.config["api_keys"] = {
            "anthropic": self._anthropic_key_var.get().strip() or None,
            "openai": self._openai_key_var.get().strip() or None,
        }

        if self._provider_var.get() == "claude_code":
            self.config["claude_code_available"] = True

        # HQPlayer
        self.config["hqplayer"] = {
            "enabled": self._hqp_enabled_var.get(),
            "host": self._hqp_host_var.get().strip() or "localhost",
            "port": int(self._hqp_port_var.get()) if self._hqp_port_var.get().isdigit() else 4321,
        }

        # Last.fm
        self.config["lastfm"] = {
            "api_key": self._lastfm_key_var.get().strip() or None,
            "api_secret": self._lastfm_secret_var.get().strip() or None,
            "username": self._lastfm_user_var.get().strip() or None,
            "session_key": self._lastfm_session_var.get().strip() or None,
        }

        save_config(self.config)
        logger.info("Settings saved")

        if self.on_save:
            self.on_save(self.config)

        self.destroy()
