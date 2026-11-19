"""The console setup (./sautium → scripts/setup.py; PI-NODE-2B §5): one
standard-library file, run as the person, that prints every root step and
runs none.

- What it reads of the machine and decides from it: memory, the preset's
  rule (desktop/machine_tier.py, the one the backend and the launcher
  apply), whether Docker's data is on a card, PostgreSQL's sizes.
- .env: only its own keys, a person's lines kept, the database's password
  made only at the volume's birth, a living node never born again, --preset
  moving what the image carries and nothing else.
- The roots: every refused path; the override it writes; what an earlier
  run named, read back from that file.
- What a container wrote, read as a stranger's bytes: the setup code through
  a link, a FIFO, an oversized file, a wrong form — each refused.
- Root: refused as root; no command it runs is sudo; the root blocks are
  printed, and --yes never waits. The automount's script: its shell parses,
  and with the system's tools replaced it mounts a plugged disk read-only
  under its UUID with the pinned time options, and leaves alone the system's
  own disk, a disk already mounted and a UUID that is not one.
- remove: the data only after the nickname; run twice, the second finds
  nothing left.
"""

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SOURCE = (REPO / "scripts" / "setup.py").read_text(encoding="utf-8")


def _load():
    spec = importlib.util.spec_from_file_location("sautium_setup", REPO / "scripts" / "setup.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


setup = _load()


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    """A checkout as the setup sees one, with nothing of Docker's behind it."""
    root = tmp_path / "sautium"
    (root / "host").mkdir(parents=True)
    (root / "data" / "node_identity").mkdir(parents=True)
    monkeypatch.setattr(setup, "ROOT", root)
    monkeypatch.setattr(setup.agent, "ROOT", root)
    return root


# -- the machine -----------------------------------------------------------------------

def test_the_machines_memory_and_versions_are_read_as_the_system_prints_them():
    meminfo = "MemTotal:        3884380 kB\nMemFree:          120000 kB\nMemAvailable:    2100000 kB\n"
    assert setup.mem_total_kib(meminfo) == 3884380
    with pytest.raises(setup.Stop):
        setup.mem_total_kib("MemFree: 1 kB\n")
    assert setup.version_tuple("29.9.3") == (29, 9) and setup.version_tuple("v2.24.5-desktop.1") == (2, 24)
    assert setup.version_tuple("25") == (25, 0) and setup.version_tuple("nonsense") == (0, 0)
    assert setup.version_tuple("24.0.7") < setup.DOCKER_MIN <= setup.version_tuple("25.0.0")


def test_the_preset_is_read_off_the_machine_by_the_one_rule():
    preset = setup.machine_tier.birth_preset
    assert preset(3.7, False, 100)[0] == "nano"              # a 4 GB board
    assert preset(7.4, False, 100)[0] == "nano" and preset(7.6, False, 100)[0] is None   # "8 GB" reads 7.6
    assert preset(15.5, True, 100) == ("nano", "Docker's data is on a card or a removable disk")
    assert preset(15.5, False, 24.9)[0] == "nano" and preset(15.5, False, 25)[0] is None
    assert preset(15.5, False, None)[0] is None              # unknown free space decides nothing
    # The tier rule is the same module's, and the backend and the launcher read it there
    assert setup.machine_tier.auto_tier("cuda", 24, 32) == "full"
    assert setup.machine_tier.auto_tier("cuda", 24, 8) == "standard"
    assert setup.machine_tier.auto_tier("cpu", 4, 4) == "lite"
    for path in ("backend/hardware_profile.py", "desktop/utils.py"):
        assert "from desktop.machine_tier import auto_tier" in (REPO / path).read_text(encoding="utf-8")


LSBLK = json.dumps({"blockdevices": [
    {"name": "mmcblk0", "rm": False, "hotplug": False, "tran": None, "children": [
        {"name": "mmcblk0p1", "rm": False, "hotplug": False, "tran": None},
        {"name": "mmcblk0p2", "rm": False, "hotplug": False, "tran": None}]},
    {"name": "nvme0n1", "rm": False, "hotplug": False, "tran": "nvme", "children": [
        {"name": "nvme0n1p2", "rm": False, "hotplug": False, "tran": "nvme"}]},
    {"name": "sda", "rm": True, "hotplug": True, "tran": "usb", "children": [
        {"name": "sda1", "rm": True, "hotplug": True, "tran": None}]},
    {"name": "sdb", "rm": False, "hotplug": False, "tran": "usb", "children": [{"name": "sdb1"}]}]})


def test_dockers_data_on_a_card_or_a_removable_disk_is_told_from_lsblk():
    assert setup.device_removable("/dev/mmcblk0p2", LSBLK)            # the Pi's card
    assert not setup.device_removable("/dev/nvme0n1p2", LSBLK)
    assert setup.device_removable("/dev/sda1", LSBLK)                 # removable
    assert setup.device_removable("/dev/sdb1", LSBLK)                 # a USB disk, by its parent
    assert not setup.device_removable("overlay", LSBLK) and not setup.device_removable("/dev/sda1", "not json")


def test_postgres_is_sized_for_the_board():
    assert setup.pg_settings(15.5) == {}                              # the compose file's own defaults
    assert setup.pg_settings(7.6)["PG_SHARED_BUFFERS"] == "512MB"
    assert setup.pg_settings(3.7) == {"PG_SHARED_BUFFERS": "256MB", "PG_EFFECTIVE_CACHE_SIZE": "1GB",
                                      "PG_MAINTENANCE_WORK_MEM": "128MB", "PG_MAX_WAL_SIZE": "1GB"}
    small = setup.pg_settings(1.8)
    assert small["PG_SHARED_BUFFERS"] == "128MB" and small["PG_WORK_MEM"] == "4MB"
    compose = (REPO / "docker-compose.pi.yml").read_text(encoding="utf-8")
    for key in set(setup.pg_settings(1.8)):
        assert "${%s:-" % key in compose, key                          # every key it writes is one the file reads


# -- .env --------------------------------------------------------------------------------

def _plan(held, **kw):
    given = dict(preset="nano", asked_preset=None, ram_gib=3.7, assistant="none", volume_exists=False)
    given.update(kw)
    return setup.env_plan(held, **given)


def test_a_first_run_writes_the_birth_and_what_the_image_carries():
    plan = _plan({})
    assert len(plan.pop("POSTGRES_PASSWORD")) >= 32
    assert plan == {
        "PG_SHARED_BUFFERS": "256MB", "PG_EFFECTIVE_CACHE_SIZE": "1GB", "PG_MAINTENANCE_WORK_MEM": "128MB",
        "PG_MAX_WAL_SIZE": "1GB", "SAUTIUM_BIRTH_PRESET": "nano", "SAUTIUM_IMAGE_MODELS": "0",
        "SAUTIUM_IMAGE_STREAMING": "0", "SAUTIUM_IMAGE_ASSISTANT_CLI": "0", "CLAUDE_CODE_ENABLED": "false",
        "DEFAULT_PROVIDER": "none", "COMPOSE_FILE": "docker-compose.pi.yml:host/compose.roots.yml",
        "MUSIC_LIBRARY_PATH": "./data/no-library"}
    full = _plan({}, preset=None, ram_gib=15.5, assistant="claude")
    assert "SAUTIUM_BIRTH_PRESET" not in full and "PG_SHARED_BUFFERS" not in full
    assert (full["SAUTIUM_IMAGE_MODELS"], full["SAUTIUM_IMAGE_STREAMING"],
            full["SAUTIUM_IMAGE_ASSISTANT_CLI"], full["CLAUDE_CODE_ENABLED"]) == ("1", "1", "1", "true")
    # What the agent's update builds with is what the setup records
    assert {key for _, key in setup.agent.IMAGE_ARGS} <= set(full)


def test_a_node_that_lives_is_not_born_again_and_keeps_its_password():
    held = {"POSTGRES_PASSWORD": "kept", "SAUTIUM_IMAGE_MODELS": "0", "MUSIC_LIBRARY_PATH": "/srv/music",
            "DEFAULT_PROVIDER": "anthropic", "LASTFM_API_KEY": "the person's"}
    plan = _plan(held, preset=None, volume_exists=True)
    for key in ("POSTGRES_PASSWORD", "SAUTIUM_BIRTH_PRESET", "MUSIC_LIBRARY_PATH", "DEFAULT_PROVIDER",
                "LASTFM_API_KEY"):
        assert key not in plan, key
    assert plan["SAUTIUM_IMAGE_MODELS"] == "0"                # the image stays what it was
    # A volume with no password line in .env: none is invented over the database's own
    assert "POSTGRES_PASSWORD" not in _plan({}, volume_exists=True)
    assert "SAUTIUM_BIRTH_PRESET" not in _plan({}, volume_exists=True)
    # --preset full on an install set up without the models: the image gains them, nothing is born
    grown = _plan(held, preset=None, asked_preset="full", volume_exists=True)
    assert (grown["SAUTIUM_IMAGE_MODELS"], grown["SAUTIUM_IMAGE_STREAMING"]) == ("1", "1")
    assert "SAUTIUM_BIRTH_PRESET" not in grown


def test_the_env_file_keeps_every_line_the_setup_does_not_own(checkout):
    env = checkout / ".env"
    env.write_text("# mine\nLASTFM_API_KEY=abc\nPOSTGRES_PASSWORD=kept\nSAUTIUM_IMAGE_MODELS=1\n")
    changed = setup.write_env({"SAUTIUM_IMAGE_MODELS": "0", "COMPOSE_FILE": "docker-compose.pi.yml"})
    assert changed == ["SAUTIUM_IMAGE_MODELS", "COMPOSE_FILE"]
    assert env.read_text() == ("# mine\nLASTFM_API_KEY=abc\nPOSTGRES_PASSWORD=kept\nSAUTIUM_IMAGE_MODELS=0\n"
                               "COMPOSE_FILE=docker-compose.pi.yml\n")
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert setup.write_env({"SAUTIUM_IMAGE_MODELS": "0"}) == []        # run again: nothing to change
    env.unlink()
    setup.write_env({"DEFAULT_PROVIDER": "none"})
    assert stat.S_IMODE(env.stat().st_mode) == 0o600 and "DEFAULT_PROVIDER=none" in env.read_text()


# -- the roots ---------------------------------------------------------------------------

def test_a_root_is_a_folder_of_music_never_a_part_of_the_system(checkout, tmp_path):
    music = tmp_path / "home" / "pi" / "Music"
    music.mkdir(parents=True)
    assert setup.root_refusal(str(music), checkout) is None
    for refused in ("/", "/home", "/usr", "/var", "/etc", "/run", "/proc", "/sys", "/dev", "/mnt", "/media",
                    "/usr/share", "/etc/ssl", "relative/music", str(checkout), str(checkout / "data"),
                    str(checkout.parent), str(tmp_path / "none"), str(music) + ":x", str(music) + " x",
                    "/home/$USER/Music"):
        assert setup.root_refusal(refused, checkout) is not None, refused
    home = Path.home()
    if home.parts[:2] == ("/", "home") and len(home.parts) == 3:
        assert "whole home" in setup.root_refusal(str(home), checkout)
    # A link is judged by where it leads
    link = tmp_path / "music-link"
    link.symlink_to("/etc")
    assert setup.root_refusal(str(link), checkout) is not None


def test_the_override_binds_each_root_at_its_own_path_read_only(checkout, tmp_path):
    music = tmp_path / "Music"
    music.mkdir()
    roots = setup.chosen_roots(str(music), [])
    assert roots == [str(music), setup.AUTOMOUNT_BASE]
    text = setup.roots_override(roots)
    assert f'SAUTIUM_LIBRARY_ROOTS: "{music}:{setup.AUTOMOUNT_BASE}"' in text
    assert text.count("read_only: true") == text.count("propagation: rslave") == text.count("- type: bind")
    assert f'source: "{music}"' in text and f'target: "{music}"' in text
    if not os.path.isdir(setup.AUTOMOUNT_BASE):
        # named, and not bound until it is there: Docker would make it as root
        assert text.count("- type: bind") == 1
    yaml = pytest.importorskip("yaml")
    parsed = yaml.safe_load(text)["services"]["backend"]
    assert parsed["environment"]["SAUTIUM_LIBRARY_ROOTS"].split(":")[0] == str(music)
    assert all(v["read_only"] and v["bind"] == {"propagation": "rslave"} and v["source"] == v["target"]
               for v in parsed["volumes"])
    # The file is the record of what an earlier run named
    (checkout / "host" / "compose.roots.yml").write_text(text)
    held = setup.held_roots(checkout / "host" / "compose.roots.yml")
    assert held == roots and setup.chosen_roots(None, held) == roots
    assert setup.held_roots(checkout / "host" / "none.yml") == []
    with pytest.raises(setup.Stop, match="--music /etc"):
        setup.chosen_roots("/etc", [])
    # With no folder named the node still picks: the automount's folder is its one root
    only = setup.roots_override(setup.chosen_roots(None, []))
    assert f'SAUTIUM_LIBRARY_ROOTS: "{setup.AUTOMOUNT_BASE}"' in only


def test_the_node_reads_the_roots_the_override_names():
    """One variable, the setup's and the node's (backend/library_root.py)."""
    assert "SAUTIUM_LIBRARY_ROOTS" in SOURCE
    assert "sautium_library_roots" in (REPO / "backend" / "config.py").read_text(encoding="utf-8")
    assert "SAUTIUM_BIRTH_PRESET: ${SAUTIUM_BIRTH_PRESET:-}" in (
        REPO / "docker-compose.pi.yml").read_text(encoding="utf-8")
    assert setup.NO_LIBRARY == "./data/no-library"
    assert "data/no-library/" in (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()


# -- what a container wrote ----------------------------------------------------------------

def test_the_setup_code_is_printed_only_as_a_regular_small_file_of_the_codes_form(checkout, tmp_path):
    path = checkout / "data" / "node_identity" / "setup_code.txt"
    assert setup.setup_code() is None                                  # no file
    path.write_text("D889-VPL4\n")
    assert setup.setup_code() == "D889-VPL4"
    for wrong in ("D889-VPL4-CT9Y\n", "d889-vpl4\n", "D889-VPL4; rm -rf ~\n", "\x1b[31mD889-VPL4\n",
                  "0000-1111\n", "A" * 5000):
        path.write_text(wrong)
        assert setup.setup_code() is None, wrong
    path.unlink()
    secret = tmp_path / "the-persons-file"
    secret.write_text("ABCD-EFGH\n")
    path.symlink_to(secret)                                            # a link is not followed
    assert setup.setup_code() is None
    path.unlink()
    os.mkfifo(path)                                                    # a FIFO is not opened for good
    assert setup.setup_code() is None
    path.unlink()
    path.mkdir()
    assert setup.setup_code() is None


def test_the_agent_and_the_setup_and_the_node_spell_the_code_alike():
    backend = (REPO / "backend" / "device_auth.py").read_text(encoding="utf-8")
    assert "SETUP_CODE_GROUPS = 2" in backend
    assert setup.SETUP_CODE.fullmatch("K7QM-ZP4X") and not setup.SETUP_CODE.fullmatch("K7QM-ZP4X-9HTR")
    assert setup.agent.SETUP_CODE.fullmatch("K7QM-ZP4X")


def test_a_picture_that_is_not_only_blocks_is_not_shown(monkeypatch):
    monkeypatch.setattr(setup.agent, "image_of", lambda root: "sautium-backend:local")
    asked = []

    def fake(args, **kw):
        asked.append((args, kw.get("stdin")))
        return subprocess.CompletedProcess(args, 0, drawn.encode(), b"")
    monkeypatch.setattr(setup, "run", fake)
    drawn = "██▀▄  \n▄▄██  \n"
    assert setup.qr("http://192.0.2.10:8800/#setup=D889-VPL4") == drawn
    args, stdin = asked[0]
    # The text goes in on stdin, never on the command line; the container sees nothing
    assert stdin == b"http://192.0.2.10:8800/#setup=D889-VPL4" and "D889-VPL4" not in " ".join(args)
    for flag in ("--network", "none", "--read-only", "--cap-drop", "ALL"):
        assert flag in args
    assert "-v" not in args and "--volume" not in args and "--mount" not in args
    drawn = "██ \x1b]0;evil\x07\n"
    assert setup.qr("x") is None


def test_the_hosts_request_is_signed_as_the_nodes_door_checks_it(checkout, monkeypatch):
    sys.path.insert(0, str(REPO / "backend"))
    auth_hmac = pytest.importorskip("auth_hmac")
    (checkout / "data" / "node_identity" / ".api_secret").write_text("the-nodes-secret\n")
    seen = {}

    class Opener:
        def open(self, request, timeout):
            seen.update(url=request.full_url, headers=dict(request.header_items()))

            class Response:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def read(self, n):
                    return b'{"code": "ABCD-EFGH"}'
            return Response()
    monkeypatch.setattr(setup.urllib.request, "build_opener", lambda *handlers: Opener())
    assert setup.signed_get("/api/auth/pin") == {"code": "ABCD-EFGH"}
    headers = {k.lower(): v for k, v in seen["headers"].items()}
    assert seen["url"] == "http://127.0.0.1:8800/api/auth/pin"
    assert headers["x-sautium-sig"] == auth_hmac.sign(
        b"the-nodes-secret", "GET", "/api/auth/pin", headers["x-sautium-ts"], b"", headers["x-sautium-nonce"])
    (checkout / "data" / "node_identity" / ".api_secret").unlink()
    assert setup.signed_get("/api/auth/pin") is None                   # no secret: nothing is sent unsigned


# -- root ------------------------------------------------------------------------------------

def test_the_setup_refuses_root_and_runs_no_sudo(monkeypatch, capsys):
    monkeypatch.setattr(setup.os, "geteuid", lambda: 0)
    assert setup.main([]) == 1
    assert "not as root" in capsys.readouterr().out
    for command in ("status", "pair", "update", "remove"):
        assert setup.main([command]) == 1
    # Every sudo in the file is inside a line it prints
    for line in SOURCE.splitlines():
        if "sudo" in line and not line.lstrip().startswith("#"):
            assert "run([" not in line and "subprocess" not in line and "Popen" not in line, line
    assert '["sudo"' not in SOURCE and "'sudo'," not in SOURCE


def test_the_root_steps_are_printed_whole_and_yes_never_waits(checkout, monkeypatch, capsys):
    monkeypatch.setattr(setup, "lingering", lambda: False)
    monkeypatch.setattr(setup, "automount_installed", lambda: False)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("--yes waited for a key"))
    setup.root_steps(types.SimpleNamespace(yes=True), {})
    printed = capsys.readouterr().out
    user = os.environ.get("USER") or str(os.getuid())
    assert f"    sudo loginctl enable-linger {user}" in printed
    assert f"    sudo loginctl disable-linger {user}" in printed          # its undo beside it
    assert "    sudo install -m 0755 " in printed and "/usr/local/lib/sautium/automount" in printed
    assert "<" not in printed.replace("<its UUID>", "")                   # no placeholder to edit
    assert "Updates while signed out: not set" in printed and "Plugged disks: not set" in printed
    files = sorted(p.name for p in (checkout / "host" / "automount").iterdir())
    assert files == ["99-sautium-automount.rules", "automount", "sautium-automount@.service"]
    # Nothing to print where both are in place
    monkeypatch.setattr(setup, "lingering", lambda: True)
    monkeypatch.setattr(setup, "automount_installed", lambda: True)
    setup.root_steps(types.SimpleNamespace(yes=False), {})
    assert capsys.readouterr().out == ""


TOOLS = {
    "findmnt": '''#!/bin/sh
case "$*" in
  "-n -o SOURCE /") echo /dev/nvme0n1p2 ;;
  "-n -S "*) grep -qx "$3" "$STUB/mounted-devices" 2>/dev/null ;;
  "-n "*) grep -qx "$2" "$STUB/mounted-points" 2>/dev/null ;;
esac
''',
    "lsblk": '''#!/bin/sh
case "$4" in /dev/nvme0n1p2) echo nvme0n1 ;; /dev/nvme0n1p3) echo nvme0n1 ;; *) cat "$STUB/pkname" 2>/dev/null ;; esac
''',
    "blkid": '''#!/bin/sh
cat "$STUB/blkid-$4" 2>/dev/null
''',
    "mount": '''#!/bin/sh
echo "$*" >> "$STUB/mount-calls"
[ ! -e "$STUB/mount-fails" ] || { case "$*" in *time_offset*) exit 32 ;; esac; }
echo "$6" >> "$STUB/mounted-points"
''',
    "umount": '''#!/bin/sh
echo "$*" >> "$STUB/umount-calls"
''',
}


@pytest.fixture
def automount(tmp_path, monkeypatch):
    """The automount's script with the system's tools replaced by records of
    what they were asked, over a base folder of the test's own. Needs a real
    block device node to name: the loop device every Linux has."""
    if not (os.path.exists("/dev/loop0") and stat.S_ISBLK(os.stat("/dev/loop0").st_mode)):
        pytest.skip("no block device node to name")
    base, state, stub, bin_dir = (tmp_path / n for n in ("media", "state", "stub", "bin"))
    for folder in (base, stub, bin_dir):
        folder.mkdir()
    for name, text in TOOLS.items():
        (bin_dir / name).write_text(text)
        (bin_dir / name).chmod(0o755)
    files = setup.automount_files(tmp_path / "out", 1000, 1000, base=str(base), state=str(state))
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "STUB": str(stub)}

    def plug(verb="mount", name="loop0", **blkid):
        for key, value in blkid.items():
            (stub / f"blkid-{key}").write_text(value + "\n")
        return subprocess.run(["sh", str(files["script"]), verb, name], env=env, capture_output=True, text=True)

    def calls(tool="mount"):
        path = stub / f"{tool}-calls"
        return path.read_text().splitlines() if path.exists() else []
    return types.SimpleNamespace(plug=plug, calls=calls, base=base, state=state, stub=stub, files=files)


