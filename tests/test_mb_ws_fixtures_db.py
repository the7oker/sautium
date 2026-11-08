"""MusicBrainz's own WS/2 answers, recorded once by MBID through the node's
client (2026-11-08; MusicBrainz's data is CC0): a found and a forwarded
artist, release group and release, a recording, and a page of an artist's
release-group browse with release-group-status=all. Each is kept through
mb_answers as the consumer keeps it, which pins the JSON the canon reads: a
release lookup carries its group with the group's own credit and every
track's recording and credit, a merged MBID answers 200 with the entity it
was merged into, and a browse lists the artist's group of promotional,
bootleg or pseudo-releases only (the website's default overview leaves it
out).

tests/fixtures/musicbrainz/*.json: {"asked": the MBID asked, "body": the
answer}; browse.json holds a page cut to five groups, its `unofficial_only`
the group(s) the dump showed with no official release."""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

psycopg2 = pytest.importorskip("psycopg2")
pytest.importorskip("sqlalchemy")

from tests.mb_fakes import slice_fed  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "musicbrainz"


def _recorded(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture
def node(scratch_dsn, monkeypatch):
    conn, pool, engine = slice_fed(scratch_dsn, monkeypatch)
    yield conn
    conn.close()
    pool.closeall()
    engine.dispose()


def test_a_release_answer_carries_its_group_with_the_groups_own_credit(node):
    import mb_answers
    rec = _recorded("release_found")
    body = rec["body"]
    mb_answers.store("release", rec["asked"], body)

    kept = mb_answers.release(body["id"])
    group = body["release-group"]
    assert kept["group"]["mbid"] == group["id"] and kept["group"]["title"] == group["title"]
    assert [c["artist"] for c in kept["group"]["credit"]] == [c["artist"]["id"]
                                                              for c in group["artist-credit"]]
    assert mb_answers.answer("release_group", group["id"]).status == "found"
    assert len(kept["tracks"]) == sum(len(m.get("tracks") or []) + bool(m.get("pregap"))
                                      + len(m.get("data-tracks") or []) for m in body["media"])
    assert all(t["recording"] and t["credit"] for t in kept["tracks"])
    for t in kept["tracks"]:
        assert mb_answers.answer("recording", t["recording"]).status == "found"


def test_a_merged_mbid_answers_with_the_entity_it_was_merged_into(node):
    import mb_answers
    for kind in ("artist", "release_group", "release"):
        rec = _recorded(f"{kind}_forwarded")
        assert rec["body"]["id"] != rec["asked"]
        mb_answers.store(kind, rec["asked"], rec["body"])
        a = mb_answers.answer(kind, rec["asked"])
        assert (a.status, a.target) == ("forwarded", rec["body"]["id"])
        assert mb_answers.answer(kind, rec["body"]["id"]).status == "found"


def test_an_artist_answer_keeps_its_names(node):
    import mb_answers
    for name in ("artist_found", "artist_forwarded"):
        rec = _recorded(name)
        body = rec["body"]
        mb_answers.store("artist", rec["asked"], body)
        kept = mb_answers.artist(body["id"])
        assert (kept["name"], kept["sort_name"]) == (body["name"], body["sort-name"])
        assert kept["disambiguation"] == body["disambiguation"]
        assert [a["name"] for a in kept["aliases"]] == [a["name"] for a in body["aliases"]
                                                        if a.get("name")]


def test_a_recording_answer_keeps_its_title_length_and_credit(node):
    import mb_answers
    rec = _recorded("recording_found")
    body = rec["body"]
    mb_answers.store("recording", rec["asked"], body)
    kept = mb_answers.recording(body["id"])
    assert (kept["title"], kept["length_ms"]) == (body["title"], body["length"])
    assert [c["artist"] for c in kept["credit"]] == [c["artist"]["id"] for c in body["artist-credit"]]


def test_a_browse_of_every_status_lists_the_group_of_unofficial_releases_only(node):
    import mb_answers
    rec = _recorded("browse")
    groups = rec["release-groups"]
    assert set(rec["unofficial_only"]) <= {g["id"] for g in groups}
    started = datetime.now(timezone.utc)
    mb_answers.store_browse_page(rec["asked"], rec["asked"], started, 0, groups, len(groups))

    assert mb_answers.answer("artist_release_groups", rec["asked"]).status == "found"
    kept = {g["mbid"]: g for g in mb_answers.artist_release_groups(rec["asked"])}
    assert set(kept) == {g["id"] for g in groups}
    for g in groups:
        assert kept[g["id"]]["primary_type"] == g.get("primary-type")
        assert kept[g["id"]]["secondary_types"] == sorted(t for t in g.get("secondary-types") or [] if t)
        assert [c["artist"] for c in kept[g["id"]]["credit"]] == [c["artist"]["id"]
                                                                   for c in g["artist-credit"]]
