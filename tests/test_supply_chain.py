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


# The interpreters a node runs its packages on: the launcher's (Windows, both
# kinds of Mac) and the Docker image's (Linux, both architectures)
_RUNTIMES = {
    "windows": {"sys_platform": "win32", "platform_system": "Windows", "os_name": "nt",
                "platform_machine": "AMD64", "python_version": "3.12"},
    "mac": {"sys_platform": "darwin", "platform_system": "Darwin", "os_name": "posix",
            "platform_machine": "arm64", "python_version": "3.12"},
    "intel-mac": {"sys_platform": "darwin", "platform_system": "Darwin", "os_name": "posix",
                  "platform_machine": "x86_64", "python_version": "3.12"},
    "linux": {"sys_platform": "linux", "platform_system": "Linux", "os_name": "posix",
              "platform_machine": "x86_64", "python_version": "3.11"},
    "linux-arm": {"sys_platform": "linux", "platform_system": "Linux", "os_name": "posix",
                  "platform_machine": "aarch64", "python_version": "3.11"},
}


def _requirements(path: Path, runtime: str = "windows") -> dict:
    """The file's requirements that apply on `runtime`, by name; two lines of
    one name never both apply."""
    env = dict(_RUNTIMES[runtime], platform_python_implementation="CPython",
               implementation_name="cpython")
    found = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            req = Requirement(line)
            if req.marker is None or req.marker.evaluate(env):
                assert req.name.lower() not in found, f"two lines of {req.name} apply on {runtime}"
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
    # 2.0.13's and every later one link OpenSSL in. The floor is the 2.0
    # line's last release wherever a wheel of it exists (_WHEELS: not on an
    # Intel Mac before macOS 15).
    for runtime in _RUNTIMES:
        req = _requirements(REPO / "desktop" / "requirements.txt", runtime)["libtorrent"]
        assert _floor(req) >= Version("2.0.11" if runtime == "intel-mac" else "2.0.15"), runtime
    assert _floor(_requirements(REPO / "desktop" / "requirements.txt", "windows")["libtorrent"]) \
        >= Version("2.0.13")


# The last published fix of each package that faces the internet, as OSV
# listed it on 2026-11-02. A newer advisory raises the requirement and this
# line together.
_FIXED = {
    "backend": {"aiohttp": "3.14.4", "starlette": "1.3.1", "h11": "0.16.0", "anyio": "4.14.2",
                "cryptography": "50.0.0", "pynacl": "1.6.2", "certifi": "2026.10.18",
                "urllib3": "2.8.0", "requests": "2.33.0"},
    "desktop": {"aiohttp": "3.14.4", "cryptography": "50.0.0", "pynacl": "1.6.2"},
}
# Where a runtime has no wheel of the fix: cryptography publishes none for an
# Intel Mac past 48; 49 and 50 fixed its X.509 path validation and PKCS#7
# decryption, which no node may call while that holds.
_BELOW_THE_FIX = {"intel-mac": {"cryptography": "48.0.1"}}
_LEFT_UNFIXED = r"x509\.verification|x509 import verification|pkcs7"
# Wherever Brotli is installed the HTTP stack decodes br with it, a peer's
# request body to the launcher's sync server included: it is held above its
# decoder's fix (1.2.1) on the runtimes where an earlier release may have
# left a copy — the launcher's backend runtime, a Mac's shared one — and
# required on no other.
_BROTLI = {"backend": {"windows", "mac", "intel-mac"}, "desktop": {"mac", "intel-mac"}}
# What a runtime has a wheel of (PyPI, read 2026-11-02): its requirement must
# admit the first and none of the second, or that runtime builds from source.
_WHEELS = {"intel-mac": {"cryptography": "48.0.1", "libtorrent": "2.0.11"}}
_NO_WHEEL = {"intel-mac": {"cryptography": "49.0.0"}}


