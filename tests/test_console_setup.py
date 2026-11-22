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
    assert setup.keeps_music_line(held)
    plan = _plan(held, preset=None, volume_exists=True, roots=False)
    for key in ("POSTGRES_PASSWORD", "SAUTIUM_BIRTH_PRESET", "MUSIC_LIBRARY_PATH", "DEFAULT_PROVIDER",
                "LASTFM_API_KEY"):
        assert key not in plan, key
    # An install made by hand keeps its music line and its compose files: no roots, no folder to pick
    assert plan["COMPOSE_FILE"] == "docker-compose.pi.yml"
    assert "COMPOSE_FILE" not in _plan({**held, "COMPOSE_FILE": "docker-compose.pi.yml:mine.yml"},
                                       preset=None, volume_exists=True, roots=False)
    assert not setup.keeps_music_line({}) and not setup.keeps_music_line({"MUSIC_LIBRARY_PATH": ""})
    assert not setup.keeps_music_line({"MUSIC_LIBRARY_PATH": setup.NO_LIBRARY})
    assert plan["SAUTIUM_IMAGE_MODELS"] == "0"                # the image stays what it was
    # A volume with no password line in .env: none is invented over the database's own
    assert "POSTGRES_PASSWORD" not in _plan({}, volume_exists=True)
    assert "SAUTIUM_BIRTH_PRESET" not in _plan({}, volume_exists=True)
    # --preset full on an install set up without the models: the image gains them, nothing is born
    grown = _plan(held, preset=None, asked_preset="full", volume_exists=True, roots=False)
    assert (grown["SAUTIUM_IMAGE_MODELS"], grown["SAUTIUM_IMAGE_STREAMING"]) == ("1", "1")
    assert "SAUTIUM_BIRTH_PRESET" not in grown


def test_the_birth_is_decided_until_the_node_is_born_and_never_after():
    born = setup.birth_of
    assert born({}, None, "nano", False) == "nano" and born({}, None, None, False) is None
    # An earlier run's line stands for the next run — a build that failed is started again as it was —
    assert born({"SAUTIUM_BIRTH_PRESET": "nano"}, None, None, False) == "nano"
    # — until --preset says otherwise: the line and the image's arguments then move together
    assert born({"SAUTIUM_BIRTH_PRESET": "nano"}, "full", "nano", False) is None
    unborn = _plan({"SAUTIUM_BIRTH_PRESET": "nano"}, preset=None, asked_preset="full")
    assert unborn["SAUTIUM_BIRTH_PRESET"] == "" and unborn["SAUTIUM_IMAGE_MODELS"] == "1"
    assert born({}, "nano", None, False) == "nano"
    # A node that lives is what it was born: neither the flag nor the machine's reading changes that
    assert born({"SAUTIUM_BIRTH_PRESET": "nano"}, "full", None, True) == "nano"
    assert born({}, "nano", "nano", True) is None
    with pytest.raises(setup.Stop, match="knows nano"):
        born({"SAUTIUM_BIRTH_PRESET": "tiny"}, None, None, False)


