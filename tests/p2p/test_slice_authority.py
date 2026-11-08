"""A catalogue slice counts under its author's birth certificate, whoever the
author (desktop/p2p/slice_trust.entry_certificate): the MB and LB verifiers
take a slice of any certified author and refuse one without a certificate,
with another key's, with one from an authority the node does not trust, or a
`pow` one without its proof — before they inflate a byte. A slice is its own
subtree: a row the protocol's walk does not reach makes the whole slice false
(slice_trust.subtree_ok). The newest LB version is never past what
MetaBrainz has published (no later than a day ahead before its listing is
read); a malformed MB entry is dropped, not raised on; a slice keeps only
Bandcamp shop pages, and the Buy button opens nothing else."""

import asyncio
import gzip
import json
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from desktop import mb_slice_client
from desktop.p2p import lb_slice_cycle, lb_slice_queries as lb, mb_slice_queries as mb, slice_trust
from tests.slice_certs import (Subtree, certificate, certified, lb_entry, mb_entry, proof, pub, trust,
                               uncertified)

ed25519 = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")
AUTHOR, ANOTHER, STRANGER = (ed25519.Ed25519PrivateKey.generate() for _ in range(3))
ARTIST = str(uuid.UUID(int=77))
VERSION = "20261101-001500"


@pytest.fixture(autouse=True)
def _authority(monkeypatch):
    trust(monkeypatch)


def _mb_one(name="Sade", rows=None):
    return {"dump_version": VERSION, "artists_matched": {name: [1]}, "truncated": [],
            "tables": rows if rows is not None else {"mb_artist": [[1] + [None] * 18]}}


def _lb_one(version=VERSION):
    return {"dump_version": version, "artist": [5, 2], "recordings": [], "truncated": False}


def test_a_slice_counts_under_any_certified_author():
    for key in (AUTHOR, ANOTHER):
        cert = certificate(pub(key))
        core, blob_gz, got_cert, got_proof = mb.verify_slice_entry("Sade", mb_entry(key, "Sade", _mb_one()))
        assert core["name_key"] == "sade" and gzip.decompress(blob_gz) == mb.name_blob("Sade", _mb_one())
        assert (got_cert, got_proof) == (cert, None)
        core, _, got_cert, got_proof = lb.verify_slice_entry(ARTIST, lb_entry(key, ARTIST, _lb_one()))
        assert core["artist_mbid"] == ARTIST and (got_cert, got_proof) == (cert, None)


def test_a_pow_author_counts_with_its_proof_bound_to_its_certificate():
    cert = certificate(pub(AUTHOR), method="pow")
    bound = proof(cert)
    out = mb.verify_slice_entry("Sade", mb_entry(AUTHOR, "Sade", _mb_one(), cert, bound))
    assert out is not None and out[2:] == (cert, bound)
    out = lb.verify_slice_entry(ARTIST, lb_entry(AUTHOR, ARTIST, _lb_one(), cert, bound))
    assert out is not None and out[2:] == (cert, bound)


def _with(cert_of, proof_of=lambda cert: None):
    def apply(entry):
        cert = cert_of()
        return certified(entry, cert, proof_of(cert))
    return apply


@pytest.mark.parametrize("certify", [
    uncertified,
    _with(lambda: certificate(pub(ANOTHER))),
    _with(lambda: certificate(pub(AUTHOR), authority=STRANGER)),
    _with(lambda: certificate(pub(AUTHOR), method="pow")),
    _with(lambda: certificate(pub(AUTHOR), method="pow"),
          lambda cert: proof(certificate(pub(AUTHOR), method="pow", issued_at="2026-10-02T12:00:00Z"))),
    _with(lambda: {**certificate(pub(AUTHOR)), "difficulty": 1}),
    _with(lambda: "a certificate"),
], ids=["none", "another-keys", "unknown-authority", "pow-without-proof", "proof-of-another-certificate",
        "altered-after-signing", "not-a-certificate"])
def test_a_slice_without_its_authors_certificate_is_refused_before_it_is_inflated(monkeypatch, certify):
    mb_good, lb_good = mb_entry(AUTHOR, "Sade", _mb_one()), lb_entry(AUTHOR, ARTIST, _lb_one())
    assert mb.verify_slice_entry("Sade", mb_good) is not None
    assert lb.verify_slice_entry(ARTIST, lb_good) is not None
    inflated = []
    monkeypatch.setattr("desktop.p2p.wire.gunzip", lambda *a: inflated.append(a) or b"")
    assert mb.verify_slice_entry("Sade", certify(mb_good)) is None
    assert lb.verify_slice_entry(ARTIST, certify(lb_good)) is None
    assert inflated == []


