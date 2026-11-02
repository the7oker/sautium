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