@pytest.fixture
def machine_ready(checkout, monkeypatch):
    """The setup's whole run on a checkout, with the machine, Docker and
    systemd replaced: what it writes is what the tests read."""
    ran = []
    for name, value in (
            ("check_checkout", lambda: None), ("check_machine", lambda: None), ("release_step", lambda: None),
            ("docker_state", lambda: ("ok", "Docker 29.1 with Compose 2.40.")),
            ("machine", lambda: {"ram_gib": 3.7, "docker_root": "/var/lib/docker", "docker_root_free_gb": 100.0,
                                 "docker_root_removable": False}),
            ("machine_line", lambda m, preset: "a small board"),
            ("database_volume_exists", lambda: False), ("node_running", lambda: False),
            ("ports_taken", lambda: []), ("undervoltage_now", lambda: False),
            ("lingering", lambda: True), ("automount_installed", lambda: True),
            ("build", lambda state, env: ran.append("build")), ("start", lambda: ran.append("start")),
            ("last_screen", lambda m, preset, passes_on: ran.append(("screen", preset, passes_on)))):
        monkeypatch.setattr(setup, name, value)
    monkeypatch.setattr(setup.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(setup.agent, "install", lambda root: 0)
    return types.SimpleNamespace(root=checkout, ran=ran)


def test_a_first_run_makes_a_node_that_picks_its_folder(machine_ready, tmp_path):
    music = tmp_path / "Music"
    music.mkdir()
    assert setup.main(["--music", str(music), "--yes"]) == 0
    env = setup.agent.read_env(machine_ready.root / ".env")
    assert env["COMPOSE_FILE"] == "docker-compose.pi.yml:host/compose.roots.yml"
    assert env["MUSIC_LIBRARY_PATH"] == setup.NO_LIBRARY and env["SAUTIUM_BIRTH_PRESET"] == "nano"
    assert setup.held_roots(machine_ready.root / "host" / "compose.roots.yml") == [str(music), setup.AUTOMOUNT_BASE]
    assert machine_ready.ran == ["build", "start", ("screen", "nano", False)]
    assert setup.load_state()["done"] is True


def test_a_folder_on_a_disk_no_fstab_line_mounts_is_said(machine_ready, monkeypatch, tmp_path, capsys):
    music = tmp_path / "Music"
    music.mkdir()
    answers = {("findmnt", "-n", "-o", "TARGET", "-T"): (0, b"/mnt/usb\n"), ("findmnt", "--fstab", "-n"): (1, b"")}

    def run(args, **kw):
        code, said = next((v for k, v in answers.items() if tuple(args[:len(k)]) == k), (0, b""))
        return subprocess.CompletedProcess(args, code, said, b"")
    monkeypatch.setattr(setup, "run", run)
    assert setup.mounted_outside_fstab(str(music)) == "/mnt/usb"
    assert setup.main(["--music", str(music), "--yes"]) == 0
    said = capsys.readouterr().out
    assert "is on a disk mounted at /mnt/usb, which no line of /etc/fstab mounts" in said
    assert "Sautium does not start: mount the disk" in said and "docker compose up -d" in said
    # The system's own disk, or one fstab mounts: nothing to say
    answers[("findmnt", "--fstab", "-n")] = (0, b"/mnt/usb /dev/sda1 ext4\n")
    assert setup.mounted_outside_fstab(str(music)) is None
    answers[("findmnt", "-n", "-o", "TARGET", "-T")] = (0, b"/\n")
    answers[("findmnt", "--fstab", "-n")] = (1, b"")
    assert setup.mounted_outside_fstab(str(music)) is None
    assert setup.main(["--music", str(music), "--yes"]) == 0
    assert "no line of /etc/fstab" not in capsys.readouterr().out


def test_an_install_made_by_hand_keeps_its_music_line(machine_ready, monkeypatch, tmp_path, capsys):
    """Its files keep their paths: the setup gives it no roots, no folder to
    pick and no automount, and --music is refused with the way to change over."""
    (machine_ready.root / ".env").write_text("MUSIC_LIBRARY_PATH=/srv/music\nPOSTGRES_PASSWORD=kept\n")
    monkeypatch.setattr(setup, "database_volume_exists", lambda: True)
    monkeypatch.setattr(setup, "automount_installed", lambda: False)
    assert setup.main(["--yes"]) == 0
    env = setup.agent.read_env(machine_ready.root / ".env")
    assert env["MUSIC_LIBRARY_PATH"] == "/srv/music" and env["COMPOSE_FILE"] == "docker-compose.pi.yml"
    assert not (machine_ready.root / "host" / "compose.roots.yml").exists()
    assert "SAUTIUM_LIBRARY_ROOTS" not in (machine_ready.root / ".env").read_text()
    assert "automount" not in capsys.readouterr().out
    assert machine_ready.ran[:2] == ["build", "start"]
    machine_ready.ran.clear()
    music = tmp_path / "Music"
    music.mkdir()
    assert setup.main(["--music", str(music), "--yes"]) == 1
    assert "delete that line from .env" in capsys.readouterr().out and machine_ready.ran == []


def test_a_folder_on_a_disk_the_desktop_mounts_gets_its_boot_mount_first(machine_ready, monkeypatch, capsys):
    """Bound as it is, the node would not start before someone signs in to
    the desktop: the setup prints the one fstab line and stops, and names the
    folder to give it afterwards."""
    found = json.dumps({"filesystems": [{"source": "/dev/sda1", "target": "/media/pi/Elements",
                                         "fstype": "exfat", "uuid": "4A21-0000"}]})
    plan = setup.boot_mount("/media/pi/Elements/Music/Jazz", found, 1000, 1000)
    systemd = ",x-systemd.before=docker.service,x-systemd.device-timeout=10s 0 0"
    assert plan == {
        "point": "/mnt/sautium-4A21-0000", "folder": "/mnt/sautium-4A21-0000/Music/Jazz", "device": "/dev/sda1",
        "line": "UUID=4A21-0000 /mnt/sautium-4A21-0000 exfat "
                "ro,nofail,nosuid,nodev,noexec,uid=1000,gid=1000,fmask=0133,dmask=0022" + systemd}
    ext4 = setup.boot_mount("/media/pi/disk", json.dumps({"filesystems": [{
        "source": "/dev/sda1; reboot", "target": "/media/pi/disk", "fstype": "ext4",
        "uuid": "0a1b2c3d-0000-4000-8000-000000000000"}]}), 1000, 1000)
    assert ext4["line"].endswith(" ext4 ro,nofail,nosuid,nodev,noexec,noload" + systemd)
    # A device name no command carries: the disk is named by its UUID's own link
    assert ext4["folder"] == ext4["point"]
    assert ext4["device"] == "/dev/disk/by-uuid/0a1b2c3d-0000-4000-8000-000000000000"
    # A mount through FUSE says `fuseblk` for NTFS and exFAT alike: the disk itself says which
    fuse = json.dumps({"filesystems": [{"source": "/dev/sdb1", "target": "/media/pi/E", "fstype": "fuseblk",
                                        "uuid": "4A21-0000"}]})
    asked = []
    for on_disk, driver in (("exfat", "exfat"), ("ntfs", "ntfs3")):
        line = setup.boot_mount("/media/pi/E", fuse, 1000, 1000, disk_type=lambda d, t=on_disk: asked.append(d) or t)
        assert f" /mnt/sautium-4A21-0000 {driver} ro,nofail," in line["line"], on_disk
    assert asked == ["/dev/sdb1", "/dev/sdb1"]
    assert setup.boot_mount("/media/pi/E", fuse, 1000, 1000, disk_type=lambda d: "") is None
    # The system's own disk (a plain folder under /media): never an fstab line for the root filesystem
    assert setup.boot_mount("/media/music/flac", json.dumps({"filesystems": [{
        "source": "/dev/mmcblk0p2", "target": "/", "fstype": "ext4", "uuid": "0a1b2c3d-0000-4000-8000-000000000000"}]}),
        1000, 1000) is None
    # What cannot be read whole, a filesystem the automount does not know, a UUID that is not one: no line
    for bad in ("", "{}", json.dumps({"filesystems": [{"target": "/media/pi/x", "fstype": "btrfs", "uuid": "abcd-ef"}]}),
                json.dumps({"filesystems": [{"target": "/media/pi/x", "fstype": "vfat", "uuid": "4A21 0000; reboot"}]}),
                json.dumps({"filesystems": [{"target": "/media/pi/other", "fstype": "vfat", "uuid": "4A21-0000"}]})):
        assert setup.boot_mount("/media/pi/x/Music", bad, 1000, 1000) is None, bad
    assert setup.desktop_mounted("/media/pi/Elements/Music") and setup.desktop_mounted("/run/media/pi/E/Music")
    assert not any(setup.desktop_mounted(p) for p in (
        "/media/sautium/4A21-0000/Music", "/mnt/music", "/home/pi/Music", "/media/pi", "/srv/music"))

    monkeypatch.setattr(setup.os.path, "isdir", lambda p: True)
    monkeypatch.setattr(setup.os.path, "realpath", lambda p: str(p))
    # Every disk a desktop mounted for one person, at once: as broad as a whole home
    for broad in ("/media/pi", "/run/media/pi", "/home/pi"):
        assert "whole home or every mounted disk" in setup.root_refusal(broad, machine_ready.root), broad
    # … but a disk mounted AT /media/<name> (its own fstab line, a Lite system's tool) is a disk
    monkeypatch.setattr(setup.os.path, "ismount", lambda p: p == "/media/hdd")
    assert setup.root_refusal("/media/hdd", machine_ready.root) is None
    assert setup.root_refusal("/media/pi/Elements/Music", machine_ready.root) is None
    assert setup.root_refusal("/run/media/pi/Elements/Music", machine_ready.root) is None
    monkeypatch.setattr(setup, "run", lambda args, **kw: subprocess.CompletedProcess(args, 0, found.encode(), b""))
    monkeypatch.setattr(setup, "desktop_automounter", lambda: True)
    monkeypatch.setattr(setup, "mounted_outside_fstab", lambda real: "/media/pi/Elements")
    monkeypatch.setattr(setup.os, "getuid", lambda: 1000)       # the person, wherever the suite runs
    monkeypatch.setattr(setup.os, "getgid", lambda: 1000)
    assert setup.main(["--music", "/media/pi/Elements/Music", "--yes"]) == 1
    said = capsys.readouterr().out
    pasted = [line[4:] for line in said.splitlines() if line.startswith("    ")]
    commands = [line for line in pasted if line.startswith("sudo ") or "| sudo tee" in line]
    # The desktop's hold let go first: the same disk is not mounted read-only beside a mount for writing
    assert commands[:5] == [
        "sudo umount /dev/sda1", "sudo mkdir -p /mnt/sautium-4A21-0000",
        "printf '\\n%s\\n' \"$L\" | sudo tee -a /etc/fstab",
        "sudo systemctl daemon-reload", "sudo mount /mnt/sautium-4A21-0000"]
    # The line itself is built in short pieces — a line a terminal or a chat wrapped arrives as two
    # commands — and the pieces build the very line (PI-NODE-2B item 8, W2)
    pieces = [line for line in pasted if line.startswith(("L='", 'L="$L,'))]
    assert len(pieces) > 1 and max(len(line) for line in pasted) <= 76, pasted
    built = subprocess.run(["sh", "-c", "\n".join(pieces) + '\nprintf %s "$L"'], capture_output=True, text=True).stdout
    assert built == ("UUID=4A21-0000 /mnt/sautium-4A21-0000 exfat ro,nofail,nosuid,nodev,noexec,uid=1000,"
                     "gid=1000,fmask=0133,dmask=0022" + systemd)
    assert "sudo sed -i '\\|^UUID=[^ ]* /mnt/sautium-4A21-0000 |d' /etc/fstab" in commands       # its undo
    assert "Elements" not in " ".join(commands)        # the volume's own name is in no root command
    # The root to name is the mount point — there with or without the disk — and the folder on it is
    # chosen in the Web UI: a bind of a folder ON the disk would stop the node's start without the disk
    assert "then run:  ./sautium --music /mnt/sautium-4A21-0000\n" in said
    assert "and choose Music as the music folder in Sautium" in said
    assert "--music /mnt/sautium-4A21-0000/Music" not in said
    assert machine_ready.ran == [] and not (machine_ready.root / ".env").exists()
    # The same path where nothing of a desktop mounted it — a Lite system, a disk fstab already mounts, a
    # plain folder of the system's disk: an ordinary root, no fstab line
    for desktop, loose in ((False, "/media/pi/Elements"), (True, None)):
        monkeypatch.setattr(setup, "desktop_automounter", lambda d=desktop: d)
        monkeypatch.setattr(setup, "mounted_outside_fstab", lambda real, l=loose: l)
        monkeypatch.setattr(setup, "root_refusal", lambda path, **kw: None)
        assert setup.main(["--music", "/media/pi/Elements/Music", "--yes"]) == 0
        assert "sudo tee -a /etc/fstab" not in capsys.readouterr().out          # no boot-mount block
        (machine_ready.root / ".env").unlink()


def test_a_desktop_that_mounts_disks_itself_is_not_given_a_second_automount(checkout, monkeypatch, capsys):
    monkeypatch.setattr(setup, "lingering", lambda: True)
    monkeypatch.setattr(setup, "automount_installed", lambda: False)
    monkeypatch.setattr(setup, "desktop_automounter", lambda: True)
    setup.root_steps(types.SimpleNamespace(yes=True), {})
    said = capsys.readouterr().out
    assert "this machine's desktop mounts them itself" in said and "sudo install" not in said
    assert not (checkout / "host" / "automount").exists()


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
                    str(checkout.parent), str(tmp_path / "none"), str(music) + ":x",
                    "/home/$USER/Music", "/run/media", "/run/user", "/media"):
        assert setup.root_refusal(refused, checkout) is not None, refused
    home = Path.home()
    if home.parts[:2] == ("/", "home") and len(home.parts) == 3:
        assert "whole home" in setup.root_refusal(str(home), checkout)
    # A link is judged by where it leads
    link = tmp_path / "music-link"
    link.symlink_to("/etc")
    assert setup.root_refusal(str(link), checkout) is not None
    # Docker's own data: the folder, one that holds it, one inside it
    docker = tmp_path / "ssd" / "docker"
    (docker / "volumes").mkdir(parents=True)
    (tmp_path / "ssd" / "Music").mkdir()
    for refused in (docker, tmp_path / "ssd", docker / "volumes"):
        assert "Docker's own data" in setup.root_refusal(str(refused), checkout, docker_root=str(docker))
    assert setup.root_refusal(str(tmp_path / "ssd" / "Music"), checkout, docker_root=str(docker)) is None
    with pytest.raises(setup.Stop, match="Docker's own data"):
        setup.chosen_roots(str(tmp_path / "ssd"), [], str(docker))


