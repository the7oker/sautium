"""The cores and the memory a process may use (PI-NODE-2C DESIGN §14, D6).

Before this every pool and every tier read `os.cpu_count()` and the machine's
total memory: a node given three of four cores (the fourth an audio daemon's)
or a container capped at 4 GiB on a 16 GiB host sized itself by the machine.
One helper reads the affinity, the CFS quota and the memory limit of the
process's own control group and the groups above it; a child process with
its affinity narrowed is how the readers are held to it."""

import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from desktop import machine_tier

REPO = Path(__file__).resolve().parent.parent
BACKEND = REPO / "backend"

linux = pytest.mark.skipif(not hasattr(os, "sched_getaffinity"), reason="no CPU affinity on this platform")


def _tree(tmp_path, membership, files):
    root = tmp_path / "cgroup"
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    root.mkdir(exist_ok=True)
    member = tmp_path / "membership"
    member.write_text(membership)
    return str(root), str(member)


def _affinity():
    return len(os.sched_getaffinity(0))


@linux
def test_with_no_quota_the_cores_are_the_affinity(tmp_path):
    root, member = _tree(tmp_path, "0::/\n", {"cpu.max": "max 100000\n"})
    assert machine_tier.usable_cores(root, member) == _affinity()
    assert machine_tier.usable_cores(root, str(tmp_path / "absent")) == _affinity()


@linux
@pytest.mark.parametrize("quota, cores", [("150000 100000", 2), ("100000 100000", 1), ("50000 100000", 1),
                                          ("400000 100000", 4)])
def test_a_quota_cuts_the_cores_to_its_ceiling(tmp_path, quota, cores):
    root, member = _tree(tmp_path, "0::/\n", {"cpu.max": quota + "\n"})
    assert machine_tier.usable_cores(root, member) == min(_affinity(), cores)


@linux
def test_a_quota_one_group_up_counts(tmp_path):
    root, member = _tree(tmp_path, "0::/user.slice/app.scope\n", {
        "user.slice/app.scope/cpu.max": "max 100000\n",
        "user.slice/cpu.max": "100000 100000\n",
        "cpu.max": "max 100000\n"})
    assert machine_tier.usable_cores(root, member) == 1


@linux
def test_a_v1_quota_is_read_from_the_cpu_hierarchy(tmp_path):
    membership = "12:memory:/docker/abc\n7:cpu,cpuacct:/docker/abc\n"
    root, member = _tree(tmp_path, membership, {
        "cpu/docker/abc/cpu.cfs_quota_us": "100000\n", "cpu/docker/abc/cpu.cfs_period_us": "100000\n"})
    assert machine_tier.usable_cores(root, member) == 1
    (Path(root) / "cpu/docker/abc/cpu.cfs_quota_us").write_text("-1\n")
    assert machine_tier.usable_cores(root, member) == _affinity()


@linux
def test_a_container_reads_the_top_it_sees_where_its_named_group_is_not_there(tmp_path):
    """v1 without a private namespace: the membership names the host's path,
    the mount shows the container's own group at its top."""
    root, member = _tree(tmp_path, "7:cpu,cpuacct:/docker/abc\n", {
        "cpu/cpu.cfs_quota_us": "200000\n", "cpu/cpu.cfs_period_us": "100000\n"})
    assert machine_tier.usable_cores(root, member) == min(_affinity(), 2)


def test_the_smallest_memory_limit_on_the_way_up_is_the_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(machine_tier, "machine_memory_kib", lambda: 16000000)
    root, member = _tree(tmp_path, "0::/a/b\n", {"a/b/memory.max": "max\n", "a/memory.max": f"{4 << 30}\n",
                                                  "memory.max": "max\n"})
    assert machine_tier.memory_limit_kib(root, member) == 4 << 20
    assert machine_tier.cgroup_memory_limit(root, member) == (4 << 30, os.path.join(root, "a"), 2)
    (Path(root) / "a/b/memory.max").write_text(f"{2 << 30}\n")
    assert machine_tier.memory_limit_kib(root, member) == 2 << 20
    (Path(root) / "a/b/memory.max").write_text(f"{64 << 30}\n")     # looser than the group above
    assert machine_tier.memory_limit_kib(root, member) == 4 << 20


def test_no_limit_is_the_machines_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(machine_tier, "machine_memory_kib", lambda: 16000000)
    root, member = _tree(tmp_path, "0::/\n", {"memory.max": "max\n"})
    assert machine_tier.memory_limit_kib(root, member) == 16000000
    assert machine_tier.cgroup_memory_limit(root, member) is None
    root, member = _tree(tmp_path / "v1", "5:memory:/\n", {"memory/memory.limit_in_bytes": f"{1 << 62}\n"})
    assert machine_tier.memory_limit_kib(root, member) == 16000000      # v1's "no limit"
    (Path(root) / "memory/memory.limit_in_bytes").write_text(f"{2 << 30}\n")
    assert machine_tier.memory_limit_kib(root, member) == 2 << 20
    assert machine_tier.cgroup_memory_limit(root, member)[2] == 1


