"""One background job per dump family — the MusicBrainz subset
(``mb_dump_load``) and the ListenBrainz statistics (``lb_dump_load``) — with
the in-memory progress the Library payload shows and the SSE wake that
animates it. Two instances of one class, because the state machine, the
phase-to-text mapping, the disk gate, the auto-update check and the delete
guard are family-agnostic; only the loader module, the settings prefix and
the post-load hook differ.

Jobs run ONE AT A TIME on a single worker thread: the wizard can tick both
downloads, and two bulk loads on one volume (7 GB + 21 GB archives, index
builds fighting for maintenance memory) is the failure mode, not a feature.
The second job waits in the queue and the Library block says "Queued…".

Settings access and the wake are injected (``read``/``write``/``notify``)
because this module is imported by routers/settings, which owns them.
"""

import importlib
import logging
import queue
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Dict, FrozenSet, Optional

logger = logging.getLogger("dump_job")


class DumpBusy(RuntimeError):
    """A job of this family is already queued or running."""


class InsufficientDisk(RuntimeError):
    """The loader's disk budget refused the job before anything was fetched."""


_queue: "queue.Queue" = queue.Queue()
_worker_lock = threading.Lock()
_worker: Optional[threading.Thread] = None


def _drain() -> None:
    while True:
        job, force = _queue.get()
        try:
            job.run(force)
        finally:
            _queue.task_done()


def _ensure_worker() -> None:
    global _worker
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_drain, name="dump-jobs", daemon=True)
            _worker.start()