def test_the_automounts_shell_parses_and_carries_the_owner(automount):
    for name in ("script",):
        assert subprocess.run(["sh", "-n", str(automount.files[name])]).returncode == 0
    script = automount.files["script"].read_text()
    assert "OWNER=1000:1000" in script and "set -eu" in script
    assert stat.S_IMODE(automount.files["script"].stat().st_mode) == 0o755
    unit = automount.files["unit"].read_text()
    assert "ExecStart=/usr/local/lib/sautium/automount mount %I" in unit and "BindsTo=dev-%i.device" in unit
    rule = automount.files["rule"].read_text()
    assert 'SUBSYSTEMS=="usb"' in rule and 'KERNEL=="mmcblk[0-9]*"' in rule
    # Root runs only the installed copy, never a file the person's own user can write
    assert str(automount.files["script"].parent) not in unit


@pytest.mark.parametrize("fstype, driver, time", [
    ("vfat", "vfat", "tz=UTC"), ("exfat", "exfat", "time_offset=0"), ("ntfs", "ntfs3", None),
    ("ext4", "ext4", None)])
def test_a_plugged_disk_is_mounted_read_only_under_its_uuid(automount, fstype, driver, time):
    result = automount.plug(TYPE=fstype, UUID="4A21-0000", LABEL="Elements <b>")
    assert result.returncode == 0, result.stderr
    (call,) = automount.calls()
    point = automount.base / "4A21-0000"
    assert call.startswith(f"-t {driver} -o ro,nosuid,nodev,noexec") and call.endswith(f"/dev/loop0 {point}")
    if fstype == "ext4":
        assert "noload" in call and "uid=" not in call
    else:
        assert "uid=1000,gid=1000,fmask=0133,dmask=0022" in call
    assert (time in call) if time else ("tz=" not in call and "time_offset" not in call)
    assert point.is_dir() and (automount.state / "loop0").read_text().strip() == "4A21-0000"
    # The label is a stranger's text, left as a file the node reads as text
    assert (automount.base / ".names" / "4A21-0000").read_text() == "Elements <b>\n"
    assert automount.plug("unmount").returncode == 0
    assert automount.calls("umount") == [f"-l {point}"] and not point.exists()
    assert not (automount.state / "loop0").exists() and not (automount.base / ".names" / "4A21-0000").exists()
    assert automount.plug("unmount").returncode == 0                   # again: nothing to do


