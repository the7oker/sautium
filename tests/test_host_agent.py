"""The host agent (scripts/host_agent.py; PI-NODE-1 F2, PI-NODE-2A): a host
process that acts on a container's request, so its rule is tested, not only
stated — the host's side never opens a path a container can write. For every
path it touches that a container writes (the requests folder, and what is in
it), a link and a FIFO are planted there from the container's side, and the
agent is shown never to follow or open one. The answer's way back is a
client of a server that may be hostile. And the starter–body interface, the
starter's own update and its way back through the restore unit's shell.

Docker and systemd are not here: the verbs that drive them are checked on a
Pi in the slice's record; the verifier's judgement is test_updater_releases.py's
(survey_fetched)."""

import hashlib
import hmac
import http.server
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BODY = REPO / "scripts" / "host_agent.py"


def _load(path=BODY, name="host_agent"):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ha = _load()
KEY = "ab" * 32


@pytest.fixture
def checkout(tmp_path):
    """A checkout as the agent sees one: host/ with the channel key and the
    starter's folder, run/requests the container's."""
    root = tmp_path / "sautium"
    (root / "host" / "bin").mkdir(parents=True)
    (root / "run" / "requests").mkdir(parents=True)
    (root / "host" / "agent.env").write_text(f"SAUTIUM_HOST_CHANNEL_KEY={KEY}\n")
    return root


@pytest.fixture
def outside(tmp_path):
    """What a link from the container's side points at: a file and a folder
    of the person's that must come out untouched."""
    folder = tmp_path / "home"
    (folder / "keep").mkdir(parents=True)
    (folder / "keep" / "authorized_keys").write_text("the person's\n")
    (folder / ".env").write_text("POSTGRES_PASSWORD=x\n")
    return folder


def _tree(path: Path) -> dict:
    return {p.relative_to(path): (p.read_bytes() if p.is_file() else None)
            for p in sorted(path.rglob("*"))}


# -- the requests folder -------------------------------------------------------

def test_the_names_are_read_and_everything_removed(checkout):
    requests = checkout / "run" / "requests"
    for name in ("update", "check", "nonsense", ".hidden"):
        (requests / name).write_text("whatever the node wrote is never read\n")
    asked, left = ha.drain(checkout)
    assert asked == ["check", "update"] and left == 0
    assert list(requests.iterdir()) == []


def test_a_link_in_the_requests_folder_is_unlinked_never_followed(checkout, outside):
    requests = checkout / "run" / "requests"
    before = _tree(outside)
    (requests / "check").symlink_to(outside / ".env")
    (requests / "update").symlink_to(outside / "keep")
    (requests / "x").symlink_to(outside)
    (requests / "dangling").symlink_to(checkout / "nowhere")
    asked, left = ha.drain(checkout)
    assert asked == ["check", "update"] and left == 0
    assert list(requests.iterdir()) == []
    assert _tree(outside) == before


def test_a_fifo_in_the_requests_folder_is_unlinked_never_opened(checkout):
    requests = checkout / "run" / "requests"
    os.mkfifo(requests / "diagnostics")
    os.mkfifo(requests / "other")
    # Opening a FIFO with no writer blocks: a drain that opened one would hang
    done = []
    worker = threading.Thread(target=lambda: done.append(ha.drain(checkout)), daemon=True)
    worker.start()
    worker.join(10)
    assert done == [(["diagnostics"], 0)]
    assert list(requests.iterdir()) == []


def test_a_folder_is_removed_bottom_up_its_links_unlinked(checkout, outside):
    requests = checkout / "run" / "requests"
    before = _tree(outside)
    deep = requests / "update" / "a" / "b"
    deep.mkdir(parents=True)
    (deep / "link").symlink_to(outside / "keep")
    (deep / "file").write_text("x")
    os.mkfifo(deep / "fifo")
    (requests / "update" / "to-home").symlink_to(outside)
    asked, left = ha.drain(checkout)
    assert asked == ["update"] and left == 0
    assert list(requests.iterdir()) == [] and _tree(outside) == before


def test_a_folder_deeper_than_the_bound_stays_and_is_said(checkout, capfd):
    requests = checkout / "run" / "requests"
    deep = requests / "check"
    for i in range(ha.DRAIN_DEPTH + 2):
        deep = deep / str(i)
    deep.mkdir(parents=True)
    (requests / "diagnostics").write_text("")
    asked, left = ha.drain(checkout)
    assert asked == ["check", "diagnostics"] and left == 1
    assert [p.name for p in requests.iterdir()] == ["check"]
    assert "could not be removed" in capfd.readouterr().err


def test_a_run_folder_or_requests_folder_that_is_a_link_is_never_entered(checkout, outside):
    (outside / "keep" / "check").write_text("")
    before = _tree(outside)
    shutil.rmtree(checkout / "run" / "requests")
    (checkout / "run" / "requests").symlink_to(outside / "keep")
    with pytest.raises(OSError):
        ha.drain(checkout)
    shutil.rmtree(checkout / "run")
    (checkout / "run").symlink_to(outside)
    with pytest.raises(OSError):
        ha.drain(checkout)
    assert _tree(outside) == before


def test_a_name_swapped_for_a_link_mid_removal_is_unlinked(checkout, outside, monkeypatch):
    """A folder the node swaps for a link between the agent's look and its
    open: the open refuses the link (O_NOFOLLOW), and the link is unlinked."""
    requests = checkout / "run" / "requests"
    (requests / "update").mkdir()
    real_open = ha._open_dir

    def swapped(name, dir_fd):
        if name == "update" and dir_fd is not None and (requests / "update").is_dir() \
                and not (requests / "update").is_symlink():
            (requests / "update").rmdir()
            (requests / "update").symlink_to(outside / "keep")
        return real_open(name, dir_fd)

    monkeypatch.setattr(ha, "_open_dir", swapped)
    before = _tree(outside)
    assert ha.drain(checkout) == (["update"], 0)
    assert _tree(outside) == before and list(requests.iterdir()) == []


def test_a_drain_removes_at_most_its_budget_and_says_the_rest(checkout, monkeypatch):
    monkeypatch.setattr(ha, "DRAIN_MAX", 5)
    requests = checkout / "run" / "requests"
    for i in range(12):
        (requests / f"x{i}").write_text("")
    asked, left = ha.drain(checkout)
    assert asked == [] and left > 0
    assert len(list((checkout / "run" / "requests").iterdir())) == 7


# -- what the agent writes: only under ./host/ -----------------------------------

def test_the_agents_files_are_written_without_following_a_link(checkout, outside):
    before = _tree(outside)
    state = checkout / "host" / "state.json"
    state.symlink_to(outside / ".env")
    ha.save_state({"check_at": 1}, checkout)
    assert not state.is_symlink() and json.loads(state.read_text()) == {"check_at": 1}
    assert _tree(outside) == before


def test_a_file_the_agent_reads_is_a_regular_file_of_bounded_size(checkout, outside):
    state = checkout / "host" / "state.json"
    state.symlink_to(outside / ".env")
    with pytest.raises(OSError):
        ha.load_state(checkout)
    state.unlink()
    os.mkfifo(state)
    with pytest.raises(OSError):
        ha.load_state(checkout)       # opened non-blocking, refused as no regular file
    state.unlink()
    state.write_bytes(b"{" + b" " * ha.STATE_MAX + b"}")
    with pytest.raises(OSError):
        ha.load_state(checkout)