class DumpJob:
    """The loader module must expose ``download_and_load(progress_cb, force)``,
    ``latest_version()``, ``loaded_version()``, ``stats()``, ``disk_budget()``
    and ``delete_dump()`` — the contract both loaders implement."""

    _IDLE_PHASES: FrozenSet[str] = frozenset({"done", "", "queued", "error"})

    def __init__(self, family: str, loader_module: str, settings_prefix: str, *,
                 read: Callable[[str], Any], write: Callable[[str, Any], None],
                 notify: Callable[[], None],
                 post_load: Optional[Callable[["DumpJob", Dict], None]] = None,
                 passive_phases: FrozenSet[str] = frozenset()):
        self.family = family
        self.prefix = settings_prefix
        self._loader_name = loader_module
        self._read, self._write, self._notify = read, write, notify
        self._post_load = post_load
        self._passive = frozenset(passive_phases)
        self.state: Dict[str, Any] = {"running": False, "phase": "", "progress": "",
                                      "pct": None, "error": None}
        self.lock = threading.Lock()

    @property
    def loader(self):
        return importlib.import_module(self._loader_name)

    # ── state ────────────────────────────────────────────────────────────────

    def load_active(self) -> bool:
        """True while the job is in a DATA-MUTATING phase (downloading /
        loading / indexing), so scan, background enrichment and AI canon
        defer — otherwise they read a half-built table. False while queued,
        done, failed, and in the family's passive phases (the MB job's own
        post-load canon runs in 'canonicalizing' and must not block itself)."""
        with self.lock:
            return (bool(self.state["running"])
                    and self.state["phase"] not in self._IDLE_PHASES | self._passive)

    def set_phase(self, phase: str, progress: str, pct: Optional[int] = None) -> None:
        with self.lock:
            self.state.update(phase=phase, progress=progress, pct=pct)
        self._notify()

    def progress(self, update: Dict) -> None:
        """Loader callback → human progress line + numeric pct (None =
        indeterminate) for the bar + SSE wake."""
        phase = update.get("phase")
        pct = None
        text = None
        table = update.get("table")
        suffix = f" ({table})" if table else ""
        if phase == "checking":
            text = "Checking for updates…"
        elif phase == "downloading":
            pct = update.get("pct")
            text = (f"Downloading… {pct or 0}% "
                    f"({update.get('downloaded_mb', 0)}/{update.get('total_mb', 0)} MB)")
        elif phase == "verifying":
            pct = update.get("pct")
            text = f"Verifying the download… {pct or 0}%"
        elif phase == "loading":
            pct = update.get("pct", 0)
            text = f"Loading database… {pct}%{suffix}"
        elif phase == "aggregating":
            text = "Aggregating…"
        elif phase == "indexing":
            pct = update.get("pct", 0)
            text = f"Building indexes… {pct}%{suffix}"
        elif phase == "analyzing":
            pct = update.get("pct")
            text = "Analyzing…" if pct is None else f"Analyzing… {pct}%"
        elif phase == "done":
            pct = 100
            text = "Up to date"
        with self.lock:
            if phase:
                self.state["phase"] = phase
            if text is not None:
                self.state["progress"] = text
            self.state["pct"] = pct
        self._notify()

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self, force: bool = False) -> None:
        """Queue the job. Refuses synchronously — the assistant must not report
        "started" — when the disk budget says no (surfaced in the state so
        the Settings screen's fire-and-forget POST shows the reason) or when
        a job of this family is already queued/running."""
        budget = self.loader.disk_budget()
        if not budget["can_fit"]:
            msg = (f"insufficient disk: needs ~{budget['required_gb']} GB, "
                   f"{budget['free_gb']} GB free")
            with self.lock:
                if not self.state["running"]:
                    self.state.update(phase="error", progress=f"Failed: {msg}", error=msg)
            self._notify()
            raise InsufficientDisk(msg)
        with self.lock:
            if self.state["running"]:
                raise DumpBusy(f"{self.family} update already running")
            self.state.update(running=True, phase="queued", progress="Queued…",
                              pct=None, error=None)
        _queue.put((self, bool(force)))
        _ensure_worker()
        self._notify()

    def run(self, force: bool) -> None:
        """The worker's body. Re-checks the budget (maybe_auto_update queues
        without the endpoint's check — a full disk must fail before the
        download, not 6 GB into it)."""
        try:
            with self.lock:
                self.state.update(running=True, phase="checking", progress="Starting…",
                                  pct=None, error=None)
            self._notify()
            loader = self.loader
            budget = loader.disk_budget()
            if not budget["can_fit"]:
                raise RuntimeError(f"insufficient disk: needs ~{budget['required_gb']} GB, "
                                   f"{budget['free_gb']} GB free")
            result = loader.download_and_load(self.progress, force=force)
            self._write(f"{self.prefix}.last_update_at", datetime.now(timezone.utc).isoformat())
            logger.info("%s dump loaded: %s", self.family, result)
            if self._post_load:
                self._post_load(self, result)
            self.progress({"phase": "done"})
        except Exception as e:
            with self.lock:
                self.state.update(error=str(e), progress=f"Failed: {e}")
            logger.error("%s update failed: %s", self.family, e, exc_info=True)
        finally:
            with self.lock:
                self.state["running"] = False
            self._notify()

    def delete(self) -> Dict:
        with self.lock:
            if self.state["running"]:
                raise DumpBusy(f"{self.family} update running")
        return self.loader.delete_dump()

    # ── reads ────────────────────────────────────────────────────────────────

    def section(self) -> Dict[str, Any]:
        """Stats + settings + live job state for the Library block."""
        try:
            st = self.loader.stats()
        except Exception as e:
            logger.warning("%s stats failed: %s", self.family, e)
            st = {"loaded": False, "version": None, "total_records": 0, "size_bytes": 0}
        with self.lock:
            update = dict(self.state)
        return {
            "loaded":          bool(st.get("loaded")),
            "catalogue":       st.get("catalogue") or {},
            # Reference tables added since this dump landed — Update fetches
            # just those (mb_dump_load.download_and_load). Empty for LB.
            "missing_tables":  st.get("missing_tables") or [],
            "version":         st.get("version"),
            "total_records":   st.get("total_records", 0),
            "size_bytes":      st.get("size_bytes", 0),
            "auto_update":     bool(self._read(f"{self.prefix}.auto_update")),
            "last_update_at":  self._read(f"{self.prefix}.last_update_at"),
            "update":          update,
        }

    def maybe_auto_update(self) -> None:
        """Called at backend startup. If the user enabled auto-update, check
        the mirror in a background thread and queue a newer dump (or the
        first one). Non-blocking: the network check never gates startup."""
        if not bool(self._read(f"{self.prefix}.auto_update")):
            return

        def _check() -> None:
            try:
                loader = self.loader
                latest = loader.latest_version()
                if not latest:
                    return
                version = latest[0] if isinstance(latest, tuple) else latest
                st = loader.stats()
                if (loader.loaded_version() == version and st.get("loaded")
                        and not st.get("missing_tables")):
                    return
                self.start(False)
            except (DumpBusy, InsufficientDisk) as e:
                logger.warning("%s auto-update not started: %s", self.family, e)
            except Exception as e:
                logger.warning("%s auto-update check failed: %s", self.family, e)

        threading.Thread(target=_check, daemon=True).start()
