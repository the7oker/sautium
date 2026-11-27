"""The console setup (./sautium → scripts/setup.py; PI-NODE-2B §5): one
standard-library file, run as the person, that prints every root step and
runs none.

- What it reads of the machine and decides from it: memory, the preset's
  rule (desktop/machine_tier.py, the one the backend and the launcher
  apply), the medium Docker's data is on, where the swap is, PostgreSQL's
  sizes.
- .env: only its own keys, a person's lines kept, the database's password
  made only at the volume's birth, a living node never born again, --preset
  moving what the image carries and nothing else.
- The roots: every refused path; the override it writes (a gone root never
  stops a start; a mounted root's label rides along); what an earlier run
  named, read back from that file; `roots` and `roots --forget`.
- What a container wrote, read as a stranger's bytes: the setup code through
  a link, a FIFO, an oversized file, a wrong form — each refused.
- Root: refused as root; no command it runs is sudo; the root blocks are
  printed, and --yes never waits. The automount's script: its shell parses,
  and with the system's tools replaced it mounts a plugged disk read-only
  under its UUID with the pinned time options (NTFS by ntfs-3g where it is
  installed), leaves alone the system's own disk, a disk already mounted or
  named in fstab and a whole disk that holds partitions, and says a volume
  it did not mount — or one no one filesystem is found on (PI-NODE-2D §5) —
  in one small file the node watches for. The fstab line of a music disk
  mounts it at the automount's own point for that disk (PI-NODE-2D §2):
  systemd's own generator makes the disk's device want its mount, and an
  install whose line mounts at the root itself is named and offered the move.
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
automount_stale = setup.automount_stale       # the function itself, where a fixture replaces it


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
    assert preset(3.7, "nvme", 100)[0] == "nano"             # a 4 GB board
    assert preset(7.4, "nvme", 100)[0] == "nano" and preset(7.6, "nvme", 100)[0] is None   # "8 GB" reads 7.6
    assert preset(15.5, "sd_card", 100) == ("nano", "Docker's data is on a microSD card")
    assert preset(15.5, "usb_stick", 100) == ("nano", "Docker's data is on a USB stick")
    # A database lives well on these, whatever plugs them in (an NVMe in a USB case is no card)
    for medium in ("emmc", "nvme", "ssd", "hard_disk", "usb_disk", "unknown"):
        assert preset(15.5, medium, 100)[0] is None, medium
    assert preset(15.5, "ssd", 24.9)[0] == "nano" and preset(15.5, "ssd", 25)[0] is None
    assert preset(15.5, "ssd", None)[0] is None              # unknown free space decides nothing
    # The tier rule is the same module's, and the backend and the launcher read it there
    assert setup.machine_tier.auto_tier("cuda", 24, 32) == "full"
    assert setup.machine_tier.auto_tier("cuda", 24, 8) == "standard"
    assert setup.machine_tier.auto_tier("cpu", 4, 4) == "lite"
    for path in ("backend/hardware_profile.py", "desktop/utils.py"):
        assert "from desktop.machine_tier import auto_tier" in (REPO / path).read_text(encoding="utf-8")


LSBLK = json.dumps({"blockdevices": [
    {"name": "mmcblk0", "rm": False, "tran": None, "rota": False, "children": [
        {"name": "mmcblk0p1", "rm": False, "tran": None, "rota": False},
        {"name": "mmcblk0p2", "rm": False, "tran": None, "rota": False}]},
    {"name": "mmcblk1", "rm": False, "tran": None, "rota": False, "children": [{"name": "mmcblk1p1"}]},
    {"name": "nvme0n1", "rm": False, "tran": "nvme", "rota": False, "children": [
        {"name": "nvme0n1p2", "rm": False, "tran": "nvme", "rota": False}]},
    {"name": "sda", "rm": True, "tran": "usb", "rota": True, "children": [
        {"name": "sda1", "rm": True, "tran": None, "rota": True}]},
    {"name": "sdb", "rm": False, "tran": "usb", "rota": False, "children": [{"name": "sdb1"}]},
    {"name": "sdc", "rm": "0", "tran": "sata", "rota": "0", "children": [
        {"name": "sdc2", "children": [{"name": "vg-root"}]}]},
    {"name": "sdd", "rm": False, "tran": "sata", "rota": True, "children": [{"name": "sdd1"}]},
    {"name": "vda", "rm": False, "tran": "virtio", "rota": True, "children": [{"name": "vda1"}]},
    {"name": "loop0", "rm": False, "tran": None, "rota": False}]})


@pytest.fixture
def sysfs(tmp_path):
    """The two of /sys/block the setup reads: a card and eMMC told apart, a
    zram device's backing file."""
    root = tmp_path / "sys-block"
    for name, kind in (("mmcblk0", "SD"), ("mmcblk1", "MMC")):
        (root / name / "device").mkdir(parents=True)
        (root / name / "device" / "type").write_text(kind + "\n")
    return root


def test_the_medium_under_dockers_data_is_told_from_lsblk_and_the_cards_own_type(sysfs):
    """A card and a stick wear out under a database and make it wait; an SSD
    in a USB case, eMMC and an NVMe do not. Before this one boolean said
    "a card or removable disk" of a USB SSD too (PI-NODE-2C §15.1, B-115)."""
    def medium(source):
        return setup.machine_tier.storage_medium(setup.device_chain(source, LSBLK), str(sysfs))
    assert medium("/dev/mmcblk0p2") == "sd_card"                      # the Pi's card
    assert medium("/dev/mmcblk1p1") == "emmc"                         # a Compute Module's own
    assert medium("/dev/nvme0n1p2") == "nvme"
    assert medium("/dev/sda1") == "usb_stick"                         # removable, by its parent
    assert medium("/dev/sdb1") == "usb_disk"                          # a USB case: fixed
    assert medium("/dev/mapper/vg-root") == "ssd"                     # through LVM, the disk says
    assert medium("/dev/sdd1") == "hard_disk" and medium("/dev/vda1") == "unknown"
    assert medium("/dev/sda1[/@]") == "usb_stick"                     # btrfs names its subvolume
    assert medium("overlay") == "unknown" and setup.device_chain("/dev/sda1", "not json") == []
    # A card whose type cannot be read is taken for the card it most often is
    assert setup.machine_tier.storage_medium([{"name": "mmcblk7"}], str(sysfs)) == "sd_card"
    assert set(setup.machine_tier.CARD_MEDIA) <= set(setup.machine_tier.MEDIA)


PROC_SWAPS = ("Filename\t\t\t\tType\t\tSize\t\tUsed\t\tPriority\n"
              "/dev/zram0                              partition\t2097136\t\t0\t\t100\n")


def test_the_swap_is_read_where_proc_swaps_does_not_tell(sysfs, capsys):
    """Raspberry Pi OS's default is compressed swap in memory WITH a file on
    the disk its idle pages are written back to — /proc/swaps lists the zram
    device alone (PI-NODE-2C §8)."""
    def read(table, size=2 * 1024 ** 3):
        return setup.swaps(table, LSBLK, str(sysfs), source_of=lambda path: {
            "/var/swap": "/dev/mmcblk0p2", "/mnt/ssd/swapfile": "/dev/sdc2"}.get(path, ""),
            size_of=lambda path: size)
    (sysfs / "zram0").mkdir()
    (sysfs / "zram0" / "backing_dev").write_text("none\n")
    alone = read(PROC_SWAPS)
    assert alone == [{"path": "/dev/zram0", "kind": "zram", "medium": "memory"}]
    setup.swap_lines(alone, step=True), setup.swap_lines(alone, step=False)
    assert capsys.readouterr().out == ""                              # swap in memory alone: nothing said
    (sysfs / "zram0" / "backing_dev").write_text("/dev/loop0\n")
    (sysfs / "zram0" / "bd_stat").write_text("     512        3     3072\n")
    (sysfs / "loop0" / "loop").mkdir(parents=True)
    (sysfs / "loop0" / "loop" / "backing_file").write_text("/var/swap\n")
    (back,) = read(PROC_SWAPS)
    assert back["writeback"] == {"path": "/var/swap", "medium": "sd_card", "written_mb": 12.0,
                                 "size_bytes": 2 * 1024 ** 3}
    setup.swap_lines([back], step=True)
    assert capsys.readouterr().out == ""                              # never a root step, nor the last screen
    setup.swap_lines([back], step=False)
    said = capsys.readouterr().out
    # the file's size beside what was written to it: a fact, no step (PI-NODE-2D §8.7)
    assert said == ("Swap: compressed memory (zram0), with idle pages written back to /var/swap (a 2.0 GB file) "
                    "on a microSD card: 12 MB written there since this start.\n")
    assert "sudo" not in said
    # a file that cannot be read has no size to name, and the sentence stands without it
    setup.swap_lines(read(PROC_SWAPS, size=None), step=False)
    assert "written back to /var/swap on a microSD card: 12 MB" in capsys.readouterr().out
    assert setup.file_size(str(sysfs / "zram0" / "bd_stat")) == 27 and setup.file_size(str(sysfs / "none")) is None
    # A swap file or a partition on the card itself, and one on an SSD
    table = PROC_SWAPS.splitlines()[0] + ("\n/var/swap file 1 0 -2\n/dev/mmcblk0p1 partition 1 0 -3\n"
                                         "/mnt/ssd/swapfile file 1 0 -4\n/my\\040swap file 1 0 -5\n")
    found = read(table)
    assert [(f["path"], f["kind"], f["medium"]) for f in found] == [
        ("/var/swap", "file", "sd_card"), ("/dev/mmcblk0p1", "partition", "sd_card"),
        ("/mnt/ssd/swapfile", "file", "ssd"), ("/my swap", "file", "unknown")]
    setup.swap_lines(found[2:], step=True)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("system, commands", [
    ("rpi-swap", ["F=CONF.d/90-sautium.conf", "sudo mkdir -p CONF.d",
                  "printf '[Main]\\nMechanism=zram\\n' | sudo tee \"$F\"", "sudo rm -f CONF.d/90-sautium.conf"]),
    ("dphys-swapfile", ["sudo dphys-swapfile swapoff", "sudo dphys-swapfile uninstall",
                        "sudo systemctl disable --now dphys-swapfile", "sudo apt-get install -y zram-tools",
                        "sudo apt-get remove -y zram-tools", "sudo systemctl enable --now dphys-swapfile"]),
    ("other", [])])
def test_a_swap_file_on_the_card_is_said_with_its_root_step(tmp_path, monkeypatch, capsys, system, commands):
    conf = tmp_path / "swap.conf"
    monkeypatch.setattr(setup, "RPI_SWAP_CONF", str(conf))
    if system == "rpi-swap":
        conf.write_text("[Main]\n")
    monkeypatch.setattr(setup.shutil, "which", lambda name, path=None: (
        "/usr/sbin/dphys-swapfile" if system == "dphys-swapfile" and name == "dphys-swapfile" else None))
    card = [{"path": "/var/swap", "kind": "file", "medium": "sd_card"}]
    setup.swap_lines(card, step=True)
    said = capsys.readouterr().out
    assert "This machine's swap is a file on its card (/var/swap)." in said
    assert "slow, and it wears the card." in said and "Swap kept in compressed memory is the better choice." in said
    pasted = [line[4:] for line in said.splitlines() if line.startswith("    ")]
    assert pasted == [c.replace("CONF", str(conf)) for c in commands]
    if system == "rpi-swap":
        # what the paste writes is the drop-in itself
        written = subprocess.run(["sh", "-c", pasted[2].split(" | ")[0]], capture_output=True, text=True).stdout
        assert written == "[Main]\nMechanism=zram\n"
    # status says the sentence and prints no step
    setup.swap_lines(card, step=False)
    assert "sudo" not in capsys.readouterr().out
    setup.swap_lines([{"path": "/dev/sda2", "kind": "partition", "medium": "usb_stick"}], step=False)
    assert "is a partition on a USB stick (/dev/sda2)" in capsys.readouterr().out


def test_postgres_is_sized_for_the_board():
    """Every key at every size — a value, or None where the compose file's
    own default fits: the plan can then take a small board's line away."""
    pg, keys = setup.machine_tier.pg_settings, setup.machine_tier.PG_KEYS
    for ram in (0.9, 1.8, 3.7, 7.6, 15.5, 64):
        assert tuple(pg(ram)) == keys
    assert set(pg(15.5).values()) == set(pg(12).values()) == {None}   # the compose file's own defaults
    assert pg(7.6)["PG_SHARED_BUFFERS"] == "512MB" and pg(11.9)["PG_SHARED_BUFFERS"] == "512MB"
    assert pg(3.7) == {"PG_SHARED_BUFFERS": "256MB", "PG_EFFECTIVE_CACHE_SIZE": "1GB",
                       "PG_MAINTENANCE_WORK_MEM": "128MB", "PG_MAX_WAL_SIZE": "1GB", "PG_WORK_MEM": None}
    small = pg(1.8)
    assert small["PG_SHARED_BUFFERS"] == "128MB" and small["PG_WORK_MEM"] == "4MB"
    compose = (REPO / "docker-compose.pi.yml").read_text(encoding="utf-8")
    for key in keys:
        assert "${%s:-" % key in compose, key                          # every key it writes is one the file reads
    assert not hasattr(setup, "pg_settings")                          # one table, the agent's update's too


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
        "PG_MAX_WAL_SIZE": "1GB", "PG_WORK_MEM": None, "SAUTIUM_BIRTH_PRESET": "nano", "SAUTIUM_IMAGE_MODELS": "0",
        "SAUTIUM_IMAGE_STREAMING": "0", "SAUTIUM_IMAGE_ASSISTANT_CLI": "0", "CLAUDE_CODE_ENABLED": "false",
        "DEFAULT_PROVIDER": "none", "COMPOSE_FILE": "docker-compose.pi.yml:host/compose.roots.yml",
        "MUSIC_LIBRARY_PATH": "./data/no-library"}
    full = _plan({}, preset=None, ram_gib=15.5, assistant="claude")
    assert "SAUTIUM_BIRTH_PRESET" not in full and full["PG_SHARED_BUFFERS"] is None
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
                                 "docker_root_medium": "nvme", "swaps": []}),
            ("machine_line", lambda m, preset: "a small board"),
            ("database_volume_exists", lambda: False), ("node_running", lambda: False),
            ("ports_taken", lambda: []), ("undervoltage_now", lambda: False),
            ("lingering", lambda: True), ("automount_installed", lambda: True),
            ("automount_stale", lambda files, places=None: False), ("ntfs_3g", lambda: True),
            ("systemd_version", lambda: 257), ("root_label", lambda root: ""),
            ("build", lambda state, env: ran.append("build")), ("start", lambda: ran.append("start")),
            ("last_screen", lambda m, preset: ran.append(("screen", preset)))):
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
    assert machine_ready.ran == ["build", "start", ("screen", "nano")]
    assert setup.load_state()["done"] is True


def _findmnt(answers):
    """setup.run with findmnt's answers of the test's own: {leading args: (exit, output)}."""
    def run(args, **kw):
        code, said = next((v for k, v in answers.items() if tuple(args[:len(k)]) == k), (0, b""))
        return subprocess.CompletedProcess(args, code, said, b"")
    return run


def test_a_folder_that_lies_on_a_mount_the_setup_has_no_line_for_is_taken_and_said(
        machine_ready, monkeypatch, tmp_path, capsys):
    """Someone's own line of /etc/fstab, a network share, a disk mounted by
    hand that cannot be read whole: the folder is the root, as named — never
    its parent — and the setup says what a mount point means to a node that
    holds it from its start (PI-NODE-2D §2.5). It said "Sautium does not
    start" of such a disk until the roots' binds made their own folders."""
    music = tmp_path / "Music"
    music.mkdir()
    at_start = "Sautium sees what is mounted there when Sautium starts."
    later = ("A disk unplugged or put back while Sautium runs is not seen until Sautium is started\n"
             "again (./sautium).")
    # a folder ON a disk no line of fstab mounts, whose mount findmnt cannot name whole
    answers = {("findmnt", "-n", "-o", "TARGET", "-T"): (0, b"/mnt/usb\n"), ("findmnt", "--fstab", "-n"): (1, b"")}
    monkeypatch.setattr(setup, "run", _findmnt(answers))
    assert setup.mounted_outside_fstab(str(music)) == "/mnt/usb"
    assert setup.main(["--music", str(music), "--yes"]) == 0
    said = capsys.readouterr().out
    assert f"{music} is on a disk mounted at /mnt/usb, which no line of /etc/fstab mounts: {at_start}\n{later}\n" in said
    assert "does not start" not in said and "sudo" not in said
    assert setup.held_roots(machine_ready.root / "host" / "compose.roots.yml") == [str(music), setup.AUTOMOUNT_BASE]
    # the folder IS a mount point, by a line of fstab the person wrote (or a share's own mount)
    answers[("findmnt", "-n", "-o", "TARGET", "-T")] = (0, str(music).encode() + b"\n")
    answers[("findmnt", "--fstab", "-n")] = (0, str(music).encode() + b" /dev/sdb1 ext4\n")
    assert setup.mounted_outside_fstab(str(music)) is None
    assert setup.main(["--music", str(music), "--yes"]) == 0
    said = capsys.readouterr().out
    assert f"{music} is a mount point: {at_start}\n{later}\n" in said and "sudo" not in said
    assert setup.held_roots(machine_ready.root / "host" / "compose.roots.yml") == [str(music), setup.AUTOMOUNT_BASE]
    # its parent is no folder of music: refused where a person names it, never taken in their place
    for parent in ("/mnt", "/media", "/srv"):
        assert setup.root_refusal(parent, machine_ready.root) is not None
    # A folder of a disk fstab mounts above it, and one of the system's own disk: nothing to say
    for target in (b"/mnt/usb\n", b"/\n"):
        answers[("findmnt", "-n", "-o", "TARGET", "-T")] = (0, target)
        assert setup.mounted_outside_fstab(str(music)) is None
        assert setup.main(["--music", str(music), "--yes"]) == 0
        said = capsys.readouterr().out
        assert "mount point" not in said and "no line of /etc/fstab" not in said