def test_one_env_key_is_set_and_every_other_line_kept(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# the person's comment\nPOSTGRES_PASSWORD=secret\nSAUTIUM_BACKEND_IMAGE=old\n"
                   "MUSIC_LIBRARY_PATH='/srv/music'\n")
    env.chmod(0o600)
    ha.set_env(env, "SAUTIUM_BACKEND_IMAGE", "sautium-backend:v0.3.0-abc")
    assert env.read_text() == ("# the person's comment\nPOSTGRES_PASSWORD=secret\n"
                               "SAUTIUM_BACKEND_IMAGE=sautium-backend:v0.3.0-abc\n"
                               "MUSIC_LIBRARY_PATH='/srv/music'\n")
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert ha.read_env(env)["MUSIC_LIBRARY_PATH"] == "/srv/music"
    ha.set_env(env, "NEW", "1")
    assert env.read_text().endswith("NEW=1\n")


def test_the_node_is_driven_on_the_files_env_names(checkout):
    """A node whose .env names COMPOSE_FILE (an override, the lab's loopback
    one) is driven on those files, as the person's own commands in the
    folder are; else on the Linux file."""
    assert ha.compose_cmd(checkout) == ["docker", "compose", "-f", "docker-compose.pi.yml"]
    (checkout / ".env").write_text("COMPOSE_FILE=docker-compose.pi.yml:lab.yml\n")
    assert ha.compose_cmd(checkout) == ["docker", "compose"]


def test_the_dockerfile_the_update_builds_is_the_linux_files():
    yaml = pytest.importorskip("yaml")
    spec = yaml.safe_load((REPO / ha.COMPOSE_FILE).read_text())["services"]["backend"]
    assert spec["build"]["dockerfile"] == ha.DOCKERFILE
    assert spec["container_name"] == ha.BACKEND
    assert yaml.safe_load((REPO / ha.COMPOSE_FILE).read_text())["services"]["postgres"][
        "container_name"] == ha.POSTGRES
    assert spec["image"] == "${SAUTIUM_BACKEND_IMAGE:-%s}" % ha.DEFAULT_IMAGE


# -- the answer ----------------------------------------------------------------

class _Node(http.server.BaseHTTPRequestHandler):
    seen = []
    status = 200
    redirect_to = None

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        type(self).seen.append((self.path, dict(self.headers), body))
        if type(self).redirect_to:
            self.send_response(307)
            self.send_header("Location", type(self).redirect_to)
        else:
            self.send_response(type(self).status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def node():
    handler = type("Node", (_Node,), {"seen": [], "status": 200, "redirect_to": None})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield handler, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_the_answer_is_signed_as_the_nodes_door_checks_it(node):
    handler, api = node
    assert ha.answer("check", "offered", "Sautium 0.3.0 is available", "v0.3.0", None, KEY.encode(), api)
    path, headers, body = handler.seen[0]
    assert path == ha.ANSWER_PATH
    sys.path.insert(0, str(REPO / "backend"))
    import auth_hmac
    assert headers["X-Sautium-Body-Sha256"] == hashlib.sha256(body).hexdigest()
    assert hmac.compare_digest(headers["X-Sautium-Sig"], auth_hmac.sign(
        KEY.encode(), "POST", ha.ANSWER_PATH, headers["X-Sautium-Ts"], body, headers["X-Sautium-Nonce"]))
    assert json.loads(body) == {"verb": "check", "outcome": "offered", "detail": "Sautium 0.3.0 is available",
                                "release": "v0.3.0", "report": None, "replayed": False}


def test_the_answer_follows_no_redirect(node):
    handler, api = node
    other = type("Other", (_Node,), {"seen": [], "status": 200, "redirect_to": None})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), other)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        handler.redirect_to = f"http://127.0.0.1:{server.server_address[1]}/elsewhere"
        assert not ha.answer("check", "current", "x", None, None, KEY.encode(), api)
        assert other.seen == []
    finally:
        server.shutdown()


def test_a_node_that_refuses_the_answer_is_said_not_fatal(node, capfd):
    handler, api = node
    handler.status = 500
    assert not ha.answer("update", "failed", "x", None, None, KEY.encode(), api)
    assert "did not take the answer" in capfd.readouterr().err


# The node's door on the answer's path (the channel's key there alone, no other
# signer there): test_host_channel_db.py, behind the real middleware and database.


# -- a run ---------------------------------------------------------------------

def test_a_run_answers_each_asked_verb_once_in_its_order(checkout, node, monkeypatch):
    handler, api = node
    monkeypatch.setenv("SAUTIUM_HOST_STARTER_API", "1")
    for name in ("update", "check", "check2", "diagnostics"):
        (checkout / "run" / "requests" / name).write_text("")
    ran = []

    def verb(name):
        def go(state, now, root):
            ran.append(name)
            return ("done", "made", None, "report") if name == "diagnostics" else ("current", name, None, None)
        return go

    assert ha.run(checkout, api, now=10.0, verbs={v: verb(v) for v in ha.VERBS},
                  start_probe=lambda: None) == 0
    assert ran == ["check", "diagnostics", "update"]
    assert [json.loads(b)["verb"] for _, _, b in handler.seen] == ran


def test_a_verbs_failure_is_an_answer_not_a_failed_start(checkout, node, monkeypatch):
    handler, api = node
    monkeypatch.setenv("SAUTIUM_HOST_STARTER_API", "1")
    (checkout / "run" / "requests" / "check").write_text("")

    def broken(state, now, root):
        raise ha.Refusal("failed", "git fetch failed: no network")

    assert ha.run(checkout, api, now=10.0, verbs={"check": broken}, start_probe=lambda: None) == 0
    assert json.loads(handler.seen[0][2])["outcome"] == "failed"
    assert ha.load_state(checkout)["failed_since_installed"] is True


def test_a_run_not_started_by_a_starter_is_refused(checkout, monkeypatch):
    monkeypatch.delenv("SAUTIUM_HOST_STARTER_API", raising=False)
    (checkout / "run" / "requests" / "check").write_text("")
    assert ha.run(checkout, "http://127.0.0.1:9", start_probe=lambda: None) == 2
    assert (checkout / "run" / "requests" / "check").exists()


def test_the_floors(checkout):
    state = {"check_at": 1000.0}
    assert ha.verb_check(state, 1000.0 + ha.CHECK_FLOOR - 1, checkout)[0] == "floor"
    state = {"update_noop_at": 1000.0}
    assert ha.verb_update(state, 1000.0 + ha.UPDATE_NOOP_FLOOR - 1, checkout)[0] == "floor"
    state = {"diagnostics_at": 1000.0}
    assert ha.verb_diagnostics(state, 1000.0 + ha.DIAGNOSTICS_FLOOR - 1, checkout)[0] == "floor"