@pytest.mark.parametrize("author", [None, 7, ["ab"], {"k": 1}])
def test_an_author_that_is_no_key_is_a_refusal_not_an_error(author):
    assert mb.verify_slice_entry("Sade", {**mb_entry(AUTHOR, "Sade", _mb_one()), "author_pubkey": author}) is None
    assert lb.verify_slice_entry(ARTIST, {**lb_entry(AUTHOR, ARTIST, _lb_one()), "author_pubkey": author}) is None


def test_an_lb_row_counts_only_in_its_shape_and_for_its_artist():
    other, rec = str(uuid.UUID(int=78)), str(uuid.UUID(int=900))
    assert lb._row_of([rec, 10, 3, [ARTIST]], ARTIST)
    assert lb._row_of([rec, 10, 3, [other, ARTIST.upper()]], ARTIST)
    assert not lb._row_of([rec, 10, 3, [other]], ARTIST)            # another artist's numbers
    assert not lb._row_of([rec, -1, 3, [ARTIST]], ARTIST)
    assert not lb._row_of([rec, 10, True, [ARTIST]], ARTIST)
    assert not lb._row_of(["not a uuid", 10, 3, [ARTIST]], ARTIST)
    assert not lb._row_of([rec, 10, 3, ARTIST], ARTIST)
    assert not lb._row_of([rec, 10, 3], ARTIST)


@pytest.mark.parametrize("version", ["20991231-235959", "v9", "2026-11-01", "20261301-000000"])
def test_an_lb_version_out_of_its_format_or_ahead_of_today_is_refused(version):
    assert lb.verify_slice_entry(ARTIST, lb_entry(AUTHOR, ARTIST, _lb_one(version))) is None


def test_an_lb_version_past_what_metabrainz_published_is_refused():
    entry = lb_entry(AUTHOR, ARTIST, _lb_one("20261102-000000"))
    assert lb.verify_slice_entry(ARTIST, entry) is not None                  # within a day of today
    assert lb.verify_slice_entry(ARTIST, entry, published="20261101-000000") is None
    assert lb.verify_slice_entry(ARTIST, entry, published="20261102-000000") is not None


def test_the_newest_lb_version_is_never_past_what_metabrainz_published():
    cycle = lb_slice_cycle.LbSliceCycle.__new__(lb_slice_cycle.LbSliceCycle)
    sources = [(SimpleNamespace(base_url="https://a:1"), pub(STRANGER),
                {"lb_dump": "20261102-000000", "lb_slices_version": "20261102-000000"}),
               (SimpleNamespace(base_url="https://b:1"), pub(AUTHOR), {"lb_dump": "20261025-000000"}),
               (SimpleNamespace(base_url="https://c:1"), pub(ANOTHER), {"lb_slices_version": "20991231-235959"})]

    class _Finder:
        async def find(self):
            return sources, True

    cycle._sources = _Finder()
    found, newest = asyncio.run(cycle.find_sources("20261101-000000"))
    assert newest == "20261025-000000"
    assert [node for _, node, _ in found] == [pub(ANOTHER), pub(STRANGER), pub(AUTHOR)]   # replicas first
    # The listing not read yet: no later than a day ahead.
    assert asyncio.run(cycle.find_sources())[1] == "20261102-000000"


def test_a_malformed_mb_entry_is_dropped_not_raised():
    bad = mb_entry(AUTHOR, "Sade", _mb_one(rows={"mb_artist": [[1, 2]]}))   # a row short of its columns
    assert mb.verify_slice_entry("Sade", bad) is not None                   # its signature holds

    class _Peer:
        base_url = "https://dump:1"

        def mb_slice(self, names):
            return {"v": mb.PROTOCOL_VERSION, "slices": {"Sade": bad}, "missing": []}

    client = mb_slice_client.MBSliceClient(_Peer(), db_dsn="postgresql://unused")
    assert client.run(["Sade"]) == {"error": "all slices failed verification"}


def _decoded(subtree: Subtree):
    core = json.loads(mb.name_blob(subtree.name, subtree.one()))
    return core, mb_slice_client._rows(core)


def test_a_slice_of_its_own_subtree_holds():
    s = Subtree("Low")
    s.group(releases=2)
    s.recording()
    partner, shared = 999_001, 999_002             # a member of a credit the artist shares
    s._add("mb_artist", id=partner, gid=str(uuid.uuid4()), name="Partner", sort_name="Partner")
    s._add("mb_artist_credit", id=shared, name="Low & Partner", artist_count=2)
    s._add("mb_artist_credit_name", artist_credit=shared, position=0, artist=s.artist_id, name="Low",
           join_phrase=" & ")
    s._add("mb_artist_credit_name", artist_credit=shared, position=1, artist=partner, name="Partner",
           join_phrase="")
    assert slice_trust.subtree_ok(*_decoded(s))
    zero = json.loads(mb.name_blob("Nobody", {"dump_version": VERSION, "artists_matched": {"Nobody": []},
                                              "truncated": [], "tables": {}}))
    assert slice_trust.subtree_ok(zero, mb_slice_client._rows(zero))