def test_a_disk_is_one_a_person_pulls_by_what_lsblk_says_of_it(sysfs):
    """The setup's own line is for a disk that is plugged in and pulled —
    what the automount's rule takes: USB storage and a card in the slot. By
    the one reader of lsblk's columns (machine_tier.storage_medium), never a
    second rule (review B3)."""
    def pulled(device, system="/dev/mmcblk0p2"):
        return setup.pulled_disk(device, LSBLK, system, str(sysfs))
    assert pulled("/dev/sda1")                             # a stick
    # A SATA disk or an SSD in a USB case says RM=0 and is pulled as a stick is: by its transport
    assert pulled("/dev/sdb1")
    for internal in ("/dev/nvme0n1p2", "/dev/sdc2", "/dev/mapper/vg-root", "/dev/sdd1", "/dev/vda1",
                     "/dev/mmcblk1p1",                     # eMMC: soldered
                     "nas:/music", "", "/dev/unknown9"):
        assert not pulled(internal), internal
    # A card is pulled — but never the one the system runs from, nor a second partition of it
    assert not pulled("/dev/mmcblk0p1") and not pulled("/dev/mmcblk0p2")
    assert pulled("/dev/mmcblk0p1", system="/dev/nvme0n1p2")
    assert not pulled("/dev/sda1", system="/dev/sda1")     # a system that runs from the stick itself
    assert set(setup.PULLED_MEDIA) <= set(setup.machine_tier.MEDIA)


def test_a_mount_someone_set_up_is_theirs(monkeypatch):
    """A line of /etc/fstab, a .mount or .automount unit somebody wrote, an
    autofs point — against a mount systemd merely saw (a desktop's, a
    hand's), for which it names no unit file."""
    answers = {("findmnt", "--fstab", "-n"): (1, b"")}
    monkeypatch.setattr(setup, "run", _findmnt(answers))
    assert not setup.someone_mounts("/media/pi/Elements")
    answers[("findmnt", "--fstab", "-n")] = (0, b"/srv/music /dev/sdb1 ext4\n")
    assert setup.someone_mounts("/srv/music")
    answers[("findmnt", "--fstab", "-n")] = (1, b"")
    asked = []
    for shown in (b"/etc/systemd/system/srv-music.mount\n\n\n", b"\n\n/etc/systemd/system/srv-music.automount\n",
                  b"/run/systemd/generator/srv-music.mount\n"):
        answers[("systemctl", "show")] = (0, shown)
        monkeypatch.setattr(setup, "run", lambda args, **kw: asked.append(args) or _findmnt(answers)(args, **kw))
        assert setup.someone_mounts("/srv/music"), shown
    assert ["systemctl", "show", "-p", "FragmentPath", "--value", "srv-music.mount", "srv-music.automount"] in asked
    answers[("systemctl", "show")] = (0, b"\n\n")           # systemd's own record of a mount it saw
    assert not setup.someone_mounts("/srv/music")
    answers[("findmnt", "-rn", "-o", "FSTYPE", "--mountpoint")] = (0, b"autofs\next4\n")
    assert setup.someone_mounts("/srv/music")


@pytest.mark.parametrize("what, source, fstype, unit, stacked, offered", [
    ("an internal NVMe partition under a .mount unit", "/dev/nvme0n1p2", "ext4", b"/etc/systemd/system/m.mount\n", b"ext4\n", False),
    ("the same, mounted by hand", "/dev/nvme0n1p2", "ext4", b"\n", b"ext4\n", False),
    ("an internal SATA disk mounted by hand", "/dev/sdd1", "ext4", b"\n", b"ext4\n", False),
    ("a network share", "nas:/music", "nfs4", b"\n", b"nfs4\n", False),
    ("a stick behind an autofs point", "/dev/sda1", "exfat", b"\n", b"autofs\nexfat\n", False),
    ("a stick under someone's .mount unit", "/dev/sda1", "exfat", b"/etc/systemd/system/m.mount\n", b"exfat\n", False),
    ("a SATA disk in a USB case, mounted by hand", "/dev/sdb1", "ext4", b"\n", b"ext4\n", True),
    ("a stick mounted by hand", "/dev/sda1", "exfat", b"\n", b"exfat\n", True)])
def test_the_line_is_offered_only_for_a_pulled_disk_nobody_set_up_a_mount_for(
        machine_ready, monkeypatch, tmp_path, capsys, what, source, fstype, unit, stacked, offered):
    """Review B3. The setup offered "The music disk, mounted whenever it is
    plugged in" for any disk no line of /etc/fstab mounted — an internal NVMe
    partition under a .mount unit among them: a block that unmounted it under
    whatever else read it and mounted it a second time at every start, printed
    again at every run. The line is the setup's to offer only for a disk a
    person pulls that nobody set up a mount for; every other mount is the
    person's own, taken as named and said (PI-NODE-2D §2.5)."""
    music = tmp_path / "music"
    music.mkdir()
    answers = {("findmnt", "-n", "-o", "TARGET", "-T"): (0, str(music).encode() + b"\n"),
               ("findmnt", "--fstab", "-n", "-o", "TARGET"): (0, b"/\n"),
               ("findmnt", "--fstab", "-n"): (1, b""),
               ("systemctl", "show"): (0, unit),
               ("findmnt", "-rn", "-o", "FSTYPE", "--mountpoint"): (0, stacked),
               ("findmnt", "-J", "-o", "SOURCE,TARGET,FSTYPE,UUID,LABEL", "-T"): (
                   0, _mount(source=source, target=str(music), fstype=fstype,
                             uuid=None if fstype == "nfs4" else "4A21-0000").encode()),
               ("findmnt", "-rn", "-o", "TARGET,FSTYPE,OPTIONS"): (0, f"{music} {fstype} rw\n".encode()),
               ("findmnt", "-rn", "-o", "TARGET", "-S", "UUID=4A21-0000"): (0, str(music).encode() + b"\n"),
               **MACHINE}
    monkeypatch.setattr(setup, "run", _findmnt(answers))
    monkeypatch.setattr(setup.os, "getuid", lambda: 1000)
    monkeypatch.setattr(setup.os, "getgid", lambda: 1000)
    for argv in (["--music", str(music), "--yes"], ["--yes"]):          # the run that names it, and a later one
        assert setup.main(argv) == 0, what
        said = capsys.readouterr().out
        assert ("── The music disk, mounted whenever it is plugged in" in said) is offered, what
        assert ("sudo umount" in said) is offered and ("roots --forget" in said) is offered, what
        if not offered and argv[0] == "--music":
            # the person's own mount: the folder is the root, as named, and what a mount point means is said
            assert (f"{music} is a mount point: Sautium sees what is mounted there when Sautium starts.\n"
                    "A disk unplugged or put back while Sautium runs is not seen") in said, what
        assert setup.held_roots(machine_ready.root / "host" / "compose.roots.yml") == [str(music), setup.AUTOMOUNT_BASE]
    for name, value in (("docker_state", lambda: ("ok", "Docker")), ("tailnet_address", lambda: None),
                        ("undervoltage_now", lambda: False)):
        monkeypatch.setattr(setup, name, value)
    monkeypatch.setattr(setup.agent, "head", lambda root: "c" * 40)
    monkeypatch.setattr(setup.agent, "image_of", lambda root: "sautium-backend:local")
    monkeypatch.setattr(setup.agent, "compose_cmd", lambda root=None: ["docker", "compose"])
    assert setup.main(["status"]) == 0
    line = next(line for line in capsys.readouterr().out.splitlines() if line.startswith(f"Music root {music}:"))
    assert "a disk mounted at the root itself" in line and line.endswith("(./sautium shows the step)") is offered, what


@pytest.mark.parametrize("options, own", [
    ("ro,nofail,nosuid,nodev,noexec,uid=1000", True),
    ("nofail,noexec,nodev,nosuid,ro", True),
    ("rw,ro,nosuid,nodev,noexec", True),                   # the later word stands
    ("defaults,ro,nosuid,nodev,noexec", True),
    ("ro,nosuid,nodev,noexec,rw", False),
    ("ro,nofail,nosuid,nodev,noexec,uid=1000,rw", False),
    ("ro,nosuid,nodev,noexec,defaults", False),
    ("ro,nosuid,nodev,noexec,exec", False),
    ("ro,nosuid,nodev,noexec,suid", False),
    ("ro,nosuid,nodev,noexec,dev", False),
    ("ro,user,exec", False), ("ro,users,suid", False), ("ro,owner", False),      # owner: no noexec with it
    ("ro,user", True), ("users,ro", True), ("ro,owner,noexec", True), ("ro,group,noexec", True),
    ("nosuid,nodev,noexec", False), ("ro,nosuid,nodev", False), ("", False)])
def test_a_line_is_the_setups_own_only_while_nothing_in_it_undoes_its_four_words(options, own):
    """Review B4. mount(8) reads options left to right: `…,ro,…,rw` mounts
    for writing. A line edited so was carried to /media/sautium/<UUID>, where
    the mount reaches the node's container with its own flags (§2.6)."""
    assert setup.mounted_safely(options) is own
    assert setup.own_line({"source": "UUID=4A21-0000", "fstype": "exfat", "options": options}, "4A21-0000") is own
    assert not setup.own_line({"source": "/dev/sda1", "fstype": "exfat", "options": options}, "4A21-0000")


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


ELEMENTS = {"source": "/dev/sda1", "target": "/media/pi/Elements", "fstype": "exfat", "uuid": "4A21-0000",
            "label": "Elements"}
PLUG_4A21 = ",x-systemd.before=docker.service,x-systemd.wanted-by=dev-disk-by\\x2duuid-4A21\\x2d0000.device 0 0"
LINE_4A21 = ("UUID=4A21-0000 /media/sautium/4A21-0000 exfat ro,nofail,nosuid,nodev,noexec,uid=1000,gid=1000,"
             "fmask=0133,dmask=0022" + PLUG_4A21)


def _mount(**over):
    return json.dumps({"filesystems": [dict(ELEMENTS, **over)]})


# What the setup asks of the machine about a disk: lsblk's own columns, and the device the system runs from
MACHINE = {("lsblk", "-J", "-o", "NAME,RM,TRAN,ROTA"): (0, LSBLK.encode()),
           ("findmnt", "-J", "-o", "SOURCE,FSTYPE,TARGET", "-T", "/"): (0, json.dumps({"filesystems": [
               {"source": "/dev/mmcblk0p2", "fstype": "ext4", "target": "/"}]}).encode())}


def test_the_line_mounts_a_music_disk_at_the_automounts_own_point():
    """The line's mount point is /media/sautium/<UUID> — where the automount
    would put that disk, under the one root the node has for disks — so no
    root is a mount point. At /mnt/sautium-<UUID>, which was then given as a
    root, a container made while the disk was in held the disk's filesystem:
    a pull was never seen and the disk never came back (PI-NODE-2D §2, F16)."""
    plan = setup.boot_mount("/media/pi/Elements/Music/Jazz", _mount(), 1000, 1000)
    # The disk's own device unit wants the mount: it starts at every plug, not only at a start of the
    # machine. `nofail` stays for a systemd that does not know the option; nothing waits for the device
    assert plan == {
        "uuid": "4A21-0000", "point": "/media/sautium/4A21-0000", "folder": "/media/sautium/4A21-0000/Music/Jazz",
        "under": "Music/Jazz", "device": "/dev/sda1", "label": "Elements", "at_plug": True, "line": LINE_4A21}
    assert setup.BOOT_MOUNT_POINT.fullmatch(plan["point"]).group(1) == "4A21-0000"
    assert not setup.BOOT_MOUNT_POINT.fullmatch("/media/sautium") and not setup.BOOT_MOUNT_POINT.fullmatch(
        "/media/sautium/4A21-0000/Music")
    # … and its mount unit is the automount's point's own, as systemd names it
    assert setup.unit_name(plan["point"], "mount") == "media-sautium-4A21\\x2d0000.mount"
    ext4 = setup.boot_mount("/media/pi/disk", _mount(source="/dev/sda1; reboot", target="/media/pi/disk", fstype="ext4",
                                                     uuid="0a1b2c3d-0000-4000-8000-000000000000", label=None),
                            1000, 1000)
    assert ext4["line"] == (
        "UUID=0a1b2c3d-0000-4000-8000-000000000000 /media/sautium/0a1b2c3d-0000-4000-8000-000000000000 ext4 "
        "ro,nofail,nosuid,nodev,noexec,noload,x-systemd.before=docker.service,x-systemd.wanted-by="
        "dev-disk-by\\x2duuid-0a1b2c3d\\x2d0000\\x2d4000\\x2d8000\\x2d000000000000.device 0 0")
    assert "device-timeout" not in ext4["line"]
    assert (ext4["folder"], ext4["under"], ext4["label"]) == (ext4["point"], "", "")
    # A device name no command carries: the disk is named by its UUID's own link
    assert ext4["device"] == "/dev/disk/by-uuid/0a1b2c3d-0000-4000-8000-000000000000"
    # A systemd that does not know the option (under 245) gets the line of before, and is told so
    old = setup.boot_mount("/media/pi/Elements/Music/Jazz", _mount(), 1000, 1000, systemd=241)
    assert old["line"].endswith(",x-systemd.before=docker.service,x-systemd.device-timeout=10s 0 0")
    assert "wanted-by" not in old["line"] and old["at_plug"] is False


def test_a_label_is_a_strangers_text_cut_and_one_with_a_quote_is_not_carried():
    """The block writes the label between single quotes; a disk whose label
    holds one goes by its UUID."""
    assert setup.boot_mount("/media/pi/x", _mount(target="/media/pi/x", label="It's \"live\""), 1000, 1000)["label"] == ""
    hostile = 'My "disk" ${HOME} $(reboot); \\ # Му\x1b[31m\n'
    found = setup.boot_mount("/media/pi/x", _mount(target="/media/pi/x", label=hostile), 1000, 1000)
    assert found["label"] == 'My "disk" ${HOME} $(reboot); \\ # Му[31m'
    steps = setup.line_steps(found)
    written = subprocess.run(["sh", "-c", "\n".join(steps[steps.index(f"T='{found['label']}'"):][:2]).replace(
        '| sudo tee "$B/.names/$U" >/dev/null', "")], capture_output=True, text=True, cwd="/")
    assert written.stdout == found["label"] + "\n" and written.stderr == ""     # each letter as itself
    # no label: no label step, and nothing of the disk's own words in the block
    bare = setup.line_steps(setup.boot_mount("/media/pi/x", _mount(target="/media/pi/x", label=None), 1000, 1000))
    assert not any(line.startswith("T=") or ".names/$U" in line for line in bare)
    assert setup.named({"label": "", "under": "Music"}) == "the disk › Music"
    assert setup.named({"label": "Elements", "under": ""}) == '"Elements"'


def test_the_block_lets_go_of_the_disks_present_hold_first(tmp_path, monkeypatch):
    """A desktop's by umount of the device; the automount's by stopping its
    unit, whose own stop unmounts and removes its label file — else a later
    pull's stop would take the line's mount point and label with it (§2.3)."""
    found = setup.boot_mount("/media/pi/Elements/Music", _mount(), 1000, 1000)
    state = tmp_path / "state"
    state.mkdir()
    assert setup.let_go(found, ["/media/pi/Elements"], str(state)) == ["sudo umount /dev/sda1"]
    assert setup.let_go(found, [], str(state)) == []                  # the disk is out: nothing holds it
    (state / "sda1").write_text("4A21-0000\n")
    assert setup.let_go(found, ["/media/sautium/4A21-0000"], str(state)) == [
        "sudo systemctl stop sautium-automount@sda1.service"]
    # held twice — the automount's point and a desktop's: the unit first, then the device's one mount left
    assert setup.let_go(found, ["/media/sautium/4A21-0000", "/media/pi/Elements"], str(state)) == [
        "sudo systemctl stop sautium-automount@sda1.service", "sudo umount /dev/sda1"]
    # a state file that names another point (a second volume claiming the UUID) is not this mount's
    (state / "sda1").write_text("4A21-0000-sda1\n")
    assert setup.let_go(found, ["/media/sautium/4A21-0000"], str(state)) == ["sudo umount /dev/sda1"]
    (state / "sda1").unlink()
    (state / "sda1").symlink_to(tmp_path / "elsewhere")              # root's folder; read as a stranger's all the same
    assert setup.let_go(found, ["/media/sautium/4A21-0000"], str(state)) == ["sudo umount /dev/sda1"]


