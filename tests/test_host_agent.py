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
    assert asked == [] and left == 7


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
                                "release": "v0.3.0", "report": None}


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


def test_the_nodes_door_admits_the_channels_key_on_the_answer_alone(monkeypatch):
    sys.path.insert(0, str(REPO / "backend"))
    import auth_hmac
    import host_channel
    monkeypatch.setenv("SAUTIUM_HOST_CHANNEL_KEY", KEY)
    door = auth_hmac.HMACAuthMiddleware(None, Path("/nonexistent"))
    monkeypatch.setattr(door, "_get_secret", lambda: b"s" * 64)
    digest = hashlib.sha256(b"{}").hexdigest()

    def judged(path, key):
        sig = auth_hmac._mac(key, "POST", path, "1", "0" * 32, digest)
        return door._judge({}, "POST", path, path, "1", 1, "0" * 32, digest, sig)

    scope = {}
    sig = auth_hmac._mac(KEY.encode(), "POST", host_channel.ANSWER_PATH, "2", "0" * 32, digest)
    refusal, _ = door._judge(scope, "POST", host_channel.ANSWER_PATH, host_channel.ANSWER_PATH,
                             "2", 2, "0" * 32, digest, sig)
    assert refusal is None and scope["state"]["signer"] == "host-agent"
    assert judged("/api/host/update", KEY.encode())[0] is not None          # nowhere else
    assert judged("/api/settings", KEY.encode())[0] is not None
    assert judged(host_channel.ANSWER_PATH, b"s" * 64)[0] is not None       # no other signer there
    monkeypatch.delenv("SAUTIUM_HOST_CHANNEL_KEY")
    assert judged(host_channel.ANSWER_PATH, KEY.encode())[0] is not None    # no agent, no door


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
    text = "setup code: K7QM-ZP4X-9HTR\nnot one: K7QM-ZP4X\n"
    assert ha._scrub(text) == "setup code: XXXX-XXXX-XXXX\nnot one: K7QM-ZP4X\n"
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


def _verified(checkout, monkeypatch, at=0.0):
    head = "f" * 40
    monkeypatch.setattr(ha, "head", lambda root=None: head)
    return {"installed": {"name": "v0.3.0", "commit": head, "at": at}}


def test_the_starter_moves_only_after_a_verified_release_ran_healthy_for_its_time(checkout, monkeypatch):
    current, previous, trial = ha._starter_paths(checkout)
    current.write_bytes(b"#!/bin/sh\n# the old starter\n")
    probes = []
    state = _verified(checkout, monkeypatch)
    assert ha.maintain_starter(state, ha.STARTER_AFTER - 1, checkout, lambda: probes.append(1)) is None
    assert ha.maintain_starter({**state, "failed_since_installed": True}, ha.STARTER_AFTER, checkout,
                               lambda: probes.append(1)) is None
    assert ha.maintain_starter({}, ha.STARTER_AFTER, checkout, lambda: probes.append(1)) is None
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
    assert calls == [("daemon-reload",), ("enable", "--now", f"{ha.UNIT}.path")]
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


def test_the_migration_ledger_is_read_by_its_own_column():
    """The update compares the ledger before and after a failed start to
    know whether to restore: on the Pi the first try named a column the
    table does not have, and the update failed before it moved."""
    sys.path.insert(0, str(REPO))
    from desktop import db_init
    source = BODY.read_text()
    assert f"SELECT string_agg(filename, ',' ORDER BY filename) FROM {db_init.MIGRATIONS_TABLE}" in source
    assert "filename VARCHAR(255) NOT NULL UNIQUE" in Path(db_init.__file__).read_text()
