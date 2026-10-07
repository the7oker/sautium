"""A launcher that cannot start says so (desktop/__main__.py). Started from
Finder or Explorer it has no console, and an error that escaped it left no
window and no line in launcher.log: support had nothing to read. The error
goes to the log however early it came, and the report the window offers
says where the log is when the report itself cannot be made."""

import logging
import threading
import types

import pytest

from desktop import __main__ as entry
from desktop import diag_bundle


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    return tmp_path / "Sautium"


def test_an_error_before_the_log_was_set_up_still_reaches_launcher_log(data_dir, monkeypatch):
    shown = []
    monkeypatch.setattr(entry, "_offer_report", lambda d, detail: shown.append((d, detail)))
    monkeypatch.setattr(logging.root, "handlers", [])     # main() never ran
    level = logging.root.level
    try:
        raise ImportError("No module named 'customtkinter'")
    except ImportError as e:
        error = e
    try:
        entry.cannot_start(error)
    finally:
        for handler in logging.root.handlers:
            handler.close()
        logging.root.setLevel(level)
        encodings = [getattr(h, "encoding", None) for h in logging.root.handlers]
    log = (data_dir / "launcher.log").read_text(encoding="utf-8")
    assert "CRITICAL: Sautium could not start" in log
    assert "Traceback" in log and "No module named 'customtkinter'" in log
    assert shown == [(data_dir, "ImportError: No module named 'customtkinter'")]
    # UTF-8 whatever the locale: a cp1252 log dropped the record of a
    # traceback naming C:\Users\Валерій, and nothing else could say it
    assert encodings == ["utf-8"]


def test_a_launcher_that_cannot_write_anything_still_says_why(monkeypatch):
    from desktop import config_manager
    shown = []
    monkeypatch.setattr(entry, "_offer_report", lambda d, detail: shown.append((d, detail)))

    def refused():
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(config_manager, "get_data_dir", refused)
    entry.cannot_start(RuntimeError("Tcl wasn't installed properly"))
    assert shown == [(None, "RuntimeError: Tcl wasn't installed properly\n\nNothing could be "
                            "written either — PermissionError: [Errno 13] Permission denied")]


def test_a_launcher_root_that_exists_is_reused_hidden_and_its_timers_cancelled():
    # One Tk per process: on Aqua a second root after the first one's window
    # went trapped (Tk 9, SIGTRAP in showRootWindow) — the report becomes a
    # window of the root the launcher left, hidden, its start sequence never run
    calls = []
    stale = types.SimpleNamespace(tk=types.SimpleNamespace(
        eval=lambda script: calls.append(("eval", script)),
        call=lambda *args: calls.append(("call",) + args)))
    tk = types.SimpleNamespace(_default_root=stale, Tk=lambda: "a new root",
                               Toplevel=lambda master: ("a window of", master))
    assert entry._report_window(tk) == ("a window of", stale)
    assert calls == [("eval", "foreach id [after info] {after cancel $id}"),
                     ("call", "wm", "withdraw", ".")]
    tk._default_root = None
    assert entry._report_window(tk) == "a new root"


def test_a_session_marker_the_disk_refuses_does_not_stop_the_start(data_dir, monkeypatch):
    launcher = pytest.importorskip("desktop.launcher")
    from desktop.p2p import diag_events

    def full_disk(path):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(diag_events, "write_session_marker", full_disk)
    started = []
    app = types.SimpleNamespace(
        service_manager=types.SimpleNamespace(
            start_all=lambda progress_cb=None: started.append(True) or True),
        ui_call=lambda fn: None, _show_pending_changelog=lambda: None,
        _on_backend_event=None, _on_backend_state=None,
        _check_updates_background=lambda: None)

    class Inline(threading.Thread):
        def start(self):
            self.run()
    monkeypatch.setattr(launcher.threading, "Thread", Inline)
    launcher.LauncherApp._startup_sequence(app)
    assert started == [True] and app._session_start == (data_dir, None)


def test_the_report_carries_the_error_and_the_log(data_dir, monkeypatch):
    data_dir.mkdir(parents=True)
    (data_dir / "launcher.log").write_text(
        "2026-10-07 CRITICAL: Sautium could not start\nTraceback (most recent call last):\n",
        encoding="utf-8")
    monkeypatch.setattr("desktop.utils.show_in_file_manager", lambda path: None)
    monkeypatch.setattr(diag_bundle, "system_facts", lambda config, probe_backend=True: {})
    line = entry.save_report(data_dir, "ImportError: No module named 'customtkinter'")
    assert line == "Saved and selected in the file manager — attach it to your message"
    report = next((data_dir / "reports").glob("sautium-report-*.txt")).read_text(encoding="utf-8")
    assert "State: Sautium could not start" in report
    assert "Detail: ImportError: No module named 'customtkinter'" in report
    assert "CRITICAL: Sautium could not start" in report.split("== launcher.log ==")[1]


def test_a_report_that_cannot_be_made_says_where_the_log_is(data_dir, monkeypatch):
    def full_disk(**kwargs):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(diag_bundle, "save_and_show", full_disk)
    assert entry.save_report(data_dir, "ImportError: x") == (
        f"Report not saved: [Errno 28] No space left on device. "
        f"The log: {data_dir / 'launcher.log'}")