def test_a_check_answers_what_the_survey_found(monkeypatch):
    commit = "a" * 40
    base = {"head": commit, "installed": {"name": "v0.2.0", "commit": commit}, "offered": None,
            "rewrite": None, "refused": [], "fork": None}
    assert ha._judged(base)[0] == "current"
    assert ha._judged({**base, "offered": {"name": "v0.3.0", "commit": "b" * 40}}) == \
        ("offered", "Sautium 0.3.0 is available", "v0.3.0")
    assert ha._judged({**base, "rewrite": {"name": "v1.0.0", "commit": "c" * 40}})[0] == "rewrite"
    assert ha._judged({**base, "refused": [["v0.3.1", "signed by a key it does not list"]]})[0] == "refused"
    assert ha._judged({**base, "fork": ["v0.3.5", "v0.3.0"]})[0] == "refused"


def test_a_diagnostics_report_carries_no_setup_code():
    # A code of before 2026-11-19 had three groups: scrubbed too, as two of them and a tail
    text = "setup code: K7QM-ZP4X\nan older one: K7QM-ZP4X-9HTR\nnot one: K7QM ZP4X\n"
    assert ha._scrub(text) == "setup code: XXXX-XXXX\nan older one: XXXX-XXXX-9HTR\nnot one: K7QM ZP4X\n"
    sys.path.insert(0, str(REPO / "backend"))
    import device_auth
    assert ha.SETUP_CODE.pattern == device_auth._SETUP_CODE_FORM.pattern


def test_the_agent_and_the_node_agree_on_the_verbs_and_their_answers():
    sys.path.insert(0, str(REPO / "backend"))
    import host_channel
    assert ha.VERBS == host_channel.VERBS and set(ha.ORDER) == set(ha.VERBS)
    assert ha.ANSWER_PATH == host_channel.ANSWER_PATH
    assert (ha.DETAIL_MAX, ha.REPORT_MAX) == (host_channel.DETAIL_MAX, host_channel.REPORT_MAX)
    outcomes = set().union(*host_channel.OUTCOMES.values())
    sql = (REPO / "desktop" / "migrations" / "001_initial.sql").read_text()
    enum = re.search(r"CREATE TYPE host_outcome AS ENUM \(([^)]*)\)", sql).group(1)
    assert outcomes == set(re.findall(r"'([a-z]+)'", enum))
    source = BODY.read_text()
    for verb, allowed in host_channel.OUTCOMES.items():
        said = set(re.findall(r'return \(?"([a-z]+)"', source[source.index(f"def verb_{verb}("):]
                              .split("\ndef ", 1)[0]))
        assert said <= allowed | {"current", "refused"}, verb


# -- the starter -----------------------------------------------------------------

def _install_copy(root: Path, body_text: str = None) -> None:
    """The checkout's body as a starter finds it."""
    (root / "scripts").mkdir(exist_ok=True)
    (root / "scripts" / "host_agent.py").write_text(body_text or BODY.read_text())


def _start(root: Path, starter: Path = None, env: dict = None) -> subprocess.CompletedProcess:
    starter = starter or root / "host" / "bin" / ha.STARTER_NAME
    clean = {k: v for k, v in os.environ.items() if not k.startswith("SAUTIUM_HOST")}
    return subprocess.run([str(starter)], env={**clean, **(env or {})}, capture_output=True,
                          text=True, timeout=60)


def test_a_starter_runs_the_body_of_the_checkout_it_sits_in(checkout):
    _install_copy(checkout)
    starter = checkout / "host" / "bin" / ha.STARTER_NAME
    starter.write_bytes(ha.starter_text())
    starter.chmod(0o755)
    (checkout / "run" / "requests" / "unknown").write_text("")
    result = _start(checkout)
    assert result.returncode == 0, result.stderr
    assert list((checkout / "run" / "requests").iterdir()) == []


def test_an_old_starter_runs_a_new_body_and_a_new_starter_an_old_body(checkout):
    newer_body = BODY.read_text().replace("STARTER_API = 1\n", "STARTER_API = 2\n", 1)
    starter = checkout / "host" / "bin" / ha.STARTER_NAME
    # an old starter (interface 1), a body that speaks 2
    _install_copy(checkout, newer_body)
    starter.write_bytes(ha.starter_text(1))
    starter.chmod(0o755)
    assert _start(checkout).returncode == 0
    # a new starter (interface 2, with an addition an old body ignores), the old body
    _install_copy(checkout)
    starter.write_bytes(ha.starter_text(2).replace(b"export SAUTIUM_HOST_STARTER_API\n",
                                                   b"export SAUTIUM_HOST_STARTER_API\n"
                                                   b"SAUTIUM_HOST_STARTER_NEW=1\nexport SAUTIUM_HOST_STARTER_NEW\n"))
    assert _start(checkout).returncode == 0


def _verified(checkout, monkeypatch, at=0.0, runs_ok=1):
    head = "f" * 40
    monkeypatch.setattr(ha, "head", lambda root=None: head)
    return {"installed": {"name": "v0.3.0", "commit": head, "at": at, "runs_ok": runs_ok}}


def test_the_starter_moves_only_after_a_verified_release_ran_healthy_for_its_time(checkout, monkeypatch):
    current, previous, trial = ha._starter_paths(checkout)
    current.write_bytes(b"#!/bin/sh\n# the old starter\n")
    probes = []
    state = _verified(checkout, monkeypatch)
    assert ha.maintain_starter(state, ha.STARTER_AFTER - 1, checkout, lambda: probes.append(1)) is None
    assert ha.maintain_starter({**state, "failed_since_installed": True}, ha.STARTER_AFTER, checkout,
                               lambda: probes.append(1)) is None
    assert ha.maintain_starter({}, ha.STARTER_AFTER, checkout, lambda: probes.append(1)) is None
    # the release's body has not run once since the move
    assert ha.maintain_starter(_verified(checkout, monkeypatch, runs_ok=0), ha.STARTER_AFTER, checkout,
                               lambda: probes.append(1)) is None
    other = {"installed": {**state["installed"], "commit": "e" * 40}}
    assert ha.maintain_starter(other, ha.STARTER_AFTER, checkout, lambda: probes.append(1)) is None
    assert probes == [] and current.read_bytes() == b"#!/bin/sh\n# the old starter\n"
    assert ha.maintain_starter(state, ha.STARTER_AFTER, checkout, lambda: probes.append(1)) == \
        "a new starter is on trial"
    assert probes == [1] and current.read_bytes() == ha.starter_text()
    assert previous.read_bytes() == b"#!/bin/sh\n# the old starter\n" and trial.exists()
    assert ha.maintain_starter(state, ha.STARTER_AFTER + 1, checkout, lambda: probes.append(1)) == \
        "the new starter ran: kept"
    assert not trial.exists() and probes == [1]


def test_the_update_that_moves_the_body_never_moves_the_starter(checkout, monkeypatch):
    """The update records the release at the moment it moved: the starter's
    time starts there, so the same run cannot replace it."""
    current, _, _ = ha._starter_paths(checkout)
    current.write_bytes(b"#!/bin/sh\n# the old starter\n")
    state = _verified(checkout, monkeypatch, at=5000.0)
    assert ha.maintain_starter(state, 5000.0, checkout, lambda: None) is None


def _restore_shell(root: Path) -> str:
    service = ha.units(root)[f"{ha.UNIT}-restore.service"]
    return re.search(r"ExecStart=/bin/sh -c '(.*)'\n", service).group(1)


