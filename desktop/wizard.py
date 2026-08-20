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
import queue
import secrets
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from tkinter import filedialog
from typing import Optional

import customtkinter as ctk

from desktop.config_manager import get_data_dir, load_config, save_config
from desktop.utils import (
    claude_auth_verified,
    claude_authenticated,
    detect_claude_cli,
    detect_git,
    detect_gpu,
    detect_node_version,
    get_claude_executable,
    install_claude_runtime,
    launch_claude_setup,
)

logger = logging.getLogger(__name__)


class SetupWizard(ctk.CTkToplevel):
    """First-run setup wizard."""

    def __init__(self, parent, on_complete=None):
        super().__init__(parent)

        self.title("Sautium - Setup")
        self.geometry("640x680")
        self.resizable(False, False)
        if sys.platform != "darwin":
            self.transient(parent)
        self.grab_set()
        self.lift()
        self.focus_force()

        self.on_complete = on_complete
        self.config = load_config()
        # Cross-thread UI marshaling — mirrors LauncherApp.ui_call (Tcl is
        # apartment-threaded; after() from a worker thread deadlocks on macOS).
        self._ui_queue: queue.Queue = queue.Queue()
        self.after(100, self._drain_ui_queue)
        self.current_step = 0
        self.steps = [
            self._step_welcome,
            self._step_account,
            self._step_provider,
            self._step_lastfm,
            self._step_catalog,
            self._step_summary,
        ]

        # Detection results
        self._gpu_available, self._gpu_name, self._gpu_vram = detect_gpu()
        self._git_available = detect_git()
        # Claude Code state is computed on-demand via _claude_state()
        # because it can change during the wizard (install, sign-in).
        self._claude_install_thread: Optional[threading.Thread] = None
        self._claude_poll_after_id: Optional[str] = None
        self._claude_poll_deadline: float = 0.0
        # Live-probe verdict over stored credentials: None = not checked yet,
        # False = file exists but auth is dead (expired beyond refresh /
        # revoked). Presence of .credentials.json alone proves nothing.
        self._claude_verified: Optional[bool] = None
        self._claude_verify_thread: Optional[threading.Thread] = None

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
        self._cancel_claude_poll()
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

    def ui_call(self, fn):
        """Thread-safe UI marshaling — see LauncherApp.ui_call for the why."""
        self._ui_queue.put(fn)

    def _drain_ui_queue(self):
        while True:
            try:
                fn = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception as e:
                logger.debug(f"ui_call callback failed: {e}")
        if self.winfo_exists():
            self.after(100, self._drain_ui_queue)

    def _step_name(self) -> str:
        """Which step we are on, by name. Positional indices used to decide this,
        so removing a step silently shifted every later step's save code onto
        the wrong screen."""
        return self.steps[self.current_step].__name__

    def _validate_step(self) -> bool:
        if self._step_name() == "_step_account":
            # Phase 2: verify email code (the Worker holds the expected
            # value — registration happens right here, with the birth
            # certificate attached by register_verified_email)
            if getattr(self, "_account_verify_phase", False):
                code = self._verify_code_var.get().strip().upper()
                if not code:
                    self._account_error.configure(text="Enter the code from email")
                    return False
                self._account_error.configure(
                    text="Verifying code...", text_color="gray"
                )
                self.update()
                from desktop.p2p.email_verify import register_verified_email
                acct = self.config["_account"]
                if not register_verified_email(
                    self._account_email_val, code,
                    acct["username"], acct["password"],
                ):
                    self._account_error.configure(text="Invalid or expired code")
                    return False
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

            if email and "@" not in email:
                self._account_error.configure(text="Invalid email address")
                return False

            if not username:
                # Skipping the form still produces a real account —
                # `anonymous-<4 hex>` username plus a 32-byte random
                # password. Without one `_get_identity()` returns None
                # downstream, which hides the invite code and leaves
                # Friends / chat / P2P sync silently broken. The user
                # can rename later from Profile. An email entered with
                # it is NOT dropped: it is verified for the anonymous
                # identity below, exactly as for a named one (an
                # anonymous account with a verified mailbox is a
                # method:email identity). The credentials are minted
                # once and reused across re-renders, so the identity
                # that received the code is the one that redeems it.
                acct = self.config.get("_account") or {}
                if not acct.get("anonymous"):
                    acct = {
                        "username": "anonymous-" + secrets.token_hex(2),
                        "password": secrets.token_urlsafe(32),
                        "anonymous": True,
                    }
                self.config["_account"] = acct
                username, password = acct["username"], acct["password"]
                if not email:
                    return True
            else:
                from desktop.node_identity import validate_username
                try:
                    validate_username(username)
                except ValueError as e:
                    self._account_error.configure(text=str(e))
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
                    send_verification_email,
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

                sent = send_verification_email(
                    to_email=email,
                    username=username,
                    password=password,
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

        if self._step_name() == "_step_provider":
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
            elif provider == "claude_code":
                if self._claude_state() != "ready" or self._claude_verified is not True:
                    self._provider_error.configure(
                        text=("Verifying Claude sign-in — give it a few seconds."
                              if self._claude_state() == "ready"
                              and self._claude_verified is None
                              else "Finish Claude setup above (or pick another provider)."),
                    )
                    return False
                self.config["claude_code_available"] = True

            return True

        if self._step_name() == "_step_lastfm":
            self.config.setdefault("lastfm", {})
            if self._lastfm_enabled_var.get():
                self.config["lastfm"]["pending_auth"] = True
            else:
                self.config["lastfm"]["pending_auth"] = False
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

        claude_status = {
            "ready": "Signed in",
            "not_authed": "Installed (sign in required)",
            "claude_missing": "Not installed",
            "node_missing": "Node.js not found",
        }[self._claude_state()]
        items = [
            ("Accelerator", f"{self._gpu_name} ({self._gpu_vram}GB)" if self._gpu_available and self._gpu_vram else
                            self._gpu_name if self._gpu_available else "Not detected"),
            ("Claude Code", claude_status),
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
                "Leave all empty to skip — an anonymous identity\n"
                "(anonymous-XXXX) will be created. You can rename it\n"
                "later from your Profile."
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

        # Claude Code (subscription) — wizard installs and signs in
        # if not already set up.
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
            self._render_claude_state_ui()

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

    # ================================================================
    # Claude Code provider — install + sign-in state machine
    # ================================================================

    def _claude_state(self) -> str:
        """Return one of 'node_missing' | 'claude_missing' | 'not_authed' | 'ready'."""
        node_ver = detect_node_version()
        if node_ver is None or node_ver[0] < 18:
            return "node_missing"
        if get_claude_executable() is None:
            return "claude_missing"
        if not claude_authenticated():
            return "not_authed"
        return "ready"

    def _render_claude_state_ui(self):
        """Draw the action UI for whatever stage of Claude setup we're in.
        Called from `_update_provider_fields` and after each state change."""
        for widget in self._provider_fields_frame.winfo_children():
            widget.destroy()
        # Stale validation error from a previous state is no longer relevant.
        self._provider_error.configure(text="")

        state = self._claude_state()

        # Stored credentials are only trusted after a live probe: the file
        # can hold tokens that are expired beyond refresh or revoked, and
        # shipping "ready" on file presence alone let a fresh install skip
        # sign-in and die on the first chat with "OAuth session expired".
        stale_signin = False
        if state == "ready":
            if self._claude_verified is True:
                ctk.CTkLabel(
                    self._provider_fields_frame,
                    text="✓ Claude Code is ready",
                    text_color="#4CAF50",
                    font=ctk.CTkFont(size=14, weight="bold"),
                ).pack(anchor="w")
                ctk.CTkLabel(
                    self._provider_fields_frame,
                    text="Signed in via subscription. No API key needed.",
                    text_color="gray",
                ).pack(anchor="w", pady=(2, 0))
                return
            if self._claude_verified is None:
                ctk.CTkLabel(
                    self._provider_fields_frame,
                    text="Verifying stored sign-in…",
                    text_color="gray",
                    font=ctk.CTkFont(size=13),
                ).pack(anchor="w")
                self._start_claude_verify()
                return
            # Probe said the stored sign-in is dead — walk the user through
            # a fresh /login using the same UI as a first sign-in.
            stale_signin = True
            state = "not_authed"

        if state == "node_missing":
            ctk.CTkLabel(
                self._provider_fields_frame,
                text="Node.js 18+ is required.",
                text_color="orange",
                font=ctk.CTkFont(size=13, weight="bold"),
            ).pack(anchor="w")
            if sys.platform == "darwin":
                ctk.CTkLabel(
                    self._provider_fields_frame,
                    text="Install Node.js via Homebrew, then click Refresh.",
                    text_color="gray",
                ).pack(anchor="w", pady=(2, 5))
                ctk.CTkButton(
                    self._provider_fields_frame,
                    text="Show install command",
                    width=200,
                    command=self._show_node_macos_dialog,
                ).pack(anchor="w", pady=(0, 5))
            else:
                ctk.CTkLabel(
                    self._provider_fields_frame,
                    text=(
                        "The installer should have placed Node next to Sautium.\n"
                        "Re-run the Sautium installer to repair, then click Refresh."
                    ),
                    text_color="gray",
                    justify="left",
                ).pack(anchor="w", pady=(2, 5))
            ctk.CTkButton(
                self._provider_fields_frame,
                text="Refresh",
                width=120,
                command=self._refresh_claude_state,
            ).pack(anchor="w")
            return

        if state == "claude_missing":
            ctk.CTkLabel(
                self._provider_fields_frame,
                text="Claude Code is not installed yet.",
                text_color="gray",
                font=ctk.CTkFont(size=13),
            ).pack(anchor="w")
            ctk.CTkLabel(
                self._provider_fields_frame,
                text="Downloads ~5 MB via npm. Internet connection required.",
                text_color="gray",
                font=ctk.CTkFont(size=12),
            ).pack(anchor="w", pady=(2, 8))

            self._claude_install_status = ctk.CTkLabel(
                self._provider_fields_frame, text="", text_color="gray",
                wraplength=420, justify="left",
            )
            self._claude_install_status.pack(anchor="w", pady=(0, 5))

            self._claude_install_btn = ctk.CTkButton(
                self._provider_fields_frame,
                text="Install Claude Code",
                width=200,
                command=self._install_claude_clicked,
            )
            self._claude_install_btn.pack(anchor="w")
            return

        # state == "not_authed"
        ctk.CTkLabel(
            self._provider_fields_frame,
            text=("Stored sign-in is expired or revoked — sign in again."
                  if stale_signin else "Claude Code installed."),
            text_color="orange" if stale_signin else "gray",
            font=ctk.CTkFont(size=13),
        ).pack(anchor="w")

        instr_frame = ctk.CTkFrame(
            self._provider_fields_frame,
            fg_color=("#F0F0F0", "#2B2B2B"),
        )
        instr_frame.pack(fill="x", pady=(4, 4))

        ctk.CTkLabel(
            instr_frame,
            text="In the new terminal window:",
            font=ctk.CTkFont(size=12, weight="bold"),
        ).pack(anchor="w", padx=12, pady=(6, 2))

        ctk.CTkLabel(
            instr_frame,
            text="1. Pick a theme (first run only)",
            font=ctk.CTkFont(size=12),
        ).pack(anchor="w", padx=20)

        step2 = ctk.CTkFrame(instr_frame, fg_color="transparent")
        step2.pack(anchor="w", padx=20, fill="x")
        ctk.CTkLabel(
            step2, text="2. Type the command:",
            font=ctk.CTkFont(size=12),
        ).pack(side="left")
        ctk.CTkLabel(
            step2, text="  /login",
            font=ctk.CTkFont(size=14, family="Consolas", weight="bold"),
            text_color="#4A7FA7",
        ).pack(side="left")

        ctk.CTkLabel(
            instr_frame,
            text="3. Choose 'Claude account with subscription'",
            font=ctk.CTkFont(size=12),
        ).pack(anchor="w", padx=20)
        ctk.CTkLabel(
            instr_frame,
            text="4. Authorize in the browser tab that opens",
            font=ctk.CTkFont(size=12),
        ).pack(anchor="w", padx=20)
        ctk.CTkLabel(
            instr_frame,
            text="5. Done — close the terminal window",
            font=ctk.CTkFont(size=12),
        ).pack(anchor="w", padx=20, pady=(0, 6))

        ctk.CTkLabel(
            self._provider_fields_frame,
            text="Sautium will detect the sign-in automatically.",
            text_color="gray",
            font=ctk.CTkFont(size=12),
        ).pack(anchor="w", pady=(0, 4))

        self._claude_signin_status = ctk.CTkLabel(
            self._provider_fields_frame, text="", text_color="gray",
        )
        self._claude_signin_status.pack(anchor="w", pady=(0, 5))

        btn_frame = ctk.CTkFrame(
            self._provider_fields_frame, fg_color="transparent"
        )
        btn_frame.pack(anchor="w")
        ctk.CTkButton(
            btn_frame,
            text="Sign in to Claude",
            width=180,
            command=self._signin_claude_clicked,
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            btn_frame,
            text="Refresh",
            width=100,
            command=self._refresh_claude_state,
            fg_color="transparent", border_width=1,
        ).pack(side="left")

    def _show_node_macos_dialog(self):
        """Mirrors `_show_homebrew_dialog` but for `brew install node`."""
        dialog = ctk.CTkToplevel(self)
        dialog.title("Install Node.js")
        dialog.geometry("520x260")
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()

        ctk.CTkLabel(
            dialog,
            text="Install Node.js",
            font=ctk.CTkFont(size=20, weight="bold"),
        ).pack(pady=(20, 5))

        ctk.CTkLabel(
            dialog,
            text="Open Terminal and paste this command:",
            text_color="gray",
        ).pack(pady=(10, 3))

        node_cmd = "brew install node"
        cmd_frame = ctk.CTkFrame(dialog)
        cmd_frame.pack(fill="x", padx=30, pady=5)

        cmd_entry = ctk.CTkEntry(
            cmd_frame, width=380,
            font=ctk.CTkFont(size=13, family="Courier"),
        )
        cmd_entry.insert(0, node_cmd)
        cmd_entry.configure(state="readonly")
        cmd_entry.pack(side="left", padx=(10, 5), pady=8)

        def _copy():
            dialog.clipboard_clear()
            dialog.clipboard_append(node_cmd)
            copy_btn.configure(text="Copied!")
            dialog.after(1500, lambda: copy_btn.configure(text="Copy"))

        copy_btn = ctk.CTkButton(
            cmd_frame, text="Copy", width=70, command=_copy,
        )
        copy_btn.pack(side="right", padx=(0, 10), pady=8)

        ctk.CTkLabel(
            dialog,
            text="After install completes, close this dialog and click Refresh.",
            text_color="gray",
            font=ctk.CTkFont(size=12),
            justify="center",
        ).pack(pady=10)

        ctk.CTkButton(
            dialog, text="Done", width=100, command=dialog.destroy,
        ).pack(pady=10)

    def _install_claude_clicked(self):
        """Run npm install in a worker thread and re-render on completion."""
        if self._claude_install_thread and self._claude_install_thread.is_alive():
            return
        self._claude_install_btn.configure(state="disabled", text="Installing...")
        self._claude_install_status.configure(
            text="Running npm install (may take a minute)...",
            text_color="gray",
        )

        def _worker():
            ok, msg = install_claude_runtime()
            self.ui_call(lambda: self._on_claude_install_done(ok, msg))

        self._claude_install_thread = threading.Thread(
            target=_worker, daemon=True
        )
        self._claude_install_thread.start()

    def _on_claude_install_done(self, ok: bool, msg: str):
        if ok:
            self._render_claude_state_ui()  # transitions to not_authed UI
        else:
            self._claude_install_btn.configure(
                state="normal", text="Install Claude Code"
            )
            self._claude_install_status.configure(
                text=f"Install failed: {msg}", text_color="red",
            )

    def _signin_claude_clicked(self):
        """Open `claude` in a new terminal and start polling for credentials."""
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

    def _start_claude_verify(self):
        """Probe stored credentials with a live CLI turn (worker thread)."""
        if self._claude_verify_thread and self._claude_verify_thread.is_alive():
            return

        def _worker():
            ok = claude_auth_verified()
            self.ui_call(lambda: self._on_claude_verify_done(ok))

        self._claude_verify_thread = threading.Thread(target=_worker, daemon=True)
        self._claude_verify_thread.start()

    def _on_claude_verify_done(self, ok: bool):
        self._claude_verified = ok
        # The probe takes seconds — the user may have navigated to another
        # step, destroying the provider frame this render would touch. The
        # verdict is cached either way; coming back re-renders from it.
        try:
            alive = bool(self._provider_fields_frame.winfo_exists())
        except Exception:
            alive = False
        if alive:
            self._render_claude_state_ui()

    def _refresh_claude_state(self):
        """Refresh button: drop the cached probe verdict so a sign-in done
        outside our console (user's own terminal) gets re-checked."""
        self._claude_verified = None
        self._render_claude_state_ui()

    def _poll_claude_auth(self):
        """Self-rescheduling timer that checks for credentials."""
        if claude_authenticated():
            self._claude_poll_after_id = None
            # Fresh credentials just landed — re-probe them instead of
            # trusting a pre-login verdict.
            self._claude_verified = None
            self._render_claude_state_ui()  # transitions to verifying → ready
            return
        if time.monotonic() >= self._claude_poll_deadline:
            self._claude_poll_after_id = None
            self._claude_signin_status.configure(
                text="Sign-in not detected. Click Refresh after authorizing.",
                text_color="orange",
            )
            return
        self._claude_poll_after_id = self.after(2000, self._poll_claude_auth)

    def _cancel_claude_poll(self):
        if self._claude_poll_after_id is not None:
            try:
                self.after_cancel(self._claude_poll_after_id)
            except Exception:
                pass
            self._claude_poll_after_id = None

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
        # `pending_auth` is the wizard's own marker — `username` is filled
        # in only after the OAuth callback returns from Last.fm.
        self._lastfm_enabled_var = ctk.BooleanVar(
            value=bool(lastfm.get("pending_auth") or lastfm.get("username"))
        )

        ctk.CTkCheckBox(
            self.content_frame,
            text="Enable Last.fm scrobbling",
            variable=self._lastfm_enabled_var,
        ).pack(pady=10)

        ctk.CTkLabel(
            self.content_frame,
            text=(
                "After setup, the app will open Last.fm in your browser\n"
                "to authorize scrobbling. Your Last.fm username is detected\n"
                "automatically once you allow access. You can also do this\n"
                "later in Settings."
            ),
            text_color="gray",
            font=ctk.CTkFont(size=12),
            justify="center",
        ).pack(pady=5)

    # The MusicBrainz dump: ~7 GB compressed, and the loaded tables plus
    # their indexes take roughly twice that again — 3× is the honest
    # headroom test, checked against the drive the data dir lives on.
    _MB_DUMP_GB = 7
    _MB_DUMP_HEADROOM = 3

    def _free_gb(self) -> float:
        try:
            return shutil.disk_usage(get_data_dir()).free / 1e9
        except OSError:
            return 0.0

    def _step_catalog(self):
        """The music-catalogue (MusicBrainz dump) opt-in.

        Pre-ticked when the disk can take it, because the dump is what makes
        the phantom layer — streaming and recommendations beyond the shelf —
        work at all, and a network where nobody holds it starves. Never
        silent: the checkbox is visible, the cost is stated, and Settings
        keeps a Delete button, which is what makes a bold default fair."""
        free = self._free_gb()
        needed = self._MB_DUMP_GB * self._MB_DUMP_HEADROOM
        enough = free >= needed

        ctk.CTkLabel(
            self.content_frame,
            text="Music catalogue",
            font=ctk.CTkFont(size=22, weight="bold"),
        ).pack(pady=(20, 6))

        ctk.CTkLabel(
            self.content_frame,
            text=(
                "Sautium can download the open MusicBrainz catalogue — the\n"
                "same database Picard uses — and keep it on this machine."
            ),
            justify="left", text_color="gray",
        ).pack(pady=(0, 12))

        for line in (
            "• Discovery beyond your shelf — every album an artist made, not"
            " only the ones you own, ready to stream and recommend.",
            "• Precise identity — releases, editions and recordings matched"
            " exactly, so search, radio and duplicates behave.",
            "• Better P2P coverage — canonized music is what peers can"
            " actually exchange analysis about; yours becomes shareable too.",
        ):
            ctk.CTkLabel(self.content_frame, text=line, justify="left",
                         wraplength=470, anchor="w").pack(
                             fill="x", padx=28, pady=2)

        self._mb_dump_var = ctk.BooleanVar(value=enough)
        chk = ctk.CTkCheckBox(
            self.content_frame,
            text=f"Download the catalogue after setup (~{self._MB_DUMP_GB} GB)",
            variable=self._mb_dump_var,
        )
        chk.pack(pady=(16, 6))
        if not enough:
            chk.configure(state="disabled")

        ctk.CTkLabel(
            self.content_frame,
            text=(
                f"Free space: {free:.0f} GB — enough (needs ~{needed} GB "
                f"with room to load)."
                if enough else
                f"Free space: {free:.0f} GB — not enough (needs ~{needed} GB). "
                f"You can enable this later in More → Library."
            ),
            text_color="gray" if enough else "#C86450",
            wraplength=470, justify="left",
        ).pack(pady=(0, 4))

        ctk.CTkLabel(
            self.content_frame,
            text=("Runs in the background after start, and can be removed at "
                  "any time from More → Library → MusicBrainz database."),
            text_color="gray", wraplength=470, justify="left",
        ).pack(pady=(6, 0))

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
        lastfm_cfg = self.config.get("lastfm", {})
        items = [
            ("Identity", account_text),
            ("AI Provider", {
                "none": "None (semantic search only)",
                "claude_code": "Claude Code",
                "anthropic": "Anthropic API",
                "openai": "OpenAI API",
            }.get(self.config.get("provider", "none"), self.config.get("provider", "none"))),
            # No HQPlayer line: setup no longer asks about it. Output is chosen
            # in Settings → Audio output, where the alternatives are visible.
            ("Audio output", "This device (change in Settings)"),
            (
                "Last.fm",
                f"{lastfm_cfg['username']} (connected)"
                if lastfm_cfg.get("session_key")
                else "Will authorize after start"
                if lastfm_cfg.get("pending_auth")
                else "Disabled",
            ),
            ("Accelerator", self._gpu_name if self._gpu_available else "CPU mode"),
            ("Music catalogue",
             f"Download after start (~{self._MB_DUMP_GB} GB)"
             if getattr(self, "_mb_dump_var", None)
             and self._mb_dump_var.get() else "Skip for now"),
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

        if getattr(self, "_mb_dump_var", None) is not None:
            self.config.setdefault("mb_slice", {})["download_dump"] = \
                bool(self._mb_dump_var.get())

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
                    self.ui_call(lambda: self._progress_label.configure(text=msg))

                # Install crypto dependencies if missing
                self._ensure_crypto_deps(progress)

                # Ensure the media CLI tools (ffmpeg, fpcalc, flac). Non-fatal:
                # setup continues; a missing tool only degrades that step
                # (ffmpeg -> no audio analysis, fpcalc -> no fingerprint).
                from desktop.db_init import ensure_media_tools
                _tools = ensure_media_tools(progress)
                _missing = [b for b, ok in _tools.items() if not ok]
                if _missing:
                    logger.warning(
                        "Media tools not installed: %s — related features "
                        "degraded until present", ", ".join(_missing)
                    )

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

                    # Email registration already happened at code entry
                    # (register_verified_email in phase 2). Fetch the birth
                    # certificate — idempotent, so a recreated account gets
                    # its original date back.
                    progress("Fetching birth certificate...")
                    from desktop.p2p.birth_cert import ensure_certificate
                    if ensure_certificate() is None:
                        logger.warning(
                            "Birth certificate fetch failed (offline?) — "
                            "will retry at P2P start"
                        )
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

                self.ui_call(self._init_complete)
            except Exception as e:
                logger.error(f"Initialization failed: {e}")
                # A partial init may have started PostgreSQL. Stop it so it
                # doesn't outlive the failed wizard and hold the port — a later
                # retry (or a data-dir wipe) would otherwise collide with it
                # ("could not bind ... Address already in use").
                try:
                    from desktop.db_init import stop_postgres
                    stop_postgres()
                except Exception as se:
                    logger.warning(f"Cleanup stop_postgres failed: {se}")
                self.ui_call(lambda: self._progress_label.configure(
                        text=f"Error: {e}", text_color="red"
                    ),
                )
                self.ui_call(lambda: self._progress_bar.stop())
                self.ui_call(lambda: self.btn_back.configure(state="normal"))

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
        self._cancel_claude_poll()
        self.destroy()
        # Quit the parent app since setup was not completed
        if self.master:
            self.master.destroy()