def test_what_is_not_a_plugged_music_disk_is_left_alone(automount):
    def left(**blkid):
        before = len(automount.calls())
        assert automount.plug(**blkid).returncode == 0
        return len(automount.calls()) == before
    assert left(TYPE="swap", UUID="4A21-0000")                         # no such filesystem
    assert left(TYPE="vfat", UUID="")                                  # no UUID
    assert left(TYPE="vfat", UUID="../../etc")                         # not one
    assert left(TYPE="vfat", UUID="4A21 0000; reboot")
    (automount.stub / "pkname").write_text("nvme0n1\n")                # the system's own disk
    assert left(TYPE="vfat", UUID="4A21-0000")
    (automount.stub / "pkname").write_text("sda\n")
    (automount.stub / "mounted-devices").write_text("/dev/loop0\n")    # something else mounted it
    assert left(TYPE="vfat", UUID="4A21-0000")
    (automount.stub / "mounted-devices").unlink()
    for name in ("", "sda1; reboot", "../sda1", "sda 1"):
        assert automount.plug(name=name, TYPE="vfat", UUID="4A21-0000").returncode == 64, name
    assert automount.plug("format", TYPE="vfat", UUID="4A21-0000").returncode == 64
    assert automount.calls() == []


def test_a_second_volume_claiming_a_mounted_uuid_gets_another_name(automount):
    (automount.stub / "mounted-points").write_text(f"{automount.base / '4A21-0000'}\n")
    assert automount.plug(TYPE="vfat", UUID="4A21-0000").returncode == 0
    assert automount.calls()[0].endswith(f"{automount.base / '4A21-0000-loop0'}")


