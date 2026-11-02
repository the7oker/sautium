"""What a node runs is what its release names. A package comes from a
requirements file the release carries, at a version a floor admits — never
installed by name while the node runs — and a dependency that faces the
internet is held above its last published fix."""

import ast
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

REPO = Path(__file__).resolve().parent.parent

# Import name → distribution name, for the third-party modules the launcher's
# P2P layer imports. A module missing here fails the test below: name its
# distribution, and put that in desktop/requirements.txt.
_DISTRIBUTIONS = {
    "aiohttp": "aiohttp", "argon2": "argon2-cffi", "cryptography": "cryptography",
    "libtorrent": "libtorrent", "miniupnpc": "miniupnpc", "nacl": "PyNaCl",
    "psutil": "psutil", "psycopg2": "psycopg2-binary",
}


def _requirements(path: Path) -> dict:
    found = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            req = Requirement(line)
            found[req.name.lower()] = req
    return found


def _imports(paths) -> set:
    names = set()
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return names


def _floor(req: Requirement) -> Version:
    floors = [Version(s.version) for s in req.specifier if s.operator in (">=", "==")]
    assert floors, f"{req.name} has no floor"
    return max(floors)


def test_the_launcher_requirements_carry_every_package_its_p2p_layer_imports():
    import sys
    sources = sorted((REPO / "desktop" / "p2p").rglob("*.py")) + [REPO / "desktop" / "portmap.py"]
    third_party = _imports(sources) - set(sys.stdlib_module_names) - {"desktop"}
    unknown = third_party - set(_DISTRIBUTIONS)
    assert not unknown, f"name the distribution of {sorted(unknown)} in _DISTRIBUTIONS"
    required = _requirements(REPO / "desktop" / "requirements.txt")
    missing = sorted(_DISTRIBUTIONS[m] for m in third_party
                     if _DISTRIBUTIONS[m].lower() not in required)
    assert missing == []


def test_the_libtorrent_floor_is_a_windows_wheel_with_its_own_openssl():
    # 2.0.11's Windows wheel imported OpenSSL 1.1 DLLs from the system;
    # 2.0.13's and every later one link OpenSSL in.
    req = _requirements(REPO / "desktop" / "requirements.txt")["libtorrent"]
    assert _floor(req) >= Version("2.0.13")


# The last published fix of each package that faces the internet, as OSV
# listed it on 2026-11-02. A newer advisory raises the requirement and this
# line together.
_FIXED = {
    "backend": {"aiohttp": "3.14.4", "starlette": "1.3.1", "h11": "0.16.0", "anyio": "4.14.2",
                "cryptography": "50.0.0", "pynacl": "1.6.2", "certifi": "2026.10.18",
                "urllib3": "2.8.0", "requests": "2.33.0"},
    "desktop": {"aiohttp": "3.14.4", "cryptography": "50.0.0", "pynacl": "1.6.2",
                "libtorrent": "2.0.15"},
}


def test_what_faces_the_internet_is_held_above_its_last_published_fix():
    low = []
    for tree, fixed in _FIXED.items():
        required = _requirements(REPO / tree / "requirements.txt")
        for name, version in fixed.items():
            if name not in required or _floor(required[name]) < Version(version):
                low.append(f"{tree}: {name} below {version}")
    assert low == []


def test_a_runtime_replaced_at_the_same_path_gets_its_packages_installed_again(tmp_path, monkeypatch):
    """The macOS app copies its runtime afresh on a new release, at the same
    path, and the backend shares that runtime: its packages go with it, so
    the record that they were installed must go too."""
    import subprocess
    import sys
    import venv

    from desktop import python_env, service_manager
    from desktop.service_manager import ServiceManager

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "data"))
    runtime = tmp_path / "runtime"
    bindir = "Scripts" if sys.platform == "win32" else "bin"
    python = str(runtime / bindir / ("python.exe" if sys.platform == "win32" else "python"))
    venv.create(runtime, with_pip=False)
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / "requirements.txt").write_text("fastapi>=0.133.0\n", encoding="utf-8")

    installs = []
    real_run = subprocess.run

    def run(cmd, *args, **kwargs):
        if "pip" in cmd and "install" in cmd:
            if "-r" in cmd:
                installs.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if "import torch" in " ".join(map(str, cmd)):      # a CUDA build in place
            return subprocess.CompletedProcess(cmd, 0, "12.6\n", "")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(service_manager.subprocess, "run", run)
    monkeypatch.setattr(python_env, "ensure_project_pth", lambda: None)
    monkeypatch.setattr(ServiceManager, "_ensure_backend_python", lambda self, cb=None: True)
    monkeypatch.setattr(ServiceManager, "_get_backend_python", lambda self: python)
    manager = ServiceManager({"ports": {"web": 18999, "postgres": 15999}})
    manager._backend_dir = backend

    assert manager._ensure_backend_deps() is None
    assert manager._ensure_backend_deps() is None
    assert len(installs) == 1                      # the second start found them in place

    import shutil
    shutil.rmtree(runtime)
    venv.create(runtime, with_pip=False)           # the same path, a fresh runtime
    assert manager._ensure_backend_deps() is None
    assert len(installs) == 2


def test_the_launcher_installs_no_package_while_it_runs():
    """The bootstraps install desktop/requirements.txt before the launcher
    starts; the launcher and its setup window install nothing by name."""
    offenders = []
    for name in ("launcher.py", "wizard.py"):
        tree = ast.parse((REPO / "desktop" / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.List):
                words = {e.value for e in node.elts if isinstance(e, ast.Constant)}
                if {"pip", "install"} <= words:
                    offenders.append(f"{name}:{node.lineno}")
    assert offenders == []