def test_a_folder_on_a_disk_the_desktop_mounts_gets_its_line_and_is_chosen_under_the_disks_folder(
        machine_ready, monkeypatch, capsys):
    """Bound as it is, the node would not see the disk before someone signs
    in to the desktop: the setup prints the one fstab line. Pasted, the disk
    lies under the folder the node reads disks in and the node is given no
    new root; not pasted, the setup stops — a folder ON a disk the desktop
    mounts is never a root."""
    fuse = _mount(source="/dev/sdb1", target="/media/pi/E", fstype="fuseblk")
    asked = []
    # NTFS by the automount's own choice: ntfs-3g where it is installed, the kernel's driver where not
    for on_disk, driver, has_3g in (("exfat", "exfat", True), ("ntfs", "ntfs-3g", True), ("ntfs", "ntfs3", False)):
        monkeypatch.setattr(setup, "ntfs_3g", lambda h=has_3g: h)
        line = setup.boot_mount("/media/pi/E", fuse, 1000, 1000, disk_type=lambda d, t=on_disk: asked.append(d) or t)
        assert f" /media/sautium/4A21-0000 {driver} ro,nofail," in line["line"], on_disk
    assert asked == ["/dev/sdb1"] * 3
    assert setup.boot_mount("/media/pi/E", fuse, 1000, 1000, disk_type=lambda d: "") is None
    # The system's own disk (a plain folder under /media): never an fstab line for the root filesystem
    assert setup.boot_mount("/media/music/flac", _mount(source="/dev/mmcblk0p2", target="/", fstype="ext4",
                                                        uuid="0a1b2c3d-0000-4000-8000-000000000000"), 1000, 1000) is None
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

    fstab = {"pasted": False}
    answers = {("findmnt", "-n", "-o", "TARGET", "-T"): (0, b"/media/pi/Elements\n"),
               ("findmnt", "--fstab", "-n", "-o", "TARGET"): (0, b"/\n"),
               ("findmnt", "--fstab", "-n"): (1, b""),
               ("findmnt", "-J", "-o", "SOURCE,TARGET,FSTYPE,UUID,LABEL", "-T"): (0, _mount().encode()),
               **MACHINE,
               ("findmnt", "-rn", "-o", "TARGET", "-S", "UUID=4A21-0000"): (0, b"/media/pi/Elements\n")}

    def run(args, **kw):
        if args[:3] == ["findmnt", "--fstab", "-J"]:
            line = {"source": "UUID=4A21-0000", "fstype": "exfat", "options": LINE_4A21.split()[3]}
            return subprocess.CompletedProcess(args, 0, json.dumps({"filesystems": [line]}).encode() if (
                fstab["pasted"] and args[-1] == "/media/sautium/4A21-0000") else b"", b"")
        if fstab["pasted"] and args[:5] == ["findmnt", "--fstab", "-n", "-o", "TARGET"]:
            return subprocess.CompletedProcess(args, 0, b"/\n/media/sautium/4A21-0000\n", b"")
        return _findmnt(answers)(args, **kw)
    monkeypatch.setattr(setup, "run", run)
    monkeypatch.setattr(setup, "desktop_automounter", lambda: True)
    monkeypatch.setattr(setup.os, "getuid", lambda: 1000)       # the person, wherever the suite runs
    monkeypatch.setattr(setup.os, "getgid", lambda: 1000)
    assert setup.main(["--music", "/media/pi/Elements/Music", "--yes"]) == 1
    said = capsys.readouterr().out
    pasted = [line[4:] for line in said.splitlines() if line.startswith("    ")]
    undo = pasted.index("P=/media/sautium/4A21-0000")
    commands = [line for line in pasted[:undo] if not line.startswith(("L=", "T="))]
    # In the block's order (§2.3): the desktop's hold let go — the same disk is not mounted read-only
    # beside a mount for writing —, the folder disks are mounted in and its labels' folder, the mount
    # point, the label, the line, systemd told, the mount
    assert commands == [
        "sudo umount /dev/sda1", "B=/media/sautium", "U=4A21-0000",
        'sudo install -d -m 0755 "$B" "$B/.names"', 'sudo mkdir -p "$B/$U"',
        'printf \'%s\\n\' "$T" | sudo tee "$B/.names/$U" >/dev/null',
        "printf '\\n%s\\n' \"$L\" | sudo tee -a /etc/fstab",
        "sudo systemctl daemon-reload", 'sudo mount "$B/$U"']
    assert "T='Elements'" in pasted[:undo]
    # The line itself is built in short pieces — a line a terminal or a chat wrapped arrives as two
    # commands — and the pieces build the very line (PI-NODE-2B item 8, W2)
    pieces = [line for line in pasted if line.startswith(("L='", 'L="$L"\''))]
    assert len(pieces) > 1 and max(len(line) for line in pasted) <= 76, pasted
    built = subprocess.run(["sh", "-c", "\n".join(pieces) + '\nprintf %s "$L"'], capture_output=True, text=True).stdout
    assert built == LINE_4A21
    assert ("/media/pi/Elements/Music is on a disk this machine's desktop mounts only while you are signed in to it.\n"
            "Sautium starts with the machine, before that.\n"
            "This mounts the same disk read-only at /media/sautium/4A21-0000 whenever it is\n"
            "plugged in — at a start of the machine and later. Sautium sees it come and go there by itself.\n"
            "Close anything that has a file of the disk open first: the desktop's hold is let go here.\n") in said
    # its undo: the line, the mount point and the label the block wrote
    assert pasted[undo:] == [
        "P=/media/sautium/4A21-0000", 'sudo umount "$P"', 'sudo sed -i "\\|^UUID=[^ ]* $P |d" /etc/fstab',
        'sudo rmdir "$P"', "sudo rm -f /media/sautium/.names/4A21-0000", "sudo systemctl daemon-reload"]
    # the volume's own name is in no root command: it goes in as data, on tee's input
    assert "Elements" not in " ".join(line for line in pasted if "sudo" in line)
    assert "nofail" in built and "no effect" not in said               # §8.6: nothing said of the generator's warning
    # Not pasted (--yes never waits): never a root, and the setup says what to do and stops
    assert ("./sautium: the music disk is not set, so Sautium would not read /media/pi/Elements/Music: paste the "
            "block above, then run ./sautium again and add \"Elements\" › Music as a music folder in Sautium "
            "(More › Library)") in said
    assert machine_ready.ran == [] and not (machine_ready.root / ".env").exists()
    assert not (machine_ready.root / "host" / "compose.roots.yml").exists()

    # Pasted while the setup waited: the disk is the line's, under the folder the node reads disks in.
    # No root is added for it, the node is made, and the folder is chosen in the Web UI
    fstab["pasted"] = True
    assert setup.main(["--music", "/media/pi/Elements/Music", "--yes"]) == 0
    said = capsys.readouterr().out
    assert setup.held_roots(machine_ready.root / "host" / "compose.roots.yml") == [setup.AUTOMOUNT_BASE]
    assert "Then add \"Elements\" › Music as a music folder in Sautium (More › Library).\n" in said
    assert "Music disk at /media/sautium/4A21-0000: " in said and "not set" not in said
    assert machine_ready.ran == ["build", "start", ("screen", "nano")]
    (machine_ready.root / ".env").unlink()
    fstab["pasted"] = False
    # The same path where nothing of a desktop mounted it and fstab already mounts its disk, or a plain
    # folder of the system's disk: an ordinary root, no fstab line
    for fstab_names, target in ((0, b"/media/pi/Elements\n"), (1, b"/\n")):
        answers[("findmnt", "--fstab", "-n")] = (fstab_names, b"")
        answers[("findmnt", "-n", "-o", "TARGET", "-T")] = (0, target)
        monkeypatch.setattr(setup, "root_refusal", lambda path, **kw: None)
        assert setup.main(["--music", "/media/pi/Elements/Music", "--yes"]) == 0
        assert "sudo tee -a /etc/fstab" not in capsys.readouterr().out          # no block
        assert "/media/pi/Elements/Music" in setup.held_roots(machine_ready.root / "host" / "compose.roots.yml")
        (machine_ready.root / ".env").unlink()
        (machine_ready.root / "host" / "compose.roots.yml").unlink()


def test_a_disk_someone_mounted_by_hand_is_offered_the_line_and_taken_as_named_meanwhile(
        machine_ready, monkeypatch, tmp_path, capsys):
    """A --music folder that is a removable disk's own mount point, which no
    line of /etc/fstab names (§2.5): the setup's block instead of the
    sentence. Declined, the folder is a root as before; and a later run, and
    `status`, still say there is a step."""
    music = tmp_path / "usb"
    music.mkdir()
    mount = _mount(target=str(music), label="It's mine")
    answers = {("findmnt", "-n", "-o", "TARGET", "-T"): (0, str(music).encode() + b"\n"),
               ("findmnt", "--fstab", "-n", "-o", "TARGET"): (0, b"/\n"),
               ("findmnt", "--fstab", "-n"): (1, b""),
               ("findmnt", "-J", "-o", "SOURCE,TARGET,FSTYPE,UUID,LABEL", "-T"): (0, mount.encode()),
               **MACHINE,
               ("findmnt", "-rn", "-o", "TARGET,FSTYPE,OPTIONS"): (0, f"{music} exfat rw\n".encode()),
               ("findmnt", "-rn", "-o", "TARGET", "-S", "UUID=4A21-0000"): (0, str(music).encode() + b"\n")}
    monkeypatch.setattr(setup, "run", _findmnt(answers))
    monkeypatch.setattr(setup.os, "getuid", lambda: 1000)
    monkeypatch.setattr(setup.os, "getgid", lambda: 1000)
    assert setup.main(["--music", str(music), "--yes"]) == 0
    said = capsys.readouterr().out
    assert said.count("── The music disk, mounted whenever it is plugged in") == 1       # once in a run
    assert (f"{music} is a mount point: Sautium sees what is mounted there when Sautium starts.\n"
            "This mounts the same disk read-only at /media/sautium/4A21-0000 whenever it is\n") in said
    assert "its present hold is let go here" in said and "    sudo umount /dev/sda1\n" in said
    assert "T=" not in said                              # a label with a quote: the disk goes by its UUID
    assert "is not seen until Sautium is started" not in said          # the block, instead of the sentence
    # what follows the block, said beside it: the root forgotten (which binds the disks' folder and
    # makes the node again), the folder chosen at its new place
    assert (f"    sudo systemctl daemon-reload\n\nThen run:  ./sautium roots --forget {music}\n"
            "and add the disk as a music folder in Sautium (More › Library).\n") in said
    assert setup.held_roots(machine_ready.root / "host" / "compose.roots.yml") == [str(music), setup.AUTOMOUNT_BASE]
    # … and a later run with no folder named offers it again, with what follows it
    assert setup.main(["--yes"]) == 0
    said = capsys.readouterr().out
    assert said.count("── The music disk, mounted whenever it is plugged in") == 1
    assert f"Then run:  ./sautium roots --forget {music}\n" in said
    assert "and add the disk as a music folder in Sautium (More › Library).\n" in said
    monkeypatch.setattr(setup, "docker_state", lambda: ("ok", "Docker"))
    monkeypatch.setattr(setup.agent, "head", lambda root: "c" * 40)
    monkeypatch.setattr(setup.agent, "image_of", lambda root: "sautium-backend:local")
    monkeypatch.setattr(setup, "tailnet_address", lambda: None)
    monkeypatch.setattr(setup, "undervoltage_now", lambda: False)
    monkeypatch.setattr(setup.agent, "compose_cmd", lambda root=None: ["docker", "compose"])
    assert setup.main(["status"]) == 0
    assert (f"Music root {music}: a disk mounted at the root itself — Sautium sees it only as it was when "
            "Sautium started (./sautium shows the step)\n") in capsys.readouterr().out


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


def test_a_board_that_grew_loses_the_small_boards_postgres_lines_and_is_told(checkout, capsys):
    """Before this the setup only set: a 2 GB board's five lines stayed on
    the 16 GB board the card moved to, for ever."""
    env = checkout / ".env"
    env.write_text("# mine\nPOSTGRES_PASSWORD=kept\nPG_SHARED_BUFFERS=128MB\nPG_EFFECTIVE_CACHE_SIZE=512MB\n"
                   "PG_MAINTENANCE_WORK_MEM=64MB\nPG_MAX_WAL_SIZE=512MB\nPG_WORK_MEM=4MB\nPG_MY_OWN=1\n")
    held = setup.agent.read_env(env)
    plan = _plan(held, preset=None, ram_gib=15.5, volume_exists=True)
    changed = setup.write_env(plan)
    assert not [key for key in changed if key.startswith("PG_")]
    assert "PG_" not in env.read_text().replace("PG_MY_OWN=1", "") and "PG_MY_OWN=1\n" in env.read_text()
    assert env.read_text().startswith("# mine\nPOSTGRES_PASSWORD=kept\n")
    said = capsys.readouterr().out
    assert (".env: PG_SHARED_BUFFERS, PG_EFFECTIVE_CACHE_SIZE, PG_MAINTENANCE_WORK_MEM, PG_MAX_WAL_SIZE, "
            "PG_WORK_MEM removed: this machine's memory fits the compose file's defaults") in said
    setup.write_env(plan)
    assert "removed" not in capsys.readouterr().out                    # run again: nothing to say
    # the 4 GB board: its own four values, the 2 GB board's fifth line gone
    env.write_text(env.read_text() + "PG_WORK_MEM=4MB\nPG_SHARED_BUFFERS=128MB\n")
    changed = setup.write_env(_plan(setup.agent.read_env(env), ram_gib=3.7, volume_exists=True))
    assert "PG_SHARED_BUFFERS" in changed and "PG_WORK_MEM" not in changed
    assert setup.agent.read_env(env)["PG_SHARED_BUFFERS"] == "256MB" and "PG_WORK_MEM" not in setup.agent.read_env(env)
    assert "PG_WORK_MEM removed: this machine's memory fits the compose file's default\n" in capsys.readouterr().out


class _Docker:
    """`docker compose` as start() asks it: the backend container's id, its
    log, and an `up` that makes the container again or leaves it."""

    def __init__(self, monkeypatch, container, remade, logged=b"backend  | the old container's last lines\n"):
        self.container, self.remade, self.asked, self.logged = container, remade, [], logged
        monkeypatch.setattr(setup, "run", self.run)
        monkeypatch.setattr(setup.agent, "_run", self.run)

    def run(self, args, **kw):
        text = " ".join(args)
        if args[-1] == "postgres":                      # PostgreSQL's own container: none in these cases
            return subprocess.CompletedProcess(args, 0, b"", b"")
        self.asked.append("ps" if " ps " in text else "logs" if " logs " in text else "up" if " up " in text else text)
        out = b""
        if " ps " in text:
            out = self.container.encode()
        elif " logs " in text:
            out = self.logged
        elif " up " in text and self.remade:
            self.container = "new" + self.container
        return subprocess.CompletedProcess(args, 0, out, b"")


def test_a_start_that_makes_the_container_again_keeps_the_old_ones_log(checkout, monkeypatch):
    docker = _Docker(monkeypatch, "abc123", remade=True)
    setup.start()
    assert docker.asked == ["ps", "logs", "up", "ps"]                  # taken before the container goes
    kept = setup.agent.kept_logs(checkout)
    assert len(kept) == 1 and kept[0].name.endswith("-setup.log")
    assert kept[0].read_text() == "backend  | the old container's last lines\n"
    assert stat.S_IMODE(kept[0].stat().st_mode) == 0o600


def test_the_log_the_setup_keeps_holds_nothing_of_a_setup_codes_form(checkout, monkeypatch, caplog):
    """PI-NODE-2C F5. The setup run again on a node with no account keeps the
    outgoing container's log, and that log has the start's line with the
    code. The scrub ran — and put "XXXX-XXXX" in the code's place, which is
    itself of the code's form: the kept log still read as holding a code.
    Here the node's own start writes the line, with a code it mints."""
    sys.path.insert(0, str(REPO / "backend"))
    device_auth = pytest.importorskip("device_auth")
    import logging
    from config import LOGGING_CONFIG
    identity = checkout / "data" / "node_identity"
    for name, value in (("_identity_dir", lambda: identity), ("account_configured", lambda: False),
                        ("identity_key_on_disk", lambda: False), ("_setup_code", None)):
        monkeypatch.setattr(device_auth, name, value)
    with caplog.at_level(logging.WARNING, logger="device_auth"):
        device_auth.prepare_setup()
    code = (identity / device_auth.SETUP_CODE_FILE).read_text().strip()
    (record,) = [r for r in caplog.records if r.name == "device_auth"]
    said = logging.Formatter(LOGGING_CONFIG["formatters"]["default"]["format"],
                             LOGGING_CONFIG["formatters"]["default"]["datefmt"]).format(record)
    assert code in said and device_auth._SETUP_CODE_FORM.fullmatch(code)
    _Docker(monkeypatch, "abc123", remade=True, logged=f"sautium-backend  | {said}\n".encode())
    setup.start()
    (kept,) = setup.agent.kept_logs(checkout)
    text = kept.read_text()
    assert kept.name.endswith("-setup.log") and code not in text
    assert device_auth._SETUP_CODE_FORM.search(text) is None and setup.agent.SETUP_CODE.search(text) is None
    # the line itself stays, and says what stood there
    assert "The setup code to create it is [setup code removed] (also in setup_code.txt" in text


class _Compose:
    """`docker compose` as start() asks it, with compose's own rule for a
    service's container: made again when the service's hash — here of the
    lines of .env that reach PostgreSQL — is not the one the container was
    made from, which is its label."""

    def __init__(self, monkeypatch, env, postgres=True, backend=True):
        self.env, self.asked = env, []
        self.backend = "running" if backend else "exited"
        self.label = self.hash() if postgres else None
        monkeypatch.setattr(setup, "run", self.run)
        monkeypatch.setattr(setup.agent, "_run", self.run)
        monkeypatch.setattr(setup.agent, "compose_cmd", lambda root=None: ["docker", "compose"])

    def hash(self):
        import hashlib
        held = setup.agent.read_env(self.env) if self.env.exists() else {}
        return hashlib.sha256(repr(sorted((k, v) for k, v in held.items() if k.startswith(("PG_", "POSTGRES_")))
                                   ).encode()).hexdigest()

    def run(self, args, **kw):
        out = b""
        if args[:3] == ["docker", "compose", "ps"]:
            running = "--status" in args
            if args[-1] == "postgres":
                out = b"pg1\n" if self.label is not None else b""
            elif not running or self.backend == "running":
                out = b"be1\n"
        elif args[:4] == ["docker", "compose", "config", "--hash"]:
            out = f"postgres {self.hash()}\n".encode()
        elif args[:2] == ["docker", "inspect"]:
            assert args[-1] == "pg1" and "com.docker.compose.config-hash" in args[3]
            out = (self.label or "").encode() + b"\n"
        elif args[:3] == ["docker", "compose", "stop"]:
            self.asked.append("stop " + " ".join(args[3:]))
            self.backend = "exited"
        elif args[:3] == ["docker", "compose", "up"]:
            # as compose does it: a service whose hash moved is stopped and made again — under
            # whatever still holds connections to it
            self.asked.append("up" + (": postgres made again under a running backend"
                                      if self.label not in (None, self.hash()) and self.backend == "running" else ""))
            self.label, self.backend = self.hash(), "running"
        return subprocess.CompletedProcess(args, 0, out, b"")


def test_a_run_that_makes_postgres_again_stops_the_backend_first(checkout, monkeypatch):
    """Review B8. PostgreSQL is stopped by its smart shutdown, which waits
    for its clients: made again under a running backend — a run of ./sautium
    that moved a PG_* line of .env with the machine's memory — it waited its
    whole 30 s out and was killed (the lab's own row). The setup stops the
    backend first, as the host agent's update does; a run that leaves
    PostgreSQL's service alone stops nothing."""
    env = checkout / ".env"
    env.write_text("POSTGRES_PASSWORD=kept\nCOMPOSE_FILE=docker-compose.pi.yml\n")
    compose = _Compose(monkeypatch, env)

    def started():
        compose.asked.clear()
        setup.start()
        return compose.asked
    assert started() == ["up"]                                     # nothing moved
    # the board's memory read anew: PostgreSQL's lines move (machine_tier.pg_settings), its service with them
    held = setup.agent.read_env(env)
    changed = setup.write_env(_plan(held, ram_gib=3.7, volume_exists=True))
    assert any(key.startswith("PG_") for key in changed)
    assert setup.postgres_remade()
    assert started() == ["stop backend", "up"]
    assert not setup.postgres_remade() and started() == ["up"]      # and once only
    # a line that does not reach PostgreSQL: its container stays, nothing is stopped
    setup.agent.set_env(env, "SAUTIUM_IMAGE_MODELS", "0")
    assert started() == ["up"]
    # a board that grew loses the small board's lines: made again as well
    setup.write_env(_plan(setup.agent.read_env(env), ram_gib=15.8, volume_exists=True))
    assert started() == ["stop backend", "up"]
    # … and the same for the other ways into start(): a root forgotten, a node that is stopped
    setup.agent.set_env(env, "PG_SHARED_BUFFERS", "64MB")
    compose.backend = "exited"
    assert started() == ["up"]                                     # no backend to cut: nothing to stop
    # a first start — no PostgreSQL yet — asks nothing of a container that is not there
    first = _Compose(monkeypatch, env, postgres=False, backend=False)
    setup.start()
    assert first.asked == ["up"]


