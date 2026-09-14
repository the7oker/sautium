"""Settings & Tools — the launcher's dialog for what only the launcher can do.

Two tabs. "General" holds the launcher-only settings (ports) and their own
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
                 backup_state: Optional[Callable] = None,
                 on_export: Optional[Callable] = None,
                 on_import: Optional[Callable] = None,
                 subscribe_job: Optional[Callable] = None):
        super().__init__(parent)

        self.title(DIALOG_TITLE)
        self.geometry("550x610")
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
        # Sharing (docs/design/BACKUP.md Product B) rides the same CLI:
        # _export_share / _import_share in the launcher.
        self.on_export = on_export
        self.on_import = on_import
        # The job's events, on the Tk thread, while this window is open: the
        # Activity panel on the Backup & Restore tab is where a running
        # backup / export / import shows its progress, its destination and
        # its Cancel — the window that started it stays open to show it.
        self._unsubscribe_job = subscribe_job(self._on_job_event) if subscribe_job else None
        self._action_buttons: list = []
        self.protocol("WM_DELETE_WINDOW", self.destroy)

        # No button row under the tabs: Save belongs to the settings it
        # applies to and sits inside General; the tools act at once, and
        # the window closes like any other.
        self.tabview = ctk.CTkTabview(self, width=510, height=560)
        self.tabview.pack(padx=20, pady=(10, 14))
        self.tabview.add(TAB_GENERAL)
        self.tabview.add(TAB_BACKUP)
        self._build_general_tab()
        self._build_backup_tab()

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
        self._hint(tab, "Save restarts the backend on the new ports.")
        ctk.CTkButton(tab, text="Save", width=100, command=self._save).pack(
            anchor="w", padx=10, pady=(8, 0))

    # ================================================================
    # Backup & Restore — actions on this node
    # ================================================================

    def _build_backup_tab(self):
        # Four sections and an activity panel: more than one fixed height
        # holds on every font scale, so the tab scrolls.
        tab = ctk.CTkScrollableFrame(self.tabview.tab(TAB_BACKUP), fg_color="transparent",
                                     width=480, height=540)
        tab.pack(fill="both", expand=True)

        # Node backup: "Create backup…" runs the same CLI a Docker node's weekly
        # task runs (desktop/backup_task.py); the restore replaces the database
        # and identity with the file's (desktop/restore.py).
        self._section(tab, "Node backup", first=True)
        self._backup_status = ctk.CTkLabel(
            tab, text=self._backup_status_text(),
            text_color="gray", font=ctk.CTkFont(size=11), anchor="w",
            justify="left", wraplength=450,
        )
        self._backup_status.pack(anchor="w", padx=10)
        backup_btns = ctk.CTkFrame(tab, fg_color="transparent")
        backup_btns.pack(fill="x", padx=10, pady=4)
        self._action_buttons.append(ctk.CTkButton(
            backup_btns, text="Create backup…", width=170,
            command=self._create_backup,
            fg_color="transparent", border_width=1,
        ))
        self._action_buttons[-1].pack(side="left", padx=(0, 6))
        self._action_buttons.append(ctk.CTkButton(
            backup_btns, text="Restore from backup…", width=170,
            command=self._restore_from_backup,
            fg_color="transparent", border_width=1,
        ))
        self._action_buttons[-1].pack(side="left")
        self._hint(tab, (
            "One encrypted file: the database (without the MusicBrainz catalogue) "
            "and this node's identity, keyed by the account password, written to "
            "the launcher's data folder — keep a copy elsewhere. Restoring replaces "
            "this node's database and identity with the file's; the current "
            "database is kept as sautium__previous."))

        # Sharing: what this node found out first-hand — sealed audio analysis,
        # bios, tags — as a file another collector merges through the same
        # verify-and-import gate a P2P pull goes through.
        self._section(tab, "Sharing")
        share_btns = ctk.CTkFrame(tab, fg_color="transparent")
        share_btns.pack(fill="x", padx=10, pady=4)
        self._action_buttons.append(ctk.CTkButton(
            share_btns, text="Export for a friend…", width=170,
            command=self._export_share,
            fg_color="transparent", border_width=1,
        ))
        self._action_buttons[-1].pack(side="left", padx=(0, 6))
        self._action_buttons.append(ctk.CTkButton(
            share_btns, text="Import from file…", width=170,
            command=self._import_share,
            fg_color="transparent", border_width=1,
        ))
        self._action_buttons[-1].pack(side="left")
        self._hint(tab, (
            "An export holds every sealed record this node has — audio analysis, "
            "bios, tags, each under its author's seal — for the albums you own or "
            "listen to, or for named artists; signed by your node key, not "
            "encrypted. A friend imports it through the same gate P2P sync uses, "
            "either adding your artists and albums to their streaming library or "
            "enriching only what they already have."))

        # Identity certificate transfer. The certificate is a public fact and
        # re-fetchable from the Worker (idempotent issuance), so export/import
        # is the offline fallback, not the primary path — and a node backup
        # carries it anyway.
        self._section(tab, "Identity certificate")
        self._cert_status = ctk.CTkLabel(
            tab, text=self._cert_status_text(),
            text_color="gray", font=ctk.CTkFont(size=11), anchor="w",
            justify="left", wraplength=450,
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

        # Activity: the one running job (backup, export or import) — what it
        # is doing, how far along, where its file goes, and Cancel. Fed by
        # the launcher's job events while this window is open; on open it
        # shows whatever is in flight.
        self._section(tab, "Activity")
        self._job_title = ctk.CTkLabel(tab, text="", anchor="w", font=ctk.CTkFont(weight="bold"))
        self._job_title.pack(anchor="w", padx=10)
        self._job_line = ctk.CTkLabel(
            tab, text="", text_color="gray", font=ctk.CTkFont(size=11), anchor="w",
            justify="left", wraplength=450)
        self._job_line.pack(anchor="w", padx=10)
        self._job_bar = ctk.CTkProgressBar(tab, width=450)
        self._job_bar.pack(anchor="w", padx=10, pady=(6, 2))
        self._job_bar.set(0)
        job_btns = ctk.CTkFrame(tab, fg_color="transparent")
        job_btns.pack(fill="x", padx=10, pady=4)
        self._job_cancel = ctk.CTkButton(
            job_btns, text="Cancel", width=110, command=self._cancel_backup,
            fg_color="transparent", border_width=1)
        self._job_cancel.pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            job_btns, text="Show folder", width=110, command=self._show_folder,
            fg_color="transparent", border_width=1).pack(side="left")
        from desktop.backup_task import backup_dir
        self._hint(tab, f"Files are written to {backup_dir()}")
        self._render_job(self.backup_state() if self.backup_state else None)

    @staticmethod
    def _section(tab, title: str, *, first: bool = False) -> None:
        ctk.CTkLabel(tab, text=title, font=ctk.CTkFont(weight="bold")).pack(
            anchor="w", pady=((5, 3) if first else (14, 3)))

    @staticmethod
    def _hint(tab, text: str) -> None:
        ctk.CTkLabel(
            tab, text=text, text_color="gray", font=ctk.CTkFont(size=11),
            anchor="w", justify="left", wraplength=450,
        ).pack(anchor="w", padx=10, pady=(2, 0))

    def _render_job(self, state: Optional[dict]) -> None:
        """The Activity panel and the action buttons from a job state
        ({"running", "job", "last_event"}) — idle when there is none."""
        from desktop.backup_task import describe_event
        running = bool(state and state.get("running"))
        job = (state or {}).get("job") or "Backup"
        ev = (state or {}).get("last_event")
        for btn in self._action_buttons:
            btn.configure(state="disabled" if running else "normal")
        self._job_cancel.configure(state="normal" if running else "disabled")
        if running:
            self._job_title.configure(text=f"{job} running")
            self._job_line.configure(text=describe_event(ev, job) if ev else f"{job}: starting…")
        elif ev:
            self._job_title.configure(text={"done": f"{job} done", "plan": f"{job} checked",
                                            "cancelled": f"{job} cancelled",
                                            "error": f"{job} failed"}.get(ev.get("phase"), job))
            self._job_line.configure(text=describe_event(ev, job))
        else:
            self._job_title.configure(text="Idle")
            self._job_line.configure(text="No backup, export or import running.")
        self._render_bar(ev, running)

    def _render_bar(self, ev: Optional[dict], running: bool) -> None:
        """Determinate when the event carries a total (tracks) or an estimate
        (a backup's bytes against the last backup's size), indeterminate for
        the phases that have none, still when nothing runs."""
        bar = self._job_bar
        fraction = None
        if ev:
            if ev.get("tracks"):
                fraction = min(1.0, (ev.get("tracks_done") or 0) / ev["tracks"])
            elif ev.get("phase") == "dumping" and ev.get("bytes") is not None:
                from desktop.backup_task import latest_backup
                last = latest_backup()
                if last and last.get("size"):
                    fraction = min(0.99, ev["bytes"] / last["size"])
            elif ev.get("phase") == "done":
                fraction = 1.0
            elif ev.get("phase") in ("cancelled", "error", "plan"):
                fraction = 0.0
        if not running:
            bar.stop()
            bar.configure(mode="determinate")
            bar.set(fraction if fraction is not None else 0.0)
        elif fraction is None:
            if bar.cget("mode") != "indeterminate":
                bar.configure(mode="indeterminate")
                bar.start()
        else:
            bar.stop()
            bar.configure(mode="determinate")
            bar.set(fraction)

    def _on_job_event(self, job: str, ev: dict) -> None:
        if not self.winfo_exists():
            return
        running = ev.get("phase") not in ("done", "plan", "cancelled", "error")
        self._render_job({"running": running, "job": job, "last_event": ev})
        if not running:
            self._backup_status.configure(text=self._backup_status_text())

    def _job_started(self, job: str) -> None:
        self._render_job({"running": True, "job": job, "last_event": None})

    def _show_folder(self):
        from desktop.backup_task import backup_dir, open_folder
        open_folder(backup_dir())

    def destroy(self):
        if self._unsubscribe_job:
            self._unsubscribe_job()
            self._unsubscribe_job = None
        super().destroy()

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
            self._job_started("Backup")
            self.on_backup(password)

        from desktop.backup_task import backup_dir
        PasswordDialog(
            self, title="Create backup",
            text=("The account password encrypts the file and opens it again on "
                  f"restore. The backup pauses while music plays. Written to {backup_dir()}"),
            on_ok=ready)

    def _cancel_backup(self):
        if self.on_backup_cancel:
            self.on_backup_cancel()

    def _export_share(self):
        if not self.on_export:
            return

        def ready(scope: str, artists: list):
            self._job_started("Export")
            self.on_export(scope, artists)

        from desktop.backup_task import backup_dir
        ExportDialog(self, on_ok=ready, folder=str(backup_dir()))

    def _import_share(self):
        from tkinter import filedialog
        if not self.on_import:
            return
        chosen = filedialog.askopenfilename(
            title="Sautium export", parent=self,
            filetypes=[("Sautium export", "*.jsonl.gz"), ("All files", "*.*")])
        if not chosen:
            return
        self._job_started("Import")
        self.on_import(chosen)

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

    def _save(self):
        """Persist port changes and notify the parent launcher (which restarts
        the backend); the dialog closes so the progress line is in view."""
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


class ExportDialog(ctk.CTkToplevel):
    """What goes into a share export: the carry gates as three choices —
    what this node listens to, what it owns, or named artists. `on_ok(scope,
    artists)`; `artists` non-empty means the artists scope."""

    def __init__(self, parent, *, on_ok: Callable[[str, list], None], folder: str = ""):
        super().__init__(parent)
        self.title("Export for a friend")
        self.geometry("480x360")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self._on_ok = on_ok

        ctk.CTkLabel(self, text="Export for a friend",
                     font=ctk.CTkFont(size=18, weight="bold")).pack(pady=(16, 4))
        ctk.CTkLabel(
            self, text_color="gray", justify="left", wraplength=430,
            text=("Every sealed record this node holds for the chosen albums, as one "
                  "file a friend imports. Everything you own can be gigabytes; a few "
                  "artists is a quick file to send."
                  + (f" Written to {folder}" if folder else "")),
        ).pack(padx=24, anchor="w")

        self._scope = ctk.StringVar(value="engaged")
        for value, label in (("engaged", "Albums I own or have listened to"),
                             ("owned", "Only albums I own"),
                             ("artists", "These artists:")):
            ctk.CTkRadioButton(self, text=label, variable=self._scope, value=value,
                               command=self._sync).pack(padx=24, anchor="w", pady=(8, 0))
        self._artists = ctk.CTkEntry(self, width=430, placeholder_text="Comma-separated names")
        self._artists.pack(padx=24, pady=(4, 0))
        self._artists.bind("<Return>", lambda _e: self._submit())
        self._artists.bind("<Key>", lambda _e: self._scope.set("artists"))
        self._error = ctk.CTkLabel(self, text="", text_color="#ef4444")
        self._error.pack(padx=24, anchor="w")

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(fill="x", padx=24, pady=(4, 14), side="bottom")
        ctk.CTkButton(btns, text="Export", width=120, command=self._submit).pack(side="right")
        ctk.CTkButton(btns, text="Cancel", width=100, command=self.destroy,
                      fg_color="transparent", border_width=1).pack(side="right", padx=(0, 8))
        self._sync()

    def _sync(self):
        self._artists.configure(state="normal" if self._scope.get() == "artists" else "disabled")

    def _submit(self):
        scope = self._scope.get()
        artists = [a.strip() for a in self._artists.get().split(",") if a.strip()] \
            if scope == "artists" else []
        if scope == "artists" and not artists:
            self._error.configure(text="Name at least one artist")
            return
        self.destroy()
        self._on_ok(scope, artists)
