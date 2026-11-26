"""Settings & Tools — the launcher's dialog for what only the launcher can do.

Two tabs. "General" holds the launcher-only settings (ports) and their own
Save. "Backup & Restore" holds actions, not settings: the node backup and
its restore (docs/design/BACKUP.md — the file lands on this machine, the
password that keys it is typed here, and a restore replaces the database
the backend serves) and the identity file's export and import
(desktop/identity_file: the key leaves and enters by the machine's own
hands, never through the Web UI). AI provider,
HQPlayer connection and Last.fm scrobbling live in the Web UI, so there is
one source of truth for each of them, not two parallel configs.
"""

import logging
import threading
from pathlib import Path
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
                 on_cancel: Optional[Callable] = None,
                 backup_state: Optional[Callable] = None,
                 on_export: Optional[Callable] = None,
                 on_export_plan: Optional[Callable] = None,
                 on_import: Optional[Callable] = None,
                 on_merge: Optional[Callable] = None,
                 subscribe_job: Optional[Callable] = None,
                 identity_ops: Optional[dict] = None):
        super().__init__(parent)

        self.title(DIALOG_TITLE)
        self.geometry("550x730")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self.config = config.copy()
        self.on_save = on_save
        self.api_client = api_client
        # LauncherApp does the work behind the Backup & Restore tab. It runs
        # the backup CLI (desktop/backup_task.py) as one job per KIND —
        # "backup", "merge", "export", "import" can overlap, each in its own row here
        # — and _restore_from_backup stops the services, replaces the
        # database and identity, starts them again. This dialog collects
        # the file and the password, hands over, and stays open: the button
        # that started a job turns into its red Cancel and the row beneath it
        # shows the progress (the launcher's own scan button is the pattern).
        self.on_restore = on_restore
        self.on_backup = on_backup
        self.on_cancel = on_cancel
        self.backup_state = backup_state
        self.on_export = on_export
        self.on_export_plan = on_export_plan
        self.on_import = on_import
        self.on_merge = on_merge
        # LauncherApp's side of the identity file: "ui_call" (its thread
        # marshal), "another_identity(pubkey)" (its database's word, asked
        # off the Tk thread) and "place(contents)" (the services stopped, the
        # identity placed, the services started again on it)
        self.identity_ops = identity_ops
        self._rows: dict = {}
        # One voice per section: while a job runs its row speaks; otherwise
        # only the most recently started job keeps its result line — a
        # finished export must not read as the output of the import that
        # follows it (Valerii, 2026-09-14). Insertion order = start order.
        self._groups = {"backup": ("backup",), "merge": ("merge",), "sharing": ("export", "import")}
        self._states: dict = {}
        self._unsubscribe_job = subscribe_job(self._on_job_event) if subscribe_job else None
        self.protocol("WM_DELETE_WINDOW", self.destroy)

        # No button row under the tabs: Save belongs to the settings it
        # applies to and sits inside General; the tools act at once, and
        # the window closes like any other.
        self.tabview = ctk.CTkTabview(self, width=510, height=680)
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
        tab = self.tabview.tab(TAB_BACKUP)
        from desktop.backup_task import backup_dir, export_dir

        # Node backup: "Create backup…" runs the same CLI a Docker node's weekly
        # task runs (desktop/backup_task.py); the restore replaces the database
        # and identity with the file's (desktop/restore.py).
        self._section(tab, "Node backup", first=True)
        self._backup_status = self._status(tab, self._backup_status_text())
        row = self._button_row(tab)
        self._rows["backup"] = _JobRow(
            tab, ctk.CTkButton(row, text="Create backup…", width=150, command=self._create_backup,
                               fg_color="transparent", border_width=1),
            cancel_text="Cancel backup", on_cancel=lambda: self._cancel("backup"))
        self._rows["backup"].button.pack(side="left", padx=(0, 6))
        self._restore_button = ctk.CTkButton(
            row, text="Restore from backup…", width=170, command=self._restore_from_backup,
            fg_color="transparent", border_width=1)
        self._restore_button.pack(side="left", padx=(0, 6))
        ctk.CTkButton(row, text="Folder", width=80, command=lambda: self._open(backup_dir()),
                      fg_color="transparent", border_width=1).pack(side="left")
        self._hint(tab, (
            "Database (without the MusicBrainz catalogue) and identity in one file, "
            "encrypted with the account password. Restoring replaces this node's; "
            "the current database is kept as sautium__previous."))
        self._rows["backup"].mount(tab)

        # Product C: what the owner did on another machine of the same account
        # — a keyed union out of that machine's backup (desktop/backup_task.py
        # → `backup merge`); a second merge changes nothing.
        self._section(tab, "Merge my data from another node")
        row = self._button_row(tab)
        self._rows["merge"] = _JobRow(
            tab, ctk.CTkButton(row, text="Merge from backup…", width=150, command=self._merge_life,
                               fg_color="transparent", border_width=1),
            cancel_text="Cancel merge", on_cancel=lambda: self._cancel("merge"))
        self._rows["merge"].button.pack(side="left")
        self._hint(tab, (
            "Listening history and sessions, friends and messages, AI chats, gear and a few "
            "preferences from a backup of this account made on another machine — added to what "
            "is here, nothing replaced. Enrichment travels as a share export instead."))
        self._rows["merge"].mount(tab)

        # Sharing: every sealed record this node holds, as a file another
        # collector merges through the same verify-and-import gate a P2P
        # pull goes through — adding the albums, or enriching only theirs.
        self._section(tab, "Share enrichment with friends")
        row = self._button_row(tab)
        self._rows["export"] = _JobRow(
            tab, ctk.CTkButton(row, text="Export enrichment…", width=150, command=self._export_share,
                               fg_color="transparent", border_width=1),
            cancel_text="Cancel export", on_cancel=lambda: self._cancel("export"))
        self._rows["export"].button.pack(side="left", padx=(0, 6))
        self._rows["import"] = _JobRow(
            tab, ctk.CTkButton(row, text="Import enrichment…", width=170, command=self._import_share,
                               fg_color="transparent", border_width=1),
            cancel_text="Cancel import", on_cancel=lambda: self._cancel("import"))
        self._rows["import"].button.pack(side="left", padx=(0, 6))
        ctk.CTkButton(row, text="Folder", width=80, command=lambda: self._open(export_dir()),
                      fg_color="transparent", border_width=1).pack(side="left")
        self._hint(tab, (
            "The audio analysis and canon this node holds — every sealed record, signed "
            "by your node key — for the albums you own or listen to, or for named "
            "artists. A friend merges the file through the sync gate, adding your "
            "albums or enriching only theirs."))
        self._rows["export"].mount(tab)
        self._rows["import"].mount(tab)

        # The identity file: the key with its certificate, its proof and the
        # keys it retired — this node on another machine (SEC-S5 §16–§17)
        self._section(tab, "Identity")
        self._cert_status = self._status(tab, self._cert_status_text())
        row = self._button_row(tab)
        ctk.CTkButton(row, text="Export identity…", width=150, command=self._export_identity,
                      fg_color="transparent", border_width=1).pack(side="left", padx=(0, 6))
        ctk.CTkButton(row, text="Import identity…", width=170, command=self._import_identity,
                      fg_color="transparent", border_width=1).pack(side="left")
        self._hint(tab, (
            "This node's key, its certificate and the keys it retired, in one file: on another "
            "machine it is this node — its friends, its chat, its name."))

        self._states = dict((self.backup_state() or {}) if self.backup_state else {})
        self._render_rows()

    @staticmethod
    def _section(tab, title: str, *, first: bool = False) -> None:
        ctk.CTkLabel(tab, text=title, font=ctk.CTkFont(weight="bold")).pack(
            anchor="w", pady=((5, 2) if first else (18, 2)))

    @staticmethod
    def _status(tab, text: str):
        label = ctk.CTkLabel(tab, text=text, text_color="gray", font=ctk.CTkFont(size=11),
                             anchor="w", justify="left", wraplength=470)
        label.pack(anchor="w", padx=10)
        return label

    @staticmethod
    def _button_row(tab):
        row = ctk.CTkFrame(tab, fg_color="transparent")
        row.pack(fill="x", padx=10, pady=(6, 8))
        return row

    @staticmethod
    def _hint(tab, text: str) -> None:
        ctk.CTkLabel(
            tab, text=text, text_color="gray", font=ctk.CTkFont(size=11),
            anchor="w", justify="left", wraplength=470,
        ).pack(anchor="w", padx=10)

    def _render_rows(self) -> None:
        """Every row from self._states under the one-voice-per-section rule,
        then the restore button (a restore replaces the database under every
        job — none may run)."""
        for kinds in self._groups.values():
            running = [k for k in kinds if (self._states.get(k) or {}).get("running")]
            if running:
                shown = set(running)
            else:
                finished = [k for k in self._states if k in kinds and self._states[k].get("last_event")]
                shown = {finished[-1]} if finished else set()
            for k in kinds:
                self._rows[k].render(self._states.get(k) if k in shown else None)
        busy = any(r.running for r in self._rows.values())
        self._restore_button.configure(state="disabled" if busy else "normal")

    def _on_job_event(self, kind: str, ev: dict) -> None:
        if not self.winfo_exists() or kind not in self._rows:
            return
        running = ev.get("phase") not in ("done", "plan", "cancelled", "error")
        self._states[kind] = {"running": running, "last_event": ev}
        self._render_rows()
        if kind == "backup" and not running:
            self._backup_status.configure(text=self._backup_status_text())

    def _started(self, kind: str) -> None:
        self._states.pop(kind, None)                     # newest start goes last
        self._states[kind] = {"running": True, "last_event": None}
        self._render_rows()

    def _cancel(self, kind: str) -> None:
        if self.on_cancel:
            self.on_cancel(kind)

    @staticmethod
    def _open(path) -> None:
        from desktop.utils import show_in_file_manager
        path.mkdir(parents=True, exist_ok=True)
        show_in_file_manager(path)

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
            self._started("backup")
            self.on_backup(password)

        from desktop.backup_task import backup_dir
        PasswordDialog(
            self, title="Create backup",
            text=("The account password encrypts the file and opens it again on "
                  f"restore. The backup pauses while music plays. Written to {backup_dir()}"),
            on_ok=ready)

    def _export_share(self):
        if not self.on_export:
            return

        def ready(scope: str, artists: list):
            self._started("export")
            self.on_export(scope, artists)

        from desktop.backup_task import export_dir
        dialog = ExportDialog(self, on_ok=ready, folder=str(export_dir()))
        # The counts come from the CLI's `export --plan` (the launcher runs
        # it): an empty scope is shown as such instead of failing afterwards.
        if self.on_export_plan:
            self.on_export_plan(dialog.set_counts)

    def _import_share(self):
        from tkinter import filedialog

        from desktop.backup_task import export_dir
        if not self.on_import:
            return
        chosen = filedialog.askopenfilename(
            title="Sautium export", parent=self, initialdir=str(export_dir()),
            filetypes=[("Sautium export", "*.jsonl.gz"), ("All files", "*.*")])
        if not chosen:
            return
        self._started("import")
        self.on_import(chosen)

    def _merge_life(self):
        from tkinter import filedialog

        from desktop import node_backup as nb
        from desktop.backup_task import backup_dir
        if not self.on_merge:
            return
        chosen = filedialog.askopenfilename(
            title="Backup of this account", parent=self, initialdir=str(backup_dir()),
            filetypes=[("Sautium backup", "*" + nb.FILE_SUFFIX), ("All files", "*.*")])
        if not chosen:
            return
        try:
            with open(chosen, "rb") as fp:
                header, _ = nb.read_header(fp)
        except (nb.BackupError, OSError) as e:
            self._states["merge"] = {"running": False,
                                     "last_event": {"phase": "error", "message": str(e)}}
            self._render_rows()
            return

        def ready(password: str):
            self._started("merge")
            self.on_merge(chosen, password)

        who, when = header["node"]["username"], (header.get("created_at") or "")[:10]
        PasswordDialog(
            self, title="Merge from backup",
            text=(f"Backup of {who} from {when}. Its listens, sessions, friends, messages, chats, "
                  "gear and preferences are added to this node — nothing here is replaced, and "
                  "merging the same file twice changes nothing. The account password opens it."),
            on_ok=ready)

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

    def _export_identity(self):
        if self.identity_ops:
            IdentityExportDialog(self, ui_call=self.identity_ops["ui_call"])

    def _import_identity(self):
        if self.identity_ops:
            IdentityImportDialog(self, ops=self.identity_ops, on_placed=self.destroy)

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