def test_what_faces_the_internet_is_held_above_its_last_published_fix():
    import re
    low = []
    for runtime in _RUNTIMES:
        for tree, fixed in _FIXED.items():
            required = _requirements(REPO / tree / "requirements.txt", runtime)
            for name, version in dict(fixed, **_BELOW_THE_FIX.get(runtime, {})).items():
                if name not in required or _floor(required[name]) < Version(version):
                    low.append(f"{tree} on {runtime}: {name} below {version}")
            if runtime in _BROTLI[tree]:
                if "brotli" not in required or _floor(required["brotli"]) < Version("1.2.1"):
                    low.append(f"{tree} on {runtime}: brotli below 1.2.1")
            elif "brotli" in required:
                low.append(f"{tree} on {runtime}: brotli installed where no copy was")
    assert low == []
    unfixed = re.compile(_LEFT_UNFIXED, re.IGNORECASE)
    assert [str(path.relative_to(REPO)) for tree in ("backend", "desktop", "mcp")
            for path in (REPO / tree).rglob("*.py")
            if unfixed.search(path.read_text(encoding="utf-8", errors="replace"))] == []


def test_each_runtime_admits_a_version_it_has_a_wheel_of_and_none_it_has_not():
    wrong = []
    for tree in ("backend", "desktop"):
        for runtime, versions in _WHEELS.items():
            required = _requirements(REPO / tree / "requirements.txt", runtime)
            wrong += [f"{tree} on {runtime}: {name} {version} not admitted"
                      for name, version in versions.items()
                      if name in required and not required[name].specifier.contains(version)]
        for runtime, versions in _NO_WHEEL.items():
            required = _requirements(REPO / tree / "requirements.txt", runtime)
            wrong += [f"{tree} on {runtime}: {name} {version} admitted"
                      for name, version in versions.items()
                      if name in required and required[name].specifier.contains(version)]
    assert wrong == []


def test_a_runtime_in_a_folder_of_any_script_is_found_whatever_the_locale(tmp_path):
    """The install record sits in the runtime's own site-packages, under a
    profile folder the user named; a launcher whose locale cannot spell that
    name still reads the path whole (Windows: a code page, not UTF-8)."""
    import subprocess
    import sys
    import venv
    runtime = tmp_path / "Користувач" / "runtime"
    venv.create(runtime, with_pip=False)
    bindir = "Scripts" if sys.platform == "win32" else "bin"
    python = runtime / bindir / ("python.exe" if sys.platform == "win32" else "python")
    probe = ("import sys; from desktop.service_manager import ServiceManager; "
             "sys.stdout.buffer.write(str(ServiceManager._deps_marker(sys.argv[1])).encode("
             "'utf-8', 'surrogateescape'))")
    env = {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "LANG": "C", "PYTHONCOERCECLOCALE": "0",
           "PYTHONUTF8": "0", "PYTHONPATH": str(REPO)}
    out = subprocess.run([sys.executable, "-X", "utf8=0", "-c", probe, str(python)],
                         capture_output=True, env=env, cwd=REPO)
    assert out.returncode == 0, out.stderr.decode(errors="replace")[-600:]
    marker = Path(out.stdout.decode("utf-8"))
    assert marker.name == ".sautium-backend-deps" and marker.parent.is_dir()
    assert "Користувач" in str(marker)


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


# -- first-run downloads -----------------------------------------------------------------

def test_every_first_run_download_is_a_version_with_its_digest():
    import re
    from desktop.downloads import ARTIFACTS
    for name, artifact in ARTIFACTS.items():
        assert artifact.url.startswith("https://"), name
        assert not re.search(r"latest|release-essentials", artifact.url), name
        assert re.fullmatch(r"[0-9a-f]{64}", artifact.sha256), name


def test_a_download_that_matches_lands_whole(tmp_path, monkeypatch):
    import hashlib
    from desktop import downloads
    source = tmp_path / "upstream.zip"
    source.write_bytes(b"the pinned bytes" * 1000)
    monkeypatch.setitem(downloads.ARTIFACTS, "thing", downloads.Artifact(
        source.as_uri(), hashlib.sha256(source.read_bytes()).hexdigest()))
    seen = []
    dest = downloads.fetch("thing", tmp_path / "got" / "thing.zip", lambda d, t: seen.append(d))
    assert dest.read_bytes() == source.read_bytes()
    assert seen[-1] == len(source.read_bytes())
    assert sorted(p.name for p in dest.parent.iterdir()) == ["thing.zip"]