def test_a_folder_is_named_as_people_name_folders(checkout, tmp_path):
    """A space, any alphabet: carried whole into the override and read back
    from it. Only the two characters the roots' list and compose read as
    their own are refused."""
    yaml = pytest.importorskip("yaml")
    named = [tmp_path / "My Music", tmp_path / "Музика" / "Джаз", tmp_path / "音楽 🎵", tmp_path / "it's \"live\""]
    for folder in named:
        folder.mkdir(parents=True)
        assert setup.root_refusal(str(folder), checkout) is None, folder
    roots = [str(f) for f in named]
    text = setup.roots_override(roots)
    parsed = yaml.safe_load(text)["services"]["backend"]
    assert parsed["environment"]["SAUTIUM_LIBRARY_ROOTS"].split(":") == roots
    assert [v["source"] for v in parsed["volumes"]] == roots == [v["target"] for v in parsed["volumes"]]
    (checkout / "host" / "compose.roots.yml").write_text(text, encoding="utf-8")
    assert setup.held_roots(checkout / "host" / "compose.roots.yml") == roots
    for refused in ("a:b", "pri$e", "new\nline"):
        (tmp_path / refused).mkdir()
        assert "cannot carry" in setup.root_refusal(str(tmp_path / refused), checkout)
    # … and a link that leads to such a folder is refused as the folder is
    (tmp_path / "plain").symlink_to(tmp_path / "a:b")
    assert "cannot carry" in setup.root_refusal(str(tmp_path / "plain"), checkout)


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


