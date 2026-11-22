"""Every thread's stack on a signal, the backend left running (PI-NODE-2C
DESIGN §12.2; PI-NODE-2B's C2: a backend that spun for 48 minutes at a 1 GB
boot, and whose only evidence left with its container).

The child runs main.py's own first lines — everything before its imports of
the application — so the registration tested is the one the backend makes."""

import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

MAIN = Path(__file__).resolve().parent.parent / "backend" / "main.py"

pytestmark = pytest.mark.skipif(not hasattr(signal, "SIGUSR1"), reason="no SIGUSR1 on this platform")


def _prelude() -> str:
    source = MAIN.read_text(encoding="utf-8")
    prelude = source[:source.index("\nimport asyncio\n")]
    assert "faulthandler.enable()" in prelude
    return prelude[prelude.index("import faulthandler"):]


_CHILD = textwrap.dedent("""
    import sys, threading
    def parked_worker():
        release.wait()
    release = threading.Event()
    threading.Thread(target=parked_worker, name="parked").start()
    print("ready", flush=True)
    sys.stdin.readline()            # the parent's word to end
    release.set()
""")


def test_sigusr1_writes_every_threads_stack_and_the_process_lives_on():
    child = subprocess.Popen([sys.executable, "-c", _prelude() + _CHILD], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "ready"
        os.kill(child.pid, signal.SIGUSR1)
        _, err = child.communicate("end\n", timeout=30)
    finally:
        if child.poll() is None:
            child.kill()
    assert child.returncode == 0, err                   # it lived on and ended by itself
    assert err.count("Current thread") == 1
    assert "parked_worker" in err                       # the other thread's stack too