class _JobRow:
    """One action's live state: its button becomes the red Cancel while the
    job runs (the launcher's scan button is the pattern), and a progress line
    plus a thin bar appear beneath the section only while there is something
    to show — a finished job leaves its result line, no bar."""

    def __init__(self, tab, button, *, cancel_text: str, on_cancel: Callable[[], None]):
        self.button = button
        self.kind_text = cancel_text.split(" ", 1)[1].capitalize()
        self._normal = {"text": button.cget("text"), "command": button.cget("command"),
                        "fg_color": "transparent", "hover_color": ("gray75", "gray25")}
        self._cancel = {"text": cancel_text, "command": on_cancel,
                        "fg_color": "#8B0000", "hover_color": "#A52A2A"}
        self.line = ctk.CTkLabel(tab, text="", text_color="gray", font=ctk.CTkFont(size=11),
                                 anchor="w", justify="left", wraplength=470)
        self.bar = ctk.CTkProgressBar(tab, width=470, height=6)
        self.bar.set(0)
        self.running = False

    def mount(self, tab) -> None:
        """Reserve the row's place after the section (packed on demand)."""
        self._anchor = ctk.CTkFrame(tab, fg_color="transparent", height=1)
        self._anchor.pack(fill="x")

    def render(self, state: Optional[dict]) -> None:
        from desktop.backup_task import describe_event
        running = bool(state and state.get("running"))
        ev = (state or {}).get("last_event")
        self.running = running
        self.button.configure(**(self._cancel if running else self._normal))
        if running:
            text = describe_event(ev, self.kind_text) if ev else f"{self.kind_text}: starting…"
        elif ev:
            text = describe_event(ev, self.kind_text)
        else:
            self.line.pack_forget()
            self.bar.pack_forget()
            return
        self.line.configure(text=text)
        self.line.pack(anchor="w", padx=10, pady=(6, 0), after=self._anchor)
        fraction = self._fraction(ev)
        if running:
            self.bar.pack(anchor="w", padx=10, pady=(4, 0), after=self.line)
            if fraction is None:
                if self.bar.cget("mode") != "indeterminate":
                    self.bar.configure(mode="indeterminate")
                    self.bar.start()
            else:
                self.bar.stop()
                self.bar.configure(mode="determinate")
                self.bar.set(fraction)
        else:
            self.bar.stop()
            self.bar.pack_forget()

    @staticmethod
    def _fraction(ev: Optional[dict]) -> Optional[float]:
        """Known when the event carries a total (tracks) or a backup's bytes
        can be sized against the last backup; None = indeterminate."""
        if not ev:
            return None
        if ev.get("tracks"):
            return min(1.0, (ev.get("tracks_done") or 0) / ev["tracks"])
        if ev.get("phase") == "dumping" and ev.get("bytes") is not None:
            from desktop.backup_task import latest_backup
            last = latest_backup()
            if last and last.get("size"):
                return min(0.99, ev["bytes"] / last["size"])
        return None


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