def test_a_folder_inside_a_root_already_read_is_not_bound_again(tmp_path, capsys):
    """It is chosen in the Web UI. A bind of its own would tie the node's
    start to one disk of the automount's folder."""
    music = tmp_path / "Music"
    (music / "Jazz").mkdir(parents=True)
    held = [str(music)]
    assert setup.chosen_roots(str(music / "Jazz"), held) == [str(music), setup.AUTOMOUNT_BASE]
    assert f"is inside {music}, which this node already reads" in capsys.readouterr().out
    assert setup.chosen_roots(str(music), held) == [str(music), setup.AUTOMOUNT_BASE]
    assert capsys.readouterr().out == ""
    other = tmp_path / "Music2"                      # a name that only begins like a root's is another folder
    other.mkdir()
    assert setup.chosen_roots(str(other), held) == [str(music), str(other), setup.AUTOMOUNT_BASE]


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


def test_the_addresses_shown_are_doors_from_another_device():
    listing = "\n".join([
        "1: lo    inet 127.0.0.1/8 scope host lo",
        "2: eth0    inet 192.168.7.20/24 brd 192.168.7.255 scope global eth0",
        "3: wlan0    inet 10.1.2.3/24 scope global wlan0",
        "4: docker0    inet 172.17.0.1/16 scope global docker0",
        "5: br-0a1b2c3d4e5f    inet 172.18.0.1/16 scope global br-0a1b2c3d4e5f",
        "6: tailscale0    inet 100.64.0.10/32 scope global tailscale0",
        "7: wwan0    inet 203.0.113.9/24 scope global wwan0",
        "8: br0    inet 192.168.8.5/24 scope global br0"])             # a LAN bridge a person made is a door
    assert setup.lan_addresses("10.1.2.3", listing) == ["10.1.2.3", "192.168.7.20", "192.168.8.5"]
    assert setup.lan_addresses(None, listing) == ["192.168.7.20", "10.1.2.3", "192.168.8.5"]
    assert setup.lan_addresses("203.0.113.9", listing)[0] == "192.168.7.20"      # a public route's source is not shown
    assert setup.lan_addresses(None, "") == []
    bridge = setup.machine_tier.container_bridge
    assert all(bridge(n) for n in ("docker0", "br-0a1b2c3d4e5f", "veth9f3a", "virbr0", "docker_gwbridge", "lxdbr0",
                                   "incusbr0", "cni-podman0", "vEthernet (WSL)", "vEthernet (Default Switch)",
                                   "vEthernet (WSL (Hyper-V firewall))"))
    assert not any(bridge(n) for n in ("eth0", "wlan0", "br0", "bridge0", "end0", "enp3s0", "tailscale0",
                                       "Ethernet", "Wi-Fi", "vEthernet (External LAN)"))