def test_a_start_that_leaves_the_container_keeps_nothing_and_trims_nothing(checkout, monkeypatch):
    """Run again with nothing changed, the setup must not push an update's
    kept log out with a copy of what Docker still holds."""
    logs = checkout / "host" / "logs"
    logs.mkdir()
    earlier = ["backend-20261120-101500-update.log", "backend-20261121-101500-undo.log",
               "backend-20261122-101500-update.log"]
    for name in earlier:
        (logs / name).write_text("kept\n")
    _Docker(monkeypatch, "abc123", remade=False)
    setup.start()
    assert sorted(p.name for p in logs.iterdir()) == earlier


def test_a_first_start_has_no_log_to_keep(checkout, monkeypatch):
    docker = _Docker(monkeypatch, "", remade=True)
    setup.start()
    assert docker.asked == ["ps", "up"] and not (checkout / "host" / "logs").exists()


# -- the release the setup leaves the folder on (PI-NODE-2E F22) ----------------------------
# The setup moves a checkout only forward, and only before the host agent
# exists: on 2D's evening a setup run again after an update checked out the
# clone's own newest tag — older than the release the agent had installed —
# and the node ran v0.2.11's code over a v0.2.14 database.

class _Moved(Exception):
    pass


def _g(root: Path, *args: str) -> str:
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t", GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
    return subprocess.run(["git", *args], cwd=root, env=env, check=True, capture_output=True,
                          text=True).stdout.strip()


def _commit(root: Path, version: str, setup_file: bool = True) -> str:
    (root / "desktop").mkdir(exist_ok=True)
    (root / "desktop" / "build_common.py").write_text(f'VERSION = "{version}"\n')
    if setup_file:
        (root / "scripts").mkdir(exist_ok=True)
        (root / "scripts" / "setup.py").write_text(f"# {version}\n")
    _g(root, "add", "-A")
    _g(root, "commit", "-q", "--allow-empty", "-m", version)
    return _g(root, "rev-parse", "HEAD")


@pytest.fixture
def releases(checkout, monkeypatch):
    """A clone whose own tags are v0.2.11; releases above it as the host
    agent's fetch keeps them (refs/sautium/releases/), and `main` past them."""
    root = checkout
    _g(root, "init", "-q", "-b", "main")
    (root / ".gitignore").write_text("host/\ndata/\n")
    commits = {}
    for version in ("0.2.11", "0.2.12", "0.2.13", "0.2.14"):
        commits[version] = _commit(root, version)
        _g(root, "tag", "-a", "-m", version, f"v{version}")
    for version in ("0.2.12", "0.2.13", "0.2.14"):
        _g(root, "update-ref", f"{setup.agent.RELEASE_REFS}v{version}", _g(root, "rev-parse", f"v{version}"))
        _g(root, "tag", "-d", f"v{version}")
    commits["main"] = _commit(root, "0.2.14")
    monkeypatch.setattr(setup.os, "execv", lambda *a: (_ for _ in ()).throw(_Moved(a)))
    return commits


def _on(root: Path, commit: str) -> None:
    _g(root, "-c", "advice.detachedHead=false", "checkout", "-q", "--detach", commit)


def _agent_installed(root: Path) -> None:
    (root / "host" / "agent.env").write_text("SAUTIUM_HOST_CHANNEL_KEY=" + "ab" * 32 + "\n")


def test_a_setup_run_again_after_the_agents_update_leaves_the_checkout_on_its_release(releases, checkout, capsys):
    _on(checkout, releases["0.2.14"])
    _agent_installed(checkout)
    setup.release_step()
    assert _g(checkout, "rev-parse", "HEAD") == releases["0.2.14"]
    said = capsys.readouterr().out
    assert "on the release v0.2.14" in said and "./sautium update" in said and "v0.2.11" not in said
    assert f"verify-tag {setup.agent.RELEASE_REFS}v0.2.14" in said      # a ref git resolves


def test_with_the_agent_installed_the_setup_moves_no_checkout_even_forward(releases, checkout, capsys):
    _on(checkout, releases["0.2.11"])
    _agent_installed(checkout)
    _g(checkout, "tag", "-a", "-m", "x", "v0.2.15", releases["main"])     # a tag the person fetched himself
    setup.release_step()
    assert _g(checkout, "rev-parse", "HEAD") == releases["0.2.11"]
    assert "./sautium update" in capsys.readouterr().out


def test_with_the_agent_installed_a_checkout_on_no_release_is_said_as_its_commit(releases, checkout, capsys):
    _on(checkout, releases["main"])
    _agent_installed(checkout)
    setup.release_step()
    said = capsys.readouterr().out
    assert releases["main"][:12] in said and "on no release" in said and "./sautium update" in said


def test_without_the_agent_the_setup_never_moves_below_the_version_head_states(releases, checkout, capsys):
    _on(checkout, releases["0.2.14"])
    setup.release_step()
    assert _g(checkout, "rev-parse", "HEAD") == releases["0.2.14"]
    assert "on the release v0.2.14" in capsys.readouterr().out


def test_a_fresh_clones_first_setup_still_moves_to_its_newest_release(releases, checkout):
    _g(checkout, "tag", "-a", "-m", "x", "v0.2.14", _g(checkout, "rev-parse", f"{setup.agent.RELEASE_REFS}v0.2.14"))
    for version in ("0.2.12", "0.2.13", "0.2.14"):
        _g(checkout, "update-ref", "-d", f"{setup.agent.RELEASE_REFS}v{version}")
    _g(checkout, "checkout", "-q", "main")
    with pytest.raises(_Moved):
        setup.release_step()
    assert _g(checkout, "rev-parse", "HEAD") == releases["0.2.14"]


def test_the_agents_fetched_refs_are_never_a_release_the_setup_moves_to(releases, checkout):
    _on(checkout, releases["0.2.11"])
    setup.release_step()
    assert _g(checkout, "rev-parse", "HEAD") == releases["0.2.11"]


def test_without_the_agent_a_newer_tag_of_the_persons_own_clone_is_taken_forward(releases, checkout):
    _on(checkout, releases["0.2.11"])
    _g(checkout, "tag", "-a", "-m", "x", "v0.2.14", _g(checkout, "rev-parse", f"{setup.agent.RELEASE_REFS}v0.2.14"))
    with pytest.raises(_Moved):
        setup.release_step()
    assert _g(checkout, "rev-parse", "HEAD") == releases["0.2.14"]


def test_a_tag_named_above_head_on_a_tree_that_states_less_is_never_moved_to(releases, checkout):
    """The floor reads the version the tag's tree states, not its name: a
    tag named v0.2.15 on v0.2.11's commit is no way forward (review round 1)."""
    _on(checkout, releases["0.2.14"])
    _g(checkout, "tag", "-a", "-m", "x", "v0.2.15", releases["0.2.11"])
    setup.release_step()
    assert _g(checkout, "rev-parse", "HEAD") == releases["0.2.14"]


def test_a_release_without_the_setup_is_never_moved_to(releases, checkout):
    _on(checkout, releases["0.2.11"])
    (checkout / "scripts" / "setup.py").unlink()
    _g(checkout, "add", "-A")
    _g(checkout, "commit", "-q", "-m", "no setup")
    _g(checkout, "tag", "-a", "-m", "x", "v0.2.12", "HEAD")
    _on(checkout, releases["0.2.11"])
    setup.release_step()
    assert _g(checkout, "rev-parse", "HEAD") == releases["0.2.11"]


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
    assert text.count("create_host_path: true") == text.count("- type: bind")
    if not os.path.isdir(setup.AUTOMOUNT_BASE):
        # named, and not bound until the automount is installed: made by Docker, it would read as one
        assert text.count("- type: bind") == 1
    # … while a folder a person named stays bound when it is gone (PI-NODE-2C §6.3)
    gone = setup.roots_override([str(tmp_path / "gone"), setup.AUTOMOUNT_BASE])
    assert f'source: "{tmp_path / "gone"}"' in gone and "create_host_path: true" in gone
    yaml = pytest.importorskip("yaml")
    parsed = yaml.safe_load(text)["services"]["backend"]
    assert parsed["environment"]["SAUTIUM_LIBRARY_ROOTS"].split(":")[0] == str(music)
    # A root whose folder is gone must not stop the node's start: Docker makes an empty one in its place
    assert all(v["read_only"] and v["bind"] == {"propagation": "rslave", "create_host_path": True}
               and v["source"] == v["target"] for v in parsed["volumes"])
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


def test_a_disk_under_the_disks_folder_adds_no_root(tmp_path, monkeypatch, capsys):
    """Mounted by the automount or by the setup's line, a disk lies under the
    one root the node has for disks: naming a folder on it binds nothing new
    — and so a disk added later needs no change of the override, no restart."""
    base = tmp_path / "media" / "sautium"
    (base / "4A21-0000" / "Music").mkdir(parents=True)
    monkeypatch.setattr(setup, "AUTOMOUNT_BASE", str(base))
    for named in (base / "4A21-0000", base / "4A21-0000" / "Music"):
        assert setup.chosen_roots(str(named), []) == [str(base)]
        assert f"{named} is inside {base}, which this node already reads" in capsys.readouterr().out
    text = setup.roots_override([str(base)])
    assert text.count("- type: bind") == 1 and "propagation: rslave" in text and "4A21" not in text
    assert "SAUTIUM_LIBRARY_ROOT_LABELS" not in text          # the label is the file beside the disk, not the override's


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
    """One form on the host — the agent's, which the setup reads the code by
    and the kept log is scrubbed by — and the node's own, the same pattern."""
    backend = (REPO / "backend" / "device_auth.py").read_text(encoding="utf-8")
    assert "SETUP_CODE_GROUPS = 2" in backend
    assert not hasattr(setup, "SETUP_CODE") and SOURCE.count("agent.SETUP_CODE.fullmatch(") == 1
    assert setup.agent.SETUP_CODE.fullmatch("K7QM-ZP4X") and not setup.agent.SETUP_CODE.fullmatch("K7QM-ZP4X-9HTR")
    sys.path.insert(0, str(REPO / "backend"))
    device_auth = pytest.importorskip("device_auth")
    assert setup.agent.SETUP_CODE.pattern == device_auth._SETUP_CODE_FORM.pattern
    for _ in range(200):                                # … and every code the node mints is of it
        assert setup.agent.SETUP_CODE.fullmatch(device_auth._code(device_auth.SETUP_CODE_GROUPS))


def test_the_setup_reads_the_pairing_code_by_the_node_s_own_form():
    """The pairing code is not the setup code: 7 groups, never typed
    (SEC-S2 A3). `./sautium pair` reads what the node mints, and nothing
    shorter."""
    sys.path.insert(0, str(REPO / "backend"))
    device_auth = pytest.importorskip("device_auth")
    assert device_auth.PIN_GROUPS == 7
    for _ in range(200):
        code = device_auth._code(device_auth.PIN_GROUPS)
        assert setup.PAIRING_CODE.fullmatch(code) and not setup.agent.SETUP_CODE.fullmatch(code)
    assert not setup.PAIRING_CODE.fullmatch("K7QM-ZP4X")


def test_a_picture_that_is_not_only_blocks_is_not_shown(monkeypatch):
    monkeypatch.setattr(setup.agent, "image_of", lambda root: "sautium-backend:local")
    asked = []

    def fake(args, **kw):
        asked.append((args, kw.get("stdin")))
        return subprocess.CompletedProcess(args, 0, drawn.encode(), b"")
    monkeypatch.setattr(setup, "run", fake)
    drawn = "██▀▄  \n▄▄██  \n\nhttp://192.0.2.10:8800/#setup=D889-VPL4\n"
    assert setup.qr("http://192.0.2.10:8800/#setup=D889-VPL4") == drawn
    args, stdin = asked[0]
    # The text goes in on stdin, never on the command line; the container sees nothing
    assert stdin == b"http://192.0.2.10:8800/#setup=D889-VPL4" and "D889-VPL4" not in " ".join(args)
    for flag in ("--network", "none", "--read-only", "--cap-drop", "ALL"):
        assert flag in args
    assert "-v" not in args and "--volume" not in args and "--mount" not in args
    drawn = "██ \x1b]0;evil\x07\n\nx\n"
    assert setup.qr("x") is None
    drawn = "██▀▄  \n\nhttp://elsewhere/\n"                # only the text sent is the QR's value
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
    for command in ("status", "roots", "pair", "update", "remove"):
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
    monkeypatch.setattr(setup, "ntfs_3g", lambda: True)
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
    # The folder a disk nothing mounted is said in is root's, made by the block
    assert 'sudo install -d -m 0755 "$B" "$B/.names" "$B/.unread"' in commands and "B=/media/sautium" in commands
    assert "<" not in printed.replace("<its UUID>", "")                   # no placeholder to edit
    assert "Updates while signed out: not set" in printed and "Plugged disks: not set" in printed
    files = sorted(p.name for p in (checkout / "host" / "automount").iterdir())
    assert files == ["99-sautium-automount.rules", "automount", "sautium-automount@.service"]
    # Nothing to print where both are in place
    monkeypatch.setattr(setup, "lingering", lambda: True)
    monkeypatch.setattr(setup, "automount_installed", lambda: True)
    monkeypatch.setattr(setup, "automount_stale", lambda files, places=None: False)
    setup.root_steps(types.SimpleNamespace(yes=False), {})
    assert capsys.readouterr().out == ""


def test_an_automount_of_an_earlier_release_is_offered_again_and_the_node_started_again(
        machine_ready, monkeypatch, tmp_path, capsys):
    """Installed is not enough: an earlier release's script knows no ntfs-3g
    and says nothing of a disk it could not mount. The three files are
    compared by their bytes (anyone may read them), the same block printed
    as an update, and the node — which holds its watch of the unread folder
    from its start — started again by the run that finds them current."""
    files = setup.automount_files(tmp_path / "written", 1000, 1000)
    places = {}
    for kind, path in files.items():
        places[kind] = str(tmp_path / f"installed-{kind}")
        shutil.copy(path, places[kind])
    assert not automount_stale(files, places)
    Path(places["script"]).write_text(files["script"].read_text().replace("ntfs-3g", "ntfs3"))
    assert automount_stale(files, places)
    Path(places["script"]).unlink()
    assert automount_stale(files, places)

    stale = [True]
    asked = []
    monkeypatch.setattr(setup, "automount_stale", lambda files, places=None: stale[0])
    monkeypatch.setattr(setup, "node_running", lambda: True)
    monkeypatch.setattr(setup, "run", lambda args, **kw: asked.append(args) or subprocess.CompletedProcess(args, 0, b"", b""))
    assert setup.main(["--yes"]) == 0
    said = capsys.readouterr().out
    assert "Plugged disks: an update of the automount" in said
    assert '    sudo install -m 0755 "$A/automount" /usr/local/lib/sautium/' in said
    assert "the automount is an earlier release's" in said
    assert setup.load_state()["automount_stale"] is True
    assert not any(a[-2:] == ["restart", "backend"] for a in asked)
    stale[0] = False                                   # the person pasted the block; the next run
    assert setup.main(["--yes"]) == 0
    assert "an update of the automount" not in capsys.readouterr().out
    assert [a for a in asked if a[-2:] == ["restart", "backend"]]
    assert "automount_stale" not in setup.load_state()
    asked.clear()
    assert setup.main(["--yes"]) == 0                  # and once only
    assert not any(a[-2:] == ["restart", "backend"] for a in asked)


def test_the_ntfs_driver_is_offered_where_it_is_missing(checkout, monkeypatch, capsys):
    monkeypatch.setattr(setup, "lingering", lambda: True)
    monkeypatch.setattr(setup, "automount_installed", lambda: True)
    monkeypatch.setattr(setup, "automount_stale", lambda files, places=None: False)
    monkeypatch.setattr(setup, "ntfs_3g", lambda: False)
    monkeypatch.setattr(setup.shutil, "which", lambda name, path=None: "/usr/bin/apt-get" if name == "apt-get" else None)
    setup.root_steps(types.SimpleNamespace(yes=True), {})
    said = capsys.readouterr().out
    assert "    sudo apt-get install -y ntfs-3g" in said and "    sudo apt-get remove -y ntfs-3g" in said
    assert "Windows-formatted disks: not set" in said
    # another package manager: the package is named, and no command is guessed
    monkeypatch.setattr(setup.shutil, "which", lambda name, path=None: None)
    setup.root_steps(types.SimpleNamespace(yes=True), {})
    said = capsys.readouterr().out
    assert "Install the package ntfs-3g" in said and "sudo" not in said
    # an install that reads its music by its .env line has no plugged disks: nothing offered
    setup.root_steps(types.SimpleNamespace(yes=True), {}, automount=False)
    assert capsys.readouterr().out == ""


