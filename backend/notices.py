"""The producer side of the notices channel. routers.settings derives the
active set and /api/events delivers it; the modules that observe a condition
— the scanner, the media proxy, the scan worker, cli.py — record and wake
through here, without reaching into the web layer.

Conditions with no ledger of their own (a vanished music folder, a missing
binary) keep their `since` here from the moment they are first observed
until they are not — process memory, which is enough: after a restart the
condition is either gone or freshly observed. Producers wake the channel on
the bad AND the good transition, each only when it changes what is shown.
"""

from datetime import datetime, timezone
from typing import Dict, Optional

from db_pool import db_execute

_derived_since: Dict[str, str] = {}


def derived(key: str, active: bool) -> Optional[str]:
    """The onset of `key` while it is active; forgotten when it ends."""
    if not active:
        _derived_since.pop(key, None)
        return None
    return _derived_since.setdefault(key, datetime.now(timezone.utc).isoformat())


def recheck(key: str) -> None:
    """A producer saw the good state again (a file served, a scan walked):
    re-derive only if the condition is shown, so a healthy node never pays."""
    if key in _derived_since:
        db_execute("NOTIFY sautium_notices")


def onset(key: str) -> None:
    """A producer saw the bad state: re-derive only if the condition is not
    shown yet, so a reader that keeps seeing it wakes the channel once."""
    if key not in _derived_since:
        db_execute("NOTIFY sautium_notices")