def _reaching_outside(kind: str) -> Subtree:
    s = Subtree("Low")
    s.group()
    stray = 999_100
    if kind == "artist":
        s.outside()
    elif kind == "alias":
        s._add("mb_artist_alias", id=stray, artist=stray + 1, name="Elsewhere")
    elif kind == "group":
        s._add("mb_release_group", id=stray, gid=str(uuid.uuid4()), name="Not Theirs", artist_credit=stray + 1)
    elif kind == "release":
        s._add("mb_release", id=stray, gid=str(uuid.uuid4()), name="Not Theirs", release_group=stray + 1)
    elif kind == "recording":
        s._add("mb_recording", id=stray, gid=str(uuid.uuid4()), name="Not Theirs", artist_credit=stray + 1)
    elif kind == "tag":
        s._add("mb_tag", id=stray, name="unrelated", ref_count=1)
    return s


@pytest.mark.parametrize("kind", ["artist", "alias", "group", "release", "recording", "tag"])
def test_a_row_the_walk_does_not_reach_makes_the_slice_false(kind):
    assert not slice_trust.subtree_ok(*_decoded(_reaching_outside(kind)))


def test_a_slice_is_about_its_matched_artists_and_the_groups_it_credits_them():
    s = Subtree("Low")
    group, _ = s.group()
    s.outside()
    assert slice_trust.subjects(*_decoded(s)) == ([s.gid], [group])


def test_evidence_counts_from_a_dump_at_most_sixty_days_old():
    now = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
    stamp = lambda days: (now - timedelta(days=days)).strftime("%Y%m%d-%H%M%S")
    assert slice_trust.fresh(stamp(59), now) and not slice_trust.fresh(stamp(61), now)
    assert not slice_trust.fresh("", now) and not slice_trust.fresh("v9", now)


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


def test_a_build_for_a_shared_requester_counts_against_the_minute_alone():
    budget = mb.BuildBudget(4, 4)                # one a minute for any one requester
    for _ in range(4):
        assert budget.admit(None)
        budget.done(None, counted=True)
    assert not budget.admit(None) and not budget.admit("198.51.100.1")   # the minute is spent
    budget = mb.BuildBudget(4, 4)
    assert budget.admit("198.51.100.1")
    budget.done("198.51.100.1", counted=True)
    assert not budget.admit("198.51.100.1") and budget.admit(None)


def test_a_matched_artist_without_its_own_row_makes_the_slice_false():
    """It would close the name for good with nothing a reader can see."""
    core = json.loads(mb.name_blob("Ghost", {"dump_version": VERSION, "artists_matched": {"Ghost": [424242]},
                                             "truncated": [], "tables": {}}))
    assert not slice_trust.subtree_ok(core, mb_slice_client._rows(core))


def test_a_matched_artist_without_its_row_is_false_whatever_the_slice_says_it_cut():
    """A build caps releases, media, tracks and recordings, never the artists:
    a slice that says it cut its artist table is malformed, and its matched
    artist without a row false all the same."""
    core = json.loads(mb.name_blob("Ghost", {"dump_version": VERSION, "artists_matched": {"Ghost": [424242]},
                                             "truncated": ["mb_artist"], "tables": {}}))
    assert mb_slice_client._rows(core) is None
    core["truncated"] = ["mb_release"]
    assert mb_slice_client._rows(core) is not None
    assert not slice_trust.subtree_ok(core, mb_slice_client._rows(core))


@pytest.mark.parametrize("field, value", [
    ("artists_matched", {"Sade": [[1]]}),
    ("artists_matched", ["Sade"]),
    ("artists_matched", {"Sade": [True]}),
    ("truncated", 5),
    ("truncated", [1]),
    ("truncated", ["mb_artist"]),
    ("dump_version", 20261101),
])
def test_a_core_field_of_the_wrong_kind_is_a_malformed_slice(field, value):
    s = Subtree("Sade")
    s.group()
    core = json.loads(mb.name_blob(s.name, s.one()))
    core[field] = value
    assert mb_slice_client._rows(core) is None


def test_a_timestamp_postgresql_refuses_is_a_malformed_cell():
    assert mb_slice_client._cell_ok("mb_artist", "last_updated", "2026-11-01T00:00:00+20:00") is False
    assert mb_slice_client._cell_ok("mb_artist", "last_updated", "2026-11-01T00:00:00+05:30") is True