TOOLS = {
    "findmnt": '''#!/bin/sh
case "$*" in
  "-n -o SOURCE /") echo /dev/nvme0n1p2 ;;
  "--fstab -n -S "*) grep -qx "$4" "$STUB/fstab-devices" 2>/dev/null ;;
  "-n -S "*) grep -qx "$3" "$STUB/mounted-devices" 2>/dev/null ;;
  "-n "*) grep -qx "$2" "$STUB/mounted-points" 2>/dev/null ;;
esac
''',
    # the column asked for (after -o) of the device named last. NAME: the
    # device's own line and its children's — the plugged device's from
    # `partitions`, its parent disk's from `siblings`
    "lsblk": '''#!/bin/sh
for dev; do :; done
col=""; prev=""
for arg; do [ "$prev" != -o ] || col="$arg"; prev="$arg"; done
case "$col:$dev" in
  PKNAME:/dev/nvme0n1p2|PKNAME:/dev/nvme0n1p3) echo nvme0n1 ;;
  PKNAME:*) cat "$STUB/pkname" 2>/dev/null ;;
  NAME:/dev/loop0) echo loop0; cat "$STUB/partitions" 2>/dev/null ;;
  NAME:*) echo "${dev#/dev/}"; cat "$STUB/siblings" 2>/dev/null ;;
  SIZE:*) cat "$STUB/size" 2>/dev/null || echo 15500000000 ;;
  PARTTYPE:*) ;;    # udev's property: none for a partition its probe found no filesystem on (F24)
esac
''',
    # `blkid -p`: the low-level probe's exit alone, which tells nothing apart:
    # 2 for a blank volume and, on util-linux 2.41, for two marks; 8 for two
    # marks before it
    "blkid": '''#!/bin/sh
[ "$1" != -p ] || exit "$(cat "$STUB/probe" 2>/dev/null || echo 2)"
cat "$STUB/blkid-$4" 2>/dev/null
''',
    # the signatures on the device, one TYPE a line as wipefs lists them (a
    # filesystem at each offset it is found at) — or the real wipefs's on an
    # image, where a test has one
    "wipefs": '''#!/bin/sh
[ "$*" = "--no-act --noheadings --parsable --output TYPE /dev/loop0" ] || exit 64
[ ! -e "$STUB/wipefs-fails" ] || exit 1
if [ -e "$STUB/image" ]; then exec "$(cat "$STUB/real-wipefs")" "$1" "$2" "$3" "$4" "$5" "$STUB/image"; fi
[ ! -e "$STUB/signatures" ] || cat "$STUB/signatures"
''',
    # the partition table of the disk `pkname` names: `table`'s lines "N TYPE",
    # the type right-aligned as partx prints it
    "partx": '''#!/bin/sh
echo "$*" >> "$STUB/partx-calls"
[ "$1 $2 $3 $4 $6" = "-g -o TYPE -n /dev/$(cat "$STUB/pkname")" ] || exit 64
[ -e "$STUB/table" ] || exit 0
while read -r n type; do [ "$n" != "$5" ] || echo " $type"; done < "$STUB/table"
''',
    "mount": '''#!/bin/sh
echo "$*" >> "$STUB/mount-calls"
[ ! -e "$STUB/mount-fails" ] || { case "$*" in *time_offset*) exit 32 ;; esac; }
[ ! -e "$STUB/mount-refuses" ] || exit 32
echo "$6" >> "$STUB/mounted-points"
''',
    "umount": '''#!/bin/sh
echo "$*" >> "$STUB/umount-calls"
''',
}
# What the script uses of the system besides: linked into the stubs' folder,
# so that the PATH is the test's own (an ntfs-3g this machine has is not found)
PLAIN_TOOLS = ("cat", "head", "tr", "wc", "grep", "sort", "mkdir", "rmdir", "rm", "mv")


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
    for name in PLAIN_TOOLS:
        (bin_dir / name).symlink_to(shutil.which(name))
    sysfs = tmp_path / "sys"                   # the kernel's: a partition's number, by its name
    files = setup.automount_files(tmp_path / "out", 1000, 1000, base=str(base), state=str(state),
                                  sysfs=str(sysfs))
    env = {"PATH": str(bin_dir), "STUB": str(stub)}
    sh = shutil.which("sh")

    def plug(verb="mount", name="loop0", **blkid):
        for key, value in blkid.items():
            (stub / f"blkid-{key}").write_text(value + "\n" if value else "")
        return subprocess.run([sh, str(files["script"]), verb, name], env=env, capture_output=True, text=True)

    def calls(tool="mount"):
        path = stub / f"{tool}-calls"
        return path.read_text().splitlines() if path.exists() else []

    def ntfs_3g():
        (bin_dir / "ntfs-3g").write_text("#!/bin/sh\n")
        (bin_dir / "ntfs-3g").chmod(0o755)

    def partition(number, disk="sda", siblings=("sda1", "sda2"), table=()):
        """The plugged volume as partition `number` of `disk`, beside
        `siblings`, its disk's table `table`'s (number, type) pairs."""
        (sysfs / "class" / "block" / "loop0").mkdir(parents=True, exist_ok=True)
        (sysfs / "class" / "block" / "loop0" / "partition").write_text(f"{number}\n")
        (stub / "pkname").write_text(disk + "\n")
        (stub / "siblings").write_text("".join(f"{s}\n" for s in siblings))
        (stub / "table").write_text("".join(f"{n} {t}\n" for n, t in table))
    return types.SimpleNamespace(plug=plug, calls=calls, base=base, state=state, stub=stub, files=files,
                                 ntfs_3g=ntfs_3g, partition=partition, unread=base / ".unread" / "loop0")


def test_the_automounts_shell_parses_and_carries_the_owner(automount):
    for name in ("script",):
        assert subprocess.run(["sh", "-n", str(automount.files[name])]).returncode == 0
    script = automount.files["script"].read_text()
    assert "OWNER=1000:1000" in script and "set -eu" in script
    assert stat.S_IMODE(automount.files["script"].stat().st_mode) == 0o755
    unit = automount.files["unit"].read_text()
    assert "ExecStart=/usr/local/lib/sautium/automount mount %I" in unit and "BindsTo=dev-%i.device" in unit
    # Active while the disk is there, mounted or not: its stop is what removes what was said of it
    assert "RemainAfterExit=yes" in unit and "ExecStop=/usr/local/lib/sautium/automount unmount %I" in unit
    rule = automount.files["rule"].read_text()
    assert 'SUBSYSTEMS=="usb"' in rule and 'KERNEL=="mmcblk[0-9]*"' in rule
    # Every event but a removal: a card put into a reader already plugged in is a change, never an add
    lines = [line for line in rule.splitlines() if line and not line.startswith("#")]
    assert len(lines) == 5 and all(line.startswith('ACTION!="remove", SUBSYSTEM=="block", ') for line in lines)
    assert all(line.endswith('TAG+="systemd", ENV{SYSTEMD_WANTS}+="sautium-automount@%k.service"')
               for line in lines[:4])
    # … and the one that ends the unit with the medium, where the reader stays
    assert lines[4].endswith(
        'SUBSYSTEMS=="usb", KERNEL!="sr[0-9]*", ATTR{removable}=="1", ATTR{size}=="0", ENV{SYSTEMD_READY}="0"')
    # Root runs only the installed copy, never a file the person's own user can write
    assert str(automount.files["script"].parent) not in unit
    # Nothing root removes is named by a variable that could be empty
    for line in script.splitlines():
        if line.lstrip().startswith(("rm ", "rmdir ", "umount ")):
            assert '"$BASE/' not in line and '"$STATE/' not in line, line


@pytest.mark.parametrize("fstype, driver, time", [
    ("vfat", "vfat", "tz=UTC"), ("exfat", "exfat", "time_offset=0"), ("ntfs", "ntfs3", None),
    ("ntfs", "ntfs-3g", None), ("ext4", "ext4", None)])
def test_a_plugged_disk_is_mounted_read_only_under_its_uuid(automount, fstype, driver, time):
    if driver == "ntfs-3g":
        automount.ntfs_3g()                    # installed: the driver that opens what Windows formats
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
    assert not automount.unread.exists()
    assert automount.plug("unmount").returncode == 0
    assert automount.calls("umount") == [f"-l {point}"] and not point.exists()
    assert not (automount.state / "loop0").exists() and not (automount.base / ".names" / "4A21-0000").exists()
    assert automount.plug("unmount").returncode == 0                   # again: nothing to do


def test_what_is_not_a_plugged_music_disk_is_left_alone(automount):
    def left(**blkid):
        before = len(automount.calls())
        assert automount.plug(**blkid).returncode == 0
        return len(automount.calls()) == before and not automount.unread.exists()
    for nothing in ("swap", "crypto_LUKS", "LVM2_member", "linux_raid_member", "zfs_member"):
        assert left(TYPE=nothing, UUID="4A21-0000"), nothing            # no filesystem of files
    (automount.stub / "pkname").write_text("nvme0n1\n")                # the system's own disk
    assert left(TYPE="vfat", UUID="4A21-0000")
    (automount.stub / "pkname").write_text("sda\n")
    (automount.stub / "mounted-devices").write_text("/dev/loop0\n")    # something else mounted it
    assert left(TYPE="vfat", UUID="4A21-0000")
    (automount.stub / "mounted-devices").unlink()
    # A disk a line of /etc/fstab mounts is that line's: two mounts of one stick are one too many
    (automount.stub / "fstab-devices").write_text("/dev/loop0\n")
    assert left(TYPE="vfat", UUID="4A21-0000") and left(TYPE="btrfs", UUID="4A21-0000")
    (automount.stub / "fstab-devices").unlink()
    # A whole disk that holds partitions, whatever signature an old format left on it: its volumes
    # are the partitions, and it is never "a disk that could not be read"
    (automount.stub / "partitions").write_text("loop0p1\n")
    assert left(TYPE="iso9660", UUID="2026-11-05-16-39-48-00") and left(TYPE="vfat", UUID="4A21-0000")
    (automount.stub / "partitions").unlink()
    for name in ("", "sda1; reboot", "../sda1", "sda 1"):
        assert automount.plug(name=name, TYPE="vfat", UUID="4A21-0000").returncode == 64, name
    assert automount.plug("format", TYPE="vfat", UUID="4A21-0000").returncode == 64
    assert automount.calls() == [] and not (automount.base / ".unread").exists()


BOOT_ID = Path("/proc/sys/kernel/random/boot_id")


@pytest.mark.parametrize("case, blkid, said", [
    ("a format the script never mounts", dict(TYPE="apfs", UUID="4A21-0000", LABEL="Mac disk"),
     ["apfs", "Mac disk", "-"]),
    ("btrfs, no label", dict(TYPE="btrfs", UUID="0a1b2c3d-0000-4000-8000-000000000000", LABEL=""), ["btrfs", "", "-"]),
    ("a type that is no word", dict(TYPE="We1rd; reboot", UUID="4A21-0000", LABEL="x"), ["unknown", "x", "-"]),
    ("a long type", dict(TYPE="a" * 40, UUID="4A21-0000", LABEL="x"), ["a" * 16, "x", "-"]),
    ("no UUID to name a mount point by", dict(TYPE="vfat", UUID="../../etc", LABEL="x"), ["vfat", "x", "-"]),
    ("ntfs3 refused it", dict(TYPE="ntfs", UUID="01DC5A7E6B2F4C10", LABEL="Windows stick"),
     ["ntfs", "Windows stick", "ntfs3"]),
    ("ntfs-3g refused it", dict(TYPE="ntfs", UUID="01DC5A7E6B2F4C10", LABEL="Windows stick"),
     ["ntfs", "Windows stick", "ntfs-3g"]),
    ("a label of several lines", dict(TYPE="ext4", UUID="4A21-0000", LABEL="one\ntwo\n" + "z" * 200),
     ["ext4", "onetwo" + "z" * 112, "ext4"])])
def test_a_disk_nothing_mounted_is_said_to_the_node(automount, case, blkid, said):
    """One small file under the folder the node watches: four lines — the
    boot's id, the type as blkid names it, the label, the driver that was
    tried — each bounded by the writer, renamed into place, gone at the pull
    (PI-NODE-2C §2.3). Before this a failed mount left nothing and the node
    heard nothing: the person plugged a disk in and it showed nothing."""
    if not BOOT_ID.exists():
        pytest.skip("no boot id on this system")
    if said[2] != "-":
        (automount.stub / "mount-refuses").write_text("")
    if said[2] == "ntfs-3g":
        automount.ntfs_3g()
    result = automount.plug(**blkid)
    # The unit stays active for it: its stop, at the pull, is what removes the file
    assert result.returncode == 0 and "is not mounted: " in result.stderr, case
    assert automount.unread.read_text() == "\n".join([BOOT_ID.read_text().strip()] + said) + "\n"
    assert len(automount.unread.read_bytes()) <= 256                   # what the node reads of it
    assert sorted(p.name for p in automount.unread.parent.iterdir()) == ["loop0"]      # no temporary file left
    if said[2] != "-":
        # No second try with the other driver: a volume one parser refused is not handed to another
        assert len(automount.calls()) == 1 and f"-t {said[2]} " in automount.calls()[0]
        assert not (automount.base / blkid["UUID"]).exists() and not (automount.base / ".names" / blkid["UUID"]).exists()
    else:
        assert automount.calls() == []
    assert not (automount.state / "loop0").exists()
    assert automount.plug("unmount").returncode == 0                   # the pull
    assert not automount.unread.exists() and automount.calls("umount") == []
    assert automount.plug("unmount").returncode == 0


def test_what_was_said_of_a_name_goes_when_the_name_is_plugged_again(automount):
    """A file a power cut left belongs to another boot (the node ignores it);
    the next volume under that kernel name gets its own verdict."""
    automount.unread.parent.mkdir()
    automount.unread.write_text("another-boot\nntfs\nold\nntfs3\n")
    assert automount.plug(TYPE="vfat", UUID="4A21-0000", LABEL="ok").returncode == 0
    assert not automount.unread.exists() and len(automount.calls()) == 1


def _rule_matches(properties: dict) -> list:
    """The lines of the automount's rule that match a device, by udev's own
    reading of a match key: `==` and `!=` against a shell pattern, an absent
    property the empty string, SUBSYSTEMS any device above it. `properties`:
    as `udevadm info` names them — ACTION, SUBSYSTEM, KERNEL, SUBSYSTEMS (a
    list), ATTR{size}, ENV{…}."""
    import fnmatch
    import re
    matched = []
    for line in setup.AUTOMOUNT_RULE.splitlines():
        if not line or line.startswith("#"):
            continue
        keys = re.findall(r'([A-Z]+(?:\{[A-Za-z_]+\})?)(==|!=)"([^"]*)"', line)
        assert keys and line.count('=="') + line.count('!="') == len(keys), line

        def holds(key, op, pattern):
            values = properties.get(key, "")
            values = values if isinstance(values, list) else [values]
            return any(fnmatch.fnmatchcase(v, pattern) for v in values) == (op == "==")
        if all(holds(*key) for key in keys):
            matched.append(line)
    return matched


USB = {"ACTION": "add", "SUBSYSTEM": "block", "SUBSYSTEMS": ["block", "scsi", "usb"], "ATTR{size}": "30310400"}


@pytest.mark.parametrize("what, properties, wants", [
    ("a stick's FAT volume", dict(USB, KERNEL="sda1", **{"ENV{ID_FS_USAGE}": "filesystem"}), 1),
    # PI-NODE-2C §11.10's stick: a quick format over an older filesystem — no ID_FS_* at all
    ("a volume the system names no filesystem on", dict(USB, KERNEL="sda1"), 1),
    ("the same, a property set and empty", dict(USB, KERNEL="sda1", **{"ENV{ID_FS_USAGE}": ""}), 1),
    ("a card in the slot, no filesystem named", {"ACTION": "change", "SUBSYSTEM": "block", "KERNEL": "mmcblk1p1",
                                                 "SUBSYSTEMS": ["block", "mmc"], "ATTR{size}": "62333952"}, 1),
    ("swap", dict(USB, KERNEL="sda2", **{"ENV{ID_FS_USAGE}": "other"}), 0),
    ("an encrypted volume", dict(USB, KERNEL="sda1", **{"ENV{ID_FS_USAGE}": "crypto"}), 0),
    ("a RAID member", dict(USB, KERNEL="sda1", **{"ENV{ID_FS_USAGE}": "raid"}), 0),
    ("a card reader's empty slot", dict(USB, KERNEL="sdb", **{"ATTR{size}": "0"}), 0),
    ("the same slot, a card with a filesystem just pulled", dict(USB, KERNEL="sdb", ACTION="change",
                                                                 **{"ATTR{size}": "0"}), 0),
    ("a USB optical drive, its tray empty or an audio CD in it", dict(USB, KERNEL="sr0"), 0),
    ("a data CD in it", dict(USB, KERNEL="sr0", **{"ENV{ID_FS_USAGE}": "filesystem"}), 1),
    ("a pull", dict(USB, KERNEL="sda1", ACTION="remove"), 0),
    ("a SATA disk", {"ACTION": "add", "SUBSYSTEM": "block", "KERNEL": "sdc1", "SUBSYSTEMS": ["block", "scsi", "ata"],
                     "ATTR{size}": "30310400"}, 0),
    ("a USB device that is no block device", dict(USB, KERNEL="ttyUSB0", SUBSYSTEM="tty"), 0)])
def test_the_rule_brings_a_volume_with_no_filesystem_named_to_the_script(what, properties, wants):
    """Until PI-NODE-2D the rule matched ID_FS_USAGE=="filesystem" alone: a
    volume udev found no one filesystem on started no unit, and the person
    who plugged it in was told nothing (F18)."""
    assert len([line for line in _rule_matches(properties) if "SYSTEMD_WANTS" in line]) == wants, what


NO_SIZE = dict(USB, ACTION="change", **{"ATTR{size}": "0"})


@pytest.mark.parametrize("what, properties, gone", [
    ("a card reader's slot, its card pulled", dict(NO_SIZE, KERNEL="sdb", **{"ATTR{removable}": "1"}), True),
    ("the same reader plugged in empty", dict(NO_SIZE, KERNEL="sdb", ACTION="add", **{"ATTR{removable}": "1"}), True),
    # a fixed disk in a USB case that reports no size for a moment (a revalidation after a reset) is
    # still there: a library in use on it must not be unmounted under its reader
    ("a USB hard disk's whole device, no size for a moment", dict(NO_SIZE, KERNEL="sda", **{"ATTR{removable}": "0"}),
     False),
    ("a device that says nothing of removable media", dict(NO_SIZE, KERNEL="sda"), False),
    ("an optical drive with no disc: systemd's own rule", dict(NO_SIZE, KERNEL="sr0", **{"ATTR{removable}": "1"}),
     False),
    ("a reader's slot with a card in it", dict(USB, ACTION="change", KERNEL="sdb", **{"ATTR{removable}": "1"}), False),
    ("a reader that is being unplugged", dict(NO_SIZE, KERNEL="sdb", ACTION="remove", **{"ATTR{removable}": "1"}),
     False),
    ("a card slot of the machine's own, not on USB", {"ACTION": "change", "SUBSYSTEM": "block", "KERNEL": "mmcblk1",
                                                     "SUBSYSTEMS": ["block", "mmc"], "ATTR{size}": "0",
                                                     "ATTR{removable}": "1"}, False)])
def test_only_a_device_whose_medium_left_is_said_not_to_be_there(what, properties, gone):
    """Review R5. The line that ends the unit with the medium matched any USB
    block device of size 0 — a hard disk's whole device among them, whose
    unit, and any mount bound to it, was then stopped under a library in use.
    It is for a reader whose medium left: the kernel's own word, removable."""
    said = [line for line in _rule_matches(properties) if 'ENV{SYSTEMD_READY}="0"' in line]
    assert bool(said) is gone, what
    assert not [line for line in _rule_matches(properties) if "SYSTEMD_WANTS" in line] or not gone


