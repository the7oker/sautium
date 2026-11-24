"""Real mounts under a root bound `rslave`, while the node's readers run
(PI-NODE-2D §2.8; F16).

Docker gives a node each root as the host's folder bound at its own place in
a mount namespace of the container's, a slave of the host's. This file makes
that with the kernel alone — a second mount namespace, the "host's" folders
bound into it as slaves — and mounts and unmounts filesystems on the host's
side while the node's own code reads its side: the picker's listing
(library_root) and the mount table's wake (mount_watch._run's poll).

- A disk mounted under the folder disks are mounted in — the automount's
  point, which is the fstab line's point too since 2026-11-24 — comes and
  goes in the node's mount table, in every order, and each change wakes the
  watcher.
- A root that IS the mount point (the line of before, at /mnt/sautium-<UUID>):
  the namespace made while the disk was in holds the disk's filesystem. Its
  pull is not seen and what is mounted there afterwards never arrives. That
  is the fault the new point removes; it is shown here so that the reason no
  root may be a mount point stays on the page.

Needs the right to mount (a privileged container, root): skipped anywhere
else. Only tmpfs is mounted, under the test's own folder.
"""

import json
import os
import select
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BACKEND = REPO / "backend"

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux") or os.geteuid() != 0,
                                reason="mounting needs root on Linux")

# The node's side: a mount namespace of its own, the roots bound as Docker's
# override binds them, then the product's readers. One line out per wake of
# the mount table's poll (the disks the node sees then), one per question.
NODE = textwrap.dedent("""
    import ctypes, json, os, subprocess, sys, threading
    host, node, backend = sys.argv[1:4]
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.unshare(0x00020000):                    # CLONE_NEWNS
        raise OSError(ctypes.get_errno(), "unshare")
    def sh(*args):
        subprocess.run(args, check=True)
    # Nothing mounted here goes back to the host, and what the host mounts
    # under a bound folder arrives: `propagation: rslave`
    sh("mount", "--make-rslave", "/")
    for name in ("disks", "old-root"):
        sh("mount", "--rbind", f"{host}/{name}", f"{node}/{name}")
    sys.path.insert(0, backend)
    import library_root, mount_watch
    from config import settings
    settings.sautium_library_roots = f"{node}/disks:{node}/old-root"
    settings.sautium_library_root_labels = json.dumps({f"{node}/old-root": "Old line"})

    def disks():
        return sorted(path for path, _root, _name in library_root._disk_points(library_root._mounts()))
    mount_watch._changed = lambda: print(json.dumps({"woke": disks()}), flush=True)
    armed, watch = threading.Event(), mount_watch.watch
    def armed_watch(wait, changed, of="the mount table"):
        armed.set()
        watch(wait, changed, of)
    mount_watch.watch = armed_watch
    threading.Thread(target=mount_watch._run, daemon=True).start()
    armed.wait()
    print(json.dumps({"ready": disks()}), flush=True)
    for line in sys.stdin:
        asked = json.loads(line)
        print(json.dumps({"places": library_root.folders()["places"],
                          "pullable": library_root.pullable(asked.get("pullable")),
                          "files": {p: sorted(os.listdir(p)) for p in asked.get("list", [])}}), flush=True)
""")


def _sh(*args):
    subprocess.run(args, check=True, capture_output=True)