_EXPORT_SCOPES = (("analysed", "Every album with audio analysis here"),
                  ("engaged", "Albums I own or have listened to"),
                  ("owned", "Only albums I own"))


class ExportDialog(ctk.CTkToplevel):
    """What goes into a share export: the carry gates as three choices —
    what this node listens to, what it owns, or named artists. `on_ok(scope,
    artists)`; `artists` non-empty means the artists scope."""

    def __init__(self, parent, *, on_ok: Callable[[str, list], None], folder: str = ""):
        super().__init__(parent)
        self.title("Export enrichment")
        self.geometry("480x390")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self._on_ok = on_ok

        ctk.CTkLabel(self, text="Export enrichment",
                     font=ctk.CTkFont(size=18, weight="bold")).pack(pady=(16, 4))
        ctk.CTkLabel(
            self, text_color="gray", justify="left", wraplength=430,
            text=("Every sealed record this node holds for the chosen albums, as one "
                  "file a friend imports. Everything you own can be gigabytes; a few "
                  "artists is a quick file to send."
                  + (f" Written to {folder}" if folder else "")),
        ).pack(padx=24, anchor="w")

        self._scope = ctk.StringVar(value="engaged")
        self._radios = {}
        for value, label in _EXPORT_SCOPES + (("artists", "These artists:"),):
            radio = ctk.CTkRadioButton(
                self, text=label + (" (counting…)" if value != "artists" else ""),
                variable=self._scope, value=value, command=self._sync)
            radio.pack(padx=24, anchor="w", pady=(8, 0))
            self._radios[value] = radio
        self._artists = ctk.CTkEntry(self, width=430, placeholder_text="Comma-separated names")
        self._artists.pack(padx=24, pady=(4, 0))
        self._artists.bind("<Return>", lambda _e: self._submit())
        self._artists.bind("<Key>", lambda _e: self._scope.set("artists"))
        self._error = ctk.CTkLabel(self, text="", text_color="#ef4444")
        self._error.pack(padx=24, anchor="w")

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(fill="x", padx=24, pady=(4, 14), side="bottom")
        self._export_btn = ctk.CTkButton(btns, text="Export", width=120, command=self._submit,
                                         state="disabled")
        self._export_btn.pack(side="right")
        ctk.CTkButton(btns, text="Cancel", width=100, command=self.destroy,
                      fg_color="transparent", border_width=1).pack(side="right", padx=(0, 8))
        self._sync()

    def set_counts(self, ev: dict) -> None:
        """The `export --plan` result (Tk thread): album counts on the two
        broad scopes; an empty one cannot be picked; the default moves to the
        first scope with anything in it. A failed plan leaves the counts
        unknown and the choice to the user."""
        if not self.winfo_exists():
            return
        scopes = (ev or {}).get("scopes") if ev.get("phase") == "plan" else None
        if scopes is None:
            for value, base in _EXPORT_SCOPES:
                self._radios[value].configure(text=base)
        else:
            for value, base in _EXPORT_SCOPES:
                n = int(scopes.get(value) or 0)
                self._radios[value].configure(
                    text=f"{base} ({n:,})" if n else f"{base} (none)",
                    state="normal" if n else "disabled")
            # the narrower human scope first, the whole enrichment when a
            # streaming-only node has nothing owned or listened to
            first = next((v for v in ("engaged", "analysed", "owned") if scopes.get(v)), "artists")
            self._scope.set(first)
        self._export_btn.configure(state="normal")
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


