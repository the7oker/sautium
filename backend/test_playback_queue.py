"""
CanonicalQueue unit tests — pure logic, no DB (the payload/mutation
contract the SSE clients and output backends rely on). Run:

    python -m pytest test_playback_queue.py -q
"""

from playback.queue import CanonicalQueue, QueueItem


def _item(mfid=None, track_id=None, title="T", preview=False, provider=None):
    if mfid is not None:
        source = {"kind": "file", "path": f"E:/Music/{mfid}.flac", "format": "FLAC"}
    elif preview:
        source = {"kind": "proxy", "token": f"tok{track_id}"}
    else:
        source = {"kind": "uri", "uri": "file:///X:/foreign.flac"}
    return QueueItem(track_id=track_id, media_file_id=mfid, source=source,
                     title=title, artist="A", album="B", track_number=1,
                     duration_seconds=100.0, cover_id=None,
                     preview=preview, provider=provider)


def test_replace_bumps_generation_and_version():
    q = CanonicalQueue()
    g1 = q.replace([_item(1, "t1")])
    v1 = q.version
    g2 = q.replace([_item(2, "t2")])
    assert g2 == g1 + 1
    assert q.version == v1 + 1
    assert len(q) == 1
    assert q.item_at(1).media_file_id == 2


def test_append_insert_remove():
    q = CanonicalQueue()
    q.replace([_item(1, "t1"), _item(2, "t2")])
    q.append([_item(3, "t3")])
    assert [it.media_file_id for it in q.snapshot()] == [1, 2, 3]
    q.insert_after(1, [_item(4, "t4")])
    assert [it.media_file_id for it in q.snapshot()] == [1, 4, 2, 3]
    removed = q.remove(2)
    assert removed.media_file_id == 4
    assert [it.media_file_id for it in q.snapshot()] == [1, 2, 3]
    assert q.remove(99) is None


def test_reorder_with_duplicates_keeps_relative_order():
    q = CanonicalQueue()
    a1, b, a2 = _item(1, "t1", "first"), _item(2, "t2"), _item(1, "t1", "second")
    q.replace([a1, b, a2])
    q.reorder_by_track_ids(["t2", "t1", "t1"])
    snap = q.snapshot()
    assert [it.media_file_id for it in snap] == [2, 1, 1]
    assert snap[1] is a1 and snap[2] is a2   # duplicate ids keep their order


def test_clear_for_radio_keeps_current_slot():
    q = CanonicalQueue()
    q.replace([_item(1, "t1"), _item(2, "t2"), _item(3, "t3")])
    g = q.clear_for_radio(2)
    assert [it.media_file_id for it in q.snapshot()] == [2]
    assert g == q.generation
    q.clear_for_radio(None)
    assert len(q) == 0


def test_payload_shapes():
    q = CanonicalQueue()
    q.replace([
        _item(7, "t-owned", "Owned"),
        _item(None, "t-ph", "Phantom", preview=True, provider="deezer"),
        _item(None, None, "Foreign"),
    ])
    rows = q.payload()["tracks"]

    owned = rows[0]
    assert owned == {"id": 7, "track_id": "t-owned", "title": "Owned",
                     "track_number": 1, "artist": "A", "album": "B",
                     "duration_seconds": 100.0, "cover_id": None, "index": 0}

    ph = rows[1]
    assert ph["preview"] is True and ph["provider"] == "deezer"
    assert ph["id"] is None and ph["track_id"] == "t-ph"
    assert "provider_cover_url" in ph and "cover_url" in ph

    foreign = rows[2]
    assert foreign["id"] is None and "album" not in foreign
    assert "preview" not in foreign and "track_id" not in foreign


def test_index_of_is_identity_based():
    q = CanonicalQueue()
    a1, b, a2 = _item(1, "t1", "first"), _item(2, "t2"), _item(1, "t1", "second")
    q.replace([a1, b, a2])
    assert q.index_of(a1) == 1 and q.index_of(a2) == 3   # same track, both slots
    q.remove(1)
    assert q.index_of(a1) is None
    assert q.index_of(a2) == 2


def test_item_at_bounds():
    q = CanonicalQueue()
    q.replace([_item(1, "t1")])
    assert q.item_at(0) is None
    assert q.item_at(1).media_file_id == 1
    assert q.item_at(2) is None
    assert q.item_at(None) is None
