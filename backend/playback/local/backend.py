"""
LocalBackend — the built-in player output (PlayerBackend over Engine).

The canonical queue IS this backend's playlist: there is nothing to
mirror, so the queue_* hooks only nudge the engine to resynchronise its
bookkeeping (current-slot index by item identity, un-closing the ring
when items are appended past a finished tail). Status is fully
event-driven — the engine emits on every transition and once per second
while playing, from its own frame counters.

Known v1 trade-off, accepted deliberately: audio that is ALREADY decoded
into the ring is never rewound. If the user edits the slot right after
the playing track while its successor is already buffered, the buffered
track finishes before the new order takes effect — the same "never touch
what the player is reading" stance the HQPlayer mirror takes, applied to
our own buffer.
"""

import logging
from typing import Optional

from playback.base import Capabilities, PlayerBackend
from playback.local.engine import Engine
from playback.queue import CanonicalQueue

logger = logging.getLogger(__name__)


class LocalBackend(PlayerBackend):
    id = "local"

    def __init__(self, emit, queue: CanonicalQueue,
                 device_id: Optional[str], exclusive: bool):
        super().__init__(emit)
        self._queue = queue
        self.label = ((device_id or "").partition("::")[2]
                      or "System default output")
        self._engine = Engine(queue, emit, device_id, exclusive)
        self._exclusive = exclusive

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        self._engine.start()
        logger.info("local playback engine started (device=%s, exclusive=%s)",
                    self.label, self._exclusive)

    def shutdown(self) -> None:
        self._engine.shutdown()
        logger.info("local playback engine stopped")

    def capabilities(self) -> Capabilities:
        return Capabilities(volume=True, volume_kind="percent", seek=True,
                            gapless=True, exclusive_mode=True)

    # -- transport ---------------------------------------------------------

    def play(self) -> bool:
        if len(self._queue) == 0:
            return False
        self._engine.post("play")
        return True

    def pause(self) -> bool:
        self._engine.post("pause")
        return True

    def stop(self) -> bool:
        self._engine.post("stop")
        return True

    def next(self) -> bool:
        self._engine.post("select", index=self._engine_index() + 1)
        return True

    def previous(self) -> bool:
        self._engine.post("select", index=max(1, self._engine_index() - 1))
        return True

    def select(self, index: int) -> bool:
        if self._queue.item_at(index) is None:
            return False
        self._engine.post("select", index=index)
        return True

    def seek(self, seconds: int) -> bool:
        self._engine.post("seek", seconds=float(seconds))
        return True

    def set_volume(self, level: float) -> bool:
        self._engine.post("volume", level=float(level))
        return True

    def volume_up(self) -> bool:
        return self.set_volume(min(100.0, self._engine.volume + 5.0))

    def volume_down(self) -> bool:
        return self.set_volume(max(0.0, self._engine.volume - 5.0))

    def _engine_index(self) -> int:
        # Racy read of a monotonic int — good enough for next/prev intent.
        return self._engine._index or 0

    # -- canonical-queue hooks --------------------------------------------------
    # The pre-commit mirror hooks are pure accepts (nothing to mirror — the
    # engine renders the canonical queue). All engine nudges happen in
    # queue_changed, AFTER the canonical commit, so the engine never acts on
    # the pre-mutation queue.

    def queue_changed(self, kind: str, *, play: bool = False) -> None:
        if kind == "replace":
            if play and len(self._queue):
                self._engine.post("select", index=1)
            else:
                self._engine.post("stop")
        else:
            self._engine.post("resync")