_RED, _AMBER = "#ef4444", "#E8B06F"


class IdentityExportDialog(ctk.CTkToplevel):
    """The identity file's export (desktop/identity_file): a passphrase first
    — the phrase drawn for the person, shown and never masked, or one of
    their own held to the account password's edge — or none, with what that
    means said. The export goes on only after the passphrase is typed back
    once: it proves the phrase was kept, not only read. The lock's derivation
    takes minutes, in a thread of its own; its estimate is said once."""

    def __init__(self, parent, *, ui_call: Callable):
        from desktop import identity_file as idf
        from desktop import node_identity, password_strength
        super().__init__(parent)
        self.title("Export identity")
        self.geometry("560x660")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self._ui_call = ui_call
        self._dir = node_identity._identity_dir()
        self._inputs = idf.estimate_inputs(self._dir)
        self._offer = password_strength.phrase_offer()
        self._busy = False

        ctk.CTkLabel(self, text="Export identity", font=ctk.CTkFont(size=18, weight="bold")).pack(pady=(16, 4))
        ctk.CTkLabel(self, text_color="gray", justify="left", wraplength=510, text=(
            "This node's key, its certificate and the keys it retired, in one file: imported on "
            "another machine, that machine is this node.")).pack(padx=24, anchor="w")

        self._mode = ctk.StringVar(value="phrase")
        ctk.CTkRadioButton(self, text="Lock it with this phrase (recommended)", variable=self._mode,
                           value="phrase", command=self._sync).pack(padx=24, anchor="w", pady=(12, 0))
        offer = ctk.CTkFrame(self, fg_color="transparent")
        offer.pack(fill="x", padx=48, pady=(4, 0))
        self._phrase = ctk.CTkLabel(offer, text="", anchor="w",
                                    font=ctk.CTkFont(family="Courier", size=16, weight="bold"))
        self._phrase.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(offer, text="Another", width=80, command=self._another,
                      fg_color="transparent", border_width=1).pack(side="right")
        self._count = ctk.CTkLabel(self, text="", text_color="gray", anchor="w", font=ctk.CTkFont(size=12))
        self._count.pack(padx=48, anchor="w")

        ctk.CTkRadioButton(self, text="Lock it with a passphrase of my own", variable=self._mode,
                           value="own", command=self._sync).pack(padx=24, anchor="w", pady=(10, 0))
        self._own = ctk.CTkEntry(self, show="*", width=462, placeholder_text="passphrase")
        self._own.pack(padx=48, pady=(4, 0), anchor="w")
        self._own.bind("<KeyRelease>", lambda _e: self._measure())
        self._meter = ctk.CTkLabel(self, text="", anchor="w", justify="left", wraplength=462,
                                   font=ctk.CTkFont(size=12))
        self._meter.pack(padx=48, anchor="w")
        self._keep = ctk.BooleanVar(value=False)
        self._keep_box = ctk.CTkCheckBox(self, text="I understand, keep it", variable=self._keep)

        ctk.CTkRadioButton(self, text="No passphrase", variable=self._mode, value="none",
                           command=self._sync).pack(padx=24, anchor="w", pady=(10, 0))

        self._words = ctk.CTkLabel(self, text="", justify="left", wraplength=510)
        self._words.pack(padx=24, anchor="w", pady=(10, 0))
        self._back_label = ctk.CTkLabel(self, text="Type the passphrase back", anchor="w")
        self._back_label.pack(padx=24, anchor="w", pady=(8, 0))
        self._back = ctk.CTkEntry(self, show="*", width=510)
        self._back.pack(padx=24, pady=(2, 0))
        self._back.bind("<Return>", lambda _e: self._submit())
        self._status = ctk.CTkLabel(self, text="", justify="left", wraplength=510)
        self._status.pack(padx=24, anchor="w", pady=(6, 0))

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(fill="x", padx=24, pady=(4, 14), side="bottom")
        self._go = ctk.CTkButton(btns, text="Export…", width=120, command=self._submit)
        self._go.pack(side="right")
        self._close = ctk.CTkButton(btns, text="Cancel", width=100, command=self.destroy,
                                    fg_color="transparent", border_width=1)
        self._close.pack(side="right", padx=(0, 8))
        self._show_offer()
        self._sync()

    def _show_offer(self):
        self._phrase.configure(text=self._offer["phrase"])
        self._count.configure(text=self._offer["count"])

    def _another(self):
        from desktop import password_strength
        self._offer = password_strength.phrase_offer()
        self._show_offer()

    def _measure(self):
        from desktop import password_strength
        text = self._own.get()
        if not text:
            self._meter.configure(text="")
            self._keep_box.pack_forget()
            return
        v = password_strength.verdict(text, self._inputs)
        self._meter.configure(text=v["meter"], text_color={"refused": _RED, "word": _AMBER}.get(v["zone"], "gray"))
        if v["zone"] == "word":
            self._keep_box.pack(padx=48, anchor="w", after=self._meter)
        else:
            self._keep.set(False)
            self._keep_box.pack_forget()

    def _sync(self):
        from desktop import identity_file as idf
        mode = self._mode.get()
        self._own.configure(state="normal" if mode == "own" else "disabled")
        locked = mode != "none"
        self._words.configure(text=idf.PASSPHRASE_WORDS if locked else idf.PLAIN_WORDS)
        for widget in (self._back_label, self._back):
            widget.configure(state="normal" if locked else "disabled")

    def _passphrase(self) -> Optional[str]:
        """The passphrase chosen and typed back, or None for the plain file;
        raises ValueError with what to do."""
        from desktop import identity_file as idf
        mode = self._mode.get()
        if mode == "none":
            return None
        chosen = self._offer["phrase"] if mode == "phrase" else self._own.get()
        if mode == "own":
            try:
                idf.check_passphrase(chosen, self._inputs, self._keep.get())
            except idf.WeakPassphrase as e:
                tail = (" Tick “I understand, keep it” to keep it, or choose another."
                        if e.verdict["zone"] == "word" else " Use the phrase above, or another.")
                raise ValueError(e.verdict["door"] + tail) from None
        if self._back.get() != chosen:
            raise ValueError("Type the passphrase back exactly as it is: it is asked when the file is "
                             "imported, and nothing else opens it.")
        return chosen

    def _submit(self):
        from tkinter import filedialog

        from desktop import identity_file as idf
        if self._busy:
            return
        try:
            passphrase = self._passphrase()
        except ValueError as e:
            self._status.configure(text=str(e), text_color=_RED)
            return
        name = "sautium-identity-" + (self._inputs[0] or "node") + idf.FILE_SUFFIX
        path = filedialog.asksaveasfilename(title="Save the identity file", parent=self,
                                            initialfile=name, defaultextension=idf.FILE_SUFFIX,
                                            filetypes=[("Sautium identity", "*" + idf.FILE_SUFFIX)])
        if not path:
            return
        self._busy = True
        self._go.configure(state="disabled")
        self._close.configure(state="disabled")
        self._status.configure(text="Making the file…", text_color="gray")
        threading.Thread(target=self._work, args=(Path(path), passphrase, self._keep.get()),
                         daemon=True, name="identity-export").start()

    def _work(self, path: Path, passphrase: Optional[str], keep: bool):
        from desktop import identity_file as idf
        from desktop import password_strength
        from desktop.identity_birth import expected_seconds
        try:
            if passphrase is not None:
                took = password_strength.words(expected_seconds())
                self._say(f"Locking the file: {took} on this machine. Sautium goes on meanwhile.", "gray")
            data = idf.export(self._dir, passphrase, keep=keep)
            path.unlink(missing_ok=True)            # the save dialog asked about a file there
            idf.write_file(path, data)
        except (idf.IdentityFileError, OSError) as e:
            logger.error("The identity file was not written: %s", e)
            self._say(str(e), _RED, done=True)
            return
        self._say(f"Written to {path}.\n\n" + (idf.PASSPHRASE_WORDS if passphrase else idf.PLAIN_WORDS),
                  ("black", "white"), done=True)

    def _say(self, text: str, color, done: bool = False):
        def show():
            if not self.winfo_exists():
                return
            self._status.configure(text=text, text_color=color)
            if done:
                self._busy = False
                self._go.configure(state="disabled")
                self._close.configure(state="normal", text="Close")
        self._ui_call(show)