def test_a_limit_above_the_machine_is_the_machine(tmp_path, monkeypatch):
    monkeypatch.setattr(machine_tier, "machine_memory_kib", lambda: 4000000)
    root, member = _tree(tmp_path, "0::/\n", {"memory.max": f"{64 << 30}\n"})
    assert machine_tier.memory_limit_kib(root, member) == 4000000


def test_this_machine_reads_its_own_memory_and_cores():
    assert machine_tier.machine_memory_kib() > 0
    assert 1 <= machine_tier.usable_cores() <= machine_tier.machine_cores()


_CHILD = """\
import json, os, sys
sys.path[:0] = [{repo!r}, {backend!r}]
os.sched_setaffinity(0, {{sorted(os.sched_getaffinity(0))[0]}})
from desktop import machine_tier
out = {{"cores": machine_tier.usable_cores(), "machine": machine_tier.machine_cores()}}
{body}
print(json.dumps(out))
"""


def _child(body=""):
    script = _CHILD.format(repo=str(REPO), backend=str(BACKEND), body=textwrap.dedent(body))
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


@linux
def test_a_process_narrowed_to_one_core_reads_one():
    out = _child()
    assert out["cores"] == 1
    assert out["machine"] == os.cpu_count()


@linux
@pytest.mark.parametrize("name", ["lite", "standard", "full"])
def test_the_profiles_figures_never_exceed_one_core(name):
    pytest.importorskip("sqlalchemy")
    out = _child(f"""
        import hardware_profile
        hardware_profile.models_switch = lambda: True
        hardware_profile._configured_profile = lambda: ({name!r}, "env")
        p = hardware_profile.resolve()
        out.update(profile=p.cores, torch=hardware_profile.HardwareProfile(
            p.name, p.source, "cpu", None, p.ram_gb, p.ram_gb, p.cores).torch_cpu_threads,
            io=p.io_workers, prefetch=p.prefetch_workers)
    """)
    assert (out["profile"], out["torch"], out["io"], out["prefetch"]) == (1, 1, 1, 1)


@linux
def test_the_translator_takes_one_thread_on_one_core():
    pytest.importorskip("transformers")
    out = _child("""
        import types
        made = {}
        fake = types.ModuleType("ctranslate2")
        fake.Translator = lambda *a, **kw: made.update(kw)
        sys.modules["ctranslate2"] = fake
        import translation
        translation.ensure_ct2_model = lambda: "/nonexistent"
        translation.AutoTokenizer = types.SimpleNamespace(from_pretrained=lambda *a, **kw: None)
        try:
            translation.QueryTranslator().load_model()
        except Exception:
            pass                      # the warm-up after the constructor has no model
        out["intra"] = made["intra_threads"]
    """)
    assert out["intra"] == 1


def test_a_two_core_node_keeps_the_floors_of_two():
    sys.path.insert(0, str(BACKEND))
    pytest.importorskip("sqlalchemy")
    import hardware_profile
    lite = hardware_profile.HardwareProfile("lite", "auto", "cpu", None, 4.0, 4.0, 2)
    assert (lite.torch_cpu_threads, lite.io_workers, lite.prefetch_workers) == (2, 2, 2)
    four = hardware_profile.HardwareProfile("lite", "auto", "cpu", None, 4.0, 4.0, 4)
    assert (four.torch_cpu_threads, four.io_workers, four.prefetch_workers) == (2, 4, 2)


def test_a_capped_container_is_a_node_of_its_cap(monkeypatch):
    sys.path.insert(0, str(BACKEND))
    pytest.importorskip("sqlalchemy")
    import hardware_profile
    monkeypatch.setattr(machine_tier, "memory_limit_kib", lambda: 4 << 20)
    assert hardware_profile._total_ram_gb() == 4.0


def test_nothing_else_reads_the_machines_core_count():
    """The rule itself: `os.cpu_count()` ignores the affinity and every
    quota, so one module reads it and says which reading it gives."""
    reads = re.compile(r"\bos\.cpu_count\(|\bmultiprocessing\.cpu_count\(")
    found = []
    for top in ("backend", "desktop", "scripts", "mcp"):
        for path in (REPO / top).rglob("*.py"):
            if "data" in path.relative_to(REPO).parts[:2] or path.name == "machine_tier.py":
                continue
            if reads.search(path.read_text(encoding="utf-8", errors="replace")):
                found.append(str(path.relative_to(REPO)))
    assert found == []