class _Reader:
    """One device's udev events taken through the rule and — as systemd acts
    on the rule's words — through the script. A unit the device wants is
    started when the device is there and the unit is not active (`mount`; a
    oneshot that stays active, so an event for a device whose unit runs
    starts nothing). A device the rule says is not ready is gone to systemd,
    as one removed is, and the unit bound to it stops (`unmount`)."""

    def __init__(self, automount):
        self.automount, self.active = automount, False

    def event(self, action="change", size="30310400", usage=None, **blkid):
        properties = dict(USB, ACTION=action, KERNEL="loop0", **{"ATTR{size}": size, "ATTR{removable}": "1"})
        if usage:
            properties["ENV{ID_FS_USAGE}"] = usage
        matched = _rule_matches(properties)
        there = action != "remove" and not any('ENV{SYSTEMD_READY}="0"' in line for line in matched)
        if not there and self.active:
            assert self.automount.plug("unmount").returncode == 0
            self.active = False
        elif there and not self.active and any("SYSTEMD_WANTS" in line for line in matched):
            assert self.automount.plug(**(blkid or dict(TYPE="", UUID="", LABEL=""))).returncode == 0
            self.active = True


def test_what_is_said_of_a_card_goes_with_the_card_where_its_reader_stays(automount):
    """Review B9. A USB card reader keeps its block device, at size 0, when
    the card is pulled: the unit stayed bound to a device that never went,
    and an `unknown` card's notice outlived the card until the reader itself
    was unplugged."""
    reader = _Reader(automount)
    reader.event(size="0")                                  # the reader plugged in, its slot empty
    assert not reader.active and not automount.unread.exists()
    reader.event()                                          # a card nothing can be told of
    assert automount.unread.read_text().splitlines()[1] == "unknown"
    reader.event()                                          # another event of the same card: nothing new
    reader.event(size="0")                                  # the card pulled, the reader stays
    assert not automount.unread.exists() and not reader.active
    reader.event()                                          # and put back
    assert automount.unread.exists()
    reader.event(action="remove")                           # the reader itself unplugged
    assert not automount.unread.exists()


def test_a_card_after_another_in_the_same_reader_gets_its_own_start(automount):
    """A partitioned card's whole device starts the unit, which finds
    partitions and has nothing to do — and stays active. A card with its
    filesystem on the whole device, put in after it, got no start: the unit
    for that name was already running. So did a second such card after a
    first, whose mount stayed over a medium that was gone."""
    reader = _Reader(automount)
    (automount.stub / "partitions").write_text("loop0p1\n")
    reader.event()                                          # a partitioned card: the whole device holds no volume
    assert reader.active and automount.calls() == [] and not automount.unread.exists()
    reader.event(size="0")                                  # pulled
    (automount.stub / "partitions").unlink()
    reader.event(usage="filesystem", TYPE="vfat", UUID="4A21-0000", LABEL="Card")        # a whole-device card
    (call,) = automount.calls()
    assert call.endswith(f"/dev/loop0 {automount.base / '4A21-0000'}")
    reader.event(size="0")                                  # pulled: its mount and its label go with it
    assert automount.calls("umount") == [f"-l {automount.base / '4A21-0000'}"]
    assert not (automount.base / ".names" / "4A21-0000").exists() and not (automount.state / "loop0").exists()
    (automount.stub / "mounted-points").unlink()
    reader.event(usage="filesystem", TYPE="exfat", UUID="0398-3084", LABEL="Other")      # another card
    assert automount.calls()[-1].endswith(f"/dev/loop0 {automount.base / '0398-3084'}") and len(automount.calls()) == 2


def _nothing_named(automount, signatures=(), **stub):
    """A plugged volume blkid names no type on, wipefs listing `signatures`;
    the stub's other files as given."""
    (automount.stub / "signatures").write_text("".join(f"{s}\n" for s in signatures))
    for name, text in stub.items():
        (automount.stub / name.replace("_", "-")).write_text(str(text) + "\n")
    return automount.plug(TYPE="", UUID="", LABEL="")


# A quick NTFS format over an ext2 stick, as wipefs lists it (PI-NODE-2E §10.9):
# the old superblock, the new boot sector, and that sector's 0x55AA, which
# libblkid reads as a DOS partition table
TWO_MARKS = ("ext2", "ntfs", "dos")


@pytest.mark.parametrize("signatures, probe, said, why", [
    (TWO_MARKS, 2, "ambivalent", "it carries the marks of more than one filesystem"),       # util-linux 2.41
    (TWO_MARKS, 8, "ambivalent", "it carries the marks of more than one filesystem"),       # before it
    (("vfat", "vfat", "vfat", "ext2"), 2, "ambivalent", "it carries the marks of more than one filesystem"),
    ((), 2, "unknown", "no filesystem is found on it"),
    ((), 8, "unknown", "no filesystem is found on it"),
    (("dos",), 8, "unknown", "no filesystem is found on it"),            # a partition table's mark alone
    (("PMBR", "gpt"), 8, "unknown", "no filesystem is found on it")])
def test_a_volume_with_no_one_filesystem_is_said_to_the_node(automount, signatures, probe, said, why):
    """wipefs's list tells the two apart: two filesystems or more (a quick
    format over an older one) — a filesystem it finds at several offsets is
    one, a partition table's own signature none — or no filesystem at all.
    `blkid -p`'s exit, which told them apart until PI-NODE-2E, says 2 for
    both on util-linux 2.41 (F24). Either way the file keeps its four lines;
    there is no label to give and no driver was tried (F18)."""
    if not BOOT_ID.exists():
        pytest.skip("no boot id on this system")
    result = _nothing_named(automount, signatures, probe=probe)
    assert result.returncode == 0 and f"loop0 ({said}) is not mounted: {why}" in result.stderr
    assert automount.unread.read_text() == f"{BOOT_ID.read_text().strip()}\n{said}\n\n-\n"
    assert automount.calls() == [] and not (automount.state / "loop0").exists()
    assert automount.plug("unmount").returncode == 0 and not automount.unread.exists()      # gone at the pull


@pytest.mark.parametrize("signatures", [("ntfs", "dos"), ("vfat", "vfat", "vfat"), ("iso9660",)])
def test_one_filesystem_blkid_did_not_name_is_the_journals_alone(automount, signatures):
    """A signature the full probe finds and plain blkid did not: a volume
    with one filesystem is no "disk Sautium cannot tell" — its boot sector's
    `dos` mark is not a second one."""
    result = _nothing_named(automount, signatures, probe=8)
    assert result.returncode == 0 and f"loop0: no filesystem named, and wipefs finds {signatures[0]}" in result.stderr
    assert not (automount.base / ".unread").exists() and automount.calls() == []


def test_what_is_no_volume_of_files_is_never_said(automount):
    def silent(name="loop0", **stub):
        for leftover in ("probe", "size", "pkname", "siblings", "table", "partitions", "signatures",
                         "wipefs-fails"):
            (automount.stub / leftover).unlink(missing_ok=True)
        for key, text in stub.items():
            (automount.stub / key).write_text(str(text) + "\n")
        result = automount.plug(name=name, TYPE="", UUID="", LABEL="")
        assert result.returncode == 0, result.stderr
        return not (automount.base / ".unread").exists() and automount.calls() == []
    assert not silent()                                           # the case itself: said
    shutil.rmtree(automount.base / ".unread")
    assert silent(size=0) and silent(size=1024) and silent(size=1048575)       # an empty slot; an extended
    assert not silent(size=1048576)                                             # partition's container
    shutil.rmtree(automount.base / ".unread")
    assert silent(size="")                                         # no size to read: not a volume
    assert silent(partitions="loop0p1")                            # a whole disk that holds partitions
    assert silent(signatures="ntfs")                               # what plain blkid did not name
    assert silent(**{"wipefs-fails": ""})                          # nothing listed: nothing to say it by
    # an eMMC's own areas never were volumes of files (the names are the kernel's)
    script = automount.files["script"].read_text()
    assert 'case "$name" in mmcblk*boot*|mmcblk*rpmb) exit 0 ;; esac' in script
    import fnmatch
    for name, own in (("mmcblk0boot0", True), ("mmcblk0boot1", True), ("mmcblk0rpmb", True), ("mmcblk0p1", False),
                      ("mmcblk1", False), ("sda1", False)):
        assert any(fnmatch.fnmatchcase(name, p) for p in ("mmcblk*boot*", "mmcblk*rpmb")) is own, name


MSR, BASIC_DATA, EFI_SYSTEM = ("e3c9e316-0b5c-4db8-817d-f92df00215ae", "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7",
                               "c12a7328-f81f-11d2-ba4b-00a0c93ec93b")


def test_a_disk_windows_partitioned_is_one_mount_and_no_unread_row(automount):
    """A GPT disk as Windows makes one: a "Microsoft reserved" partition with
    no filesystem beside the NTFS one. Every plug of such a disk would have
    said "a disk Sautium cannot tell" of a partition nobody put music on
    (PI-NODE-2D §14, Q10)."""
    automount.partition(1, table=[(1, MSR), (2, BASIC_DATA)])
    assert automount.plug(TYPE="", UUID="", LABEL="").returncode == 0           # the reserved partition
    assert automount.calls() == [] and not (automount.base / ".unread").exists()
    assert automount.calls("partx") == ["-g -o TYPE -n 1 /dev/sda"]             # its type from the table
    automount.partition(2, table=[(1, MSR), (2, BASIC_DATA)])
    automount.ntfs_3g()
    assert automount.plug(TYPE="ntfs", UUID="01DC5A7E6B2F4C10", LABEL="Music").returncode == 0
    (call,) = automount.calls()                                                   # one mount
    assert call.startswith("-t ntfs-3g -o ro,nosuid,nodev,noexec") and not (automount.base / ".unread").exists()


@pytest.mark.parametrize("parttype, said", [
    (MSR, False), (EFI_SYSTEM, False), ("21686148-6449-6e6f-744e-656564454649", False),        # BIOS boot
    ("426f6f74-0000-11aa-aa11-00306543ecac", False), ("52637672-7900-11aa-aa11-00306543ecac", False),  # Apple's
    ("0x5", False), ("0xef", False), ("0x27", False), ("", False), ("a type; reboot", False),  # 0x27: recovery
    (BASIC_DATA, True), (BASIC_DATA.upper(), True), ("0fc63daf-8483-4772-8e79-3d69d8477de4", True),
    ("48465300-0000-11aa-aa11-00306543ecac", True), ("0x7", True), ("0xc", True), ("0x83", True)])
def test_beside_other_partitions_only_one_for_data_is_said_as_unknown(automount, parttype, said):
    """The type is the partition table's, read by partx: udev exports no
    PARTTYPE for a partition its probe found no filesystem on — exactly the
    partition asked about here — so the lsblk column it was read from was
    empty on a real disk, and an NTFS partition quick-formatted over another
    was silent (F24; the fake lsblk answers that column empty)."""
    automount.partition(2, table=[(1, "0xc"), (2, parttype)])
    assert _nothing_named(automount).returncode == 0
    assert automount.unread.exists() is said
    assert automount.calls("partx") == ["-g -o TYPE -n 2 /dev/sda"]
    if said:
        assert automount.unread.read_text().splitlines()[1:] == ["unknown", "", "-"]


@pytest.mark.parametrize("number", ["", "2; reboot", "../1", "-n 1", "x"])
def test_a_partition_number_that_is_no_number_is_never_handed_to_partx(automount, number):
    automount.partition(number, table=[(1, BASIC_DATA), (2, BASIC_DATA)])
    assert _nothing_named(automount).returncode == 0
    assert automount.calls("partx") == [] and not (automount.base / ".unread").exists()


def test_a_disks_only_volume_is_said_whatever_its_partition_type(automount):
    """A blank stick with its one partition, and an unpartitioned one: the
    person plugged a disk in and is owed why it shows nothing."""
    automount.partition(1, siblings=("sda1",), table=[(1, MSR)])
    assert _nothing_named(automount).returncode == 0
    assert automount.unread.read_text().splitlines()[1] == "unknown"
    assert automount.calls("partx") == []                                          # no type to ask
    automount.plug("unmount")
    (automount.stub / "pkname").write_text("")                                    # a whole disk, no table
    (automount.stub / "siblings").unlink()
    assert _nothing_named(automount).returncode == 0
    assert automount.unread.read_text().splitlines()[1] == "unknown"


def test_the_marks_of_two_filesystems_are_said_wherever_they_are_met(automount):
    automount.partition(1, table=[(1, MSR), (2, BASIC_DATA)])
    assert _nothing_named(automount, TWO_MARKS).returncode == 0
    assert automount.unread.read_text().splitlines()[1] == "ambivalent"


def _image(tmp_path, first, second):
    """PI-NODE-2D §5.2's recipe, on a plain file with no root: a filesystem,
    its superblock's block saved, a second format over it — and the block,
    which the caller puts back: what a quick format over an older filesystem
    leaves."""
    image = tmp_path / "stick.img"
    with open(image, "wb") as f:
        f.truncate(64 * 1024 * 1024)
    subprocess.run([first, "-q", "-F", "-L", "ALIEN", str(image)], check=True, capture_output=True)
    with open(image, "rb") as f:
        f.seek(1024)
        superblock = f.read(1024)
    subprocess.run(second + [str(image)], check=True, capture_output=True)
    return image, superblock


@pytest.mark.parametrize("over, second", [("ntfs", ["mkntfs", "-Q", "-F", "-L", "16Gb"]),
                                          ("vfat", ["mkfs.vfat", "-F", "32", "-n", "STICK"])],
                         ids=["ntfs over ext2", "fat over ext2"])
def test_the_real_wipefs_tells_two_signatures_from_none(automount, tmp_path, over, second):
    """The script's branch rests on what the system's own wipefs lists: run
    here on a real two-signature image and on a blank one, through the
    script (the stub hands the question to the real wipefs over the image)."""
    tools = {name: shutil.which(name, path=os.environ["PATH"] + ":/usr/sbin:/sbin")
             for name in ("blkid", "wipefs", "mkfs.ext2", second[0])}
    if not all(tools.values()) or not BOOT_ID.exists():
        pytest.skip("no " + ", ".join(name for name, path in tools.items() if not path) + " on this system")
    image, superblock = _image(tmp_path, tools["mkfs.ext2"], [tools[second[0]]] + second[1:])
    with open(image, "r+b") as f:
        f.seek(1024)
        f.write(superblock)
    listed = subprocess.run([tools["wipefs"], "--no-act", "--noheadings", "--parsable", "--output", "TYPE",
                             str(image)], capture_output=True, text=True, check=True).stdout.split()
    assert {"ext2", over} <= set(listed)                           # each at one offset or several
    plain = subprocess.run([tools["blkid"], "-c", "/dev/null", "-o", "value", "-s", "TYPE", str(image)],
                           capture_output=True)
    assert plain.returncode == 2 and plain.stdout == b""           # what the script's first question gets
    (automount.stub / "real-wipefs").write_text(tools["wipefs"])
    shutil.copy(image, automount.stub / "image")
    assert automount.plug(TYPE="", UUID="", LABEL="").returncode == 0
    assert automount.unread.read_text().splitlines()[1:] == ["ambivalent", "", "-"]
    automount.plug("unmount")
    with open(automount.stub / "image", "wb") as f:                # a blank volume
        f.truncate(64 * 1024 * 1024)
    assert automount.plug(TYPE="", UUID="", LABEL="").returncode == 0
    assert automount.unread.read_text().splitlines()[1:] == ["unknown", "", "-"]


def test_a_second_volume_claiming_a_mounted_uuid_gets_another_name(automount):
    (automount.stub / "mounted-points").write_text(f"{automount.base / '4A21-0000'}\n")
    assert automount.plug(TYPE="vfat", UUID="4A21-0000").returncode == 0
    assert automount.calls()[0].endswith(f"{automount.base / '4A21-0000-loop0'}")


def test_an_exfat_driver_that_refuses_the_time_option_still_mounts_and_says_so(automount):
    (automount.stub / "mount-fails").write_text("")
    result = automount.plug(TYPE="exfat", UUID="4A21-0000")
    assert result.returncode == 0 and "mounted without time_offset" in result.stderr
    assert len(automount.calls()) == 2 and "time_offset" not in automount.calls()[1]
    assert not automount.unread.exists()


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