def test_an_exfat_driver_that_refuses_the_time_option_still_mounts_and_says_so(automount):
    (automount.stub / "mount-fails").write_text("")
    result = automount.plug(TYPE="exfat", UUID="4A21-0000")
    assert result.returncode == 0 and "mounted without time_offset" in result.stderr
    assert len(automount.calls()) == 2 and "time_offset" not in automount.calls()[1]


# -- the build's resume, the removal ---------------------------------------------------------

def test_a_run_started_again_does_not_build_what_is_built(checkout, monkeypatch):
    monkeypatch.setattr(setup.agent, "head", lambda root: "c" * 40)
    monkeypatch.setattr(setup.agent, "image_of", lambda root: "sautium-backend:local")
    started = []
    monkeypatch.setattr(setup.subprocess, "Popen", lambda *a, **k: started.append(a) or pytest.fail("built again"))
    monkeypatch.setattr(setup, "run", lambda args, **kw: subprocess.CompletedProcess(args, 0, b"sha256:abc\n", b""))
    env = {"SAUTIUM_IMAGE_MODELS": "0", "SAUTIUM_IMAGE_ASSISTANT_CLI": "0", "SAUTIUM_IMAGE_STREAMING": "0"}
    state = {"built": setup.image_key(env)}
    setup.build(state, env)
    assert started == []
    # Another commit, or other arguments, is another image
    assert setup.image_key({**env, "SAUTIUM_IMAGE_MODELS": "1"}) != setup.image_key(env)
    setup.save_state({"built": "x", "build": {"pid": 1, "key": "k"}})
    assert setup.load_state() == {"built": "x", "build": {"pid": 1, "key": "k"}}
    assert stat.S_IMODE(setup.state_path().stat().st_mode) == 0o600
    setup.state_path().write_text("not json")
    assert setup.load_state() == {}


