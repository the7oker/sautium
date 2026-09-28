"""HQPlayer URI layer — pure logic, no HQPlayer, no network. Run inside the
backend container:

    python -m pytest test_hqp_uri.py -q
"""

import pytest

from config import settings
from playback import hqp_backend as hb
from playback.queue import QueueItem
from streaming import service as streaming_service
from streaming.proxy import MediaProxy


@pytest.fixture
def docker_paths(monkeypatch):
    """A Docker node: the scanner sees /music, the DB stores E:/Music/…"""
    monkeypatch.setattr(settings, "music_library_path", "/music")
    monkeypatch.setattr(settings, "music_host_path", "E:/Music")
    monkeypatch.setattr(settings, "hqplayer_host", "localhost")     # on this machine: by path


@pytest.fixture
def proxy(monkeypatch):
    p = MediaProxy(port=0, advertised_host="127.0.0.1", file_token_key=b"k")
    monkeypatch.setattr(streaming_service, "_proxy", p)
    return p


def _file(path, fmt="FLAC", **span):
    return QueueItem(track_id="t", media_file_id=1,
                     source={"kind": "file", "path": path, "format": fmt, **span},
                     title="T", artist="A", album="B")


def test_library_uri_is_the_stored_path(docker_paths):
    assert hb._library_uri("E:/Music/A/01.flac") == "file:///E:/Music/A/01.flac"


def test_library_db_path_is_the_inverse(docker_paths):
    assert hb._library_db_path("file:///E:/Music/A/%5BTR24%5D%2001.flac") == \
        "E:/Music/A/[TR24] 01.flac"
    assert hb._library_db_path("http://127.0.0.1:0/file/abc") is None


def test_the_mode_is_read_off_the_address(docker_paths, monkeypatch):
    """An HQPlayer on this machine opens the files by path; any other is
    handed streams — nothing to configure."""
    for own in ("localhost", "127.0.0.1", "host.docker.internal", "::1"):
        monkeypatch.setattr(settings, "hqplayer_host", own)
        assert hb._stream_mode() is False, own
    monkeypatch.setattr(settings, "hqplayer_host", "192.168.1.253")
    assert hb._stream_mode() is True
    monkeypatch.setattr(settings, "hqplayer_host", "hqplayer-pi")
    assert hb._stream_mode() is True


def test_slot_identity_resolves_file_uris_and_owned_tokens(docker_paths, proxy):
    assert hb._slot_identity("file:///E:/Music/A/01.flac", proxy) == \
        ("E:/Music/A/01.flac", None)
    tok = proxy.register_file("/music/A/img.flac", "audio/flac", start=12.5, end=200.0)
    assert hb._slot_identity(f"http://192.168.1.188:0/file/{tok}", proxy) == \
        ("E:/Music/A/img.flac", 12.5)
    assert hb._slot_identity("http://192.168.1.188:0/file/unknown", proxy) is None
    assert hb._slot_identity("http://192.168.1.188:0/preview/abc", proxy) is None


def test_http_served_by_slot_kind_and_mode(docker_paths, monkeypatch):
    assert hb._http_served(_file("E:/Music/A/01.flac")) is False
    assert hb._http_served(_file("E:/Music/A/img.flac", cue_start=0.0, cue_end=10.0)) is True
    assert hb._http_served(_file("E:/Music/A/01.m4a", fmt="M4A")) is True
    assert hb._http_served(QueueItem(track_id=None, media_file_id=None,
                                     source={"kind": "proxy", "token": "x"},
                                     title="", artist="")) is True
    foreign = QueueItem(track_id=None, media_file_id=None,
                        source={"kind": "uri", "uri": "file:///X:/foreign.flac"},
                        title="", artist="")
    assert hb._http_served(foreign) is False
    monkeypatch.setattr(settings, "hqplayer_host", "192.168.1.253")   # elsewhere: streams
    assert hb._http_served(_file("E:/Music/A/01.flac")) is True


def test_owned_play_uri_by_mode(docker_paths, proxy, monkeypatch):
    item = _file("E:/Music/A/01.flac")
    assert hb._owned_play_uri(item, "192.168.1.188") == "file:///E:/Music/A/01.flac"
    monkeypatch.setattr(settings, "hqplayer_host", "192.168.1.253")
    tok = proxy.file_token("/music/A/01.flac")
    assert hb._owned_play_uri(item, "192.168.1.188") == f"http://192.168.1.188:0/file/{tok}"
    assert proxy.file_entry(tok).path == "/music/A/01.flac"


def test_cue_slice_rides_the_proxy_and_falls_back_to_the_image(docker_paths, proxy, monkeypatch):
    from streaming import transcode
    item = _file("E:/Music/A/img.flac", cue_start=12.5, cue_end=200.0)
    monkeypatch.setattr(transcode, "flac_slice_path_for_file", lambda *a, **k: "/tmp/cut.flac")
    tok = proxy.file_token("/music/A/img.flac", 12.5, 200.0)
    assert hb._owned_play_uri(item, "127.0.0.1") == f"http://127.0.0.1:0/file/{tok}"
    assert proxy.file_entry(tok).start == 12.5

    def boom(*a, **k):
        raise OSError("no such image")
    monkeypatch.setattr(transcode, "flac_slice_path_for_file", boom)
    assert hb._owned_play_uri(item, "127.0.0.1") == "file:///E:/Music/A/img.flac"
    monkeypatch.setattr(settings, "hqplayer_host", "192.168.1.253")
    whole = proxy.file_token("/music/A/img.flac")
    assert hb._owned_play_uri(item, "127.0.0.1") == f"http://127.0.0.1:0/file/{whole}"
