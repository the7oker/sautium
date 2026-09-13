"""Restore a node from a backup — the launcher side.

`desktop/node_backup.py` knows the file and the database; this module binds
it to the launcher's bundled PostgreSQL (role `postgres` for the maintenance
work, `sautium` for the data — both under the config's postgres_password),
its identity dir and its config, and gives the two entry points one dialog:

  * Settings > Maintenance > "Restore from backup…" on a running node — the
    launcher stops P2P and the backend, this module replaces the database
    (the old one is kept as `sautium__previous`) and writes the identity,
    the launcher starts everything again;
  * the setup wizard's "Restore from a backup…" beside the account fields —
    the dialog only opens and checks the file; the restore itself runs at
    the end of the wizard, once the fresh cluster exists.

The restore never runs beside a serving backend: the database is renamed
under it. Callers stop services first (LauncherApp does), and the CLI on
Docker says so.
"""

import logging
import threading
from pathlib import Path
from typing import Callable, Dict, Optional

import customtkinter as ctk

from desktop import node_backup as nb
from desktop.config_manager import get_config_dir, load_config

logger = logging.getLogger(__name__)

ProgressFn = Callable[[str], None]


def launcher_target(config: Optional[dict] = None, dbname: str = "sautium") -> nb.PgTarget:
    from desktop.db_init import get_pg_bin_dir
    config = config or load_config()
    password = config.get("postgres_password", "changeme")
    port = config.get("ports", {}).get("postgres", 15432)
    try:
        pg_bin = get_pg_bin_dir()
    except FileNotFoundError:
        pg_bin = None
    return nb.PgTarget(host="localhost", port=port, dbname=dbname, user="sautium",
                       password=password, admin_user="postgres", admin_password=password,
                       pg_bin=pg_bin)


def identity_dir() -> Path:
    return get_config_dir() / "node_identity"


def open_backup(path: Path, password: str) -> dict:
    """Unlock and read the manifest — the pre-flight both entry points run
    before anything is touched. Raises node_backup errors."""
    with open(path, "rb") as fp:
        reader = nb.BackupReader(fp)
        reader.unlock(password)
        manifest = reader.read_manifest()
    nb.check_compatible(manifest)
    return manifest


def describe(manifest: dict) -> str:
    db = manifest.get("database") or {}
    tables = db.get("tables") or {}
    node = manifest.get("node") or {}
    return (f"{node.get('username', '?')} · {manifest.get('created_at', '')[:10]} · "
            f"{tables.get('media_files', 0):,} files · {tables.get('tracks', 0):,} tracks · "
            f"{tables.get('listening_history', 0):,} listens")


def restore_launcher_node(path: Path, password: str, *, config: Optional[dict] = None,
                          replace: bool = False, identity: bool = True,
                          progress: Optional[ProgressFn] = None) -> dict:
    """Replace the launcher's database with the backup's and (by default)
    make this machine the node. Services must be stopped by the caller."""
    progress = progress or (lambda _m: None)
    target = launcher_target(config)
    total = path.stat().st_size

    def show(phase: str, **f) -> None:
        if phase == "restoring":
            done = f.get("bytes") or 0
            progress(f"Restoring database… {done * 100 // max(total, 1)}%")
        else:
            progress({"preparing": "Preparing a fresh database…",
                      "migrating": "Applying newer migrations…",
                      "swapping": "Switching to the restored database…"}.get(phase, phase))

    identity_files: Dict[str, bytes] = {}
    with open(path, "rb") as fp:
        reader = nb.BackupReader(fp)
        reader.unlock(password)
        manifest = reader.read_manifest()
        result = nb.restore_database(reader, target, manifest=manifest, replace=replace,
                                     progress=show, file_size=total,
                                     identity_sink=identity_files.__setitem__)
    if identity:
        progress("Writing identity…")
        result["identity"] = nb.write_identity(identity_dir(), identity_files,
                                               username=manifest["node"]["username"],
                                               password=password)
        # A Docker node with env credentials keeps no node_info.json, so its
        # backup carries none; the launcher needs one — the same pair derives
        # the same key, so this is the account, not a new one.
        if not (identity_dir() / "node_info.json").exists():
            from desktop.node_identity import create_account
            create_account(manifest["node"]["username"], password)
    return result