def test_a_board_too_small_to_build_the_image_is_told_before_anything_is_asked_of_it(checkout, monkeypatch):
    """The image's Python takes 1.6 GB to link: a 2 GB board would spend an
    hour on a build that fails, twice. The run says so before it asks the
    person for any root step, and an image already built for this commit and
    these choices is no build at all."""
    monkeypatch.setattr(setup.agent, "head", lambda root: "c" * 40)
    monkeypatch.setattr(setup.agent, "image_of", lambda root: "sautium-backend:local")
    monkeypatch.setattr(setup.machine_tier, "machine_memory_kib", lambda: int(1.8 * 1024 ** 2))
    monkeypatch.setattr(setup, "run", lambda args, **kw: subprocess.CompletedProcess(args, 0, b"sha256:abc\n", b""))
    env = setup.image_choice({}, preset="nano", asked_preset=None, assistant="none")
    assert env == {"SAUTIUM_IMAGE_MODELS": "0", "SAUTIUM_IMAGE_STREAMING": "0", "SAUTIUM_IMAGE_ASSISTANT_CLI": "0"}
    refusal = setup.build_refusal({}, env)
    assert "1.8 GB of memory" in refusal and "needs 2.5 GB" in refusal and "cannot be set up on it yet" in refusal
    assert setup.build_refusal({"built": setup.image_key(env)}, env) is None
    other = {**env, "SAUTIUM_IMAGE_ASSISTANT_CLI": "1"}                 # another image is another build
    assert setup.build_refusal({"built": setup.image_key(env)}, other) is not None
    # An image the host agent's update built for this commit, from these choices, is built too
    monkeypatch.setattr(setup.agent, "image_of", lambda root: "sautium-backend:v0.4.0-cccccccccccc")
    (checkout / ".env").write_text("".join(f"{k}={v}\n" for k, v in env.items()))
    assert setup.build_refusal({}, env) is None
    assert setup.build_refusal({}, other) is not None
    monkeypatch.setattr(setup.agent, "image_of", lambda root: "sautium-backend:v0.3.0-bbbbbbbbbbbb")
    assert setup.build_refusal({}, env) is not None
    monkeypatch.setattr(setup.machine_tier, "machine_memory_kib", lambda: int(2.8 * 1024 ** 2))
    assert setup.build_refusal({}, env) is None
    assert setup.machine_tier.image_build_refusal(None) is None
    # The run asks it before the root steps
    source = Path(setup.__file__).read_text(encoding="utf-8")
    assert source.index("refusal = build_refusal(state, ") < source.index("    root_steps(args, state, ")


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
    """Of both shapes: the line at the automount's point, with the label the
    block wrote there, and the line of before 2026-11-24 at /mnt/sautium-<UUID>."""
    fstab = (b"/\n/boot/firmware\n/mnt/sautium-4A21-0000\n/mnt/sautium-x; reboot\n/mnt/music\n"
             b"/media/sautium/0398-3084\n/media/sautium\n/media/sautium/0398-3084/Music\n/media/other/0398-3084\n")
    monkeypatch.setattr(setup, "run", lambda args, **kw: installed.asked.append(args) or subprocess.CompletedProcess(
        args, 0, fstab if args[:2] == ["findmnt", "--fstab"] else b"", b""))
    assert setup.boot_mounts(fstab.decode()) == ["/mnt/sautium-4A21-0000", "/media/sautium/0398-3084"]
    assert setup.main(["remove"]) == 0
    pasted = [line[4:] for line in capsys.readouterr().out.splitlines() if line.startswith("    ")]
    sed = 'sudo sed -i "\\|^UUID=[^ ]* $P |d" /etc/fstab'
    lines = [
        "P=/mnt/sautium-4A21-0000", 'sudo umount "$P"', sed, 'sudo rmdir "$P"', "sudo systemctl daemon-reload",
        "P=/media/sautium/0398-3084", 'sudo umount "$P"', sed, 'sudo rmdir "$P"',
        "sudo rm -f /media/sautium/.names/0398-3084", "sudo systemctl daemon-reload"]
    assert [line for line in pasted if not line.startswith("sudo rmdir /")][-11:] == lines
    assert not any("reboot" in line or "/mnt/music" in line or "other" in line for line in pasted)
    # … and, last, the folder disks were mounted in — which the line's own block made where no
    # automount had: by rmdir, which takes nothing that holds anything
    base = installed.root.parent / "media" / "sautium"
    (base / ".names").mkdir(parents=True)
    monkeypatch.setattr(setup, "AUTOMOUNT_BASE", str(base))
    assert setup.main(["remove"]) == 0
    pasted = [line[4:] for line in capsys.readouterr().out.splitlines() if line.startswith("    ")]
    assert pasted[-1] == f"sudo rmdir {base}/.names {base}" and "rm -r" not in " ".join(pasted)
    (base / ".unread").mkdir()                          # the automount's own, where it was installed
    assert setup.main(["remove"]) == 0
    assert f"    sudo rmdir {base}/.names {base}/.unread {base}\n" in capsys.readouterr().out


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
    assert f"Music root {installed.root.parent}: there" in said
    assert "Tailscale: this machine is 100.64.0.10" in said and "Funnel is on" not in said
    assert sorted(p.name for p in (installed.root / "host").iterdir()) == before
    asked = [" ".join(a) for a in installed.asked]
    assert not any(word in line for line in asked for word in (" up", " down", " stop", " rm", "sudo")), asked


@pytest.mark.parametrize("fstype, uuid", [("ext4", "02f61d5e-92cb-4f9c-a781-099d97795020"), ("exfat", "4A21-0000"),
                                          ("vfat", "0398-3084"), ("ntfs", "01DC5A7E6B2F4C10"), ("ntfs3", "01DC5A7E6B2F4C10")])
def test_the_boot_mount_lines_are_short_and_build_the_very_line(fstype, uuid):
    """Every filesystem's line, with the longest UUID there is (ext4's 36
    characters, twice in the line): no piece and no undo line long enough to
    be wrapped, and the pieces put together are the line byte for byte."""
    found = setup.boot_mount(f"/media/pi/Disk/Music", json.dumps({"filesystems": [
        {"source": "/dev/sda1", "target": "/media/pi/Disk", "fstype": fstype, "uuid": uuid,
         "label": "W" * 32}]}), 1000, 1000)               # the longest label a filesystem here carries (NTFS)
    pieces = setup.in_pieces(found["line"])
    lines = setup.line_steps(found) + setup.boot_mount_undo(found["point"])
    assert max(len(line) for line in lines) <= 76, lines
    assert [line for line in setup.line_steps(found) if line.startswith("L=")] == pieces
    assert found["point"] == f"/media/sautium/{uuid}"
    built = subprocess.run(["sh", "-c", "\n".join(pieces) + '\nprintf %s "$L"'], capture_output=True, text=True).stdout
    assert built == found["line"]
    # The undo's sed address, as the shell hands it to sed: that one line of fstab and no other
    script = "\n".join(setup.boot_mount_undo(found["point"])[:1]) + '\nprintf %s "\\|^UUID=[^ ]* $P |d"'
    address = subprocess.run(["sh", "-c", script], capture_output=True, text=True).stdout
    assert address == f"\\|^UUID=[^ ]* {found['point']} |d"
    # The unit's name in the plug option holds backslashes, and is longer than a line: cut inside,
    # single-quoted, it arrives as itself
    assert "\\x2d" in found["line"] and all(piece.endswith("'") for piece in pieces)
    with pytest.raises(ValueError):
        setup.in_pieces("UUID=x /mnt/y ext4 ro,it's 0 0")


GENERATOR = "/lib/systemd/system-generators/systemd-fstab-generator"


@pytest.mark.skipif(not os.access(GENERATOR, os.X_OK), reason="no systemd fstab generator on this system")
@pytest.mark.skipif(os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"),
                    reason="inside a container systemd's generator leaves every device line out")
@pytest.mark.parametrize("uuid", ["4A21-0000", "02f61d5e-92cb-4f9c-a781-099d97795020"])
def test_systemds_own_generator_makes_the_disks_device_want_the_mount(tmp_path, uuid):
    """The line as the paste writes it, read by systemd's fstab generator
    with no root (SYSTEMD_FSTAB): the mount is wanted by the disk's device
    unit — started whenever the disk appears — and no longer by
    local-fs.target, so a start without the disk waits for nothing. Since
    systemd 242 nothing else starts an fstab mount at a later plug
    (PI-NODE-2C §3; PI-NODE-2B §8.10 found the line acting at boot only)."""
    found = setup.boot_mount("/media/pi/Disk", json.dumps({"filesystems": [
        {"source": "/dev/sda1", "target": "/media/pi/Disk", "fstype": "ext4", "uuid": uuid}]}), 1000, 1000)
    fstab = tmp_path / "fstab"
    paste = "\n".join(setup.in_pieces(found["line"])) + "\nprintf '\\n%s\\n' \"$L\""
    fstab.write_text("# the system's own lines\n" + subprocess.run(
        ["sh", "-c", paste], capture_output=True, text=True).stdout)
    out = tmp_path / "generated"
    out.mkdir()
    done = subprocess.run([GENERATOR, str(out), str(out), str(out)], capture_output=True, text=True,
                          env={"SYSTEMD_FSTAB": str(fstab), "PATH": os.environ["PATH"]})
    assert done.returncode == 0, done.stderr
    mount = setup.unit_name(found["point"], "mount")
    assert mount.startswith("media-sautium-") and mount.endswith(".mount")
    device = setup.unit_name(f"/dev/disk/by-uuid/{uuid}", "device")
    assert (out / mount).is_file() and f"What=/dev/disk/by-uuid/{uuid}" in (out / mount).read_text()
    assert (out / f"{device}.wants" / mount).is_symlink()
    assert not (out / "local-fs.target.wants" / mount).exists() and not (out / "local-fs.target.requires" / mount).exists()
    # … which is where the setup itself looks, after the paste, with no root
    said = _boot_mount_said(fstab, out)
    assert said == f"Music disk at {found['point']}: mounted whenever it is plugged in.\n"
    escape = shutil.which("systemd-escape")
    if escape:                                         # the names are systemd's own
        assert subprocess.run([escape, "-p", "--suffix=mount", found["point"]], capture_output=True,
                              text=True).stdout.strip() == mount


def _boot_mount_said(fstab, generated, systemd=257, roots=("/srv/music", "/media/sautium"), labels=None):
    """boot_mount_steps over an fstab and a generator folder of the test's
    own (findmnt reads the file by -F); nothing is mounted for the roots."""
    import contextlib
    import io
    real = setup.run

    def run(args, **kw):
        if args[:2] == ["findmnt", "--fstab"]:
            return real(args[:2] + ["-F", str(fstab)] + args[2:], **kw)
        if args[:2] == ["systemctl", "--version"]:
            return subprocess.CompletedProcess(args, 0, f"systemd {systemd} (x)\n".encode(), b"")
        if args[0] == "findmnt":                             # no root lies on a mount of its own
            return subprocess.CompletedProcess(args, 1, b"", b"")
        return real(args, **kw)
    setup.run = run
    try:
        with contextlib.redirect_stdout(io.StringIO()) as said:
            setup.boot_mount_steps(list(roots), labels or {}, generator_dir=str(generated))
    finally:
        setup.run = real
    return said.getvalue()


def _roots_said(fstab, roots):
    """say_roots over an fstab of the test's own; nothing is mounted, and
    the node gives no answer."""
    import contextlib
    import io
    real, saved = setup.run, (setup.held_roots, setup.held_labels, setup.held_by_the_node)

    def run(args, **kw):
        if args[:2] == ["findmnt", "--fstab"]:
            return real(args[:2] + ["-F", str(fstab)] + args[2:], **kw)
        if args[:2] == ["systemctl", "--version"]:
            return subprocess.CompletedProcess(args, 0, b"systemd 257 (x)\n", b"")
        return subprocess.CompletedProcess(args, 1, b"", b"")
    setup.run = run
    setup.held_roots, setup.held_labels, setup.held_by_the_node = (lambda o: list(roots)), (lambda o: {}), (lambda: None)
    try:
        with contextlib.redirect_stdout(io.StringIO()) as said:
            setup.say_roots()
    finally:
        setup.run = real
        setup.held_roots, setup.held_labels, setup.held_by_the_node = saved
    return said.getvalue().splitlines()


@pytest.mark.skipif(shutil.which("findmnt") is None, reason="no findmnt on this system")
def test_a_boot_mount_line_of_before_is_offered_its_replacement(tmp_path):
    """PI-NODE-2B's line acts at a start of the machine only. The setup that
    finds its own line without the plug option prints the removal of that
    line and the new one as one step; one whose systemd has not read the new
    line yet is told the one command; a systemd too old for the option keeps
    the line it has."""
    found = setup.boot_mount("/media/pi/Disk", json.dumps({"filesystems": [
        {"source": "/dev/sda1", "target": "/media/pi/Disk", "fstype": "exfat", "uuid": "4A21-0000"}]}), 1000, 1000)
    before = setup.boot_mount("/media/pi/Disk", json.dumps({"filesystems": [
        {"source": "/dev/sda1", "target": "/media/pi/Disk", "fstype": "exfat", "uuid": "4A21-0000"}]}),
        1000, 1000, systemd=241)["line"]
    fstab, generated = tmp_path / "fstab", tmp_path / "generated"
    fstab.write_text(f"PARTUUID=abc-02 / ext4 defaults,noatime 0 1\n{before}\n")
    said = _boot_mount_said(fstab, generated)
    pasted = [line[4:] for line in said.splitlines() if line.startswith("    ")]
    assert "whenever it is plugged in — at a start of the machine and later" in said
    assert pasted[:2] == ["P=/media/sautium/4A21-0000", 'sudo sed -i "\\|^UUID=[^ ]* $P |d" /etc/fstab']
    pieces = [line for line in pasted if line.startswith(("L='", 'L="$L"\''))]
    built = subprocess.run(["sh", "-c", "\n".join(pieces) + '\nprintf %s "$L"'], capture_output=True, text=True).stdout
    assert built == found["line"]                       # the old line's own options, the timeout gone
    assert "sudo systemctl daemon-reload" in pasted and max(len(line) for line in pasted) <= 76
    assert not any(line.startswith(("sudo umount", "sudo mount")) for line in pasted[:pasted.index("sudo systemctl daemon-reload")])
    # The new line pasted, systemd not told yet
    fstab.write_text(found["line"] + "\n")
    assert "systemd has not read it yet — run: sudo systemctl daemon-reload" in _boot_mount_said(fstab, generated)
    # A systemd that does not know the option: the old line is what it has
    fstab.write_text(before + "\n")
    assert _boot_mount_said(fstab, generated, systemd=241) == (
        "Music disk at /media/sautium/4A21-0000: mounted at a start of the machine only.\n")
    # No line of the setup's in fstab (undone, or mounted by other means): nothing said
    fstab.write_text("PARTUUID=abc-02 / ext4 defaults 0 1\n")
    assert _boot_mount_said(fstab, generated) == ""


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
    # a root with a space in its name: findmnt -r writes it \\x20
    assert setup.mounts_under(["/srv/My Music"], "/srv/My\\x20Music/disk ext4 ro\n/srv/My ext4 rw\n") == [
        "/srv/My\\x20Music/disk ext4 ro"]
    # … and a name outside ASCII written a byte at a time
    escaped = "".join(f"\\x{b:02x}" for b in "Музика".encode())
    assert setup.mounts_under(["/srv/Музика"], f"/srv/{escaped}/disk ext4 ro\n") == [f"/srv/{escaped}/disk ext4 ro"]
    assert setup.mounts_under(["/srv/Музика"], "/srv/Музика/disk ext4 ro\n") == ["/srv/Музика/disk ext4 ro"]


def test_a_root_is_there_or_empty_or_gone_and_says_how_to_forget_it(tmp_path, monkeypatch):
    """A root whose disk is not plugged in, or whose folder was removed, is a
    library that is away — never a node that does not start (PI-NODE-2C §6)."""
    there, empty, mounted, gone = (tmp_path / n for n in ("Music", "Empty", "Disk", "Gone"))
    (there / "Jazz").mkdir(parents=True)
    empty.mkdir(), mounted.mkdir()
    assert setup.root_state(str(there), []) == "there" and setup.root_state(str(empty), []) == "empty"
    assert setup.root_state(str(gone), []) == "gone"
    assert setup.root_state(str(mounted), [str(mounted)]) == "there"          # a disk with nothing on it yet
    monkeypatch.setattr(setup, "AUTOMOUNT_BASE", str(tmp_path / "plugged"))
    roots = [str(there), str(empty), str(mounted), str(gone), setup.AUTOMOUNT_BASE]
    table = f"/ ext4 rw\n{mounted} exfat ro,nosuid\n{tmp_path}/plugged/4A21-0000 vfat ro\n"
    lines = setup.root_lines(roots, table, {str(mounted): "Elements <b>", str(gone): "Old"})
    # each mount under the root it lies under (PI-NODE-2E H6)
    assert lines == [
        f"Music root {there}: there",
        f"Music root {empty}: empty — its disk is not mounted, or the folder was removed. Its albums show as not connected. "
        f"To forget it: ./sautium roots --forget {empty}",
        # a root that is a mount point holds what was mounted there when the node started (§2.5)
        f"Music root {mounted}: a disk mounted at the root itself — Sautium sees it only as it was when Sautium "
        "started",
        f"  mounted: {mounted} exfat ro,nosuid",
        f"Music root {gone}: gone — its disk is not mounted, or the folder was removed. Its albums show as not connected. "
        f"To forget it: ./sautium roots --forget {gone}",
        f"Music root {tmp_path}/plugged: plugged disks — the automount is not installed (./sautium shows how)",
        f"  mounted: {tmp_path}/plugged/4A21-0000 vfat ro"]
    (tmp_path / "plugged").mkdir()                   # installed, no disk plugged in: not a library gone
    assert f"Music root {tmp_path}/plugged: there  (plugged disks)" in setup.root_lines(roots, "", {})
    # An empty root the node never read a copy under is a new folder, not a library that is away
    # (PI-NODE-2C F2); one it holds copies from, and one the node said nothing of, may have lost its music
    new = setup.root_lines([str(empty)], "", {}, held={str(empty): False})
    assert new == [f"Music root {empty}: empty — nothing is in it yet"]
    for held in ({str(empty): True}, {}, None):
        assert "its disk is not mounted, or the folder was removed" in setup.root_lines(
            [str(empty)], "", {}, held=held)[0], held
    assert "is not mounted, or the folder was removed" in setup.root_lines(      # gone is never new
        [str(gone)], "", {}, held={str(gone): False})[0]
    # the node's answer is a stranger's: only a path with a true or a false is believed
    monkeypatch.setattr(setup, "signed_get", lambda path: {"places": [
        {"path": str(empty), "kind": "folder", "empty": True, "held": False},
        {"path": str(there), "kind": "folder", "empty": False, "held": None},
        {"path": None, "kind": "unread", "held": True}, "not a place", {"path": 7, "held": True}]})
    assert setup.held_by_the_node() == {str(empty): False}
    for answer in (None, [], {"places": "none"}, {"detail": "no"}):
        monkeypatch.setattr(setup, "signed_get", lambda path, a=answer: a)
        assert setup.held_by_the_node() is None
    # … and where the setup has a step for it, that is said
    assert setup.root_lines([str(mounted)], table, {}, [str(mounted)])[0].endswith(
        "when Sautium started (./sautium shows the step)")
    # a disk the line mounts under the automount's folder is listed under that root: where it is
    under = setup.root_lines([setup.AUTOMOUNT_BASE], table, {})
    assert under == [f"Music root {tmp_path}/plugged: there  (plugged disks)",
                     f"  mounted: {tmp_path}/plugged/4A21-0000 vfat ro"]


