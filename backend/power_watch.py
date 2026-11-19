"""A Raspberry Pi's weak power supply, said once (PI-NODE-1 §D, PI-NODE-2B).

The firmware flags an under-voltage; the kernel's `raspberrypi-hwmon` driver
asks it every two seconds, clears the latch as it does, and exposes what it
read as one world-readable attribute, `in0_lcrit_alarm` under the hwmon
device named `rpi_volt` — a container sees it with no mount. On a change the
driver notifies the attribute (`hwmon_notify_event`), so a `poll()` for
POLLPRI wakes: one thread blocks there, with no timer. On a machine with no
such attribute there is no thread.

The first under-voltage of a start raises the notice `power.undervoltage`
(routers/settings.py), a report that stays until the owner has seen it; the
watch ends there — a supply that dipped once is the thing to say, and saying
it at every dip of a starving board would bury everything else.
"""

import logging
import select
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_HWMON = Path("/sys/class/hwmon")
_DEVICE, _ATTRIBUTE = "rpi_volt", "in0_lcrit_alarm"

_since: Optional[str] = None      # the first under-voltage of this start


def attribute(root: Path = _HWMON) -> Optional[Path]:
    """The under-voltage attribute of this machine, or None where the driver
    is not (anything that is not a Raspberry Pi)."""
    try:
        devices = sorted(root.iterdir())
    except OSError:
        return None
    for device in devices:
        try:
            if (device / "name").read_text().strip() == _DEVICE and (device / _ATTRIBUTE).exists():
                return device / _ATTRIBUTE
        except OSError:
            continue
    return None


def watch(read: Callable[[], str], wait: Callable[[], None], raised: Callable[[], None]) -> None:
    """Until the attribute reads 1: read it, then block until the driver says
    it changed. `read` and `wait` are the file's (a sysfs attribute signals
    by POLLPRI, which no pipe can stand in for: tests drive this seam)."""
    while read().strip() != "1":
        wait()
    raised()


def _raise() -> None:
    global _since
    _since = datetime.now(timezone.utc).isoformat()
    logger.warning("the power supply dropped under its voltage: the board can switch off under load")
    from db_pool import db_execute
    db_execute("NOTIFY sautium_notices")


def _run(path: Path) -> None:
    with open(path, "rb", buffering=0) as f:
        poller = select.poll()
        poller.register(f, select.POLLPRI | select.POLLERR)

        def read() -> str:
            f.seek(0)
            return f.read(16).decode("ascii", "replace")

        watch(read, poller.poll, _raise)


def start() -> bool:
    """Watch this machine's under-voltage attribute, where it has one."""
    path = attribute()
    if path is None:
        return False
    threading.Thread(target=_run, args=(path,), name="power-watch", daemon=True).start()
    logger.info("watching the power supply's under-voltage flag (%s)", path)
    return True


def notice() -> Optional[dict]:
    """The report `power.undervoltage`, from the first under-voltage of this
    start on."""
    if _since is None:
        return None
    return {"key": "power.undervoltage", "kind": "warning", "since": _since, "until": None, "data": {}}