class RestoreDialog(ctk.CTkToplevel):
    """Pick a file, type the password, see what is inside, confirm.

    `on_ready(path, password, manifest)` fires on the Tk thread once the file
    is open and compatible — the caller decides what happens next (the wizard
    remembers it, the launcher restores now). The KDF runs off the UI thread
    (~1–2 s, 256 MiB)."""

    def __init__(self, parent, *, on_ready: Callable[[Path, str, dict], None],
                 confirm_replace: bool = False):
        super().__init__(parent)
        self.title("Restore from backup")
        self.geometry("520x330")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self._on_ready = on_ready
        self._confirm_replace = confirm_replace
        self._path: Optional[Path] = None
        self._manifest: Optional[dict] = None
        self._busy = False

        ctk.CTkLabel(self, text="Restore from backup",
                     font=ctk.CTkFont(size=18, weight="bold")).pack(pady=(16, 4))
        ctk.CTkLabel(
            self, text_color="gray", justify="left", wraplength=470,
            text=("A .sbk file written by Settings › Library › Backup. It is opened with the "
                  "account password of the node that wrote it; that identity becomes "
                  "this machine's."),
        ).pack(padx=24, anchor="w")

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=24, pady=(12, 4))
        self._file_label = ctk.CTkLabel(row, text="No file chosen", anchor="w", text_color="gray")
        self._file_label.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(row, text="Choose file…", width=120, command=self._choose,
                      fg_color="transparent", border_width=1).pack(side="right")

        ctk.CTkLabel(self, text="Account password", anchor="w").pack(padx=24, anchor="w", pady=(8, 0))
        self._password = ctk.CTkEntry(self, show="*", width=470)
        self._password.pack(padx=24, pady=(2, 6))
        self._password.bind("<Return>", lambda _e: self._open())

        self._status = ctk.CTkLabel(self, text="", text_color="gray", wraplength=470, justify="left")
        self._status.pack(padx=24, anchor="w")

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(fill="x", padx=24, pady=(10, 14), side="bottom")
        self._btn_go = ctk.CTkButton(btns, text="Open backup", width=140, command=self._open)
        self._btn_go.pack(side="right")
        ctk.CTkButton(btns, text="Cancel", width=100, command=self.destroy,
                      fg_color="transparent", border_width=1).pack(side="right", padx=(0, 8))

    def _choose(self) -> None:
        from tkinter import filedialog
        chosen = filedialog.askopenfilename(
            title="Sautium backup", parent=self,
            filetypes=[("Sautium backup", "*" + nb.FILE_SUFFIX), ("All files", "*.*")])
        if not chosen:
            return
        self._path = Path(chosen)
        self._manifest = None
        self._file_label.configure(text=self._path.name, text_color=("black", "white"))
        try:
            with open(self._path, "rb") as fp:
                header, _ = nb.read_header(fp)
            self._status.configure(
                text=f"Backup of {header['node']['username']} from {header.get('created_at', '')[:10]}",
                text_color="gray")
            self._btn_go.configure(text="Open backup")
        except nb.BackupError as e:
            self._status.configure(text=str(e), text_color="#ef4444")

    def _open(self) -> None:
        if self._busy:
            return
        if self._manifest is not None:
            self._finish()
            return
        if self._path is None:
            self._status.configure(text="Choose a backup file first", text_color="#ef4444")
            return
        password = self._password.get()
        if not password:
            self._status.configure(text="Type the account password", text_color="#ef4444")
            return
        self._busy = True
        self._btn_go.configure(state="disabled")
        self._status.configure(text="Deriving the key…", text_color="gray")
        path = self._path

        def work() -> None:
            try:
                manifest = open_backup(path, password)
            except nb.BackupError as e:
                self.after(0, lambda: self._fail(str(e)))
                return
            except OSError as e:
                self.after(0, lambda: self._fail(f"Cannot read the file: {e}"))
                return
            self.after(0, lambda: self._opened(manifest, password))

        threading.Thread(target=work, daemon=True).start()

    def _fail(self, message: str) -> None:
        self._busy = False
        self._btn_go.configure(state="normal")
        self._status.configure(text=message, text_color="#ef4444")

    def _opened(self, manifest: dict, password: str) -> None:
        self._busy = False
        self._manifest = manifest
        self._opened_password = password
        text = "Contents: " + describe(manifest)
        if self._confirm_replace:
            text += ("\n\nRestoring replaces everything on this node; the current database is "
                     "kept as sautium__previous until the next restore.")
        self._status.configure(text=text, text_color=("black", "white"))
        self._btn_go.configure(state="normal",
                               text="Restore now" if self._confirm_replace else "Use this backup")
        self._password.configure(state="disabled")

    def _finish(self) -> None:
        path, password, manifest = self._path, self._opened_password, self._manifest
        self.destroy()
        self._on_ready(path, password, manifest)