def test_a_download_that_does_not_match_is_refused_and_leaves_nothing(tmp_path, monkeypatch):
    import pytest
    from desktop import downloads
    source = tmp_path / "upstream.zip"
    source.write_bytes(b"something else")
    monkeypatch.setitem(downloads.ARTIFACTS, "thing", downloads.Artifact(source.as_uri(), "0" * 64))
    with pytest.raises(downloads.Refused, match="not the file this release pins"):
        downloads.fetch("thing", tmp_path / "got" / "thing.zip")
    assert list((tmp_path / "got").iterdir()) == []


def test_the_embedded_pythons_pip_comes_from_the_pinned_wheel_not_a_script():
    from desktop import python_env
    from desktop.downloads import ARTIFACTS
    assert ARTIFACTS["pip"].url.endswith("-py3-none-any.whl")
    source = (REPO / "desktop" / "python_env.py").read_text(encoding="utf-8")
    assert "get-pip" not in source and "--no-index" in python_env._PIP_FROM_WHEEL


# -- the agents' MCP servers -------------------------------------------------------------

def test_the_launchers_agents_start_only_servers_from_the_tree(tmp_path, monkeypatch):
    import json
    import os
    from desktop import config_manager, db_init, python_env
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "config"))
    monkeypatch.setattr(python_env, "get_backend_python", lambda: "/the/backends/python")
    backend = tmp_path / "backend"
    backend.mkdir()
    out = backend / "mcp-windows.json"
    config_manager.generate_mcp_config(
        {"ports": {"postgres": 15432, "web": 18000}, "postgres_password": "pw"}, out)
    servers = json.loads(out.read_text(encoding="utf-8"))["mcpServers"]
    assert sorted(servers) == ["assistant", "postgres"]
    assert {s["command"] for s in servers.values()} == {"/the/backends/python"}
    assert servers["postgres"]["args"] == [str(tmp_path / "mcp" / "postgres_server.py")]
    assert servers["assistant"]["args"] == [str(tmp_path / "mcp" / "assistant_server.py")]
    # The assistant's login, never the node's own role
    assert (servers["postgres"]["env"]["DB_USER"], servers["postgres"]["env"]["DB_PASSWORD"]) == (
        db_init.ASSISTANT_ROLE, db_init.assistant_password("pw"))
    if os.name == "posix":
        assert out.stat().st_mode & 0o777 == 0o600


def test_the_docker_node_writes_the_same_servers_from_its_own_settings(tmp_path, monkeypatch):
    import json
    import sys
    import claude_code_runner
    from config import settings
    from desktop import db_init
    monkeypatch.setattr(claude_code_runner, "MCP_CONFIG_PATH", str(tmp_path / "mcp-docker.json"))
    monkeypatch.setattr(claude_code_runner, "STALE_MCP_CONFIG", tmp_path / "stale" / "mcp-docker.json")
    monkeypatch.setattr(claude_code_runner.os, "geteuid", lambda: 1000)
    claude_code_runner.write_docker_mcp_config()
    servers = json.loads((tmp_path / "mcp-docker.json").read_text(encoding="utf-8"))["mcpServers"]
    backend = Path(claude_code_runner.__file__).resolve().parent
    assert sorted(servers) == ["assistant", "postgres"]
    assert {s["command"] for s in servers.values()} == {sys.executable}
    assert servers["postgres"]["args"] == [str(backend / "mcp" / "postgres_server.py")]
    env = servers["postgres"]["env"]
    assert (env["DB_HOST"], env["DB_NAME"], env["DB_USER"]) == (
        settings.postgres_host, settings.postgres_db, db_init.ASSISTANT_ROLE)
    assert servers["assistant"]["env"]["P2P_IDENTITY_DIR"] == settings.p2p_identity_dir


