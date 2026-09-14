"""Settings & Tools — the launcher's dialog for what only the launcher can do.

Two tabs. "General" holds the launcher-only settings (ports), saved with
Save. "Backup & Restore" holds actions, not settings: the node backup and
its restore (docs/design/BACKUP.md — the file lands on this machine, the
password that keys it is typed here, and a restore replaces the database
the backend serves) and the identity certificate transfer. AI provider,
HQPlayer connection and Last.fm scrobbling live in the Web UI, so there is
one source of truth for each of them, not two parallel configs.
"""

import logging
from typing import Callable, Optional

import customtkinter as ctk

from desktop.api_client import BackendAPIClient
from desktop.config_manager import save_config

logger = logging.getLogger(__name__)

DIALOG_TITLE = "Settings & Tools"
TAB_GENERAL = "General"
TAB_BACKUP = "Backup & Restore"


class SettingsDialog(ctk.CTkToplevel):
    def __init__(self, parent, config: dict, on_save: Optional[Callable] = None,
                 api_client: Optional[BackendAPIClient] = None,
                 on_restore: Optional[Callable] = None,
                 on_backup: Optional[Callable] = None,
                 on_backup_cancel: Optional[Callable] = None,
                 backup_state: Optional[Callable] = None):
        super().__init__(parent)

        self.title(DIALOG_TITLE)
        self.geometry("550x560")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self.config = config.copy()
        self.on_save = on_save
        self.api_client = api_client
        # LauncherApp does the work behind the Backup & Restore tab: _create_backup
        # runs the CLI (desktop/backup_task.py) and shows its progress in the
        # launcher window, backup_state() says whether one is running so the tab
        # offers Cancel instead of Create, _restore_from_backup stops the services,
        # replaces the database and identity, starts them again. This dialog only
        # collects the file and the password and hands over.
        self.on_restore = on_restore
        self.on_backup = on_backup
        self.on_backup_cancel = on_backup_cancel
        self.backup_state = backup_state

        self.tabview = ctk.CTkTabview(self, width=510, height=460)
        self.tabview.pack(padx=20, pady=(10, 0))
        self.tabview.add(TAB_GENERAL)
        self.tabview.add(TAB_BACKUP)
        self._build_general_tab()
        self._build_backup_tab()

        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(fill="x", padx=20, pady=10)
        ctk.CTkButton(
            btn_frame, text="Save", width=100,
            command=self._save,
        ).pack(side="right", padx=5)
        ctk.CTkButton(
            btn_frame, text="Close", width=100,
            command=self.destroy,
            fg_color="transparent", border_width=1,
        ).pack(side="right", padx=5)

    # ================================================================
    # General — launcher-only settings
    # ================================================================

    def _build_general_tab(self):
        tab = self.tabview.tab(TAB_GENERAL)
        ports = self.config.get("ports", {})

        self._section(tab, "Ports", first=True)
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
        self._hint(tab, "Changing ports requires a restart; Save applies them.")

    # ================================================================
    # Backup & Restore — actions on this node
    # ================================================================

    def _build_backup_tab(self):
        tab = self.tabview.tab(TAB_BACKUP)
        running = bool((self.backup_state() or {}).get("running")) if self.backup_state else False

        # Node backup: "Create backup…" runs the same CLI a Docker node's weekly
        # task runs (desktop/backup_task.py); the restore replaces the database
        # and identity with the file's (desktop/restore.py).
        self._section(tab, "Node backup", first=True)
        self._backup_status = ctk.CTkLabel(
            tab, text=self._backup_status_text(),
            text_color="gray", font=ctk.CTkFont(size=11), anchor="w",
            justify="left", wraplength=480,
        )
        self._backup_status.pack(anchor="w", padx=10)
        backup_btns = ctk.CTkFrame(tab, fg_color="transparent")
        backup_btns.pack(fill="x", padx=10, pady=4)
        ctk.CTkButton(
            backup_btns, width=170,
            text="Cancel backup" if running else "Create backup…",
            command=self._cancel_backup if running else self._create_backup,
            fg_color="transparent", border_width=1,
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            backup_btns, text="Restore from backup…", width=170,
            command=self._restore_from_backup,
            fg_color="transparent", border_width=1,
            state="disabled" if running else "normal",
        ).pack(side="left")
        self._hint(tab, (
            "One encrypted file: the database (without the MusicBrainz catalogue) "
            "and this node's identity, keyed by the account password, written to "
            "the launcher's data folder — keep a copy elsewhere. Restoring replaces "
            "this node's database and identity with the file's; the current "
            "database is kept as sautium__previous."))

        # Identity certificate transfer. The certificate is a public fact and
        # re-fetchable from the Worker (idempotent issuance), so export/import
        # is the offline fallback, not the primary path — and a node backup
        # carries it anyway.
        self._section(tab, "Identity certificate")
        self._cert_status = ctk.CTkLabel(
            tab, text=self._cert_status_text(),
            text_color="gray", font=ctk.CTkFont(size=11), anchor="w",
            justify="left", wraplength=480,
        )
        self._cert_status.pack(anchor="w", padx=10)
        cert_btns = ctk.CTkFrame(tab, fg_color="transparent")
        cert_btns.pack(fill="x", padx=10, pady=4)
        ctk.CTkButton(
            cert_btns, text="Export certificate…", width=170,
            command=self._export_birth_cert,
            fg_color="transparent", border_width=1,
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            cert_btns, text="Import certificate…", width=170,
            command=self._import_birth_cert,
            fg_color="transparent", border_width=1,
        ).pack(side="left")
        self._hint(tab, (
            "The birth certificate is the network's record of when this identity "
            "was issued. It is fetched automatically and included in every node "
            "backup; export/import is the offline fallback."))

    @staticmethod
    def _section(tab, title: str, *, first: bool = False) -> None:
        ctk.CTkLabel(tab, text=title, font=ctk.CTkFont(weight="bold")).pack(
            anchor="w", pady=((5, 3) if first else (14, 3)))

    @staticmethod
    def _hint(tab, text: str) -> None:
        ctk.CTkLabel(
            tab, text=text, text_color="gray", font=ctk.CTkFont(size=11),
            anchor="w", justify="left", wraplength=480,
        ).pack(anchor="w", padx=10, pady=(2, 0))

    def _backup_status_text(self) -> str:
        from desktop.backup_task import backup_dir, fmt_bytes, latest_backup
        state = self.backup_state() if self.backup_state else None
        if state and state.get("running"):
            return "Backup running — progress in the launcher window."
        last = latest_backup()
        if last is None:
            return f"No backups yet. Folder: {backup_dir()}"
        when = (last.get("created_at") or "")[:10]
        return f"Last backup: {when} · {fmt_bytes(last['size'])} · {last['name']}"

    def _create_backup(self):
        if not self.on_backup:
            return

        def ready(password: str):
            self.destroy()
            self.on_backup(password)

        PasswordDialog(
            self, title="Create backup",
            text=("The account password encrypts the file and opens it again on "
                  "restore. The backup pauses while music plays."),
            on_ok=ready)

    def _cancel_backup(self):
        if self.on_backup_cancel:
            self.on_backup_cancel()
        self.destroy()

    def _restore_from_backup(self):
        from desktop.restore import RestoreDialog

        def ready(path, password, manifest):
            self.destroy()
            if self.on_restore:
                self.on_restore(path, password, manifest)

        RestoreDialog(self, on_ready=ready, confirm_replace=True)

    def _cert_status_text(self) -> str:
        from desktop.p2p.birth_cert import load_certificate, load_proof
        cert = load_certificate()
        if cert:
            if cert["method"] == "email":
                work = "email-verified, no proof needed"
            elif load_proof() is not None:
                work = "proof ready"
            else:
                work = "proof pending (mined in the background while P2P runs)"
            return (f"Issued {cert['issued_at']} ({cert['method']}) — {work}")
        return "None yet (fetched automatically at P2P start)"

    def _export_birth_cert(self):
        from tkinter import filedialog

        from desktop.p2p.birth_cert import export_certificate
        path = filedialog.asksaveasfilename(
            title="Export Birth Certificate",
            defaultextension=".json",
            initialfile="sautium_birth_certificate.json",
            filetypes=[("JSON", "*.json")],
        )
        if not path:
            return
        ok = export_certificate(path)
        self._cert_status.configure(
            text="Certificate exported." if ok
            else "Export failed — no certificate stored yet.",
            text_color="#22c55e" if ok else "#ef4444",
        )

    def _import_birth_cert(self):
        from tkinter import filedialog

        from desktop.p2p.birth_cert import import_certificate
        path = filedialog.askopenfilename(
            title="Import Birth Certificate",
            filetypes=[("JSON", "*.json")],
        )
        if not path:
            return
        ok = import_certificate(path)
        self._cert_status.configure(
            text=self._cert_status_text() if ok
            else "Import failed — invalid certificate or wrong identity.",
            text_color="#22c55e" if ok else "#ef4444",
        )

    # ================================================================
    # Save — the General tab
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