def test_a_tailnets_funnel_and_key_are_read_from_tailscales_own_answers():
    assert setup.funnel_on('{"AllowFunnel": {"node.example.ts.net:443": true}}', "")
    assert not setup.funnel_on('{"TCP": {"443": {"HTTPS": true}}}', "")           # served on the tailnet only
    assert not setup.funnel_on('{"AllowFunnel": {"node.example.ts.net:443": false}}', "")
    assert not setup.funnel_on("{}", "") and not setup.funnel_on("null", "No serve config")
    # A CLI that prints no JSON: the words of its status
    assert setup.funnel_on("", "# Funnel on:\n#     - https://node.example.ts.net")
    assert not setup.funnel_on("", "No serve config")
    assert setup.key_expiry('{"Self": {"KeyExpiry": "2027-05-17T09:30:00Z"}}') == "2027-05-17"
    assert setup.key_expiry('{"Self": {"KeyExpiry": "2027-05-17T09:30:00.123456789+02:00"}}') == "2027-05-17"
    for none in ('{"Self": {}}', "{}", "", "[]", '{"Self": {"KeyExpiry": "\u001b[31msoon"}}'):
        assert setup.key_expiry(none) is None, none


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
    monkeypatch.setattr(setup, "desktop_automounter", lambda: False)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("--yes waited for a key"))
    setup.root_steps(types.SimpleNamespace(yes=True), {})
    printed = capsys.readouterr().out
    user = os.environ.get("USER") or str(os.getuid())
    assert f"    sudo loginctl enable-linger {user}" in printed
    assert f"    sudo loginctl disable-linger {user}" in printed          # its undo beside it
    assert '    sudo install -m 0755 "$A/automount" /usr/local/lib/sautium/' in printed
    assert f"    A={checkout / 'host' / 'automount'}\n" in printed          # the folder once, in a variable
    assert "    sudo rm -f /usr/local/lib/sautium/automount" in printed      # its undo, a line a file
    # No command of a block is long enough to be wrapped on its way to a root terminal (item 8, W2):
    # the lines after the folder's own are fixed text
    commands = [line[4:] for line in printed.splitlines() if line.startswith("    ")]
    assert max(len(line) for line in commands if not line.startswith("A=")) <= 76, commands
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