def test_a_broken_new_starter_comes_back_to_the_old_one_on_its_own(checkout, monkeypatch, tmp_path):
    """What the user manager does with the units: the probe starts the new
    starter, every failed start runs the restore unit (OnFailure=), which
    counts and starts the probe again; after STARTER_TRIES the previous
    starter is back, and it runs."""
    _install_copy(checkout)
    current, previous, trial = ha._starter_paths(checkout)
    current.write_bytes(ha.starter_text())
    current.chmod(0o755)
    good = current.read_bytes()
    broken = b"#!/bin/sh\nexec python3 -I /nowhere/host_agent.py run\n"
    monkeypatch.setattr(ha, "starter_text", lambda api=ha.STARTER_API: broken)
    state = _verified(checkout, monkeypatch)
    probes = []
    assert ha.maintain_starter(state, ha.STARTER_AFTER, checkout, lambda: probes.append(1)) == \
        "a new starter is on trial"
    # A fake systemctl: the restore's "start the probe" is recorded, the loop is the test's
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    calls = bin_dir.parent / "systemctl.calls"
    (bin_dir / "systemctl").write_text(f"#!/bin/sh\necho \"$@\" >> {calls}\n")
    (bin_dir / "systemctl").chmod(0o755)
    shell = _restore_shell(checkout)
    starts = 0
    while starts < 10:
        starts += 1
        result = _start(checkout)
        if result.returncode == 0:
            break
        subprocess.run(["/bin/sh", "-c", shell], check=True, env={"PATH": f"{bin_dir}:/usr/bin:/bin"})
    assert starts == ha.STARTER_TRIES + 1
    assert current.read_bytes() == good
    assert not trial.exists()
    assert calls.read_text().count(f"start --no-block {ha.UNIT}-probe.service") == ha.STARTER_TRIES


def test_the_restore_unit_does_nothing_without_a_trial(checkout, tmp_path):
    current, previous, trial = ha._starter_paths(checkout)
    current.write_bytes(b"current")
    previous.write_bytes(b"previous")
    subprocess.run(["/bin/sh", "-c", _restore_shell(checkout)], check=True,
                   env={"PATH": "/nonexistent"})
    assert current.read_bytes() == b"current"


def test_the_units_watch_the_requests_folder_and_start_the_starter(checkout):
    units = ha.units(checkout)
    for name in (f"{ha.UNIT}.service", f"{ha.UNIT}-probe.service"):
        assert f"StandardError=append:{checkout}/host/agent.log\n" in units[name]
    assert f"DirectoryNotEmpty={checkout}/run/requests\n" in units[f"{ha.UNIT}.path"]
    for name in (f"{ha.UNIT}.service", f"{ha.UNIT}-probe.service"):
        assert f"ExecStart={checkout}/host/bin/{ha.STARTER_NAME}\n" in units[name]
        assert f"OnFailure={ha.UNIT}-restore.service\n" in units[name]
        assert "Type=oneshot" in units[name]
    assert "OnFailure" not in units[f"{ha.UNIT}-restore.service"]


def test_install_makes_the_folders_the_key_the_starter_and_the_units(tmp_path, monkeypatch):
    root = tmp_path / "sautium"
    root.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.setattr(ha, "_run", lambda *a, **k: subprocess.CompletedProcess(a, 0, b"yes", b""))
    calls = []
    ha.install(root, systemctl=lambda *args: calls.append(args))
    assert calls == [("daemon-reload",), ("enable", f"{ha.UNIT}-boot.service"), ("enable", "--now", f"{ha.UNIT}.path")]
    for folder in (root / "host", root / "host" / "bin", root / "run" / "requests"):
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    key = ha.channel_key(root)
    assert stat.S_IMODE((root / "host" / "agent.env").stat().st_mode) == 0o600
    starter = root / "host" / "bin" / ha.STARTER_NAME
    assert starter.read_bytes() == ha.starter_text() and os.access(starter, os.X_OK)
    assert sorted(p.name for p in (tmp_path / "config" / "systemd" / "user").iterdir()) == sorted(ha.units(root))
    ha.install(root, systemctl=lambda *args: None)
    assert ha.channel_key(root) == key