@pytest.fixture
def installed(checkout, monkeypatch):
    """A node as a setup left it, with Docker and systemd replaced by a
    record of what they were asked."""
    asked = []
    monkeypatch.setattr(setup.os, "geteuid", lambda: 1000)      # the person, wherever the suite runs
    monkeypatch.setattr(setup, "run", lambda args, **kw: asked.append(args) or subprocess.CompletedProcess(args, 0, b"", b""))
    monkeypatch.setattr(setup.agent, "uninstall", lambda root: asked.append(["agent", "uninstall"]) or 0)
    monkeypatch.setattr(setup.agent, "compose_cmd", lambda root=None: ["docker", "compose"])
    monkeypatch.setattr(setup.agent, "unit_dir", lambda: checkout / "units")
    monkeypatch.setattr(setup, "lingering", lambda: True)
    monkeypatch.setattr(setup, "automount_installed", lambda: False)
    (checkout / ".env").write_text("SAUTIUM_BACKEND_IMAGE=sautium-backend:v0.3.0-abc\n")
    (checkout / "host" / "agent.env").write_text("SAUTIUM_HOST_CHANNEL_KEY=k\n")
    (checkout / "host" / "bin").mkdir()
    (checkout / "host" / "compose.roots.yml").write_text("services: {}\n")
    (checkout / "data" / "node_identity" / "node_info.json").write_text(json.dumps({"username": "owner"}))
    (checkout / "data" / "backup").mkdir()
    (checkout / "data" / "backup" / "x.sbk").write_bytes(b"a backup")
    (checkout / "backend" / "data").mkdir(parents=True)
    return types.SimpleNamespace(root=checkout, asked=asked)


