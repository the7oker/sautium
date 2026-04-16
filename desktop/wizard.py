"""
First-run setup wizard for Sautium.

A multi-step customtkinter wizard that collects:
1. Welcome / intro
2. Music library path
3. AI provider selection + API key
4. HQPlayer settings
5. Summary + initialization
"""

import logging
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from tkinter import filedialog
from typing import Optional

import customtkinter as ctk

from desktop.config_manager import load_config, save_config
from desktop.utils import detect_claude_cli, detect_gpu, detect_git

logger = logging.getLogger(__name__)


class SetupWizard(ctk.CTkToplevel):
    """First-run setup wizard."""

    def __init__(self, parent, on_complete=None):
        super().__init__(parent)

        self.title("Sautium - Setup")
        self.geometry("600x560")
        self.resizable(False, False)
        if sys.platform != "darwin":
            self.transient(parent)
        self.grab_set()
        self.lift()
        self.focus_force()

        self.on_complete = on_complete
        self.config = load_config()
        self.current_step = 0
        self.steps = [
            self._step_welcome,
            self._step_account,
            self._step_provider,
            self._step_hqplayer,
            self._step_lastfm,
            self._step_summary,
        ]

        # Detection results
        self._claude_available = detect_claude_cli()
        self._gpu_available, self._gpu_name, self._gpu_vram = detect_gpu()
        self._git_available = detect_git()

        # Main container
        self.container = ctk.CTkFrame(self, fg_color="transparent")
        self.container.pack(fill="both", expand=True, padx=20, pady=20)

        # Step content frame
        self.content_frame = ctk.CTkFrame(self.container, fg_color="transparent")
        self.content_frame.pack(fill="both", expand=True)

        # Navigation buttons
        self.nav_frame = ctk.CTkFrame(self.container, fg_color="transparent")
        self.nav_frame.pack(fill="x", pady=(10, 0))

        self.btn_back = ctk.CTkButton(
            self.nav_frame, text="Back", width=100,
            command=self._go_back, state="disabled",
        )
        self.btn_back.pack(side="left")

        self.btn_next = ctk.CTkButton(
            self.nav_frame, text="Next", width=100,
            command=self._go_next,
        )
        self.btn_next.pack(side="right")

        # Step indicator
        self.step_label = ctk.CTkLabel(
            self.nav_frame, text="", text_color="gray",
        )
        self.step_label.pack(side="right", padx=20)

        # Show first step
        self._show_step()

        # Handle close
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _clear_content(self):
        for widget in self.content_frame.winfo_children():
            widget.destroy()

    def _show_step(self):
        self._clear_content()
        self.steps[self.current_step]()
        self.step_label.configure(
            text=f"Step {self.current_step + 1} of {len(self.steps)}"
        )
        self.btn_back.configure(
            state="normal" if self.current_step > 0 else "disabled"
        )

        is_last = self.current_step == len(self.steps) - 1
        self.btn_next.configure(text="Start" if is_last else "Next")

    def _go_back(self):
        if self.current_step > 0:
            self.current_step -= 1
            self._show_step()

    def _go_next(self):
        # Validate current step
        if not self._validate_step():
            return

        if self.current_step < len(self.steps) - 1:
            self.current_step += 1
            self._show_step()
        else:
            self._finish()

    def _validate_step(self) -> bool:
        if self.current_step == 1:  # Account
            # Phase 2: verify email code
            if getattr(self, "_account_verify_phase", False):
                code = self._verify_code_var.get().strip().upper()
                if not code:
                    self._account_error.configure(text="Enter the code from email")
                    return False
                if code != self._account_verify_code:
                    self._account_error.configure(text="Invalid code")
                    return False
                # Verified!
                self.config["_account"]["email"] = self._account_email_val
                self.config["_account"]["email_verified"] = True
                self._account_verify_phase = False
                return True

            # Phase 1: account fields
            username = self._account_user_var.get().strip()
            password = self._account_pass_var.get()
            password2 = self._account_pass2_var.get()
            email = self._account_email_var.get().strip()

            # Persist values for re-render
            self._account_user_val = username
            self._account_pass_val = password
            self._account_pass2_val = password2
            self._account_email_val = email

            if not username:
                # Skip account — will use random identity
                self.config["_account"] = None
                return True

            if len(username) < 3:
                self._account_error.configure(
                    text="Username must be at least 3 characters"
                )
                return False

            if not password:
                self._account_error.configure(text="Password is required")
                return False

            if len(password) < 8:
                self._account_error.configure(
                    text="Password must be at least 8 characters"
                )
                return False

            if password != password2:
                self._account_error.configure(text="Passwords don't match")
                return False

            if email and "@" not in email:
                self._account_error.configure(text="Invalid email address")
                return False

            self.config["_account"] = {
                "username": username,
                "password": password,
            }

            # If email provided — check if already verified, otherwise send code
            if email:
                self._account_error.configure(
                    text="Checking email...", text_color="gray"
                )
                self.update()

                from desktop.p2p.email_verify import (
                    generate_code, send_verification_email,
                    is_email_already_verified,
                )

                # Skip verification if email was already verified for this identity
                if is_email_already_verified(email, username, password):
                    self.config["_account"]["email"] = email
                    self.config["_account"]["email_verified"] = True
                    self._account_error.configure(
                        text="Email already verified!", text_color="green"
                    )
                    self.update()
                    return True

                self._account_error.configure(
                    text="Sending verification code...", text_color="gray"
                )
                self.update()

                self._account_verify_code = generate_code()
                sent = send_verification_email(
                    to_email=email,
                    code=self._account_verify_code,
                    from_username=username,
                )
                if sent:
                    self._account_verify_phase = True
                    self._show_step()  # re-render as phase 2
                    return False  # don't advance step
                else:
                    self._account_error.configure(
                        text="Failed to send email. Proceeding without verification.",
                        text_color="orange",
                    )
                    self.config["_account"]["email"] = email
                    self.config["_account"]["email_verified"] = False

            return True

        if self.current_step == 2:  # Provider
            provider = self._provider_var.get()
            self.config["provider"] = provider

            if provider == "anthropic":
                key = self._anthropic_key_var.get().strip()
                if not key:
                    self._provider_error.configure(text="API key is required")
                    return False
                self.config["api_keys"]["anthropic"] = key
            elif provider == "openai":
                key = self._openai_key_var.get().strip()
                if not key:
                    self._provider_error.configure(text="API key is required")
                    return False
                self.config["api_keys"]["openai"] = key
            elif provider == "openai_compat":
                url = self._compat_url_var.get().strip()
                model = self._compat_model_var.get().strip()
                if not url or not model:
                    self._provider_error.configure(
                        text="Base URL and model name are required"
                    )
                    return False
                self.config["openai_compat"] = {
                    "base_url": url,
                    "api_key": self._compat_key_var.get().strip() or None,
                    "model": model,
                    "name": self._compat_name_var.get().strip() or None,
                }
            elif provider == "claude_code":
                self.config["claude_code_available"] = True

            return True

        if self.current_step == 3:  # HQPlayer
            self.config["hqplayer"]["enabled"] = self._hqp_enabled_var.get()
            if self._hqp_enabled_var.get():
                self.config["hqplayer"]["host"] = self._hqp_host_var.get().strip() or "localhost"
                try:
                    self.config["hqplayer"]["port"] = int(self._hqp_port_var.get())
                except ValueError:
                    self.config["hqplayer"]["port"] = 4321
            return True

        if self.current_step == 4:  # Last.fm
            if self._lastfm_enabled_var.get():
                username = self._lastfm_user_var.get().strip()
                if not username:
                    return True  # Allow empty — will skip auth
                self.config.setdefault("lastfm", {})["username"] = username
                self.config["lastfm"]["pending_auth"] = True
            else:
                self.config.setdefault("lastfm", {})["username"] = None
                self.config.setdefault("lastfm", {})["pending_auth"] = False
            return True

        return True

    # ================================================================
    # Step implementations
    # ================================================================

    def _step_welcome(self):
        ctk.CTkLabel(
            self.content_frame,
            text="Sautium",
            font=ctk.CTkFont(size=28, weight="bold"),
        ).pack(pady=(30, 10))

        ctk.CTkLabel(
            self.content_frame,
            text=(
                "AI-powered music library management.\n"
                "Search your collection by sound, mood, lyrics, or description.\n"
                "Works standalone or with an AI agent for chat recommendations."
            ),
            font=ctk.CTkFont(size=14),
            justify="center",
        ).pack(pady=10)

        # System info
        info_frame = ctk.CTkFrame(self.content_frame)
        info_frame.pack(fill="x", pady=20, padx=40)

        items = [
            ("Accelerator", f"{self._gpu_name} ({self._gpu_vram}GB)" if self._gpu_available and self._gpu_vram else
                            self._gpu_name if self._gpu_available else "Not detected"),
            ("Claude CLI", "Available" if self._claude_available else "Not found"),
            ("Git", "Available" if self._git_available else "Not found"),
        ]
        for label, value in items:
            row = ctk.CTkFrame(info_frame, fg_color="transparent")
            row.pack(fill="x", padx=10, pady=2)
            ctk.CTkLabel(row, text=f"{label}:", width=100, anchor="w").pack(side="left")
            ctk.CTkLabel(row, text=value, anchor="w").pack(side="left", padx=5)

    def _step_account(self):
        # Two-phase step: phase 1 = fields, phase 2 = email verification
        if getattr(self, "_account_verify_phase", False):
            self._step_account_verify()
            return

        ctk.CTkLabel(
            self.content_frame,
            text="P2P Identity",
            font=ctk.CTkFont(size=22, weight="bold"),
        ).pack(pady=(20, 5))

        ctk.CTkLabel(
            self.content_frame,
            text=(
                "Create an account for P2P chat and friend discovery.\n"
                "Same username + password on any device = same identity."
            ),
            justify="center",
        ).pack(pady=5)

        fields_frame = ctk.CTkFrame(self.content_frame, fg_color="transparent")
        fields_frame.pack(fill="x", padx=40, pady=10)

        self._account_user_var = ctk.StringVar(
            value=getattr(self, "_account_user_val", "")
        )
        self._account_pass_var = ctk.StringVar(
            value=getattr(self, "_account_pass_val", "")
        )
        self._account_pass2_var = ctk.StringVar(
            value=getattr(self, "_account_pass2_val", "")
        )
        self._account_email_var = ctk.StringVar(
            value=getattr(self, "_account_email_val", "")
        )

        ctk.CTkLabel(fields_frame, text="Username:").pack(anchor="w")
        ctk.CTkEntry(
            fields_frame,
            textvariable=self._account_user_var,
            width=300,
            placeholder_text="your nickname",
        ).pack(fill="x", pady=(0, 8))

        ctk.CTkLabel(fields_frame, text="Password:").pack(anchor="w")
        ctk.CTkEntry(
            fields_frame,
            textvariable=self._account_pass_var,
            width=300,
            show="*",
            placeholder_text="min 8 characters",
        ).pack(fill="x", pady=(0, 8))

        ctk.CTkLabel(fields_frame, text="Confirm password:").pack(anchor="w")
        ctk.CTkEntry(
            fields_frame,
            textvariable=self._account_pass2_var,
            width=300,
            show="*",
        ).pack(fill="x", pady=(0, 8))

        ctk.CTkLabel(fields_frame, text="Email (optional):").pack(anchor="w")
        ctk.CTkEntry(
            fields_frame,
            textvariable=self._account_email_var,
            width=300,
            placeholder_text="for friend discovery",
        ).pack(fill="x", pady=(0, 5))

        self._account_error = ctk.CTkLabel(
            self.content_frame, text="", text_color="red",
        )
        self._account_error.pack()

        ctk.CTkLabel(
            self.content_frame,
            text=(
                "Leave all empty to skip — a random identity will be created.\n"
                "You can create an account later in Settings."
            ),
            text_color="gray",
            font=ctk.CTkFont(size=12),
            justify="center",
        ).pack(pady=(5, 0))

    def _step_account_verify(self):
        """Phase 2: email verification code input."""
        email = self._account_email_val

        ctk.CTkLabel(
            self.content_frame,
            text="Email Verification",
            font=ctk.CTkFont(size=22, weight="bold"),
        ).pack(pady=(20, 5))

        ctk.CTkLabel(
            self.content_frame,
            text=f"Verification code sent to:\n{email}",
            justify="center",
        ).pack(pady=10)

        fields_frame = ctk.CTkFrame(self.content_frame, fg_color="transparent")
        fields_frame.pack(fill="x", padx=40, pady=10)

        self._verify_code_var = ctk.StringVar()

        ctk.CTkLabel(fields_frame, text="Enter code from email:").pack(anchor="w")
        ctk.CTkEntry(
            fields_frame,
            textvariable=self._verify_code_var,
            width=200,
            placeholder_text="e.g. X7K9M2",
            font=ctk.CTkFont(size=18, family="Consolas"),
        ).pack(anchor="w", pady=(0, 10))

        self._account_error = ctk.CTkLabel(
            self.content_frame, text="", text_color="red",
        )
        self._account_error.pack()

        skip_frame = ctk.CTkFrame(self.content_frame, fg_color="transparent")
        skip_frame.pack(pady=10)
        ctk.CTkButton(
            skip_frame,
            text="Skip verification",
            width=150,
            fg_color="gray",
            command=self._skip_email_verify,
        ).pack()

        ctk.CTkLabel(
            self.content_frame,
            text="Check your inbox (and spam folder).",
            text_color="gray",
            font=ctk.CTkFont(size=12),
        ).pack(pady=(5, 0))

    def _skip_email_verify(self):
        """Skip email verification and proceed."""
        self.config["_account"]["email"] = self._account_email_val
        self.config["_account"]["email_verified"] = False
        self._account_verify_phase = False
        self.current_step += 1
        self._show_step()

    def _step_music_path(self):
        ctk.CTkLabel(
            self.content_frame,
            text="Music Library",
            font=ctk.CTkFont(size=22, weight="bold"),
        ).pack(pady=(20, 5))

        ctk.CTkLabel(
            self.content_frame,
            text="Select the folder containing your music collection.",
        ).pack(pady=5)

        path_frame = ctk.CTkFrame(self.content_frame, fg_color="transparent")
        path_frame.pack(fill="x", pady=20, padx=20)

        self._music_path_var = ctk.StringVar(
            value=self.config.get("music_path", "")
        )
        entry = ctk.CTkEntry(
            path_frame, textvariable=self._music_path_var, width=400,
        )
        entry.pack(side="left", fill="x", expand=True, padx=(0, 10))

        ctk.CTkButton(
            path_frame, text="Browse...", width=100,
            command=self._browse_music_path,
        ).pack(side="right")

        self._music_path_error = ctk.CTkLabel(
            self.content_frame, text="", text_color="red",
        )
        self._music_path_error.pack()

        ctk.CTkLabel(
            self.content_frame,
            text=(
                "Expected structure: Music / Genre / Artist / Album / Track.flac\n"
                "The library will be accessed read-only."
            ),
            text_color="gray",
            font=ctk.CTkFont(size=12),
            justify="center",
        ).pack(pady=20)

    def _browse_music_path(self):
        path = filedialog.askdirectory(title="Select Music Library Folder")
        if path:
            self._music_path_var.set(path)
            self._music_path_error.configure(text="")

    def _step_provider(self):
        ctk.CTkLabel(
            self.content_frame,
            text="AI Provider",
            font=ctk.CTkFont(size=22, weight="bold"),
        ).pack(pady=(20, 5))

        ctk.CTkLabel(
            self.content_frame,
            text="Choose how the AI DJ will generate recommendations.",
        ).pack(pady=5)

        self._provider_var = ctk.StringVar(
            value=self.config.get("provider", "none")
        )

        providers_frame = ctk.CTkFrame(self.content_frame, fg_color="transparent")
        providers_frame.pack(fill="x", padx=20, pady=10)

        # No AI — use built-in semantic search
        ctk.CTkRadioButton(
            providers_frame,
            text="No AI agent (built-in semantic search)",
            variable=self._provider_var,
            value="none",
            command=self._update_provider_fields,
        ).pack(anchor="w", pady=3)

        # Claude Code option (only if CLI available)
        if self._claude_available:
            ctk.CTkRadioButton(
                providers_frame,
                text="Claude Code (subscription — recommended)",
                variable=self._provider_var,
                value="claude_code",
                command=self._update_provider_fields,
            ).pack(anchor="w", pady=3)

        # Anthropic
        ctk.CTkRadioButton(
            providers_frame,
            text="Anthropic API (Claude)",
            variable=self._provider_var,
            value="anthropic",
            command=self._update_provider_fields,
        ).pack(anchor="w", pady=3)

        # OpenAI
        ctk.CTkRadioButton(
            providers_frame,
            text="OpenAI API (GPT-4)",
            variable=self._provider_var,
            value="openai",
            command=self._update_provider_fields,
        ).pack(anchor="w", pady=3)

        # OpenAI-compatible
        ctk.CTkRadioButton(
            providers_frame,
            text="OpenAI-compatible (Ollama, LM Studio, etc.)",
            variable=self._provider_var,
            value="openai_compat",
            command=self._update_provider_fields,
        ).pack(anchor="w", pady=3)

        # Dynamic fields container
        self._provider_fields_frame = ctk.CTkFrame(self.content_frame, fg_color="transparent")
        self._provider_fields_frame.pack(fill="x", padx=30, pady=5)

        # Variables for API keys
        self._anthropic_key_var = ctk.StringVar(
            value=self.config.get("api_keys", {}).get("anthropic") or ""
        )
        self._openai_key_var = ctk.StringVar(
            value=self.config.get("api_keys", {}).get("openai") or ""
        )
        compat = self.config.get("openai_compat", {})
        self._compat_url_var = ctk.StringVar(value=compat.get("base_url") or "")
        self._compat_key_var = ctk.StringVar(value=compat.get("api_key") or "")
        self._compat_model_var = ctk.StringVar(value=compat.get("model") or "")
        self._compat_name_var = ctk.StringVar(value=compat.get("name") or "")

        self._provider_error = ctk.CTkLabel(
            self.content_frame, text="", text_color="red",
        )
        self._provider_error.pack()

        self._update_provider_fields()

    def _update_provider_fields(self):
        for widget in self._provider_fields_frame.winfo_children():
            widget.destroy()

        provider = self._provider_var.get()
        self._provider_error.configure(text="")

        if provider == "none":
            ctk.CTkLabel(
                self._provider_fields_frame,
                text=(
                    "Search by sound, lyrics, artist bio, album description,\n"
                    "genre, and audio features — all powered by local embeddings.\n"
                    "You can add an AI provider later in Settings."
                ),
                text_color="gray",
                justify="left",
            ).pack(anchor="w")

        elif provider == "claude_code":
            ctk.CTkLabel(
                self._provider_fields_frame,
                text="Uses your Claude Code subscription. No API key needed.",
                text_color="gray",
            ).pack(anchor="w")

        elif provider == "anthropic":
            ctk.CTkLabel(
                self._provider_fields_frame, text="API Key:",
            ).pack(anchor="w")
            ctk.CTkEntry(
                self._provider_fields_frame,
                textvariable=self._anthropic_key_var,
                width=400, show="*",
            ).pack(fill="x")

        elif provider == "openai":
            ctk.CTkLabel(
                self._provider_fields_frame, text="API Key:",
            ).pack(anchor="w")
            ctk.CTkEntry(
                self._provider_fields_frame,
                textvariable=self._openai_key_var,
                width=400, show="*",
            ).pack(fill="x")

        elif provider == "openai_compat":
            ctk.CTkLabel(
                self._provider_fields_frame, text="Base URL:",
            ).pack(anchor="w")
            ctk.CTkEntry(
                self._provider_fields_frame,
                textvariable=self._compat_url_var,
                width=400, placeholder_text="http://localhost:11434/v1",
            ).pack(fill="x", pady=(0, 5))

            ctk.CTkLabel(
                self._provider_fields_frame, text="API Key (optional):",
            ).pack(anchor="w")
            ctk.CTkEntry(
                self._provider_fields_frame,
                textvariable=self._compat_key_var,
                width=400, show="*",
            ).pack(fill="x", pady=(0, 5))

            ctk.CTkLabel(
                self._provider_fields_frame, text="Model name:",
            ).pack(anchor="w")
            ctk.CTkEntry(
                self._provider_fields_frame,
                textvariable=self._compat_model_var,
                width=400, placeholder_text="llama3:70b",
            ).pack(fill="x", pady=(0, 5))

            ctk.CTkLabel(
                self._provider_fields_frame, text="Display name (optional):",
            ).pack(anchor="w")
            ctk.CTkEntry(
                self._provider_fields_frame,
                textvariable=self._compat_name_var,
                width=400, placeholder_text="My Local LLM",
            ).pack(fill="x")

    def _step_hqplayer(self):
        ctk.CTkLabel(
            self.content_frame,
            text="HQPlayer Integration",
            font=ctk.CTkFont(size=22, weight="bold"),
        ).pack(pady=(20, 5))

        ctk.CTkLabel(
            self.content_frame,
            text="Connect to HQPlayer Desktop for playback control and Last.fm scrobbling.",
        ).pack(pady=5)

        hqp = self.config.get("hqplayer", {})
        self._hqp_enabled_var = ctk.BooleanVar(value=hqp.get("enabled", True))
        self._hqp_host_var = ctk.StringVar(value=hqp.get("host", "localhost"))
        self._hqp_port_var = ctk.StringVar(value=str(hqp.get("port", 4321)))

        ctk.CTkCheckBox(
            self.content_frame,
            text="Enable HQPlayer integration",
            variable=self._hqp_enabled_var,
            command=self._toggle_hqp_fields,
        ).pack(pady=10)

        self._hqp_fields_frame = ctk.CTkFrame(self.content_frame, fg_color="transparent")
        self._hqp_fields_frame.pack(fill="x", padx=30, pady=5)

        self._toggle_hqp_fields()

    def _toggle_hqp_fields(self):
        for widget in self._hqp_fields_frame.winfo_children():
            widget.destroy()

        if not self._hqp_enabled_var.get():
            return

        ctk.CTkLabel(self._hqp_fields_frame, text="Host:").pack(anchor="w")
        ctk.CTkEntry(
            self._hqp_fields_frame,
            textvariable=self._hqp_host_var,
            width=300,
        ).pack(fill="x", pady=(0, 5))

        ctk.CTkLabel(self._hqp_fields_frame, text="Port:").pack(anchor="w")
        ctk.CTkEntry(
            self._hqp_fields_frame,
            textvariable=self._hqp_port_var,
            width=100,
        ).pack(anchor="w", pady=(0, 10))

        ctk.CTkButton(
            self._hqp_fields_frame,
            text="Test Connection",
            width=150,
            command=self._test_hqplayer,
        ).pack(anchor="w")

        self._hqp_status_label = ctk.CTkLabel(
            self._hqp_fields_frame, text="", text_color="gray",
        )
        self._hqp_status_label.pack(anchor="w", pady=5)

    def _test_hqplayer(self):
        """Test HQPlayer connection."""
        import socket

        host = self._hqp_host_var.get().strip() or "localhost"
        try:
            port = int(self._hqp_port_var.get())
        except ValueError:
            self._hqp_status_label.configure(
                text="Invalid port", text_color="red"
            )
            return

        self._hqp_status_label.configure(
            text="Testing...", text_color="gray"
        )
        self.update()

        try:
            sock = socket.create_connection((host, port), timeout=3)
            sock.close()
            self._hqp_status_label.configure(
                text="Connection successful!", text_color="green"
            )
        except (ConnectionRefusedError, socket.timeout, OSError) as e:
            self._hqp_status_label.configure(
                text=f"Connection failed: {e}", text_color="red"
            )

    def _step_lastfm(self):
        ctk.CTkLabel(
            self.content_frame,
            text="Last.fm Scrobbling",
            font=ctk.CTkFont(size=22, weight="bold"),
        ).pack(pady=(20, 5))

        ctk.CTkLabel(
            self.content_frame,
            text="Track your listening history on Last.fm.\nScrobbling works automatically with HQPlayer playback.",
        ).pack(pady=5)

        lastfm = self.config.get("lastfm", {})
        self._lastfm_enabled_var = ctk.BooleanVar(
            value=bool(lastfm.get("username"))
        )

        ctk.CTkCheckBox(
            self.content_frame,
            text="Enable Last.fm scrobbling",
            variable=self._lastfm_enabled_var,
            command=self._toggle_lastfm_fields,
        ).pack(pady=10)

        self._lastfm_fields_frame = ctk.CTkFrame(
            self.content_frame, fg_color="transparent"
        )
        self._lastfm_fields_frame.pack(fill="x", padx=30, pady=5)

        self._lastfm_user_var = ctk.StringVar(
            value=lastfm.get("username") or ""
        )

        self._toggle_lastfm_fields()

    def _toggle_lastfm_fields(self):
        for widget in self._lastfm_fields_frame.winfo_children():
            widget.destroy()

        if not self._lastfm_enabled_var.get():
            return

        ctk.CTkLabel(
            self._lastfm_fields_frame, text="Last.fm Username:"
        ).pack(anchor="w")
        ctk.CTkEntry(
            self._lastfm_fields_frame,
            textvariable=self._lastfm_user_var,
            width=300,
            placeholder_text="your Last.fm username",
        ).pack(anchor="w", pady=(0, 10))

        ctk.CTkLabel(
            self._lastfm_fields_frame,
            text=(
                "After setup, the app will open Last.fm in your browser\n"
                "to authorize scrobbling. You can also do this later in Settings."
            ),
            text_color="gray",
            font=ctk.CTkFont(size=12),
            justify="left",
        ).pack(anchor="w", pady=5)

    def _step_summary(self):
        ctk.CTkLabel(
            self.content_frame,
            text="Summary",
            font=ctk.CTkFont(size=22, weight="bold"),
        ).pack(pady=(20, 10))

        summary_frame = ctk.CTkFrame(self.content_frame)
        summary_frame.pack(fill="x", padx=20, pady=10)

        account = self.config.get("_account")
        account_text = account["username"] if account else "Random (no account)"
        lastfm_user = self.config.get("lastfm", {}).get("username")
        items = [
            ("Identity", account_text),
            ("AI Provider", {
                "none": "None (semantic search only)",
                "claude_code": "Claude Code",
                "anthropic": "Anthropic API",
                "openai": "OpenAI API",
                "openai_compat": "Custom API",
            }.get(self.config.get("provider", "none"), self.config.get("provider", "none"))),
            (
                "HQPlayer",
                f"{self.config['hqplayer']['host']}:{self.config['hqplayer']['port']}"
                if self.config.get("hqplayer", {}).get("enabled")
                else "Disabled",
            ),
            (
                "Last.fm",
                f"{lastfm_user} (will authorize after start)"
                if lastfm_user
                else "Disabled",
            ),
            ("Accelerator", self._gpu_name if self._gpu_available else "CPU mode"),
        ]

        for label, value in items:
            row = ctk.CTkFrame(summary_frame, fg_color="transparent")
            row.pack(fill="x", padx=10, pady=3)
            ctk.CTkLabel(row, text=f"{label}:", width=120, anchor="w",
                         font=ctk.CTkFont(weight="bold")).pack(side="left")
            ctk.CTkLabel(row, text=str(value), anchor="w", wraplength=350).pack(
                side="left", padx=5
            )

        ctk.CTkLabel(
            self.content_frame,
            text=(
                "Click 'Start' to initialize the database and start services.\n"
                "This may take a minute on first run."
            ),
            text_color="gray",
            font=ctk.CTkFont(size=12),
            justify="center",
        ).pack(pady=15)

        self._progress_label = ctk.CTkLabel(
            self.content_frame, text="", text_color="gray",
        )
        self._progress_label.pack()

        self._progress_bar = ctk.CTkProgressBar(self.content_frame, width=400)
        self._progress_bar.pack(pady=5)
        self._progress_bar.set(0)
        self._progress_bar.pack_forget()  # Hidden until start

    @staticmethod
    def _ensure_crypto_deps(progress_cb=None):
        """Install cryptography dependencies if missing."""
        missing = []
        for pkg, pip_name in [
            ("cryptography", "cryptography>=41.0.0"),
            ("argon2", "argon2-cffi>=23.1.0"),
            ("nacl", "PyNaCl>=1.5.0"),
        ]:
            try:
                __import__(pkg)
            except ImportError:
                missing.append(pip_name)

        if not missing:
            return

        if progress_cb:
            progress_cb(f"Installing: {', '.join(missing)}...")
        logger.info(f"Installing crypto deps: {missing}")

        cmd = [sys.executable, "-m", "pip", "install", "--quiet"] + missing
        kwargs = {"capture_output": True, "text": True, "timeout": 300}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        result = subprocess.run(cmd, **kwargs)
        if result.returncode != 0:
            logger.error(f"Crypto deps install failed: {result.stderr[:300]}")
            raise RuntimeError(
                f"Failed to install dependencies: {', '.join(missing)}"
            )
        logger.info("Crypto dependencies installed")

    def _check_macos_homebrew(self) -> bool:
        """On macOS, check if Homebrew is installed. Show dialog if not."""
        if sys.platform != "darwin":
            return True

        from desktop.db_init import _find_brew
        if _find_brew():
            return True

        # Homebrew not found — show dialog
        self._show_homebrew_dialog()
        return False

    def _show_homebrew_dialog(self):
        """Show a dialog explaining how to install Homebrew."""
        dialog = ctk.CTkToplevel(self)
        dialog.title("Homebrew Required")
        dialog.geometry("520x310")
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()

        ctk.CTkLabel(
            dialog,
            text="Homebrew Required",
            font=ctk.CTkFont(size=20, weight="bold"),
        ).pack(pady=(20, 5))

        ctk.CTkLabel(
            dialog,
            text=(
                "Sautium needs Homebrew to install PostgreSQL\n"
                "on macOS. It's a one-time setup."
            ),
            justify="center",
        ).pack(pady=5)

        ctk.CTkLabel(
            dialog,
            text="Open Terminal and paste this command:",
            text_color="gray",
        ).pack(pady=(10, 3))

        brew_cmd = '/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'

        cmd_frame = ctk.CTkFrame(dialog)
        cmd_frame.pack(fill="x", padx=30, pady=5)

        cmd_entry = ctk.CTkEntry(
            cmd_frame, width=420,
            font=ctk.CTkFont(size=12, family="Courier"),
        )
        cmd_entry.insert(0, brew_cmd)
        cmd_entry.configure(state="readonly")
        cmd_entry.pack(side="left", padx=(10, 5), pady=8)

        def _copy():
            dialog.clipboard_clear()
            dialog.clipboard_append(brew_cmd)
            copy_btn.configure(text="Copied!")
            dialog.after(1500, lambda: copy_btn.configure(text="Copy"))

        copy_btn = ctk.CTkButton(
            cmd_frame, text="Copy", width=60, command=_copy,
        )
        copy_btn.pack(side="right", padx=(0, 10), pady=8)

        ctk.CTkLabel(
            dialog,
            text=(
                "After installing Homebrew, close Terminal\n"
                "and click 'Retry' below."
            ),
            text_color="gray",
            font=ctk.CTkFont(size=12),
            justify="center",
        ).pack(pady=8)

        btn_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_frame.pack(pady=10)

        def _retry():
            from desktop.db_init import _find_brew
            if _find_brew():
                dialog.destroy()
                self._finish()
            else:
                retry_status.configure(
                    text="Homebrew still not found. Check Terminal.",
                    text_color="red",
                )

        ctk.CTkButton(
            btn_frame, text="Retry", width=120, command=_retry,
        ).pack(side="left", padx=10)

        ctk.CTkButton(
            btn_frame, text="Cancel", width=100,
            command=dialog.destroy,
            fg_color="transparent", border_width=1,
        ).pack(side="left", padx=10)

        retry_status = ctk.CTkLabel(dialog, text="", text_color="gray")
        retry_status.pack()

    def _finish(self):
        """Save config and start initialization."""
        # macOS: check Homebrew before proceeding
        if not self._check_macos_homebrew():
            return

        # Create account before saving config (don't persist password)
        account_data = self.config.pop("_account", None)

        self.config["first_run_complete"] = True
        save_config(self.config)

        # Show progress
        self._progress_bar.pack(pady=5)
        self._progress_bar.configure(mode="indeterminate")
        self._progress_bar.start()
        self.btn_next.configure(state="disabled")
        self.btn_back.configure(state="disabled")

        def _init_thread():
            try:
                def progress(msg):
                    self.after(0, lambda: self._progress_label.configure(text=msg))

                # Install crypto dependencies if missing
                self._ensure_crypto_deps(progress)

                # Create account identity (or random if skipped)
                if account_data:
                    progress("Creating account identity...")
                    from desktop.node_identity import create_account
                    info = create_account(
                        account_data["username"],
                        account_data["password"],
                        email=account_data.get("email", ""),
                        email_verified=account_data.get("email_verified", False),
                    )
                    logger.info(
                        f"Account created: {info['invite_code']}"
                    )

                    # Register verified email on Worker (CA mapping)
                    if account_data.get("email_verified") and account_data.get("email"):
                        progress("Registering verified email...")
                        from desktop.p2p.email_verify import register_verified_email
                        if register_verified_email(account_data["email"]):
                            logger.info("Email registered on Worker CA")
                        else:
                            logger.warning("Email registration on Worker failed")
                else:
                    progress("Generating node identity...")
                    from desktop.node_identity import (
                        has_identity, generate_identity,
                    )
                    if not has_identity():
                        generate_identity()

                # Initialize database
                from desktop.db_init import full_init

                password = self.config.get("postgres_password", "changeme")
                port = self.config.get("ports", {}).get("postgres", 5432)

                full_init(password, port=port, progress_cb=progress)

                self.after(0, self._init_complete)
            except Exception as e:
                logger.error(f"Initialization failed: {e}")
                self.after(
                    0,
                    lambda: self._progress_label.configure(
                        text=f"Error: {e}", text_color="red"
                    ),
                )
                self.after(0, lambda: self._progress_bar.stop())
                self.after(0, lambda: self.btn_back.configure(state="normal"))

        threading.Thread(target=_init_thread, daemon=True).start()

    def _init_complete(self):
        self._progress_bar.stop()
        self._progress_label.configure(text="Initialization complete!")
        if self.on_complete:
            self.on_complete(self.config)
        self.destroy()

    def _on_close(self):
        """Handle window close — quit the whole app if wizard not completed."""
        if self.current_step == len(self.steps) - 1:
            return  # Don't close during init
        self.destroy()
        # Quit the parent app since setup was not completed
        if self.master:
            self.master.destroy()