def test_install_refuses_root_a_strange_path_and_a_requests_folder_docker_made(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    with pytest.raises(SystemExit, match="not as root"):
        ha.install(tmp_path, systemctl=lambda *a: None)
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    spaced = tmp_path / "my music"
    spaced.mkdir()
    with pytest.raises(SystemExit, match="character"):
        ha.install(spaced, systemctl=lambda *a: None)


# -- the compose files ---------------------------------------------------------

def _mounts(spec: dict) -> list:
    out = []
    for volume in spec.get("volumes", []):
        rest, last = volume.rsplit(":", 1)
        source, target = rest.rsplit(":", 1) if last in ("ro", "rw") else (rest, last)
        out.append((source, target, last == "ro"))
    return out


COMPOSE_FILES = ("docker-compose.yml", "docker-compose.wsl.yml", "docker-compose.mac.yml",
                 "docker-compose.pi.yml")


@pytest.mark.parametrize("name", COMPOSE_FILES)
def test_no_compose_file_mounts_the_code_writable_or_the_agents_folder_at_all(name):
    yaml = pytest.importorskip("yaml")
    spec = yaml.safe_load((REPO / name).read_text())["services"]["backend"]
    mounts = _mounts(spec)
    by_source = {source: ro for source, _, ro in mounts}
    assert by_source["./backend"] is True and by_source["./mcp"] is True
    assert by_source["./desktop"] is True and by_source["./.git"] is True
    for source, target, ro in mounts:
        assert not source.startswith(("./host", "./scripts", "./run/out")), (name, source)
        if source.startswith("./run"):
            assert (source, target, ro) == ("./run/requests", "/run/sautium/requests", False)
        if not ro:
            # every folder the node writes is one of its own, never a folder of the code's
            assert not target.startswith("/app/") or target.startswith("/app/data"), (name, target)
    assert "/app/data" in {t for _, t, ro in mounts if not ro}


@pytest.mark.parametrize("name", COMPOSE_FILES)
def test_every_mount_inside_the_read_only_code_has_its_mount_point_in_the_clone(name):
    """Docker cannot make a mount point inside a read-only folder: a fresh
    clone's start failed on each one the checkout did not ship. /app/data is
    its own source (Docker makes that); anything under it mounts in a
    folder the node writes."""
    yaml = pytest.importorskip("yaml")
    if shutil.which("git") is None or not (REPO / ".git").exists():
        pytest.skip("needs the checkout's git")
    spec = yaml.safe_load((REPO / name).read_text())["services"]["backend"]
    tracked = set(subprocess.run(["git", "ls-files", "backend"], cwd=REPO, capture_output=True, text=True,
                                 check=True).stdout.split())
    for source, target, _ in _mounts(spec):
        if not target.startswith("/app/") or target == "/app/data" or target.startswith("/app/data/"):
            continue
        point = "backend/" + target[len("/app/"):]
        assert any(path.startswith(point + "/") for path in tracked), (name, target)
    assert ("./backend/data", "/app/data", False) in _mounts(spec)


@pytest.mark.parametrize("name", COMPOSE_FILES)
def test_every_folder_a_container_writes_is_ignored_whole_and_holds_no_tracked_file(name):
    """A tracked file in a folder the node writes lets the node make the
    checkout "changed", and an update refuses a changed checkout; a folder
    ignored as `folder/*` beside a keep-file is looked into by git, so a
    `.gitignore` the node puts there could un-ignore what it writes."""
    yaml = pytest.importorskip("yaml")
    if shutil.which("git") is None or not (REPO / ".git").exists():
        pytest.skip("needs the checkout's git")
    spec = yaml.safe_load((REPO / name).read_text())["services"]["backend"]
    for source, _, ro in _mounts(spec):
        if ro or not source.startswith("./"):
            continue
        rel = source[2:]
        tracked = subprocess.run(["git", "ls-files", "--", rel], cwd=REPO, capture_output=True,
                                 text=True, check=True).stdout
        assert tracked == "", (name, source, tracked)
        rule = subprocess.run(["git", "check-ignore", "-v", "--no-index", f"{rel}/probe"], cwd=REPO,
                              capture_output=True, text=True).stdout
        assert rule, (name, source)
        pattern = rule.split("\t")[0].split(":", 2)[2]
        assert pattern.endswith("/") and "*" not in pattern and not pattern.startswith("!"), (name, pattern)


@pytest.mark.parametrize("name", COMPOSE_FILES)
def test_the_lan_only_ports_are_published_on_ipv4_alone(name):
    yaml = pytest.importorskip("yaml")
    spec = yaml.safe_load((REPO / name).read_text())["services"]["backend"]
    if name == "docker-compose.pi.yml":
        assert spec["network_mode"] == "host" and "ports" not in spec
        return
    lan = [p for p in spec["ports"] if "8800" in p or "8830" in p or "8831" in p]
    if name == "docker-compose.yml":
        # The Windows host's file: its LAN forward (netsh portproxy) holds 0.0.0.0 on these ports, so a
        # publish there would keep the backend from starting; they carry no address.
        assert len(lan) == 3 and not any(p.startswith("0.0.0.0:") for p in lan), lan
        return
    assert len(lan) == 3 and all(p.startswith("0.0.0.0:") for p in lan), lan


@pytest.mark.parametrize("name", COMPOSE_FILES)
def test_the_allowed_hosts_reach_the_container(name):
    yaml = pytest.importorskip("yaml")
    env = yaml.safe_load((REPO / name).read_text())["services"]["backend"]["environment"]
    assert env["SAUTIUM_ALLOWED_HOSTS"] == "${SAUTIUM_ALLOWED_HOSTS:-}"


def test_the_linux_file_drops_every_capability_but_the_entrypoints():
    yaml = pytest.importorskip("yaml")
    spec = yaml.safe_load((REPO / "docker-compose.pi.yml").read_text())["services"]
    backend = spec["backend"]
    assert backend["cap_drop"] == ["ALL"]
    assert sorted(backend["cap_add"]) == ["CHOWN", "DAC_OVERRIDE", "SETGID", "SETUID"]
    assert "NET_RAW" not in backend["cap_add"] and "NET_ADMIN" not in backend["cap_add"]
    assert backend["environment"]["UVICORN_PORT"] == 8800
    assert backend["environment"]["POSTGRES_HOST"] == "127.0.0.1"
    assert backend["environment"]["BACKEND_URL"] == "http://127.0.0.1:8800"
    assert spec["postgres"]["ports"] == ["127.0.0.1:5432:5432"]
    assert backend["env_file"] == [{"path": "./host/agent.env", "required": False}]


def test_the_database_is_copied_and_put_back_inside_its_own_container(monkeypatch):
    """The way back is exact: a copy of the whole database (the dump
    loaders' tables too) made beside it while the backend is stopped, renamed
    into place, the live one renamed away first and dropped last. On the Pi a
    clean restore of a dump that leaves those tables out failed on what they
    hold on the rest — a key, then a function their indexes use — and the
    first one left the node down."""
    ran = []

    def fake(args, **kw):
        ran.append(args)
        out = b"music_ai\nmusic_ai_pre_update\npostgres" if any("SELECT datname" in a for a in args) else b""
        return subprocess.CompletedProcess(args, 0, out, b"")

    monkeypatch.setattr(ha, "_run", fake)
    env = {"POSTGRES_DB": "music_ai", "POSTGRES_USER": "musicai"}
    copy = ha._snapshot(Path("/x"), env)
    ha._restore(Path("/x"), env, copy)
    statements = [a[i + 1] for a in ran for i, v in enumerate(a) if v == "-c"]
    ddl = [s for s in statements if not s.startswith("SELECT")]
    assert ddl == ['DROP DATABASE IF EXISTS "music_ai_pre_update"',
                   'CREATE DATABASE "music_ai_pre_update" TEMPLATE "music_ai" STRATEGY FILE_COPY',
                   'DROP DATABASE IF EXISTS "music_ai_failed_update" WITH (FORCE)',
                   'ALTER DATABASE "music_ai" RENAME TO "music_ai_failed_update"',
                   'ALTER DATABASE "music_ai_pre_update" RENAME TO "music_ai"']
    # this node's database container, by its compose project: never a container's global name
    assert all(a[:6] == ["docker", "compose", "-f", ha.COMPOSE_FILE, "exec", "-T"] and a[6:8] == ["postgres", "psql"]
               for a in ran)
    for bad in ('x"; DROP DATABASE y; --', "Music", "a-b", ""):
        with pytest.raises(ha.Refusal):
            ha._snapshot(Path("/x"), {"POSTGRES_DB": bad})


def test_a_way_back_cut_short_after_its_renames_renames_nothing_more(monkeypatch):
    """A way back interrupted after its renames is resumed: the copy is in
    place already, and the failed release's database stays beside it."""
    ran = []

    def fake(args, **kw):
        ran.append(args)
        out = b"music_ai\nmusic_ai_failed_update\npostgres" if any("SELECT datname" in a for a in args) else b""
        return subprocess.CompletedProcess(args, 0, out, b"")

    monkeypatch.setattr(ha, "_run", fake)
    ha._restore(Path("/x"), {"POSTGRES_DB": "music_ai"}, "music_ai_pre_update")
    assert len(ran) == 1

def test_an_answer_the_node_did_not_take_is_said_first_at_the_next_run(checkout, node, monkeypatch):
    handler, api = node
    monkeypatch.setenv("SAUTIUM_HOST_STARTER_API", "1")
    (checkout / "run" / "requests" / "update").write_text("")
    ha.run(checkout, "http://127.0.0.1:9", now=10.0, verbs={"update": lambda s, n, r: ("undone", "back", "v0.3.0", None)},
           start_probe=lambda: None)
    assert ha.load_state(checkout)["unsent"] == {"update": ["undone", "back", "v0.3.0"]}
    (checkout / "run" / "requests" / "check").write_text("")
    ha.run(checkout, api, now=20.0, verbs={"check": lambda s, n, r: ("current", "x", None, None)},
           start_probe=lambda: None)
    assert [(json.loads(b)["verb"], json.loads(b)["replayed"]) for _, _, b in handler.seen] == \
        [("update", True), ("check", False)]        # the earlier run's, said again: no answer to this one
    assert "unsent" not in ha.load_state(checkout)


def test_a_starter_the_restore_unit_put_back_is_never_tried_again(checkout, monkeypatch):
    """The probe failed it three times and the restore unit put the previous
    one back: the body must not install the same starter at its next run (a
    loop of trials, the review's probe)."""
    current, previous, trial = ha._starter_paths(checkout)
    current.write_bytes(b"#!/bin/sh\n# the old starter\n")
    state = _verified(checkout, monkeypatch)
    assert ha.maintain_starter(state, ha.STARTER_AFTER, checkout, lambda: None) == "a new starter is on trial"
    # what the restore unit does after the third failed start
    os.replace(previous, current)
    trial.unlink()
    assert "not tried again" in ha.maintain_starter(state, ha.STARTER_AFTER + 1, checkout, lambda: None)
    assert ha.maintain_starter(state, ha.STARTER_AFTER + 2, checkout, lambda: None) is None
    assert current.read_bytes() == b"#!/bin/sh\n# the old starter\n"


def test_a_good_run_counts_for_the_installed_release_and_a_failed_one_stops_the_starters_move(
        checkout, node, monkeypatch):
    handler, api = node
    monkeypatch.setenv("SAUTIUM_HOST_STARTER_API", "1")
    ha.save_state({"installed": {"name": "v0.3.0", "commit": "f" * 40, "at": 0.0, "runs_ok": 0}}, checkout)
    (checkout / "run" / "requests" / "check").write_text("")
    ha.run(checkout, api, now=10.0, verbs={"check": lambda s, n, r: ("current", "x", None, None)},
           start_probe=lambda: None)
    assert ha.load_state(checkout)["installed"]["runs_ok"] == 1
    (checkout / "run" / "requests" / "check").write_text("")
    ha.run(checkout, api, now=20.0, verbs={"check": lambda s, n, r: ("failed", "x", None, None)},
           start_probe=lambda: None)
    assert ha.load_state(checkout)["failed_since_installed"] is True


def test_a_verbs_unexpected_error_is_an_answer_with_its_trace_in_the_log(checkout, node, monkeypatch, capfd):
    handler, api = node
    monkeypatch.setenv("SAUTIUM_HOST_STARTER_API", "1")
    (checkout / "run" / "requests" / "check").write_text("")

    def broken(state, now, root):
        raise KeyError("offered")

    assert ha.run(checkout, api, now=10.0, verbs={"check": broken}, start_probe=lambda: None) == 0
    assert json.loads(handler.seen[0][2])["outcome"] == "failed"
    assert "Traceback" in capfd.readouterr().err


def test_the_body_answers_its_selftest(checkout):
    _install_copy(checkout)
    env = {k: v for k, v in os.environ.items() if not k.startswith("SAUTIUM_HOST")}
    body = checkout / "scripts" / "host_agent.py"
    assert subprocess.run([sys.executable, "-I", str(body), "selftest"], env=env).returncode == 2
    assert subprocess.run([sys.executable, "-I", str(body), "selftest"],
                          env={**env, "SAUTIUM_HOST_STARTER_API": "1"}).returncode == 0


class _Update:
    """verb_update with git, docker and the survey replaced by a record of
    what they were asked: the order of the steps, and what a failure at each
    one leaves."""

    def __init__(self, checkout, monkeypatch, *, fail_at=None, healthy=(True,), body_ok=True):
        self.calls, self.fail_at, self.healthy, self.root = [], fail_at, list(healthy), checkout
        (checkout / ".env").write_text("POSTGRES_DB=music_ai\nSAUTIUM_BACKEND_IMAGE=sautium-backend:v0.2.0-old\n")
        offered = {"name": "v0.3.0", "commit": "c" * 40}
        monkeypatch.setattr(ha, "survey", lambda root=None: {"head": "a" * 40, "installed": None, "offered": offered,
                                                            "rewrite": None, "refused": [], "fork": None})
        monkeypatch.setattr(ha, "leftovers", lambda root=None: [])
        monkeypatch.setattr(ha, "_room_for_copy", lambda root, env: None)
        monkeypatch.setattr(ha, "head", lambda root=None: "a" * 40)
        monkeypatch.setattr(ha, "_body_answers", lambda root: body_ok)
        monkeypatch.setattr(ha, "_front_restarted", lambda root, old, new: None)
        self.started = False

        def healthy(root):
            self.started = True
            return self.healthy.pop(0) if self.healthy else True
        monkeypatch.setattr(ha, "_healthy", healthy)
        monkeypatch.setattr(ha, "_run", self.run)
        monkeypatch.setattr(ha.subprocess, "Popen", self.popen)

    def step(self, name):
        self.calls.append(name)
        if name == self.fail_at:
            raise ha.Refusal("failed", f"{name} failed")

    def run(self, args, **kw):
        joined = " ".join(args)
        for needle, name in (("stop backend", "stop"), ("CREATE DATABASE", "copy"),
                             ("RENAME TO", "restore"), ("checkout --quiet --detach", "move"),
                             ("checkout --quiet --force --detach", "back"), ("docker build", "build"),
                             ("SELECT datname", "databases"), ("status --porcelain", "status")):
            if needle in joined:
                self.step(name)
                out = b"music_ai music_ai_pre_update" if name == "databases" else b""
                return subprocess.CompletedProcess(args, 0, out, b"")
        return subprocess.CompletedProcess(args, 0, b"", b"")

    def popen(self, args, **kw):
        class Archive:
            stdout = type("S", (), {"close": lambda self: None})()
            returncode = 0

            def wait(self):
                return 0
        return Archive()


def test_an_update_moves_with_its_database_copied_first(checkout, monkeypatch):
    up = _Update(checkout, monkeypatch)
    state = {}
    outcome, detail, release, _ = ha.verb_update(state, 1000.0, checkout)
    assert (outcome, release) == ("updated", "v0.3.0")
    assert up.calls == ["status", "build", "stop", "copy", "move"]
    assert "moving" not in state and state["installed"]["commit"] == "c" * 40
    assert ha.read_env(checkout / ".env")["SAUTIUM_BACKEND_IMAGE"] == "sautium-backend:v0.3.0-cccccccccccc"


@pytest.mark.parametrize("fail_at", ["copy", "move"])
def test_a_failure_after_the_stop_always_ends_on_the_way_back_with_the_node_started(checkout, monkeypatch, fail_at):
    up = _Update(checkout, monkeypatch, fail_at=fail_at)
    state = {}
    outcome, detail, release, _ = ha.verb_update(state, 1000.0, checkout)
    assert outcome in ("undone", "failed") and "could not be put in place" in detail
    # the way back stops the node again first, then puts each thing back
    assert up.calls[up.calls.index("stop"):] == (["stop", "copy", "stop", "back"] if fail_at == "copy"
                                                 else ["stop", "copy", "move", "stop", "back", "databases", "restore"])
    assert up.started, "the way back ends with the node started"
    assert "moving" not in state and state["update_failed"]["commit"] == "c" * 40
    assert ha.read_env(checkout / ".env")["SAUTIUM_BACKEND_IMAGE"] == "sautium-backend:v0.2.0-old"


def test_a_release_whose_agent_does_not_start_is_taken_back(checkout, monkeypatch):
    up = _Update(checkout, monkeypatch, body_ok=False)
    state = {}
    outcome, detail, _, _ = ha.verb_update(state, 1000.0, checkout)
    assert outcome == "undone" and "host agent does not start" in detail
    assert "restore" in up.calls and "back" in up.calls


def test_a_release_that_did_not_start_is_not_tried_again_for_a_day(checkout, monkeypatch):
    up = _Update(checkout, monkeypatch, healthy=(False, True))
    state = {}
    assert ha.verb_update(state, 1000.0, checkout)[0] == "undone"
    up.calls.clear()
    assert ha.verb_update(state, 1001.0, checkout)[0] == "floor"
    assert up.calls == []                                   # no build, no stop: the forged loop costs nothing
    up.healthy = [True]
    assert ha.verb_update(state, 1000.0 + ha.UPDATE_NOOP_FLOOR, checkout)[0] == "updated"


def test_an_update_a_run_did_not_finish_is_undone_at_the_next_start(checkout, node, monkeypatch):
    handler, api = node
    up = _Update(checkout, monkeypatch)
    monkeypatch.setenv("SAUTIUM_HOST_STARTER_API", "1")
    ha.save_state({"moving": {"from": "a" * 40, "image": "sautium-backend:v0.2.0-old", "to": "c" * 40,
                              "name": "v0.3.0", "tag": "sautium-backend:v0.3.0-cccccccccccc",
                              "copy": "music_ai_pre_update"}}, checkout)
    ha.set_env(checkout / ".env", "SAUTIUM_BACKEND_IMAGE", "sautium-backend:v0.3.0-cccccccccccc")
    assert ha.run(checkout, api, now=50.0, verbs={}, start_probe=lambda: None) == 0
    said = json.loads(handler.seen[0][2])
    assert (said["verb"], said["outcome"]) == ("update", "undone") and "did not finish" in said["detail"]
    assert "back" in up.calls and "restore" in up.calls
    assert "moving" not in ha.load_state(checkout)
    assert ha.read_env(checkout / ".env")["SAUTIUM_BACKEND_IMAGE"] == "sautium-backend:v0.2.0-old"


def test_files_git_does_not_know_among_the_code_stop_the_update(tmp_path):
    root = tmp_path / "co"
    (root / "backend" / "routers").mkdir(parents=True)
    (root / "mcp").mkdir()
    (root / "backend" / "main.py").write_text("x = 1\n")
    (root / ".gitignore").write_text("backend/data/\n__pycache__/\n*.pyc\n**/.env\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"],
                   check=True)
    (root / "backend" / "data").mkdir()
    (root / "backend" / "data" / ".node_key").write_text("k")
    (root / "backend" / "__pycache__").mkdir()
    (root / "backend" / "__pycache__" / "main.cpython-311.pyc").write_bytes(b"x")
    assert ha.leftovers(root) == []
    (root / "backend" / ".env").write_text("POSTGRES_HOST=elsewhere\n")
    (root / "backend" / "json.py").write_text("print('shadow')\n")
    (root / "backend" / "routers" / "sync.pyc").write_bytes(b"x")
    (root / "mcp" / "extra.py").write_text("x")
    assert sorted(ha.leftovers(root)) == ["backend/.env", "backend/json.py", "backend/routers/sync.pyc",
                                          "mcp/extra.py"]