@pytest.fixture
def machine(tmp_path):
    """The host's side — a shared mount holding the folder disks are mounted
    in and an old-shape mount point with its disk in — and a node started
    over it."""
    host, node = tmp_path / "host", tmp_path / "node"
    host.mkdir()
    try:
        _sh("mount", "-t", "tmpfs", "tmpfs", str(host))
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip("this process may not mount")
    mounted = [str(host)]
    proc = None

    def mount(point, *files, ro=False):
        Path(point).mkdir(exist_ok=True)
        _sh("mount", "-t", "tmpfs", "-o", "size=1m", "tmpfs", str(point))
        mounted.append(str(point))
        for name in files:
            (Path(point) / name).mkdir()
        if ro:
            _sh("mount", "-o", "remount,ro", str(point))

    def umount(point):
        _sh("umount", str(point))
        mounted.remove(str(point))

    pending = bytearray()

    def heard(key, seconds=20):
        """The node's next line with `key`: a wake's, or an answer's. Read
        off the descriptor itself: a line waiting in a reader's buffer is not
        one select() knows of."""
        while True:
            while b"\n" not in pending:
                if not select.select([proc.stdout], [], [], seconds)[0]:
                    pytest.fail(f"the node said no {key!r} in {seconds} s")
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    pytest.fail(f"the node ended: {proc.stderr.read().decode('utf-8', 'replace')[-2000:]}")
                pending.extend(chunk)
            line, _, rest = bytes(pending).partition(b"\n")
            pending[:] = rest
            said = json.loads(line)
            if key in said:
                return said if key == "places" else said[key]

    def woke_with(disks, seconds=20):
        """Wait for the wake at which the node sees exactly `disks`."""
        while True:
            if heard("woke", seconds) == sorted(str(d) for d in disks):
                return

    def ask(pullable=None, files=()):
        proc.stdin.write(json.dumps({"pullable": pullable and str(pullable),
                                     "list": [str(f) for f in files]}).encode() + b"\n")
        return heard("places")

    try:
        _sh("mount", "--make-shared", str(host))
        (host / "disks" / ".names").mkdir(parents=True)
        (host / "disks" / ".names" / "4A21-0000").write_text("Elements\n")
        mount(host / "old-root", "music of the first disk")
        for name in ("disks", "old-root"):
            (node / name).mkdir(parents=True)
        proc = subprocess.Popen([sys.executable, "-c", NODE, str(host), str(node), str(BACKEND)], bufsize=0,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        ready = heard("ready", 60)
        yield type("Machine", (), {"host": host, "node": node, "mount": staticmethod(mount), "ready": ready,
                                   "umount": staticmethod(umount), "woke_with": staticmethod(woke_with),
                                   "ask": staticmethod(ask)})
    finally:
        if proc is not None:
            proc.kill()
            proc.wait()
        for point in reversed(mounted):
            subprocess.run(["umount", "-l", point], capture_output=True)


def test_a_disk_under_the_disks_folder_comes_and_goes_in_a_node_that_runs(machine):
    disk, old = machine.node / "disks" / "4A21-0000", machine.node / "old-root"
    assert machine.ready == [str(old)]
    # plugged: mounted on the host under the bound folder — by the automount or by the line, the same point
    machine.mount(machine.host / "disks" / "4A21-0000", "Music", ro=True)
    machine.woke_with([disk, old])
    said = machine.ask(pullable=disk / "Music")
    row = next(p for p in said["places"] if p["path"] == str(disk))
    # listed as any disk the automount mounts: by the label beside it, read-only, safe to pull
    assert (row["kind"], row["name"], row["label"], row["read_only"]) == ("disk", "4A21-0000", "Elements", True)
    assert row["size_bytes"] > 0 and said["pullable"] is True
    # pulled
    machine.umount(machine.host / "disks" / "4A21-0000")
    machine.woke_with([old])
    assert str(disk) not in [p["path"] for p in machine.ask()["places"]]
    # and put back, in a node that never stopped: seen again, with what it holds now
    machine.mount(machine.host / "disks" / "4A21-0000", "Music", "New album", ro=True)
    machine.woke_with([disk, old])
    said = machine.ask(files=[disk])
    assert str(disk) in [p["path"] for p in said["places"]]
    assert said["files"][str(disk)] == ["Music", "New album"]
    # a second disk beside it: no root added, nothing started again
    machine.mount(machine.host / "disks" / "0398-3084", ro=True)
    machine.woke_with([machine.node / "disks" / "0398-3084", disk, old])


def test_a_root_that_is_the_mount_point_holds_the_disk_it_started_with(machine):
    """F16: the reason the setup's line no longer mounts at a root."""
    old = machine.node / "old-root"
    assert machine.ask(files=[old])["files"][str(old)] == ["music of the first disk"]
    # pulled on the host: the node's root still is that disk's filesystem
    machine.umount(machine.host / "old-root")
    machine.mount(machine.host / "disks" / "marker")            # a change the node does hear, to read after it
    machine.woke_with([machine.node / "disks" / "marker", old])
    said = machine.ask(files=[old])
    assert said["files"][str(old)] == ["music of the first disk"]
    assert [p["kind"] for p in said["places"] if p["path"] == str(old)] == ["disk"]
    # … and the disk put back — another filesystem at the host's mount point — never arrives
    machine.mount(machine.host / "old-root", "music of the disk put back")
    machine.mount(machine.host / "disks" / "marker2")
    machine.woke_with([machine.node / "disks" / "marker", machine.node / "disks" / "marker2", old])
    assert machine.ask(files=[old])["files"][str(old)] == ["music of the first disk"]
    assert os.listdir(machine.host / "old-root") == ["music of the disk put back"]