def test_remove_takes_what_runs_and_keeps_the_data(installed, capsys):
    assert setup.main(["remove"]) == 0
    said = capsys.readouterr().out
    joined = [" ".join(a) for a in installed.asked]
    assert joined[0] == "docker compose stop"                          # stopped first: its port mapping goes
    assert "docker compose down --remove-orphans --rmi local" in joined      # no --volumes
    assert "agent uninstall" in joined
    assert (installed.root / "data" / "backup" / "x.sbk").exists() and (installed.root / ".env").exists()
    assert not (installed.root / "host" / "bin").exists()
    assert not (installed.root / "host" / "compose.roots.yml").exists()
    assert "sudo loginctl disable-linger" in said and "remove --data takes those" in said
    assert not any("sudo" in line for line in joined)
    # Run again: nothing left to take, and it says so
    installed.asked.clear()
    (installed.root / "host" / "agent.env").unlink(missing_ok=True)
    assert setup.main(["remove"]) == 0
    assert "agent uninstall" not in [" ".join(a) for a in installed.asked]


def test_the_data_goes_only_after_the_nodes_nickname(installed, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda prompt: "someone else")
    assert setup.main(["remove", "--data"]) == 1
    assert "nothing was removed" in capsys.readouterr().out and installed.asked == []
    assert (installed.root / "data" / "backup" / "x.sbk").exists()
    monkeypatch.setattr("builtins.input", lambda prompt: "owner")
    assert setup.main(["remove", "--data"]) == 0
    joined = [" ".join(a) for a in installed.asked]
    assert "docker compose down --remove-orphans --rmi local --volumes" in joined
    assert not (installed.root / "data").exists() and not (installed.root / ".env").exists()
    assert installed.root.exists()                                     # the folder itself is the person's to delete
    assert "rm -rf " in capsys.readouterr().out


def test_the_wrapper_runs_the_setup_isolated():
    wrapper = REPO / "sautium"
    assert os.access(wrapper, os.X_OK)
    text = wrapper.read_text()
    assert text.startswith("#!/bin/sh\n") and 'exec python3 -I "$here/scripts/setup.py" "$@"' in text
    assert subprocess.run(["sh", "-n", str(wrapper)]).returncode == 0
    # Standard library alone: the setup runs before anything is installed
    import ast
    tree = ast.parse(SOURCE)
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | {
        n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert imported <= set(sys.stdlib_module_names), imported - set(sys.stdlib_module_names)