def test_the_config_an_earlier_release_left_in_the_code_folder_is_removed(tmp_path, monkeypatch,
                                                                         caplog):
    """It holds the database password; a mount that will not let it go is said
    in the log, and the start goes on."""
    import claude_code_runner
    stale = tmp_path / "backend" / "mcp-docker.json"
    stale.parent.mkdir()
    stale.write_text("{}")
    monkeypatch.setattr(claude_code_runner, "MCP_CONFIG_PATH", str(tmp_path / "mcp-docker.json"))
    monkeypatch.setattr(claude_code_runner, "STALE_MCP_CONFIG", stale)
    monkeypatch.setattr(claude_code_runner.os, "geteuid", lambda: 1000)
    claude_code_runner.write_docker_mcp_config()
    assert not stale.exists()
    stale.write_text("{}")
    real = Path.unlink

    def unlink(self, *a, **kw):
        if self == stale:
            raise PermissionError(1, "Operation not permitted", str(self))
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", unlink)
    claude_code_runner.write_docker_mcp_config()
    assert stale.exists() and "delete it on the host" in caplog.text


# -- the Docker image --------------------------------------------------------------------

def _dockerfile(name: str) -> str:
    return (REPO / "backend" / name).read_text(encoding="utf-8")


def test_the_image_installs_its_agent_clis_from_their_lockfiles_only():
    import json
    for name, clis in (("Dockerfile", ("claude-code", "codex")), ("Dockerfile.mac", ("claude-code",))):
        text = _dockerfile(name)
        assert "npm install" not in text and "@latest" not in text and "nodesource" not in text
        for cli in clis:
            assert f"npm ci --prefix /opt/agents/{cli}" in text
    for cli in ("claude-code", "codex"):
        folder = REPO / "backend" / "agents" / cli
        wanted = json.loads((folder / "package.json").read_text(encoding="utf-8"))["dependencies"]
        assert all(v[0].isdigit() for v in wanted.values()), "an exact version, not a range"
        lock = json.loads((folder / "package-lock.json").read_text(encoding="utf-8"))
        packages = {k: v for k, v in lock["packages"].items() if k}
        assert packages and all(p["integrity"].startswith("sha512-")
                                and p["resolved"].startswith("https://registry.npmjs.org/")
                                for p in packages.values())


def test_every_download_the_image_makes_is_checked_before_it_is_used():
    import re
    for name in ("Dockerfile", "Dockerfile.mac"):
        text = _dockerfile(name)
        assert "| bash" not in text and "| python" not in text
        assert "get-pip" not in text and "/latest/" not in text
        fetched = re.findall(r"curl -fsSLo (\S+)", text)
        for target in fetched:
            assert f'echo "${{' in text and f"  {target}\" | sha256sum -c -" in text, (name, target)


def test_nothing_in_the_image_updates_or_runs_a_cli_as_root_at_start():
    """The image's one entrypoint is root's own copy of node_user.py, run
    isolated: it drops root before the command — the backend — starts."""
    assert not (REPO / "backend" / "entrypoint.sh").exists()
    for name in ("Dockerfile", "Dockerfile.mac"):
        text = _dockerfile(name)
        assert "COPY node_user.py /usr/local/lib/sautium/node_user.py" in text
        assert [line for line in text.splitlines() if line.startswith("ENTRYPOINT")] == [
            'ENTRYPOINT ["python3", "-I", "-S", "/usr/local/lib/sautium/node_user.py"]']
    for compose in COMPOSE_FILES:
        text = (REPO / compose).read_text(encoding="utf-8")
        assert "command: python entrypoint.py" in text and "/root/.cache" not in text
        assert "no-new-privileges:true" in text


COMPOSE_FILES = ("docker-compose.yml", "docker-compose.wsl.yml", "docker-compose.mac.yml",
                 "docker-compose.pi.yml")