class PasswordDialog(ctk.CTkToplevel):
    """A masked entry and two buttons; `on_ok(password)` on Enter or OK."""

    def __init__(self, parent, *, title: str, text: str, on_ok: Callable[[str], None]):
        super().__init__(parent)
        self.title(title)
        self.geometry("460x230")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self._on_ok = on_ok

        ctk.CTkLabel(self, text=title, font=ctk.CTkFont(size=18, weight="bold")).pack(pady=(16, 4))
        ctk.CTkLabel(self, text=text, text_color="gray", justify="left",
                     wraplength=410).pack(padx=24, anchor="w")
        ctk.CTkLabel(self, text="Account password", anchor="w").pack(padx=24, anchor="w", pady=(10, 0))
        self._entry = ctk.CTkEntry(self, show="*", width=410)
        self._entry.pack(padx=24, pady=(2, 4))
        self._entry.bind("<Return>", lambda _e: self._submit())
        self._error = ctk.CTkLabel(self, text="", text_color="#ef4444")
        self._error.pack(padx=24, anchor="w")

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(fill="x", padx=24, pady=(4, 14), side="bottom")
        ctk.CTkButton(btns, text="Start", width=120, command=self._submit).pack(side="right")
        ctk.CTkButton(btns, text="Cancel", width=100, command=self.destroy,
                      fg_color="transparent", border_width=1).pack(side="right", padx=(0, 8))
        self.after(100, self._entry.focus_set)

    def _submit(self):
        password = self._entry.get()
        if not password:
            self._error.configure(text="Type the account password")
            return
        self.destroy()
        self._on_ok(password)