def test_an_install_whose_line_mounts_its_disk_at_the_root_itself_is_named_and_moved(tmp_path, monkeypatch, capsys):
    """Until 2026-11-24 the line mounted at /mnt/sautium-<UUID> and that
    point was the root. `roots` and `status` name it, with the disk in or
    out; ./sautium prints one block — the old line's undo and the line at the
    new point, with the label — and says what follows (PI-NODE-2D §2.4)."""
    old_point = "/mnt/sautium-4A21-0000"
    # the line as PI-NODE-2C wrote it, and as PI-NODE-2B did (a start of the machine only)
    at_plug = LINE_4A21.replace("/media/sautium/4A21-0000", old_point)
    at_boot = at_plug.split(",x-systemd.wanted-by=")[0] + ",x-systemd.device-timeout=10s 0 0"
    fstab, generated = tmp_path / "fstab", tmp_path / "generated"
    fstab.write_text(f"PARTUUID=abc-02 / ext4 defaults,noatime 0 1\n{at_plug}\n")
    roots = ["/srv/music", old_point, "/media/sautium"]

    def line_of(point):
        real = setup.run
        setup.run = lambda args, **kw: real(args[:2] + ["-F", str(fstab)] + args[2:], **kw)
        try:
            return setup.old_boot_mount(point)
        finally:
            setup.run = real
    if shutil.which("findmnt") is None:
        pytest.skip("no findmnt on this system")
    assert line_of(old_point) == {"uuid": "4A21-0000", "fstype": "exfat", "options": at_plug.split()[3]}
    assert line_of("/mnt/music") is None and line_of("/media/sautium/4A21-0000") is None
    # named whether its disk is in or out: the mount point is the root either way
    for table in (f"{old_point} exfat ro,nosuid\n", ""):
        assert setup.root_lines(roots, table, {old_point: "Elements"}, [old_point])[1] == (
            "Music root /mnt/sautium-4A21-0000: a disk mounted at the root itself — Sautium sees it only as it "
            "was when Sautium started (./sautium shows the step)")
    said = _boot_mount_said(fstab, generated, roots=roots, labels={old_point: "Elements"})
    assert said.count("Paste into a terminal that may use sudo:") == 1                 # one block
    assert ("The line of /etc/fstab that mounts the music disk at /mnt/sautium-4A21-0000 gives Sautium the\n"
            "mount point itself: a disk pulled or put back while Sautium runs is not seen. This moves it to\n"
            "/media/sautium/4A21-0000, where Sautium sees it come and go. Sautium finds its albums at the new\n"
            "place by itself; nothing is read or analysed again.\n") in said
    pasted = [line[4:] for line in said.splitlines() if line.startswith("    ")]
    undo = pasted.index("P=/media/sautium/4A21-0000")
    assert [line for line in pasted[:undo] if not line.startswith("L=")] == [
        "P=/mnt/sautium-4A21-0000", 'sudo umount "$P"', 'sudo sed -i "\\|^UUID=[^ ]* $P |d" /etc/fstab',
        'sudo rmdir "$P"', "B=/media/sautium", "U=4A21-0000", 'sudo install -d -m 0755 "$B" "$B/.names"',
        'sudo mkdir -p "$B/$U"', "T='Elements'", 'printf \'%s\\n\' "$T" | sudo tee "$B/.names/$U" >/dev/null',
        "printf '\\n%s\\n' \"$L\" | sudo tee -a /etc/fstab", "sudo systemctl daemon-reload", 'sudo mount "$B/$U"']
    pieces = [line for line in pasted[:undo] if line.startswith("L=")]
    built = subprocess.run(["sh", "-c", "\n".join(pieces) + '\nprintf %s "$L"'], capture_output=True, text=True).stdout
    assert built == LINE_4A21 and max(len(line) for line in pasted) <= 76
    # the chosen folder moves with its files at the node's next start (PI-NODE-2E §2): nothing to choose
    assert "Then run:  ./sautium roots --forget /mnt/sautium-4A21-0000\n\n" in said
    assert "as a music folder" not in said
    # a line of before the plug option gains it at its move; an older systemd keeps the line it has
    fstab.write_text(at_boot + "\n")
    said = _boot_mount_said(fstab, generated, roots=roots)
    pieces = [line[4:] for line in said.splitlines() if line.startswith(("    L='", '    L="$L"\''))]
    assert subprocess.run(["sh", "-c", "\n".join(pieces) + '\nprintf %s "$L"'], capture_output=True,
                          text=True).stdout == LINE_4A21
    assert "T=" not in said and "as a music folder" not in said     # no label known: none written
    said = _boot_mount_said(fstab, generated, systemd=241, roots=roots)
    assert "── The music disk, mounted when the machine starts (optional) ──" in said
    assert " /media/sautium/4A21-0000 exfat " in "".join(
        line[4:].split("'")[1] for line in said.splitlines() if line.startswith(("    L='", '    L="$L"\'')))
    assert "device-timeout=10s" in said.replace("'\n    L=\"$L\"'", "")
    # the root forgotten, its line left in fstab: its disk never appears in Sautium. Said, with the move
    # block and no forget after it — the root is gone already, and the node takes the files along (H10)
    said = _boot_mount_said(fstab, generated, roots=["/srv/music", "/media/sautium"])
    assert ("A line of /etc/fstab mounts the disk 4A21-0000 at /mnt/sautium-4A21-0000, where Sautium does not\n"
            "look: it never appears in Sautium. The block below mounts it where Sautium sees it.\n"
            "Sautium finds its albums at the new place when it starts again; nothing is read or analysed again.\n"
            ) in said
    assert said.count("Paste into a terminal that may use sudo:") == 1 and "--forget" not in said
    pasted = [line[4:] for line in said.splitlines() if line.startswith("    ")]
    assert pasted[:2] == ["P=/mnt/sautium-4A21-0000", 'sudo umount "$P"'] and "U=4A21-0000" in pasted
    # … and `status` and `roots` say it, where the setup has the step
    assert _roots_said(fstab, ["/srv/music", "/media/sautium"])[-1] == (
        "A line of /etc/fstab mounts the disk 4A21-0000 at /mnt/sautium-4A21-0000, where Sautium does not look: "
        "it never appears in Sautium (./sautium shows the step)")
    assert not any("A line of /etc/fstab" in line for line in _roots_said(fstab, roots))      # a root names it
    # … and where the disk's line at the new point is in already, the old line alone is taken out:
    # no second line for the disk (review round 1, L2)
    fstab.write_text(at_boot + "\n" + LINE_4A21 + "\n")
    said = _boot_mount_said(fstab, generated, roots=["/srv/music", "/media/sautium"])
    assert ("A line of /etc/fstab still mounts the disk 4A21-0000 at /mnt/sautium-4A21-0000 as well as at\n"
            "/media/sautium/4A21-0000, where Sautium sees it: the block takes the old line out.") in said
    before, undo = said.split("To undo it later:")
    assert "tee -a /etc/fstab" not in before and max(len(line[4:]) for line in undo.splitlines()) <= 76
    fstab.write_text(at_boot + "\n")
    # a line at that point the setup did not write is the person's own: no step — one that names the
    # disk otherwise, and one that would mount it for writing or run what is on it (the node's bind
    # does not make a mount that arrives later read-only)
    for theirs in ("/dev/sda1 /mnt/sautium-4A21-0000 exfat ro,nosuid,nodev,noexec 0 0",
                   at_plug.replace("ro,nofail,", "rw,nofail,"), at_plug.replace(",noexec", ""),
                   at_plug.replace(",nosuid,nodev", ""),
                   # … or that says the four words and undoes one further on (mount reads left to right)
                   at_plug.replace(",x-systemd.before=", ",rw,x-systemd.before="),
                   at_plug.replace(",x-systemd.before=", ",defaults,x-systemd.before="),
                   at_plug.replace(",x-systemd.before=", ",exec,x-systemd.before=")):
        fstab.write_text(theirs + "\n")
        assert line_of(old_point) is None and _boot_mount_said(fstab, generated, roots=roots) == "", theirs
    assert "a disk mounted at the root itself" not in setup.root_lines(roots, "", {}, [])[1]     # not a step of ours


def test_a_roots_label_rides_in_the_override_for_the_node_to_show(checkout, tmp_path, monkeypatch):
    """A root a line of /etc/fstab mounts was listed as a plain folder with
    no name: a container cannot ask the disk. The setup can (findmnt, no
    root), and says it in the node's environment — a stranger's text, cut,
    and quoted so that neither YAML nor compose reads a letter of it as its
    own (PI-NODE-2C §13)."""
    def findmnt(target, label):
        return lambda args, **kw: subprocess.CompletedProcess(args, 0, json.dumps(
            {"filesystems": [{"target": target, "label": label}]}).encode(), b"")
    monkeypatch.setattr(setup, "run", findmnt("/mnt/sautium-4A21-0000", "Elements"))
    assert setup.root_label("/mnt/sautium-4A21-0000") == "Elements"
    assert setup.root_label("/mnt/sautium-4A21-0000/Music") == ""       # a folder ON a disk is not the disk
    monkeypatch.setattr(setup, "run", findmnt("/", "rootfs"))
    assert setup.root_label("/srv/music") == ""                         # a plain folder: the system disk's is not its
    monkeypatch.setattr(setup, "run", findmnt("/mnt/x", None))
    assert setup.root_label("/mnt/x") == ""
    monkeypatch.setattr(setup, "run", lambda args, **kw: subprocess.CompletedProcess(args, 1, b"", b""))
    assert setup.root_label("/mnt/x") == ""
    assert setup.cut_label("Му" * 100) == "Му" * 30 and len(setup.cut_label("x" * 500)) == 120
    assert setup.cut_label("a\x1b[31m\nb\x00") == "a[31mb"

    hostile = 'My "disk" ${HOME} $(reboot): \\ # Му'
    roots = ["/mnt/sautium-4A21-0000", "/srv/music", setup.AUTOMOUNT_BASE]
    text = setup.roots_override(roots, {"/mnt/sautium-4A21-0000": hostile, "/srv/music": "", "/not/a/root": "x"})
    yaml = pytest.importorskip("yaml")
    environment = yaml.safe_load(text)["services"]["backend"]["environment"]
    # compose reads `$$` as the letter itself; the node then reads a JSON object
    labels = json.loads(environment["SAUTIUM_LIBRARY_ROOT_LABELS"].replace("$$", "$"))
    assert labels == {"/mnt/sautium-4A21-0000": hostile}
    assert "$" not in environment["SAUTIUM_LIBRARY_ROOT_LABELS"].replace("$$", "")
    assert "SAUTIUM_LIBRARY_ROOT_LABELS" not in setup.roots_override(roots, {})
    # The file is the record: a label read at an earlier run stays for a disk that is out at this one
    override = checkout / "host" / "compose.roots.yml"
    override.write_text(text)
    assert setup.held_roots(override) == roots and setup.held_labels(override) == {"/mnt/sautium-4A21-0000": hostile}
    monkeypatch.setattr(setup, "root_label", lambda root: "New name" if root == "/srv/music" else "")
    setup.write_roots(roots)
    assert setup.held_labels(override) == {"/mnt/sautium-4A21-0000": hostile, "/srv/music": "New name"}
    setup.write_roots(["/srv/music", setup.AUTOMOUNT_BASE])               # a root forgotten takes its label along
    assert setup.held_labels(override) == {"/srv/music": "New name"}


def test_roots_lists_them_and_forgets_one_leaving_the_folder(installed, tmp_path, monkeypatch, capsys):
    music, gone = tmp_path / "Music", tmp_path / "Gone"
    (music / "Jazz").mkdir(parents=True)
    override = installed.root / "host" / "compose.roots.yml"
    roots = [str(music), str(gone), "/mnt/sautium-4A21-0000", setup.AUTOMOUNT_BASE]
    override.write_text(setup.roots_override(roots, {str(gone): "Old disk"}))
    started = []
    monkeypatch.setattr(setup, "start", lambda: started.append("start"))
    monkeypatch.setattr(setup, "node_running", lambda: True)
    monkeypatch.setattr(setup, "root_label", lambda root: "")
    assert setup.main(["roots"]) == 0
    said = capsys.readouterr().out
    assert f"Music root {music}: there" in said and f"Music root {gone}: gone — " in said
    assert f"./sautium roots --forget {gone}" in said and started == []
    # What is not a root, and the automount's own folder: refused, nothing written
    before = override.read_bytes()
    for refused, why in ((str(tmp_path / "Other"), "is not one of this node's music roots"),
                         (setup.AUTOMOUNT_BASE, "it leaves with the automount itself")):
        assert setup.main(["roots", "--forget", refused]) == 1
        assert why in capsys.readouterr().out
    assert override.read_bytes() == before and started == []
    # Forgotten: the override as a setup that never had it would write it, the node made again
    assert setup.main(["roots", "--forget", str(gone) + "/"]) == 0
    said = capsys.readouterr().out
    assert f"Albums under {gone} will show as not connected in Sautium; nothing is removed." in said
    assert "Forget them, or add another folder, in More › Library." in said
    assert "The folder itself is untouched." in said
    left = [str(music), "/mnt/sautium-4A21-0000", setup.AUTOMOUNT_BASE]
    assert override.read_bytes() == setup.roots_override(left).encode() and started == ["start"]
    assert stat.S_IMODE(override.stat().st_mode) == 0o644
    assert (music / "Jazz").is_dir() and "sudo" not in said
    # A root the setup's own fstab line mounts: that line's undo is printed, for the person
    monkeypatch.setattr(setup, "fstab_line", lambda point: {"source": "UUID=4A21-0000", "fstype": "exfat", "options": "ro"})
    monkeypatch.setattr(setup, "node_running", lambda: False)
    # (the node re-points where its disks' folder holds the labels' folder: the automount's or the line's)
    monkeypatch.setattr(setup, "disks_folder_ready", lambda: True)
    assert setup.main(["roots", "--forget", "/mnt/sautium-4A21-0000"]) == 0
    said = capsys.readouterr().out
    assert "    P=/mnt/sautium-4A21-0000" in said and '    sudo sed -i "\\|^UUID=[^ ]* $P |d" /etc/fstab' in said
    assert "from its next start" in said and started == ["start"]          # a stopped node is not started
    # … and its disk's new line is in: the node finds the albums there at its start (PI-NODE-2E §2)
    assert ("Sautium finds the albums under /mnt/sautium-4A21-0000 at /media/sautium/4A21-0000 when it starts "
            "again; nothing is removed.") in said
    assert "will show as not connected" not in said and "add another folder" not in said
    # … and where its disk has the line at the new point and is not mounted there (the node that held
    # the old mount point held the disk too), that is said
    assert "Its disk is not mounted at /media/sautium/4A21-0000 now: plug it in again" in said
    assert setup.held_roots(override) == [str(music), setup.AUTOMOUNT_BASE]
    # The node's own condition, not the line's (review M2): with the automount alone — the old line
    # undone, no new line — the node re-points as well, and the setup says so; with no disks' folder
    # it does not, and the albums are away
    for ready, words in ((True, "Sautium finds the albums under /mnt/sautium-5B32-0000 at "
                                "/media/sautium/5B32-0000 when it starts again"),
                         (False, "Albums under /mnt/sautium-5B32-0000 will show as not connected")):
        override.write_text(setup.roots_override(["/mnt/sautium-5B32-0000", setup.AUTOMOUNT_BASE]))
        monkeypatch.setattr(setup, "fstab_line", lambda point: None)
        monkeypatch.setattr(setup, "disks_folder_ready", lambda: ready)
        assert setup.main(["roots", "--forget", "/mnt/sautium-5B32-0000"]) == 0
        assert words in capsys.readouterr().out
    # The empty folder Docker made where a root's folder was is root's: its rmdir is the person's
    made = tmp_path / "Made"
    made.mkdir()
    override.write_text(setup.roots_override([str(made), setup.AUTOMOUNT_BASE]))
    real_stat = os.stat

    def as_roots(path, *a, **kw):
        found = real_stat(path, *a, **kw)
        return os.stat_result(found[:4] + (0,) + found[5:10]) if str(path) == str(made) else found
    monkeypatch.setattr(setup.os, "stat", as_roots)
    assert setup.main(["roots", "--forget", str(made)]) == 0
    assert f"sudo rmdir {made}" in capsys.readouterr().out and made.is_dir()
    # An install that reads its music by its .env line has no roots
    (installed.root / ".env").write_text("MUSIC_LIBRARY_PATH=/srv/music\n")
    assert setup.main(["roots"]) == 1 and "it has no roots" in capsys.readouterr().out


def test_the_last_screen_says_what_a_nano_node_does(monkeypatch, capsys):
    """A node born Nano passes on what it holds, as every node (the owner's
    decision of 2026-11-22): the paragraph says so, and where the switch is."""
    monkeypatch.setattr(setup, "own_addresses", lambda: ["192.0.2.10"])
    monkeypatch.setattr(setup, "setup_code", lambda: None)
    monkeypatch.setattr(setup, "tailnet_address", lambda: None)
    m = {"ram_gib": 3.9, "cores": 4, "docker_root_medium": "sd_card", "docker_root_free_gb": 50.0, "swaps": []}
    setup.last_screen(m, "nano")
    said = " ".join(capsys.readouterr().out.split())
    assert ("This machine: 4 GB RAM, 4 cores, a microSD card → Nano. Nothing is analysed here; sound features "
            "arrive from other Sautium nodes. Your node asks other Sautium nodes about your artists and "
            "recordings and takes the sound features they hold, and answers them in turn — you can switch "
            "that off in Sautium (More › Sync & P2P).") in said
    assert "passes nothing on" not in said and "Swap" not in said
    # … and a swap file on the card is said there, with its step
    monkeypatch.setattr(setup, "RPI_SWAP_CONF", "/nonexistent/swap.conf")
    monkeypatch.setattr(setup.shutil, "which", lambda name, path=None: None)
    setup.last_screen(dict(m, swaps=[{"path": "/var/swap", "kind": "file", "medium": "sd_card"}]), "nano")
    assert "This machine's swap is a file on its card (/var/swap)." in capsys.readouterr().out
    assert "passes_on" not in SOURCE


def test_the_machine_line_says_an_asked_preset_as_asked():
    """The arrow is the machine's own verdict: Nano asked for on a board the
    rule reads as a full node is not said as read off it (item 8, W1)."""
    big = {"ram_gib": 15.8, "cores": 4, "docker_root_medium": "nvme", "docker_root_free_gb": 800.0}
    small = dict(big, ram_gib=3.9)
    assert setup.machine_line(big, None) == "16 GB RAM, 4 cores, an NVMe SSD → the full node"
    assert setup.machine_line(big, "nano").endswith("an NVMe SSD; set up as Nano")
    assert setup.machine_line(small, "nano").endswith("an NVMe SSD → Nano")
    assert setup.machine_line(small, None).endswith("an NVMe SSD; set up as the full node")
    # The medium is named as read: a card alone makes a board with memory to spare Nano
    card = dict(small, docker_root_medium="sd_card")
    assert setup.machine_line(card, "nano") == "4 GB RAM, 4 cores, a microSD card → Nano"
    assert setup.machine_line(dict(big, docker_root_medium="sd_card"), "nano").endswith("a microSD card → Nano")
    assert setup.machine_line(dict(big, docker_root_medium="unknown"), None).endswith("a disk → the full node")


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