def test_a_second_run_attaches_to_its_own_build_and_to_no_other_process(checkout, monkeypatch):
    monkeypatch.setattr(setup.agent, "head", lambda root: "c" * 40)
    monkeypatch.setattr(setup.agent, "image_of", lambda root: "sautium-backend:local")
    monkeypatch.setattr(setup.agent, "compose_cmd", lambda root=None: ["docker", "compose"])
    monkeypatch.setattr(setup, "run", lambda args, **kw: subprocess.CompletedProcess(args, 1, b"", b""))
    env = {"SAUTIUM_IMAGE_MODELS": "0", "SAUTIUM_IMAGE_ASSISTANT_CLI": "0", "SAUTIUM_IMAGE_STREAMING": "0"}
    key = setup.image_key(env)
    started, waited = [], []

    def popen(args, **kw):
        started.append(args)
        (checkout / "host" / "setup-build.exit").write_text("0\n")
        return types.SimpleNamespace(pid=os.getpid())
    monkeypatch.setattr(setup.subprocess, "Popen", popen)
    monkeypatch.setattr(setup, "wait_for", waited.append)
    me = setup.born(os.getpid())
    assert me and me.isdigit() and setup.born(2 ** 22 + 7) is None
    # The stored number now belongs to another process (it started at another tick): a new build
    (checkout / "host" / "setup-build.exit").write_text("1\n")      # the stale answer of the build that died
    state = {"build": {"key": key, "pid": os.getpid(), "born": "1"}}
    setup.build(state, env)
    assert len(started) == 1 and state == {"built": key}
    # The same process as when the state was written: attached, nothing started
    (checkout / "host" / "setup-build.exit").write_text("0\n")
    state = {"build": {"key": key, "pid": os.getpid(), "born": me}}
    setup.build(state, env)
    assert len(started) == 1 and waited == [os.getpid(), os.getpid()]
    # A state written before the start time was kept names no build to attach to
    state = {"build": {"key": key, "pid": os.getpid()}}
    setup.build(state, env)
    assert len(started) == 2


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
    # The roots override stays with the .env that names it: the folder's compose commands still work
    assert (installed.root / "host" / "compose.roots.yml").exists()
    assert "sudo loginctl disable-linger" in said and "remove --data takes those" in said
    assert "/etc/fstab" not in said                                    # no boot mount of the setup's here
    assert not any("sudo" in line for line in joined)
    # Run again: nothing left to take, and it says so
    installed.asked.clear()
    (installed.root / "host" / "agent.env").unlink(missing_ok=True)
    assert setup.main(["remove"]) == 0
    assert "agent uninstall" not in [" ".join(a) for a in installed.asked]


