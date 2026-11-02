"""A catalogue slice counts only under a dump holder the release names
(desktop/p2p/master_node.SLICE_AUTHORS): the MB and LB verifiers refuse any
other author before they inflate a byte; the newest LB version comes from
an author's own /health only, in the dump's format and no later than a day
ahead; a malformed MB entry is dropped, not raised on; a slice keeps only
Bandcamp shop pages, and the Buy button opens nothing else."""

import asyncio
import base64
import gzip
import json
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from desktop import mb_slice_client
from desktop.p2p import lb_slice_cycle, lb_slice_queries as lb, master_node, mb_slice_queries as mb

ed25519 = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")
AUTHOR, STRANGER = (ed25519.Ed25519PrivateKey.generate() for _ in range(2))
pub = lambda k: k.public_key().public_bytes_raw().hex()
ARTIST = str(uuid.UUID(int=77))


@pytest.fixture(autouse=True)
def _release_names_the_author(monkeypatch):
    monkeypatch.setattr(master_node, "SLICE_AUTHORS", (pub(AUTHOR),))


def _mb_entry(key, name="Sade", rows=None):
    one = {"dump_version": "20261101-001500", "artists_matched": {name: [1]}, "truncated": [],
           "tables": rows if rows is not None else {"mb_artist": [[1] + [None] * 18]}}
    blob = mb.name_blob(name, one)
    return {"dump_version": one["dump_version"], "author_pubkey": pub(key),
            "sig": key.sign(mb.receipt_message_for(blob)).hex(),
            "blob_gz": base64.b64encode(gzip.compress(blob)).decode()}


def _lb_entry(key, version="20261101-001500"):
    one = {"dump_version": version, "artist": [5, 2], "recordings": [], "truncated": False}
    blob = lb.slice_blob(ARTIST, one)
    return {"dump_version": version, "author_pubkey": pub(key),
            "sig": key.sign(lb.receipt_message_for(blob)).hex(),
            "blob_gz": base64.b64encode(gzip.compress(blob)).decode()}


def test_a_slice_signed_by_any_other_key_is_refused_before_it_is_inflated(monkeypatch):
    assert mb.verify_slice_entry("Sade", _mb_entry(AUTHOR)) is not None
    assert lb.verify_slice_entry(ARTIST, _lb_entry(AUTHOR)) is not None
    inflated = []
    monkeypatch.setattr("desktop.p2p.wire.gunzip", lambda *a: inflated.append(a) or b"")
    assert mb.verify_slice_entry("Sade", _mb_entry(STRANGER)) is None
    assert lb.verify_slice_entry(ARTIST, _lb_entry(STRANGER)) is None
    assert inflated == []


@pytest.mark.parametrize("version", ["20991231-235959", "v9", "2026-11-01", "20261301-000000"])
def test_an_lb_version_out_of_its_format_or_ahead_of_today_is_refused(version):
    assert lb.verify_slice_entry(ARTIST, _lb_entry(AUTHOR, version)) is None


def test_the_newest_lb_version_is_an_authors_word_only():
    cycle = lb_slice_cycle.LbSliceCycle.__new__(lb_slice_cycle.LbSliceCycle)
    sources = [(SimpleNamespace(base_url="https://a:1"), pub(STRANGER),
                {"lb_dump": "20261102-000000", "lb_slices_version": "20261102-000000"}),
               (SimpleNamespace(base_url="https://b:1"), pub(AUTHOR),
                {"lb_dump": "20261025-000000"})]

    class _Finder:
        async def find(self):
            return sources, True

    cycle._sources = _Finder()
    found, newest = asyncio.run(cycle.find_sources())
    assert newest == "20261025-000000" and len(found) == 2


def test_a_malformed_mb_entry_is_dropped_not_raised():
    bad = _mb_entry(AUTHOR, rows={"mb_artist": [[1, 2]]})          # a row short of its columns

    class _Peer:
        base_url = "https://dump:1"

        def mb_slice(self, names):
            return {"v": mb.PROTOCOL_VERSION, "slices": {"Sade": bad}, "missing": []}

    client = mb_slice_client.MBSliceClient(_Peer(), db_dsn="postgresql://unused")
    assert client.run(["Sade"]) == {"error": "all slices failed verification"}


def test_a_slice_keeps_only_shop_pages():
    cols = mb.SLICE_TABLES
    url = lambda i, u: [i, str(uuid.uuid4()), u, 0, None]
    link = lambda i, url_id: [i, 1, 10, url_id, 0, None, 0, "", ""]
    tables = {t: [] for t in cols}
    tables["mb_url"] = [url(1, "https://artist.bandcamp.com/album/a"),
                        url(2, "https://elsewhere.example/buy"),
                        url(3, "https://daily.bandcamp.com/feature")]
    tables["mb_l_release_url"] = [link(11, 1), link(12, 2), link(13, 3)]
    tables["mb_l_artist_url"] = [link(21, 2)]
    kept = mb_slice_client._shop_pages_only(tables)
    assert [r[0] for r in kept["mb_url"]] == [1]
    assert [r[0] for r in kept["mb_l_release_url"]] == [11] and kept["mb_l_artist_url"] == []


def test_the_buy_button_opens_a_shop_page_only():
    src = (Path(__file__).resolve().parents[2] / "backend/static/app-shell.js").read_text(encoding="utf-8")
    i = src.index("function bandcampShopPage")
    body = src[i:src.index("\n  }", i) + 4]
    cases = ["https://artist.bandcamp.com/album/x", "http://artist.bandcamp.com/album/x",
             "https://daily.bandcamp.com/x", "https://artist.bandcamp.com.elsewhere.example/",
             "https://elsewhere.example/x", "data:text/plain,x", ""]
    script = (f"{body}\nconsole.log(JSON.stringify({json.dumps(cases)}.map(bandcampShopPage)));")
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout
    assert json.loads(out) == ["https://artist.bandcamp.com/album/x"] + [None] * 6
