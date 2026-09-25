"""ServiceManager.close() is final (desktop/service_manager.py).

Quitting the launcher used to stop the services while a flow on another
thread — a scan's backend restart, settings being applied, a restore — could
still start them again, leaving a backend (or PostgreSQL) behind the closed
window. close() and every start share one lock: a start that is spawning
when close() comes is stopped by it, and no start begins afterwards. The
spawned "backend" is a real process; the steps before the spawn are stubbed."""

import subprocess
import sys
import threading

import pytest

from desktop import config_manager, db_init, service_manager, utils
from desktop.service_manager import ServiceManager

REAL_POPEN = subprocess.Popen


@pytest.fixture
def sm(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "data"))
    for name in ("generate_env_file", "generate_mcp_config"):
        monkeypatch.setattr(config_manager, name, lambda *a, **k: None)
    monkeypatch.setattr(db_init, "ensure_media_tools", lambda *a, **k: {})
    monkeypatch.setattr(db_init, "media_tool_dirs", lambda *a, **k: [])
    monkeypatch.setattr(utils, "keep_awake", lambda enabled: None)
    monkeypatch.setattr(ServiceManager, "_ensure_backend_deps", lambda self, cb=None: True)
    monkeypatch.setattr(ServiceManager, "_kill_orphan_on_port", staticmethod(lambda port: None))
    monkeypatch.setattr(ServiceManager, "_ensure_firewall_rule", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(ServiceManager, "_get_backend_python", lambda self: sys.executable)
    monkeypatch.setattr(ServiceManager, "backend_env", lambda self, dirs=None: {})
    monkeypatch.setattr(ServiceManager, "_wait_for_backend", lambda self, port, timeout=120: True)
    monkeypatch.setattr(ServiceManager, "stop_postgres", lambda self: True)
    manager = ServiceManager({"ports": {"web": 18999, "postgres": 15999}})
    manager._backend_dir = tmp_path
    yield manager
    if manager.backend_proc and manager.backend_proc.poll() is None:
        manager.backend_proc.kill()


class SlowSpawn:
    """Popen that holds the spawn open until released, then starts a real
    long-lived stand-in for uvicorn."""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.spawned = []

    def __call__(self, cmd, **kwargs):
        self.entered.set()
        assert self.release.wait(10)
        proc = REAL_POPEN([sys.executable, "-c", "import time; time.sleep(120)"])
        self.spawned.append(proc)
        return proc


def test_close_stops_a_start_caught_mid_spawn_and_refuses_the_next(sm, monkeypatch):
    spawn = SlowSpawn()
    monkeypatch.setattr(service_manager.subprocess, "Popen", spawn)
    result = {}
    starter = threading.Thread(target=lambda: result.setdefault("started", sm.start_backend()))
    starter.start()
    assert spawn.entered.wait(10)

    closer = threading.Thread(target=sm.close)
    closer.start()
    closer.join(0.5)
    assert closer.is_alive()            # waits for the spawn in flight

    spawn.release.set()
    starter.join(10)
    closer.join(20)
    assert not closer.is_alive()
    assert result["started"] is True
    assert spawn.spawned[0].wait(10) is not None   # close() stopped what that start spawned

    spawn.release.clear()
    assert sm.start_backend() is False
    assert len(spawn.spawned) == 1


def test_postgres_does_not_start_after_close(sm, monkeypatch):
    started = []
    monkeypatch.setattr(db_init, "download_portable_postgres", lambda *a, **k: True)
    monkeypatch.setattr(db_init, "get_pg_bin_dir", lambda *a, **k: "/nonexistent")
    monkeypatch.setattr(db_init, "initialize_cluster", lambda *a, **k: None)
    monkeypatch.setattr(db_init, "is_postgres_running", lambda *a, **k: False)
    monkeypatch.setattr(db_init, "start_postgres", lambda *a, **k: started.append(1) or True)

    sm.close()

    assert sm.start_postgres() is False
    assert started == []


def test_reopen_lets_starts_through_again(sm, monkeypatch):
    spawn = SlowSpawn()
    spawn.release.set()
    monkeypatch.setattr(service_manager.subprocess, "Popen", spawn)

    sm.close()
    assert sm.start_backend() is False
    sm.reopen()
    assert sm.start_backend() is True
    assert len(spawn.spawned) == 1