def test_remove_prints_the_undo_of_a_boot_mount_the_setup_printed(installed, monkeypatch, capsys):
    fstab = b"/\n/boot/firmware\n/mnt/sautium-4A21-0000\n/mnt/sautium-x; reboot\n/mnt/music\n"
    monkeypatch.setattr(setup, "run", lambda args, **kw: installed.asked.append(args) or subprocess.CompletedProcess(
        args, 0, fstab if args[:2] == ["findmnt", "--fstab"] else b"", b""))
    assert setup.boot_mounts(fstab.decode()) == ["/mnt/sautium-4A21-0000"]
    assert setup.main(["remove"]) == 0
    said = capsys.readouterr().out
    for line in ("    sudo umount /mnt/sautium-4A21-0000",
                 "    sudo sed -i '\\|^UUID=[^ ]* /mnt/sautium-4A21-0000 |d' /etc/fstab",
                 "    sudo rmdir /mnt/sautium-4A21-0000"):
        assert line in said, line
    assert "reboot" not in said and "/mnt/music" not in said


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
    assert not (installed.root / "host" / "compose.roots.yml").exists()
    assert installed.root.exists()                                     # the folder itself is the person's to delete
    assert "rm -rf " in capsys.readouterr().out


def test_a_compose_step_that_fails_is_said_and_the_data_stays_beside_a_volume_that_stayed(
        installed, monkeypatch, capsys):
    def failing(args, **kw):
        installed.asked.append(args)
        return subprocess.CompletedProcess(args, 1, b"", b"WARN old\nopen host/compose.roots.yml: no such file\x1b[0m\n")
    monkeypatch.setattr(setup, "run", failing)
    monkeypatch.setattr("builtins.input", lambda prompt: "owner")
    assert setup.main(["remove", "--data"]) == 1
    said = capsys.readouterr().out
    assert "could not stop the node: open host/compose.roots.yml: no such file" in said and "\x1b" not in said
    assert "nothing of the node's data was deleted" in said and "data removed" not in said
    assert (installed.root / "data" / "backup" / "x.sbk").exists() and (installed.root / ".env").exists()
    # Without --data the same failure is said, and what it could take it still takes
    assert setup.main(["remove"]) == 0
    assert "could not remove the containers" in capsys.readouterr().out


def test_update_leaves_a_name_and_opens_nothing_a_container_left(installed, capsys):
    requests = installed.root / "run" / "requests"
    requests.mkdir(parents=True)
    (installed.root / "host" / "agent.log").write_text("earlier\n")
    assert setup.main(["update"]) == 0
    assert (requests / "update").is_file() and (requests / "update").stat().st_size == 0
    assert ["systemctl", "--user", "start", "--wait", "sautium-host-agent.service"] in installed.asked
    assert "earlier" not in capsys.readouterr().out               # only what the agent wrote since
    # A FIFO, a folder or a link a container left under the name: none is opened, the request stands
    for leave in (lambda p: os.mkfifo(p), lambda p: p.mkdir(), lambda p: p.symlink_to("/etc/hostname")):
        (requests / "update").unlink() if not (requests / "update").is_dir() or (requests / "update").is_symlink() \
            else (requests / "update").rmdir()
        leave(requests / "update")
        assert setup.main(["update"]) == 0
    # The folder itself a link: refused
    shutil.rmtree(installed.root / "run")
    (installed.root / "run").mkdir()
    (installed.root / "run" / "requests").symlink_to(installed.root / "data")
    assert setup.main(["update"]) == 1
    assert not (installed.root / "data" / "update").exists()
    (installed.root / "host" / "agent.env").unlink()
    assert setup.main(["update"]) == 1 and "no host agent" in capsys.readouterr().out