def test_the_arm64_image_fingerprints_with_the_release_images_ffmpeg():
    """The fingerprint is the content address and FFmpeg's decode makes it:
    the arm64 image sits on the release image's Ubuntu 22.04 (FFmpeg 4.4), not
    a Debian whose FFmpeg moves prints, with the release's tools."""
    release, arm64 = _dockerfile("Dockerfile"), _dockerfile("Dockerfile.mac")
    assert release.splitlines()[1] == "FROM nvidia/cuda:12.2.0-runtime-ubuntu22.04"
    assert [line for line in arm64.splitlines() if line.startswith("FROM ")] == ["FROM ubuntu:22.04"]
    for text in (release, arm64):
        for wanted in ("ppa:deadsnakes/ppa", "libchromaprint-tools", "lbzip2",
                       "unidic-lite==1.0.8 && touch /usr/local/etc/mecabrc", "jammy-pgdg main",
                       "pip install --no-index /tmp/${PIP_WHEEL}"):
            assert wanted in text, wanted


def _compose(name: str) -> dict:
    yaml = __import__("pytest").importorskip("yaml")
    return yaml.safe_load((REPO / name).read_text(encoding="utf-8"))


def _mount(volume: str) -> tuple:
    """(source, target) of a short-syntax bind; a source may hold `:` (${X:-y})"""
    rest, last = volume.rsplit(":", 1)
    return tuple(rest.rsplit(":", 1)) if last in ("ro", "rw") else (rest, last)


def _backend(name: str) -> tuple:
    """(environment keys, mount targets, published ports, postgres flags)"""
    spec = _compose(name)["services"]
    backend = spec["backend"]
    return (set(backend["environment"]), {_mount(v)[1] for v in backend["volumes"]},
            set(backend["ports"]), set(spec["postgres"]["command"].split()))


def test_the_arm64_compose_files_are_the_release_file_but_for_what_their_host_needs():
    """docker-compose.mac.yml lacked the release's WAL lines, its peer and
    media ports and its identity mounts; the arm64 files follow the release
    file, short only of what their image or host has none of: CUDA, the
    Codex CLI and, on Linux, a host user's ~/.claude."""
    env, mounts, ports, flags = _backend("docker-compose.yml")
    cuda_and_codex = {"CUDA_VISIBLE_DEVICES", "PYTORCH_CUDA_ALLOC_CONF", "CODEX_CLI_ENABLED"}
    for name, other_mounts in (("docker-compose.mac.yml", set()),
                               ("docker-compose.pi.yml", {"/home/agent/.claude/projects"})):
        their_env, their_mounts, their_ports, their_flags = _backend(name)
        assert their_env == env - cuda_and_codex, name
        assert their_mounts == mounts - {"/home/agent/.codex"} - other_mounts, name
        assert their_ports == ports, name
        assert {"wal_level=minimal", "max_wal_senders=0", "jit=off"} <= their_flags, name
        backend = _compose(name)["services"]["backend"]
        assert backend["build"]["dockerfile"] == "Dockerfile.mac" and "deploy" not in backend
    assert flags <= _backend("docker-compose.mac.yml")[3]


def test_the_linux_arm64_file_binds_and_mounts_by_the_security_rules():
    """Postgres on loopback alone, the Web UI on every address (the phone on
    the Wi-Fi), no host user's home in the container; the Postgres sizing of
    a 16 GB Pi 5 on NVMe; the music folder named, never guessed."""
    spec = _compose("docker-compose.pi.yml")["services"]
    assert spec["postgres"]["ports"] == ["127.0.0.1:5432:5432"]
    assert "8800:8000" in spec["backend"]["ports"]
    sources = [_mount(v)[0] for v in spec["backend"]["volumes"]]
    assert not [s for s in sources if "HOME" in s or "USERPROFILE" in s or "SAUTIUM_WSL_HOME" in s]
    assert "./data/claude" in sources
    assert "${MUSIC_LIBRARY_PATH:?Set MUSIC_LIBRARY_PATH to the music folder in .env}" in sources
    flags = set(spec["postgres"]["command"].split())
    assert {"shared_buffers=${PG_SHARED_BUFFERS:-2GB}",
            "effective_cache_size=${PG_EFFECTIVE_CACHE_SIZE:-8GB}", "random_page_cost=1.1",
            "effective_io_concurrency=200", "checkpoint_timeout=15min"} <= flags
    assert not {"synchronous_commit=off", "fsync=off"} & flags