class IdentityImportDialog(ctk.CTkToplevel):
    """The identity file's import: the file chosen and what it says of
    itself, the folder's refusal (another identity here) before anything
    else, the database's word (another identity of record: the owner's
    question, asked before the derivation), the passphrase for a locked
    file, and the opening — minutes, in a thread, said once. Once open, the
    launcher places it with its services stopped (`ops["place"]`)."""

    def __init__(self, parent, *, ops: dict, on_placed: Callable[[], None]):
        super().__init__(parent)
        self.title("Import identity")
        self.geometry("540x420")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self._ops, self._on_placed = ops, on_placed
        self._data: Optional[bytes] = None
        self._facts: Optional[dict] = None
        self._busy = False

        ctk.CTkLabel(self, text="Import identity", font=ctk.CTkFont(size=18, weight="bold")).pack(pady=(16, 4))
        ctk.CTkLabel(self, text_color="gray", justify="left", wraplength=490, text=(
            "An identity file made by Export identity… on another machine. This node becomes that "
            "identity: its friends, its chat, its name. It is brought to a node that has none.")).pack(
            padx=24, anchor="w")
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=24, pady=(12, 4))
        self._file = ctk.CTkLabel(row, text="No file chosen", anchor="w", text_color="gray")
        self._file.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(row, text="Choose file…", width=120, command=self._choose,
                      fg_color="transparent", border_width=1).pack(side="right")
        self._pass_label = ctk.CTkLabel(self, text="The file's passphrase", anchor="w")
        self._pass = ctk.CTkEntry(self, show="*", width=490)
        self._pass.bind("<Return>", lambda _e: self._submit())
        self._status = ctk.CTkLabel(self, text="", justify="left", wraplength=490)
        self._status.pack(padx=24, anchor="w", pady=(6, 0))
        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(fill="x", padx=24, pady=(4, 14), side="bottom")
        self._go = ctk.CTkButton(btns, text="Import", width=170, command=self._submit, state="disabled")
        self._go.pack(side="right")
        ctk.CTkButton(btns, text="Cancel", width=100, command=self.destroy,
                      fg_color="transparent", border_width=1).pack(side="right", padx=(0, 8))

    def _choose(self):
        from tkinter import filedialog

        from desktop import identity_file as idf
        from desktop import node_identity
        if self._busy:
            return
        chosen = filedialog.askopenfilename(
            title="Sautium identity file", parent=self,
            filetypes=[("Sautium identity", "*" + idf.FILE_SUFFIX), ("All files", "*.*")])
        if not chosen:
            return
        self._file.configure(text=Path(chosen).name, text_color=("black", "white"))
        self._go.configure(state="disabled")
        self._pass_label.pack_forget()
        self._pass.pack_forget()
        try:
            with open(chosen, "rb") as f:
                data = f.read(idf.FILE_MAX + 1)
            facts = idf.summary(data)
            idf.here(node_identity._identity_dir(), facts["public_key_hex"])
        except (idf.IdentityFileError, OSError) as e:
            self._status.configure(text=str(e), text_color=_RED)
            return
        self._data, self._facts = data, facts
        self._busy = True
        self._status.configure(text="Reading what this node's database names…", text_color="gray")
        threading.Thread(target=self._ask_database, daemon=True, name="identity-import-check").start()

    def _ask_database(self):
        import psycopg2

        from desktop import identity_file as idf
        try:
            another = self._ops["another_identity"](self._facts["public_key_hex"])
        except psycopg2.Error as e:
            logger.error("The database could not be read before an identity's import: %s", e)
            words = f"This node's database could not be read: {e}"
            self._call(lambda: self._refuse(words))
            return
        facts = self._facts
        text = (f"The identity file of {facts['invite_code'] or facts['public_key_hex'][:16] + '…'}, "
                f"made {facts['created_at'][:10]}"
                + (", locked with a passphrase." if facts["locked"] else
                   ", with no passphrase.\n\n" + idf.PLAIN_WORDS))
        if another:
            text += "\n\n" + idf.ANOTHER_IDENTITY

        def show():
            self._busy = False
            self._status.configure(text=text, text_color=_AMBER if another else ("black", "white"))
            if facts["locked"]:
                self._pass_label.pack(padx=24, anchor="w", pady=(8, 0), before=self._status)
                self._pass.pack(padx=24, pady=(2, 0), before=self._status)
            self._go.configure(state="normal", text="Bring it here anyway" if another else "Import")
        self._call(show)

    def _submit(self):
        if self._busy or self._data is None:
            return
        passphrase = self._pass.get() if self._facts["locked"] else None
        if self._facts["locked"] and not passphrase:
            self._status.configure(text="Type the file's passphrase", text_color=_RED)
            return
        self._busy = True
        self._go.configure(state="disabled")
        threading.Thread(target=self._open, args=(passphrase,), daemon=True,
                         name="identity-import").start()

    def _open(self, passphrase: Optional[str]):
        from desktop import identity_file as idf
        from desktop import password_strength
        try:
            seconds = idf.opening_seconds(self._data)
            if seconds is not None:
                took = password_strength.words(seconds)
                self._call(lambda: self._status.configure(
                    text=f"Opening the file: {took} on this machine.", text_color="gray"))
            contents = idf.open_file(self._data, passphrase)
        except idf.IdentityFileError as e:
            words = str(e)
            self._call(lambda: self._fail(words))
            return

        def placed():
            self.destroy()
            self._on_placed()
            self._ops["place"](contents)
        self._call(placed)

    def _refuse(self, text: str):
        self._data = None
        self._fail(text)

    def _fail(self, text: str):
        self._busy = False
        self._status.configure(text=text, text_color=_RED)
        self._go.configure(state="normal" if self._data is not None else "disabled")

    def _call(self, fn):
        self._ops["ui_call"](lambda: self.winfo_exists() and fn())
