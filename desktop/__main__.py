"""`python -m desktop`: the launcher — and what is left of it when it cannot
start. Started from Finder or Explorer it has no console, so an error that
escaped it left no window, no line in launcher.log and nothing for the owner
to send to support. Here the error is logged, and a bare window offers the
report the node-down panel offers (diag_bundle.save_and_show). Plain Tk: what
broke may be the launcher's own widgets."""

import logging
import queue
import sys
import threading
from pathlib import Path

logger = logging.getLogger("desktop")


def cannot_start(error: Exception) -> None:
    from desktop.config_manager import get_data_dir
    data_dir = get_data_dir()
    if not logging.getLogger().handlers:      # it died before main() set up the log
        logging.basicConfig(filename=data_dir / "launcher.log", level=logging.INFO,
                            format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    logger.critical("Sautium could not start", exc_info=error)
    try:
        _offer_report(data_dir, f"{type(error).__name__}: {error}")
    except Exception:
        logger.error("Sautium could not show why it did not start", exc_info=True)


def save_report(data_dir: Path, detail: str) -> str:
    """The line the window shows: the report saved and shown, or why not and
    where the log is. The config is not read — it may be what broke."""
    try:
        from desktop import diag_bundle
        return diag_bundle.save_and_show(data_dir=data_dir, config={},
                                         state="Sautium could not start", detail=detail)
    except Exception as e:
        logger.error("Diagnostic report failed", exc_info=True)
        return f"Report not saved: {e}. The log: {data_dir / 'launcher.log'}"


def _offer_report(data_dir: Path, detail: str) -> None:
    import tkinter as tk
    from tkinter import font as tkfont

    root = tk.Tk()
    root.title("Sautium")
    root.resizable(False, False)
    heading = tkfont.nametofont("TkDefaultFont").copy()
    heading.configure(size=15, weight="bold")
    body = tk.Frame(root, padx=24, pady=20)
    body.pack(fill="both")
    tk.Label(body, text="Sautium could not start", font=heading).pack(anchor="w")
    tk.Label(body, text=detail, wraplength=420, justify="left").pack(anchor="w", pady=(6, 0))
    tk.Label(body, wraplength=420, justify="left",
             text="Save a report (the logs, without passwords or keys) and send it to "
                  "support by email or any messenger.").pack(anchor="w", pady=(14, 12))
    row = tk.Frame(body)
    row.pack(anchor="w")
    button = tk.Button(row, text="Save Report for Support")
    button.pack(side="left")
    tk.Button(row, text="Quit", command=root.destroy).pack(side="left", padx=(8, 0))
    note = tk.Label(body, text="", wraplength=420, justify="left")
    note.pack(anchor="w", pady=(10, 0))
    outcome: queue.Queue = queue.Queue()

    def collect():
        # The launcher's ui_call pump in small: Tk is touched from its own
        # thread only (LauncherApp.ui_call says why), while a report is made.
        try:
            line = outcome.get_nowait()
        except queue.Empty:
            root.after(100, collect)
            return
        note.configure(text=line)
        button.configure(state="normal")

    def save():
        # Off the Tk thread: the system facts probe the tools.
        button.configure(state="disabled")
        note.configure(text="Collecting the report…")
        threading.Thread(target=lambda: outcome.put(save_report(data_dir, detail)),
                         daemon=True).start()
        root.after(100, collect)

    button.configure(command=save)
    root.lift()
    root.focus_force()
    root.mainloop()


if __name__ == "__main__":
    try:
        from desktop.launcher import main
        main()
    except Exception as error:
        cannot_start(error)
        sys.exit(1)