def test_status_says_what_is_there_and_changes_nothing(installed, monkeypatch, capsys):
    monkeypatch.setattr(setup, "docker_state", lambda: ("ok", "Docker 29.1 with Compose 2.40."))
    monkeypatch.setattr(setup.agent, "head", lambda root: "c" * 40)
    monkeypatch.setattr(setup.agent, "image_of", lambda root: "sautium-backend:local")
    monkeypatch.setattr(setup, "tailnet_address", lambda: "100.64.0.10")
    monkeypatch.setattr(setup, "undervoltage_now", lambda: False)
    (installed.root / "host" / "compose.roots.yml").write_text(setup.roots_override([str(installed.root.parent)]))
    before = sorted(p.name for p in (installed.root / "host").iterdir())
    assert setup.main(["status"]) == 0
    said = capsys.readouterr().out
    assert f"Music root: {installed.root.parent}  there" in said
    assert "Tailscale: this machine is 100.64.0.10" in said and "Funnel is on" not in said
    assert sorted(p.name for p in (installed.root / "host").iterdir()) == before
    asked = [" ".join(a) for a in installed.asked]
    assert not any(word in line for line in asked for word in (" up", " down", " stop", " rm", "sudo")), asked


def test_status_names_the_disks_mounted_under_the_roots():
    """`findmnt -R /media/sautium` answered nothing — the automount's folder is
    no mount point — so a plugged disk never showed (PI-NODE-2B item 8, S1)."""
    table = ("/ ext4 rw,noatime\n"
             "/boot/firmware vfat rw\n"
             "/media/sautium/B269-E281 exfat ro,nosuid,nodev,noexec,uid=1002\n"
             "/mnt/sautium-4A21-0000 exfat ro,nofail\n"
             "/media/sautium-other/X ext4 ro\n"
             "/home/pi/music-nas nfs ro\n")
    roots = ["/home/pi/music", "/mnt/sautium-4A21-0000", "/media/sautium"]
    assert setup.mounts_under(roots, table) == [
        "/media/sautium/B269-E281 exfat ro,nosuid,nodev,noexec,uid=1002", "/mnt/sautium-4A21-0000 exfat ro,nofail"]
    assert setup.mounts_under([], table) == []


def test_the_machine_line_says_an_asked_preset_as_asked():
    """The arrow is the machine's own verdict: Nano asked for on a board the
    rule reads as a full node is not said as read off it (item 8, W1)."""
    big = {"ram_gib": 15.8, "cores": 4, "docker_root_removable": False, "docker_root_free_gb": 800.0}
    small = dict(big, ram_gib=3.9)
    assert setup.machine_line(big, None).endswith("on a fixed disk → the full node")
    assert setup.machine_line(big, "nano").endswith("on a fixed disk; set up as Nano")
    assert setup.machine_line(small, "nano").endswith("on a fixed disk → Nano")
    assert setup.machine_line(small, None).endswith("on a fixed disk; set up as the full node")


def test_the_wrapper_runs_the_setup_isolated():
    wrapper = REPO / "sautium"
    assert os.access(wrapper, os.X_OK)
    text = wrapper.read_text()
    assert text.startswith("#!/bin/sh\n") and 'exec python3 -I "$here/scripts/setup.py" "$@"' in text
    assert subprocess.run(["sh", "-n", str(wrapper)]).returncode == 0
    # Standard library alone: the setup runs before anything is installed —
    # by whatever python3 the system has, down to the one it says it needs
    import ast
    assert "sys.version_info < (3, 9)" in SOURCE
    for path in ("scripts/setup.py", "scripts/host_agent.py", "desktop/machine_tier.py"):
        ast.parse((REPO / path).read_text(encoding="utf-8"), feature_version=(3, 9))
    tree = ast.parse(SOURCE)
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | {
        n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert imported <= set(sys.stdlib_module_names), imported - set(sys.stdlib_module_names)
