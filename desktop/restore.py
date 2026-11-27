"""Restore a node from a backup — the launcher side.

`desktop/node_backup.py` knows the file and the database; this module binds
it to the launcher's bundled PostgreSQL (role `postgres` for the maintenance
work, `sautium` for the data — both under the config's postgres_password),
its identity dir and its config, and gives the two entry points one dialog:

  * Settings & Tools > Backup & Restore > "Restore from backup…" on a running node — the
    launcher stops P2P and the backend, this module replaces the database
    (the old one is kept as `sautium__previous`) and fills in what the
    identity folder lacks of the backup's identity, the launcher starts
    everything again;
  * the setup wizard, once the identity is on this machine.

The identity comes first (SEC-S5 §16): a backup opens with a seed this node
holds — its live key's, or a retired key's its archive keeps — and nothing
is typed. A backup of another identity is refused with the ways to bring
that identity here (its identity file, or the account made with the same
nickname and password); a restore never changes the node's identity.

The restore never runs beside a serving backend: the database is renamed
under it. Callers stop services first (LauncherApp does), and the CLI on
Docker says so.
"""

import logging
from pathlib import Path
from typing import Callable, Dict, Optional

import customtkinter as ctk
from cryptography.hazmat.primitives import serialization

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


def held_seeds() -> list:
    """The seeds this machine's identity opens a backup with
    (node_backup.seeds_held): Refused, in words, while it has no key."""
    d = identity_dir()
    key_path = d / "node_ed25519.key"
    key = (serialization.load_pem_private_key(key_path.read_bytes(), password=None)
           if key_path.exists() else None)
    return nb.seeds_held(key, d if d.is_dir() else None, to="restore")


def open_backup(path: Path) -> dict:
    """Open with the seeds this node holds and read the manifest — the
    pre-flight every entry point runs before anything is touched. Raises
    node_backup errors (Refused: no key here yet, or another identity's
    backup)."""
    with open(path, "rb") as fp:
        reader = nb.BackupReader(fp)
        reader.unlock_with_seeds(held_seeds())
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


def restore_launcher_node(path: Path, *, config: Optional[dict] = None, replace: bool = False,
                          progress: Optional[ProgressFn] = None) -> dict:
    """Replace the launcher's database with the backup's — a backup of this
    node's identity, opened with the seeds it holds — and fill in what the
    identity folder lacks of it (node_backup.write_identity). Services must
    be stopped by the caller."""
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

    seeds = held_seeds()
    identity_files: Dict[str, bytes] = {}
    with open(path, "rb") as fp:
        reader = nb.BackupReader(fp)
        reader.unlock_with_seeds(seeds)
        manifest = reader.read_manifest()
        result = nb.restore_database(reader, target, manifest=manifest, replace=replace,
                                     progress=show, file_size=total,
                                     identity_sink=identity_files.__setitem__)
    progress("Completing the identity folder…")
    result["identity"] = nb.write_identity(identity_dir(), identity_files,
                                           backup_key=reader.node["pubkey"],
                                           live_key=nb.seed_pubkey(seeds[0]))
    return result


def _said(e: Exception) -> str:
    return str(e) if isinstance(e, nb.BackupError) else f"Cannot read the file: {e}"


class RestoreDialog(ctk.CTkToplevel):
    """Pick a file, see whose it is and what is inside, confirm.

    The file is opened on the pick by the seeds this node holds
    (open_backup: no derivation, a read of the header and the small first
    member, so on the Tk thread), and the header's facts show with the
    manifest — or with why it cannot open: no identity here yet, the key
    still being made, another identity's backup. `on_ready(path, manifest)`
    fires once the file is open and compatible — the caller decides what
    happens next."""

    def __init__(self, parent, *, on_ready: Callable[[Path, dict], None],
                 confirm_replace: bool = False):
        super().__init__(parent)
        self.title("Restore from backup")
        self.geometry("520x300")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self._on_ready = on_ready
        self._confirm_replace = confirm_replace
        self._path: Optional[Path] = None
        self._manifest: Optional[dict] = None

        ctk.CTkLabel(self, text="Restore from backup",
                     font=ctk.CTkFont(size=18, weight="bold")).pack(pady=(16, 4))
        ctk.CTkLabel(
            self, text_color="gray", justify="left", wraplength=470,
            text=("A .sbk file written by Settings › Library › Backup. It opens on a node that "
                  "holds its identity: on a new machine, bring the identity here first (its "
                  "identity file, or the account made with the same nickname and password)."),
        ).pack(padx=24, anchor="w")

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=24, pady=(12, 4))
        self._file_label = ctk.CTkLabel(row, text="No file chosen", anchor="w", text_color="gray")
        self._file_label.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(row, text="Choose file…", width=120, command=self._choose,
                      fg_color="transparent", border_width=1).pack(side="right")

        self._status = ctk.CTkLabel(self, text="", text_color="gray", wraplength=470, justify="left")
        self._status.pack(padx=24, anchor="w", pady=(8, 0))

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(fill="x", padx=24, pady=(10, 14), side="bottom")
        self._btn_go = ctk.CTkButton(btns, width=140, command=self._finish, state="disabled",
                                     text="Restore now" if confirm_replace else "Use this backup")
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
        path = Path(chosen)
        self._path, self._manifest = path, None
        self._btn_go.configure(state="disabled")
        self._file_label.configure(text=path.name, text_color=("black", "white"))
        try:
            with open(path, "rb") as fp:
                header, _ = nb.read_header(fp)
        except (nb.BackupError, OSError) as e:
            self._status.configure(text=_said(e), text_color="#ef4444")
            return
        facts = (f"Backup of {header['node']['username']} from "
                 f"{header.get('created_at', '')[:10]}, key {header['node']['pubkey'][:12]}…")
        try:
            manifest = open_backup(path)
        except (nb.BackupError, OSError) as e:
            self._status.configure(text=f"{facts}\n\n{_said(e)}", text_color="#ef4444")
            return
        self._manifest = manifest
        text = f"{facts}\nContents: " + describe(manifest)
        if self._confirm_replace:
            text += ("\n\nRestoring replaces everything on this node; the current database is "
                     "kept as sautium__previous until the next restore.")
        self._status.configure(text=text, text_color=("black", "white"))
        self._btn_go.configure(state="normal")

    def _finish(self) -> None:
        if self._manifest is None:
            return
        path, manifest = self._path, self._manifest
        self.destroy()
        self._on_ready(path, manifest)