def test_uninstall_takes_the_key_so_the_node_learns_it_has_no_agent(checkout, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    ha.uninstall(checkout, systemctl=lambda *a: None)
    assert not (checkout / "host" / "agent.env").exists()


def test_the_agent_runs_at_the_user_managers_start_too_until_the_database_is_up():
    units = ha.units(Path("/srv/sautium"))
    boot = units[f"{ha.UNIT}-boot.service"]
    assert "[Install]\nWantedBy=default.target\n" in boot and "Restart=on-failure\n" in boot
    assert "StartLimitIntervalSec=0\n" in boot
    for name in (f"{ha.UNIT}.service", f"{ha.UNIT}-probe.service"):
        assert "[Install]" not in units[name] and "Restart=" not in units[name]


# Mount points inside a folder a container writes, as they stood before
# PI-NODE-2A: a node can leave a link where a NEW one would be, and the mount
# lands where the link points (over the code). None may be added.
NESTED_IN_WRITABLE = {"/app/data/tls", "/app/data/node_identity", "/app/data/mbdump", "/app/data/lbdump",
                      "/app/data/support", "/app/data/backup", "/app/data/export", "/app/data/seed",
                      "/app/data/atlas", "/home/agent/.claude/projects"}


@pytest.mark.parametrize("name", COMPOSE_FILES)
def test_no_new_mount_lands_inside_a_folder_the_node_writes(name):
    yaml = pytest.importorskip("yaml")
    mounts = _mounts(yaml.safe_load((REPO / name).read_text())["services"]["backend"])
    writable = [target for _, target, ro in mounts if not ro and target != "/app"]
    nested = {target for _, target, _ in mounts
              if any(target != w and target.startswith(w + "/") for w in writable)}
    assert nested <= NESTED_IN_WRITABLE, (name, nested - NESTED_IN_WRITABLE)


def test_an_update_builds_what_env_says_the_image_carries(checkout, monkeypatch):
    """The console setup records what it built without (the models, the
    assistant's CLI, the demo channel's runtime); an update that built the
    Dockerfile's defaults would put them back, gigabytes of them on a card."""
    up = _Update(checkout, monkeypatch)
    built = []
    real = up.run

    def run(args, **kw):
        if args[:2] == ["docker", "build"]:
            built.append(args)
        return real(args, **kw)

    monkeypatch.setattr(ha, "_run", run)
    with open(checkout / ".env", "a") as f:
        f.write("SAUTIUM_IMAGE_MODELS=0\nSAUTIUM_IMAGE_STREAMING=0\n")
    assert ha.verb_update({}, 1000.0, checkout)[0] == "updated"
    assert built == [["docker", "build", "-f", "Dockerfile.mac", "--build-arg", "MODELS=0",
                      "--build-arg", "STREAMING=0", "-t", "sautium-backend:v0.3.0-cccccccccccc", "-"]]


def test_a_value_that_is_no_choice_stops_the_update_before_a_build(checkout, monkeypatch):
    up = _Update(checkout, monkeypatch)
    with open(checkout / ".env", "a") as f:
        f.write("SAUTIUM_IMAGE_MODELS=0 --network=host\n")
    state = {}
    outcome, detail, _, _ = ha.verb_update(state, 1000.0, checkout)
    assert outcome == "refused" and "SAUTIUM_IMAGE_MODELS in .env is neither 0 nor 1" in detail
    assert "build" not in up.calls and "update_failed" not in state


def test_the_image_arguments_are_the_dockerfiles_the_compose_files_and_the_agents():
    yaml = pytest.importorskip("yaml")
    dockerfile = (REPO / "backend" / ha.DOCKERFILE).read_text(encoding="utf-8")
    args = yaml.safe_load((REPO / ha.COMPOSE_FILE).read_text())["services"]["backend"]["build"]["args"]
    assert args == {arg: f"${{{key}:-1}}" for arg, key in ha.IMAGE_ARGS}
    for arg, _ in ha.IMAGE_ARGS:
        assert f"ARG {arg}=1\n" in dockerfile


def test_a_release_whose_image_did_not_build_is_not_built_again_for_a_day(checkout, monkeypatch):
    up = _Update(checkout, monkeypatch, fail_at="build")
    state = {}
    with pytest.raises(ha.Refusal, match="did not build"):
        ha.verb_update(state, 1000.0, checkout)
    assert "stop" not in up.calls
    up.calls.clear()
    assert ha.verb_update(state, 1001.0, checkout)[0] == "floor" and up.calls == []


def test_an_update_whose_run_died_after_a_good_start_is_kept_not_undone(checkout, node, monkeypatch):
    """The power went while the release was starting; Docker started it again
    at boot and it ran: the agent's next start keeps it (the checkout moved,
    .env names its image, it is up and its agent answers) — it never puts
    the old database over what the release has written since."""
    handler, api = node
    up = _Update(checkout, monkeypatch)
    monkeypatch.setenv("SAUTIUM_HOST_STARTER_API", "1")
    monkeypatch.setattr(ha, "git", lambda *a, **k: "c" * 40 if a[:2] == ("rev-parse", "--verify") else "")
    ha.set_env(checkout / ".env", "SAUTIUM_BACKEND_IMAGE", "sautium-backend:v0.3.0-cccccccccccc")
    ha.save_state({"moving": {"from": "a" * 40, "image": "sautium-backend:v0.2.0-old", "to": "c" * 40,
                              "name": "v0.3.0", "tag": "sautium-backend:v0.3.0-cccccccccccc",
                              "copy": "music_ai_pre_update"}}, checkout)
    assert ha.run(checkout, api, now=50.0, verbs={}, start_probe=lambda: None) == 0
    said = json.loads(handler.seen[0][2])
    assert (said["verb"], said["outcome"]) == ("update", "updated") and "finished" in said["detail"]
    assert "restore" not in up.calls and "back" not in up.calls
    state = ha.load_state(checkout)
    assert "moving" not in state and state["installed"]["commit"] == "c" * 40


def test_an_unfinished_update_waits_for_the_database_and_touches_nothing_before(checkout, node, monkeypatch):
    handler, api = node
    up = _Update(checkout, monkeypatch)
    monkeypatch.setenv("SAUTIUM_HOST_STARTER_API", "1")
    monkeypatch.setattr(ha, "_database_up", lambda root: False)
    moving = {"from": "a" * 40, "image": "sautium-backend:v0.2.0-old", "to": "c" * 40, "name": "v0.3.0",
              "tag": "sautium-backend:v0.3.0-cccccccccccc", "copy": "music_ai_pre_update"}
    ha.save_state({"moving": moving}, checkout)
    assert ha.run(checkout, api, now=50.0, verbs={}, start_probe=lambda: None) == 3     # the boot unit retries
    assert up.calls == [] and handler.seen == [] and ha.load_state(checkout)["moving"] == moving


def test_a_way_back_whose_database_did_not_go_back_is_tried_again(checkout, monkeypatch):
    up = _Update(checkout, monkeypatch, fail_at="restore", healthy=(False, True, True))
    state = {}
    outcome, detail, _, _ = ha.verb_update(state, 1000.0, checkout)
    assert outcome == "failed" and "put the database back" in detail
    assert state["moving"]["back_tried_at"] == 1000.0                      # kept: not the new schema forever
    assert ha.resume(state, 1000.0 + ha.RETRY_BACK - 1, checkout) is None
    up.fail_at = None
    outcome, detail, _, _ = ha.resume(state, 1000.0 + ha.RETRY_BACK, checkout)
    assert outcome == "undone" and "moving" not in state


def test_a_successful_update_is_saved_before_its_copy_is_dropped(checkout, monkeypatch):
    up = _Update(checkout, monkeypatch)
    seen = {}
    real = ha._psql

    def psql(root, env, *statements, **kw):
        if any(s.startswith("DROP DATABASE IF EXISTS \"music_ai_pre_update\"") for s in statements) and "copy" in up.calls:
            seen["saved"] = ha.load_state(checkout)
        return real(root, env, *statements, **kw)

    monkeypatch.setattr(ha, "_psql", psql)
    assert ha.verb_update({}, 1000.0, checkout)[0] == "updated"
    assert "moving" not in seen["saved"] and seen["saved"]["installed"]["commit"] == "c" * 40


def test_a_build_that_times_out_is_not_built_again_for_a_day(checkout, monkeypatch):
    up = _Update(checkout, monkeypatch)
    real = up.run

    def slow(args, **kw):
        if args[:2] == ["docker", "build"]:
            raise subprocess.TimeoutExpired(args, 3 * 3600)
        return real(args, **kw)

    monkeypatch.setattr(ha, "_run", slow)
    state = {}
    with pytest.raises(ha.Refusal, match="did not build"):
        ha.verb_update(state, 1000.0, checkout)
    assert ha.verb_update(state, 1001.0, checkout)[0] == "floor"


def test_a_refusal_for_the_checkouts_own_files_holds_for_minutes_not_a_day(checkout, monkeypatch):
    up = _Update(checkout, monkeypatch)
    monkeypatch.setattr(ha, "leftovers", lambda root=None: ["backend/.env"])
    state = {}
    assert ha.verb_update(state, 1000.0, checkout)[0] == "refused"
    assert ha.verb_update(state, 1001.0, checkout)[0] == "floor"
    monkeypatch.setattr(ha, "leftovers", lambda root=None: [])
    assert ha.verb_update(state, 1000.0 + ha.HELD_FLOOR, checkout)[0] == "updated"


def test_provider_packages_are_the_ones_the_person_named(tmp_path):
    root = tmp_path / "co"
    providers = root / "backend" / "streaming" / "providers"
    providers.mkdir(parents=True)
    (root / "mcp").mkdir()
    (root / ".gitignore").write_text("backend/streaming/providers/\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"],
                   check=True)
    (providers / "mine").mkdir()
    (providers / "planted").mkdir()
    (root / "host").mkdir()
    (root / "host" / "allowed-providers").write_text("# the person's\nmine\n")
    assert ha.leftovers(root) == ["backend/streaming/providers/planted"]
    (providers / "planted").rmdir()
    (providers / "mine").rmdir()
    (providers / "mine").symlink_to(tmp_path)
    assert ha.leftovers(root) == ["backend/streaming/providers/mine"]


def test_a_folder_with_more_entries_than_one_listing_is_left_not_a_crash(checkout, monkeypatch):
    monkeypatch.setattr(ha, "DRAIN_MAX", 4)
    deep = checkout / "run" / "requests" / "update"
    deep.mkdir()
    for i in range(10):
        (deep / f"x{i}").write_text("")
    asked, left = ha.drain(checkout)
    assert asked == ["update"] and left > 0


def test_env_lines_are_split_at_newlines_alone(tmp_path):
    env = tmp_path / ".env"
    env.write_bytes(b"A=1\r\nB=x\x0cy\nSAUTIUM_BACKEND_IMAGE=old\n")
    ha.set_env(env, "SAUTIUM_BACKEND_IMAGE", "new")
    assert env.read_bytes() == b"A=1\r\nB=x\x0cy\nSAUTIUM_BACKEND_IMAGE=new\n"
