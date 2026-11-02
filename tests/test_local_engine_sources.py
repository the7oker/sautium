"""What the built-in output opens (playback/local/engine.py): a rip here or
the proxy's stream, nothing else a slot may name — as the DLNA and browser
outputs — and ffprobe takes its input as an input."""

import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from config import settings  # noqa: E402
from playback.local import engine as engine_mod  # noqa: E402
from playback.queue import CanonicalQueue, QueueItem  # noqa: E402


def _slot(source: dict) -> QueueItem:
    return QueueItem(track_id="t", media_file_id=None, source=source, title="t")


def test_the_built_in_output_opens_a_rip_or_a_stream_and_nothing_else(monkeypatch):
    from streaming import service as streaming_service

    class Proxy:
        def url_for(self, token):
            return f"http://127.0.0.1:8832/preview/{token}"
    monkeypatch.setattr(streaming_service, "get_proxy", lambda: Proxy())
    engine = engine_mod.Engine(CanonicalQueue(), lambda _s: None, None, False)
    assert engine._source_url(_slot({"kind": "file", "path": "/music/a.flac"})) \
        == settings.translate_to_local_path("/music/a.flac")
    assert engine._source_url(_slot({"kind": "proxy", "token": "tok"})).endswith("/preview/tok")
    for source in ({"kind": "uri", "uri": "file:///elsewhere/a.flac"},
                   {"kind": "uri", "uri": "http://192.0.2.4/a.flac"},
                   {"kind": "uri", "uri": "/elsewhere/a.flac"},
                   {"kind": "hqp", "path": "/box/a.flac", "format": "FLAC"},
                   {"kind": "track"}, {"kind": "unplayable", "reason": "x"}):
        assert engine._source_url(_slot(source)) is None, source


def test_ffprobe_takes_the_source_as_its_input(monkeypatch):
    seen = []

    def run(cmd, **_kw):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="44100,2\n180.5\n", stderr="")
    monkeypatch.setattr(engine_mod.subprocess, "run", run)
    assert engine_mod._probe("-a-name.flac") == (44100, 2, 180.5)
    assert seen[0][-2:] == ["-i", "-a-name.flac"]
