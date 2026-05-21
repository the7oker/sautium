"""
Settings dialog for Sautium.

A tabbed CTkToplevel dialog for modifying application settings.
"""

import logging
import sys
import threading
import time
import webbrowser
from typing import Callable, Optional

import customtkinter as ctk

from desktop.api_client import BackendAPIClient
from desktop.config_manager import load_config, save_config
from desktop.utils import (
    claude_authenticated,
    detect_claude_cli,
    detect_node_version,
    get_claude_executable,
    install_claude_runtime,
    launch_claude_setup,
)

logger = logging.getLogger(__name__)


class SettingsDialog(ctk.CTkToplevel):
    """Settings dialog with tabs: General, AI Provider, HQPlayer, Last.fm."""

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
        # State for the Claude Code install/sign-in machine (mirrors
        # the wizard's). Kept on `self` so the worker callbacks and
        # poll timer can reach it.
        self._claude_install_thread: Optional[threading.Thread] = None
        self._claude_poll_after_id = None
        self._claude_poll_deadline = 0.0
        self._claude_state_frame: Optional[ctk.CTkFrame] = None

        ctk.CTkLabel(tab, text="Default Provider",
                      font=ctk.CTkFont(weight="bold")).pack(anchor="w", pady=(5, 3))

        providers_frame = ctk.CTkFrame(tab, fg_color="transparent")
        providers_frame.pack(fill="x", padx=10)

        # Always offer Claude Code — the state UI below handles install
        # and sign-in. Showing it conditionally on detect_claude_cli()
        # was wrong because it hid the option exactly when the user
        # needed help installing it.
        ctk.CTkRadioButton(
            providers_frame, text="Claude Code (subscription)",
            variable=self._provider_var, value="claude_code",
            command=self._on_provider_change,
        ).pack(anchor="w", pady=2)

        ctk.CTkRadioButton(
            providers_frame, text="Anthropic API",
            variable=self._provider_var, value="anthropic",
            command=self._on_provider_change,
        ).pack(anchor="w", pady=2)

        ctk.CTkRadioButton(
            providers_frame, text="OpenAI API",
            variable=self._provider_var, value="openai",
            command=self._on_provider_change,
        ).pack(anchor="w", pady=2)

        ctk.CTkRadioButton(
            providers_frame, text="OpenAI-compatible",
            variable=self._provider_var, value="openai_compat",
            command=self._on_provider_change,
        ).pack(anchor="w", pady=2)

        # Claude Code state machine — shown when claude_code is selected.
        self._claude_state_frame = ctk.CTkFrame(tab, fg_color="transparent")
        self._claude_state_frame.pack(fill="x", padx=10, pady=(8, 0))
        self._refresh_claude_state_ui()

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

    # ----------------------------------------------------------------
    # Claude Code state machine — mirror of wizard._render_claude_state_ui
    # ----------------------------------------------------------------

    def _claude_state(self) -> str:
        """'node_missing' | 'claude_missing' | 'not_authed' | 'ready'."""
        node_ver = detect_node_version()
        if node_ver is None or node_ver[0] < 18:
            return "node_missing"
        if get_claude_executable() is None:
            return "claude_missing"
        if not claude_authenticated():
            return "not_authed"
        return "ready"

    def _on_provider_change(self):
        self._refresh_claude_state_ui()

    def _refresh_claude_state_ui(self):
        """Redraw the state-machine sub-panel. Empty when the user
        hasn't picked claude_code."""
        if self._claude_state_frame is None:
            return
        for widget in self._claude_state_frame.winfo_children():
            widget.destroy()

        if self._provider_var.get() != "claude_code":
            return

        state = self._claude_state()

        if state == "ready":
            ctk.CTkLabel(
                self._claude_state_frame,
                text="✓ Claude Code is ready",
                text_color="#4CAF50",
                font=ctk.CTkFont(size=13, weight="bold"),
            ).pack(anchor="w")
            ctk.CTkLabel(
                self._claude_state_frame,
                text="Signed in via subscription. No API key needed.",
                text_color="gray", font=ctk.CTkFont(size=11),
            ).pack(anchor="w")
            return

        if state == "node_missing":
            ctk.CTkLabel(
                self._claude_state_frame,
                text="Node.js 18+ is required.",
                text_color="orange",
                font=ctk.CTkFont(size=12, weight="bold"),
            ).pack(anchor="w")
            ctk.CTkLabel(
                self._claude_state_frame,
                text=(
                    "macOS: brew install node\n"
                    "Windows: re-run the Sautium installer (Node is bundled),\n"
                    "then click Refresh."
                ),
                text_color="gray", font=ctk.CTkFont(size=11),
                justify="left",
            ).pack(anchor="w", pady=(2, 4))
            ctk.CTkButton(
                self._claude_state_frame, text="Refresh", width=100,
                command=self._refresh_claude_state_ui,
            ).pack(anchor="w")
            return

        if state == "claude_missing":
            ctk.CTkLabel(
                self._claude_state_frame,
                text="Claude Code is not installed yet.",
                font=ctk.CTkFont(size=12),
            ).pack(anchor="w")
            ctk.CTkLabel(
                self._claude_state_frame,
                text="Downloads ~5 MB via npm. Internet connection required.",
                text_color="gray", font=ctk.CTkFont(size=11),
            ).pack(anchor="w", pady=(2, 4))
            self._claude_install_status = ctk.CTkLabel(
                self._claude_state_frame, text="",
                text_color="gray", font=ctk.CTkFont(size=11),
                wraplength=420, justify="left",
            )
            self._claude_install_status.pack(anchor="w", pady=(0, 4))
            self._claude_install_btn = ctk.CTkButton(
                self._claude_state_frame, text="Install Claude Code",
                width=180, command=self._install_claude_clicked,
            )
            self._claude_install_btn.pack(anchor="w")
            return

        # state == "not_authed"
        ctk.CTkLabel(
            self._claude_state_frame,
            text="Claude Code installed.",
            font=ctk.CTkFont(size=12),
        ).pack(anchor="w")
        ctk.CTkLabel(
            self._claude_state_frame,
            text=(
                "Sautium will open a terminal running 'claude'. In it:\n"
                "  1. Pick a theme (first run)\n"
                "  2. Type /login\n"
                "  3. Choose 'Claude account with subscription'\n"
                "  4. Authorize in the browser tab"
            ),
            text_color="gray", font=ctk.CTkFont(size=11),
            justify="left",
        ).pack(anchor="w", pady=(2, 4))
        self._claude_signin_status = ctk.CTkLabel(
            self._claude_state_frame, text="",
            text_color="gray", font=ctk.CTkFont(size=11),
        )
        self._claude_signin_status.pack(anchor="w", pady=(0, 4))
        btns = ctk.CTkFrame(self._claude_state_frame, fg_color="transparent")
        btns.pack(anchor="w")
        ctk.CTkButton(
            btns, text="Sign in to Claude", width=160,
            command=self._signin_claude_clicked,
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            btns, text="Refresh", width=90,
            command=self._refresh_claude_state_ui,
            fg_color="transparent", border_width=1,
        ).pack(side="left")

    def _install_claude_clicked(self):
        if self._claude_install_thread and self._claude_install_thread.is_alive():
            return
        self._claude_install_btn.configure(state="disabled", text="Installing...")
        self._claude_install_status.configure(
            text="Running npm install (may take a minute)...",
            text_color="gray",
        )

        def _worker():
            ok, msg = install_claude_runtime()
            self.after(0, lambda: self._on_claude_install_done(ok, msg))

        self._claude_install_thread = threading.Thread(target=_worker, daemon=True)
        self._claude_install_thread.start()

    def _on_claude_install_done(self, ok: bool, msg: str):
        if ok:
            self._refresh_claude_state_ui()
        else:
            self._claude_install_btn.configure(state="normal", text="Install Claude Code")
            self._claude_install_status.configure(
                text=f"Install failed: {msg}", text_color="red",
            )

    def _signin_claude_clicked(self):
        try:
            launch_claude_setup()
        except Exception as e:
            self._claude_signin_status.configure(
                text=f"Could not launch claude: {e}", text_color="red",
            )
            return
        self._claude_signin_status.configure(
            text="Waiting for sign-in (poll every 2s for 5 min)...",
            text_color="gray",
        )
        self._claude_poll_deadline = time.monotonic() + 300
        self._poll_claude_auth()

    def _poll_claude_auth(self):
        if claude_authenticated():
            self._claude_poll_after_id = None
            self._refresh_claude_state_ui()
            return
        if time.monotonic() >= self._claude_poll_deadline:
            self._claude_poll_after_id = None
            self._claude_signin_status.configure(
                text="Sign-in not detected. Click Refresh after authorizing.",
                text_color="orange",
            )
            return
        self._claude_poll_after_id = self.after(2000, self._poll_claude_auth)

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

        ctk.CTkLabel(
            tab, text="Track your listening history on Last.fm.\nAPI keys are built into the app.",
            text_color="gray", font=ctk.CTkFont(size=11),
        ).pack(anchor="w", padx=10, pady=(0, 8))

        # Username
        self._lastfm_user_var = ctk.StringVar(value=lastfm.get("username") or "")
        user_row = ctk.CTkFrame(tab, fg_color="transparent")
        user_row.pack(fill="x", padx=10, pady=2)
        ctk.CTkLabel(user_row, text="Username:", width=100, anchor="w").pack(side="left")
        ctk.CTkEntry(user_row, textvariable=self._lastfm_user_var, width=350).pack(side="left")

        # Session key (hidden, managed by auth flow)
        self._lastfm_session_var = ctk.StringVar(value=lastfm.get("session_key") or "")

        # Auth status
        has_session = bool(lastfm.get("session_key"))
        status_text = "Authorized" if has_session else "Not authorized"
        status_color = "#22c55e" if has_session else "gray"

        auth_frame = ctk.CTkFrame(tab, fg_color="transparent")
        auth_frame.pack(fill="x", padx=10, pady=(10, 2))

        ctk.CTkLabel(auth_frame, text="Scrobbling:", width=100, anchor="w").pack(side="left")
        self._lastfm_status = ctk.CTkLabel(
            auth_frame, text=status_text, text_color=status_color,
            font=ctk.CTkFont(size=12),
        )
        self._lastfm_status.pack(side="left", padx=(0, 10))

        self._lastfm_auth_btn = ctk.CTkButton(
            auth_frame, text="Authorize Scrobbling", width=160,
            command=self._lastfm_authorize,
        )
        self._lastfm_auth_btn.pack(side="left")

        if has_session:
            self._lastfm_disconnect_btn = ctk.CTkButton(
                auth_frame, text="Disconnect", width=90,
                command=self._lastfm_disconnect,
                fg_color="transparent", border_width=1,
                text_color="#ef4444", border_color="#ef4444",
            )
            self._lastfm_disconnect_btn.pack(side="left", padx=(5, 0))

        # Auth message area
        self._lastfm_msg = ctk.CTkLabel(
            tab, text="", text_color="gray", font=ctk.CTkFont(size=11),
            wraplength=450,
        )
        self._lastfm_msg.pack(anchor="w", padx=10, pady=(5, 0))

    def _lastfm_authorize(self):
        """Start Last.fm authorization flow."""
        if not self.api_client:
            self._lastfm_msg.configure(text="Backend not available", text_color="#ef4444")
            return

        self._lastfm_auth_btn.configure(state="disabled", text="Opening browser...")
        self._lastfm_msg.configure(text="", text_color="gray")

        def _auth():
            # Step 1: Get auth URL
            result = self.api_client.lastfm_auth_start()
            if not result or not result.get("auth_url"):
                self.after(0, lambda: self._lastfm_msg.configure(
                    text="Failed to start authorization", text_color="#ef4444"))
                self.after(0, lambda: self._lastfm_auth_btn.configure(
                    state="normal", text="Authorize Scrobbling"))
                return

            auth_url = result["auth_url"]
            webbrowser.open(auth_url)

            self.after(0, lambda: self._lastfm_auth_btn.configure(
                text="Complete Authorization", state="normal",
                command=self._lastfm_complete_auth))
            self.after(0, lambda: self._lastfm_msg.configure(
                text="A browser window has opened. Authorize the app, then click 'Complete Authorization'.",
                text_color="#f59e0b"))

        threading.Thread(target=_auth, daemon=True).start()

    def _lastfm_complete_auth(self):
        """Complete the Last.fm authorization after user allowed in browser."""
        if not self.api_client:
            return

        self._lastfm_auth_btn.configure(state="disabled", text="Checking...")

        def _complete():
            result = self.api_client.lastfm_auth_complete()
            if result and result.get("success"):
                self._lastfm_session_var.set(result.get("session_key", ""))
                if result.get("username"):
                    self._lastfm_user_var.set(result["username"])
                msg_text = (
                    f"Authorized as {result['username']}! Click Save to apply."
                    if result.get("username")
                    else "Authorization successful! Click Save to apply."
                )
                self.after(0, lambda: self._lastfm_status.configure(
                    text="Authorized", text_color="#22c55e"))
                self.after(0, lambda: self._lastfm_msg.configure(
                    text=msg_text, text_color="#22c55e"))
                self.after(0, lambda: self._lastfm_auth_btn.configure(
                    state="normal", text="Authorize Scrobbling"))
            else:
                detail = ""
                if result and result.get("detail"):
                    detail = f" — {result['detail']}"
                self.after(0, lambda: self._lastfm_msg.configure(
                    text=f"Authorization failed{detail}", text_color="#ef4444"))
                self.after(0, lambda: self._lastfm_auth_btn.configure(
                    state="normal", text="Authorize Scrobbling"))

        threading.Thread(target=_complete, daemon=True).start()

    def _lastfm_disconnect(self):
        """Remove Last.fm session key."""
        self._lastfm_session_var.set("")
        self._lastfm_status.configure(text="Not authorized", text_color="gray")
        self._lastfm_msg.configure(
            text="Disconnected. Click Save to apply.", text_color="#f59e0b")

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

        # Last.fm (API key/secret are built into the app)
        self.config["lastfm"] = {
            "username": self._lastfm_user_var.get().strip() or None,
            "session_key": self._lastfm_session_var.get().strip() or None,
        }

        save_config(self.config)
        logger.info("Settings saved")

        if self.on_save:
            self.on_save(self.config)

        self.destroy()
