"""The console's identity file and its QRs (scripts/setup.py; SEC-S5 §16–§18).

`./sautium identity export|import` reaches the node's identity folder only
through desktop/identity_file's command in a container of its own: the host
never opens a path a container writes, keeps the person's file 0600, and
places an identity only after the file opened, with the node stopped for
the placement alone. Every printed QR carries its value as text under it,
drawn by the one presentation (backend/pairing.present)."""

import builtins
import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("sautium_setup_identity", REPO / "scripts" / "setup.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


setup = _load()
FILE = setup.IDENTITY_MAGIC + b"\x00\x01rest-of-a-file"


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    root = tmp_path / "sautium"
    for folder in ("host", "data/node_identity", "backend/data", "run/requests"):
        (root / folder).mkdir(parents=True)
    monkeypatch.setattr(setup, "ROOT", root)
    monkeypatch.setattr(setup.agent, "ROOT", root)
    monkeypatch.setattr(setup, "refuse_root", lambda: None)
    return root


@pytest.fixture
def opened(checkout, monkeypatch):
    """Every path the host opens, by any door."""
    seen = []
    real_open, real_os_open = builtins.open, os.open

    def spy_open(path, *a, **kw):
        seen.append(Path(path).absolute())
        return real_open(path, *a, **kw)

    def spy_os_open(path, *a, **kw):
        seen.append(Path(path).absolute())
        return real_os_open(path, *a, **kw)
    monkeypatch.setattr(builtins, "open", spy_open)
    monkeypatch.setattr(os, "open", spy_os_open)
    return seen


def _theirs(checkout, paths):
    folders = [(checkout / f).resolve() for f in setup.CONTAINER_WRITES]
    return [p for p in paths if any(p.resolve() == f or f in p.resolve().parents for f in folders)]


def test_the_console_s_constants_are_the_module_s():
    sys.path.insert(0, str(REPO / "backend"))
    idf = pytest.importorskip("desktop.identity_file")
    assert setup.IDENTITY_MAGIC == idf.MAGIC
    assert setup.IDENTITY_SUFFIX == idf.FILE_SUFFIX
    assert setup.IDENTITY_FILE_MAX == idf.FILE_MAX


def test_the_identity_command_runs_in_a_container_of_its_own(checkout):
    cmd = setup.identity_cmd("peek")
    assert cmd[cmd.index("run"):] == ["run", "--rm", "--no-deps", "-T", "backend", "python", "-m",
                                      "desktop.identity_file", "peek"]


def test_the_export_is_written_by_the_host_0600_and_never_where_a_container_writes(
        checkout, opened, monkeypatch, tmp_path):
    asked = []

    def fake(args, **kw):
        asked.append((args[args.index("desktop.identity_file") + 1:], kw.get("stdin")))
        command = args[args.index("desktop.identity_file") + 1]
        if command == "phrase":
            reply = {"phrase": "tundra-maple-orbit-kettle", "count": "4 words", "words": "W", "plain": "P"}
            return subprocess.CompletedProcess(args, 0, json.dumps(reply).encode(), b"")
        if command == "check":
            reply = {"zone": "clear", "door": "", "expected_s": 300, "expected_words": "about 5 minutes"}
            return subprocess.CompletedProcess(args, 0, json.dumps(reply).encode(), b"")
        return subprocess.CompletedProcess(args, 0, FILE, b"")
    monkeypatch.setattr(setup, "run", fake)
    typed = iter(["tundra-maple-orbit-kettle", "tundra-maple-orbit-kettle"])
    monkeypatch.setattr(setup.getpass, "getpass", lambda prompt="": next(typed))
    out = tmp_path / ("mine" + setup.IDENTITY_SUFFIX)
    args = setup.argparse.Namespace(action="export", no_passphrase=False, out=str(out))
    assert setup.cmd_identity(args) == 0
    assert out.read_bytes() == FILE and not out.stat().st_mode & 0o077
    # The passphrase rides stdin alone, never a command line
    assert all("tundra" not in " ".join(a) for a, _ in asked)
    assert asked[-1] == (["export"], b"tundra-maple-orbit-kettle\n")
    assert not _theirs(checkout, opened)
    for inside in (checkout / "data" / "node_identity" / "x", checkout / "backend" / "data" / "x",
                   checkout / "run" / "x"):
        with pytest.raises(setup.Stop, match="containers write"):
            setup.out_path(str(inside))
    with pytest.raises(setup.Stop, match="there already"):
        setup.out_path(str(out))


def test_two_different_passphrases_write_nothing(checkout, monkeypatch, tmp_path):
    monkeypatch.setattr(setup, "run", lambda args, **kw: subprocess.CompletedProcess(
        args, 0, b'{"phrase": "a", "count": "", "words": "", "plain": ""}', b""))
    typed = iter(["one-passphrase", "another-one"])
    monkeypatch.setattr(setup.getpass, "getpass", lambda prompt="": next(typed))
    out = tmp_path / "x.sautium-identity"
    with pytest.raises(setup.Stop, match="not the same"):
        setup.cmd_identity(setup.argparse.Namespace(action="export", no_passphrase=False, out=str(out)))
    assert not out.exists()


class _Child:
    """desktop/identity_file's import as the console meets it: the opening's
    line, then — on "place" — the placement's."""

    def __init__(self, opens: bool, log: list):
        self.log = log
        self.stdin = self
        self.stdout = io.BytesIO((b'{"opened": {}}\n' if opens else b"") + b'{"placed": {}}\n')
        self.stderr = io.BytesIO(b"" if opens else b"That passphrase does not open this identity file.\n")

    def write(self, data):
        self.log.append(("stdin", data))

    def flush(self):
        pass

    def close(self):
        self.log.append(("closed",))

    def wait(self, timeout=None):
        return 0


def _import(checkout, monkeypatch, tmp_path, *, opens=True, another=False, answer="yes"):
    log = []
    peek = {"public_key_hex": "ab" * 32, "invite_code": "alex#AAAA-BBBB-CCCC", "locked": True,
            "created_at": "2026-11-26T10:00:00Z", "refusal": None, "another_identity": another,
            "another_words": "ANOTHER", "plain": "P", "expected_words": "about 5 minutes"}

    def fake(args, **kw):
        if "desktop.identity_file" in args:
            log.append(("peek", kw.get("stdin")))
            return subprocess.CompletedProcess(args, 0, json.dumps(peek).encode(), b"")
        log.append(("compose", args[args.index("backend") - 1]))
        return subprocess.CompletedProcess(args, 0, b"", b"")
    monkeypatch.setattr(setup, "run", fake)
    monkeypatch.setattr(setup.subprocess, "Popen", lambda args, **kw: log.append(("import", args)) or _Child(opens, log))
    monkeypatch.setattr(setup.getpass, "getpass", lambda prompt="": "tundra-maple-orbit-kettle")
    monkeypatch.setattr(builtins, "input", lambda prompt="": answer)
    src = tmp_path / "theirs.sautium-identity"
    src.write_bytes(FILE)
    return log, src


def test_the_import_places_only_after_the_file_opened_with_the_node_stopped_for_it(
        checkout, monkeypatch, tmp_path, opened):
    log, src = _import(checkout, monkeypatch, tmp_path)
    assert setup.cmd_identity(setup.argparse.Namespace(action="import", file=str(src))) == 0
    steps = [entry[0] if entry[0] != "compose" else entry[1] for entry in log]
    assert steps[:2] == ["peek", "import"]
    assert steps.index("stop") > steps.index("stdin") and log[steps.index("stop") + 1] == ("stdin", b"place\n")
    assert steps[-1] == "start"
    first = next(data for kind, *data in log if kind == "stdin")[0]
    assert first == b"tundra-maple-orbit-kettle\n" + str(len(FILE)).encode() + b"\n" + FILE
    assert not _theirs(checkout, opened)


def test_a_file_that_does_not_open_never_stops_the_node(checkout, monkeypatch, tmp_path):
    log, src = _import(checkout, monkeypatch, tmp_path, opens=False)
    with pytest.raises(setup.Stop, match="does not open"):
        setup.cmd_identity(setup.argparse.Namespace(action="import", file=str(src)))
    assert ("compose", "stop") not in log and ("stdin", b"place\n") not in log


def test_another_identity_of_record_is_the_owner_s_word(checkout, monkeypatch, tmp_path):
    log, src = _import(checkout, monkeypatch, tmp_path, another=True, answer="no")
    with pytest.raises(setup.Stop, match="nothing was changed"):
        setup.cmd_identity(setup.argparse.Namespace(action="import", file=str(src)))
    assert not any(kind == "import" for kind, *_ in log)
    log, src = _import(checkout, monkeypatch, tmp_path, another=True, answer="yes")
    assert setup.cmd_identity(setup.argparse.Namespace(action="import", file=str(src))) == 0
    assert next(args for kind, args in (e for e in log if e[0] == "import"))[-1] == "--another-identity"


def test_a_file_that_is_not_one_is_not_sent(checkout, monkeypatch, tmp_path):
    monkeypatch.setattr(setup, "run", lambda *a, **kw: pytest.fail("nothing is run"))
    other = tmp_path / "x"
    other.write_bytes(b"SAUTIUM-BACKUP\0...")
    with pytest.raises(setup.Stop, match="not a Sautium identity file"):
        setup.cmd_identity(setup.argparse.Namespace(action="import", file=str(other)))


def test_a_qr_s_text_is_the_one_presentation_s(monkeypatch):
    """The console draws its QRs with backend/pairing.present, run as the
    image runs it: the picture, then its value as text."""
    pytest.importorskip("qrcode")

    def local(args, stdin=None, **kw):
        code = args[args.index("-c") + 1]
        return subprocess.run([sys.executable, "-I", "-c", code], input=stdin, capture_output=True)
    monkeypatch.setattr(setup.agent, "image_of", lambda root: "sautium-backend:local")
    monkeypatch.setattr(setup, "run", local)
    url = "http://192.0.2.10:8800/#pair=ABCD-EFGH"
    drawn = setup.qr(url)
    sys.path.insert(0, str(REPO / "backend"))
    import pairing
    assert drawn == pairing.present(url) and drawn.endswith("\n\n" + url + "\n")
    assert "█" in drawn or "▀" in drawn
