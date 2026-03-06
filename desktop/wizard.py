"""
First-run setup wizard for Music AI DJ.

A multi-step customtkinter wizard that collects:
1. Welcome / intro
2. Music library path
3. AI provider selection + API key
4. HQPlayer settings
5. Summary + initialization
"""

import logging
import shutil
import threading
from pathlib import Path
from tkinter import filedialog
from typing import Optional

import customtkinter as ctk

from desktop.config_manager import load_config, save_config
from desktop.utils import detect_claude_cli, detect_cuda, detect_git

logger = logging.getLogger(__name__)


class SetupWizard(ctk.CTkToplevel):
    """First-run setup wizard."""

    def __init__(self, parent, on_complete=None):
        super().__init__(parent)

        self.title("Music AI DJ - Setup")
        self.geometry("600x500")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self.on_complete = on_complete
        self.config = load_config()
        self.current_step = 0
        self.steps = [
            self._step_welcome,
            self._step_provider,
            self._step_hqplayer,
            self._step_summary,
        ]

        # Detection results
        self._claude_available = detect_claude_cli()
        self._cuda_available, self._gpu_name, self._gpu_vram = detect_cuda()
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
        if self.current_step == 1:  # Provider
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

        if self.current_step == 2:  # HQPlayer
            self.config["hqplayer"]["enabled"] = self._hqp_enabled_var.get()
            if self._hqp_enabled_var.get():
                self.config["hqplayer"]["host"] = self._hqp_host_var.get().strip() or "localhost"
                try:
                    self.config["hqplayer"]["port"] = int(self._hqp_port_var.get())
                except ValueError:
                    self.config["hqplayer"]["port"] = 4321
            return True

        return True

    # ================================================================
    # Step implementations
    # ================================================================

    def _step_welcome(self):
        ctk.CTkLabel(
            self.content_frame,
            text="Music AI DJ",
            font=ctk.CTkFont(size=28, weight="bold"),
        ).pack(pady=(30, 10))

        ctk.CTkLabel(
            self.content_frame,
            text=(
                "AI-powered music library management.\n"
                "Search your collection by sound, mood, or description.\n"
                "Get intelligent recommendations from AI."
            ),
            font=ctk.CTkFont(size=14),
            justify="center",
        ).pack(pady=10)

        # System info
        info_frame = ctk.CTkFrame(self.content_frame)
        info_frame.pack(fill="x", pady=20, padx=40)

        items = [
            ("GPU", f"{self._gpu_name} ({self._gpu_vram}GB)" if self._cuda_available else "Not detected"),
            ("Claude CLI", "Available" if self._claude_available else "Not found"),
            ("Git", "Available" if self._git_available else "Not found"),
        ]
        for label, value in items:
            row = ctk.CTkFrame(info_frame, fg_color="transparent")
            row.pack(fill="x", padx=10, pady=2)
            ctk.CTkLabel(row, text=f"{label}:", width=100, anchor="w").pack(side="left")
            ctk.CTkLabel(row, text=value, anchor="w").pack(side="left", padx=5)

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
            value=self.config.get("provider", "anthropic")
        )

        providers_frame = ctk.CTkFrame(self.content_frame, fg_color="transparent")
        providers_frame.pack(fill="x", padx=20, pady=10)

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

        if provider == "claude_code":
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

    def _step_summary(self):
        ctk.CTkLabel(
            self.content_frame,
            text="Summary",
            font=ctk.CTkFont(size=22, weight="bold"),
        ).pack(pady=(20, 10))

        summary_frame = ctk.CTkFrame(self.content_frame)
        summary_frame.pack(fill="x", padx=20, pady=10)

        items = [
            ("AI Provider", self.config.get("provider", "anthropic")),
            (
                "HQPlayer",
                f"{self.config['hqplayer']['host']}:{self.config['hqplayer']['port']}"
                if self.config.get("hqplayer", {}).get("enabled")
                else "Disabled",
            ),
            ("GPU", f"{self._gpu_name}" if self._cuda_available else "CPU mode"),
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

    def _finish(self):
        """Save config and start initialization."""
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
                from desktop.db_init import full_init

                password = self.config.get("postgres_password", "changeme")
                port = self.config.get("ports", {}).get("postgres", 5432)

                def progress(msg):
                    self.after(0, lambda: self._progress_label.configure(text=msg))

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
