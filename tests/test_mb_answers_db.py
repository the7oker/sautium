"""MusicBrainz's answers on a node with catalogue slices and no dump
(backend/mb_answers.py, backend/musicbrainz.py): asked by MBID only, at
MusicBrainz's pace and User-Agent, kept by MBID — a merged MBID answered with
its target, a 404 as unknown, a silence as no answer at all — and asked in
order: the person's own act first, authors in turn within a priority (a
question counted under the author whose kept slice raised it), each waiting
subject woken once its last answer is in. One item's failure backs that item
off and the queue goes on; a browse goes a page a step; a node that does not
ask queues nothing and checks no slice.

On a scratch PostgreSQL built from the migrations; the web service is
tests/mb_fakes.FakeMusicBrainz, and the queue is worked in the test's thread."""

import gzip
import json
import secrets
import sys
import threading
import time
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

psycopg2 = pytest.importorskip("psycopg2")
pytest.importorskip("sqlalchemy")

from tests.mb_fakes import FakeMusicBrainz, Slice, credit, drain, mbid, slice_fed  # noqa: E402


@pytest.fixture
def node(scratch_dsn, monkeypatch):
    conn, pool, engine = slice_fed(scratch_dsn, monkeypatch)
    import mb_answers
    monkeypatch.setattr(mb_answers, "_wakers", {})
    yield conn
    conn.close()
    pool.closeall()
    engine.dispose()


@pytest.fixture
def mb(monkeypatch):
    return FakeMusicBrainz().install(monkeypatch)


def test_an_answer_is_kept_by_mbid_and_a_merged_one_names_the_entity_it_leads_to(node, mb):
    import mb_answers
    band, old, member = mbid(), mbid(), mbid()
    mb.artist(band, "Portishead", aliases=["Portis Head"], genres=["Trip Hop"], members=[member],
              urls=["https://portishead.bandcamp.com/"], sort_name="Portishead", area="United Kingdom")
    mb.merge("artist", old, band)
    mb_answers.enqueue([("artist", old)], mb_answers.PRIORITY_OWNED)

    assert drain() == 1
    a = mb_answers.answer("artist", old)
    assert (a.status, a.target, a.fresh) == ("forwarded", band, True)
    assert mb_answers.answer("artist", band).status == "found"
    kept = mb_answers.artist(band)
    assert kept["name"] == "Portishead" and kept["area"] == "United Kingdom"
    assert [x["name"] for x in kept["aliases"]] == ["Portis Head"]
    assert kept["genres"] == {"trip hop": 1}
    assert kept["members"] == [(member, band)]
    assert kept["urls"] == ["https://portishead.bandcamp.com/"]
    assert mb.asked == [("artist", old)]


def test_a_404_is_unknown_and_a_silence_is_no_answer(node, mb):
    import mb_answers
    gone = mbid()
    mb_answers.enqueue([("recording", gone)], mb_answers.PRIORITY_OWNED)
    drain()
    assert mb_answers.answer("recording", gone).status == "unknown"

    quiet = mbid()
    mb.down = True
    mb_answers.enqueue([("recording", quiet)], mb_answers.PRIORITY_OWNED)
    assert drain() == 0
    assert mb_answers.answer("recording", quiet) is None
    with node.cursor() as cur:
        cur.execute("SELECT mbid::text FROM mbws_queue")
        assert cur.fetchall() == [(quiet,)]


def test_a_release_answer_carries_its_group_its_tracklist_and_its_recordings(node, mb):
    import mb_answers
    artist, group, rel, rec1, rec2 = (mbid() for _ in range(5))
    by = credit(("Low", artist))
    mb.artist(artist, "Low")
    mb.release_group(group, "Things We Lost in the Fire", by, first_release_date="2001-01-30")
    mb.release(rel, "Things We Lost in the Fire", group, by, [
        {"title": "Sunflower", "length": 284000, "recording": rec1},
        {"title": "Whitetail", "length": 260000, "recording": rec2},
    ], barcode="4005902618429", urls=["https://low.bandcamp.com/album/things"])
    mb_answers.enqueue([("release", rel)], mb_answers.PRIORITY_DISCOVERY)
    drain()

    got = mb_answers.release(rel)
    assert (got["title"], got["barcode"], got["release_group"]) == (
        "Things We Lost in the Fire", "4005902618429", group)
    assert got["group"]["credit"][0]["artist"] == artist
    assert [(t["position"], t["title"], t["length_ms"], t["recording"]) for t in got["tracks"]] == [
        (1, "Sunflower", 284000, rec1), (2, "Whitetail", 260000, rec2)]
    assert got["tracks"][0]["credit"][0]["name"] == "Low"
    assert got["urls"] == ["https://low.bandcamp.com/album/things"]
    # what the release carried is answered too, and not asked again
    assert mb_answers.answer("recording", rec1).status == "found"
    assert mb_answers.recording(rec2)["length_ms"] == 260000
    assert mb_answers.answer("release_group", group).status == "found"


def test_an_artists_groups_are_browsed_under_its_own_mbid(node, mb):
    import mb_answers
    artist, old, g1, g2 = (mbid() for _ in range(4))
    mb.artist(artist, "Boards of Canada")
    mb.release_group(g1, "Geogaddi", credit(("Boards of Canada", artist)))
    mb.release_group(g2, "Twoism", credit(("Boards of Canada", artist)), primary="EP")
    mb.merge("artist", old, artist)
    mb_answers.enqueue([("artist_release_groups", old)], mb_answers.PRIORITY_DISCOVERY)
    drain()

    assert mb.asked == [("artist", old), ("artist_release_groups", artist)]
    a = mb_answers.answer("artist_release_groups", old)
    assert (a.status, a.target) == ("forwarded", artist)
    groups = mb_answers.artist_release_groups(artist)
    assert sorted((g["title"], g["primary_type"]) for g in groups) == [
        ("Geogaddi", "Album"), ("Twoism", "EP")]


def test_the_persons_own_act_goes_first_and_authors_take_turns(node, mb):
    import mb_answers
    flood = [mbid() for _ in range(3)]
    other = mbid()
    person = mbid()
    for m in flood + [other, person]:
        mb.recording(m, "x", 1000, credit(("y", mbid())))
    mb_answers.enqueue([("recording", m) for m in flood], mb_answers.PRIORITY_DISCOVERY,
                       author="a" * 64)
    mb_answers.enqueue([("recording", other)], mb_answers.PRIORITY_DISCOVERY, author="b" * 64)
    mb_answers.enqueue([("recording", person)], mb_answers.PRIORITY_PERSON)
    drain()

    order = [m for _, m in mb.asked]
    # one of the flooding author's, then the other author's one, then the rest
    assert order[0] == person and order[2] == other
    assert {order[1], order[3], order[4]} == set(flood)


def test_a_subject_is_woken_once_every_answer_it_waits_on_is_in(node, mb, monkeypatch):
    import mb_answers
    woken = []
    mb_answers.on_ready("discography", woken.append)
    first, second = mbid(), mbid()
    for m in (first, second):
        mb.recording(m, "x", 1000, credit(("y", mbid())))
    asks = mb_answers.Asks("discography", "artist-1", mb_answers.PRIORITY_DISCOVERY)
    assert asks.need("recording", first) is None and asks.need("recording", second) is None
    asks.flush()
    assert mb_answers.waiting("discography", "artist-1")

    assert drain(limit=1) == 1
    assert woken == []
    drain()
    assert woken == [["artist-1"]]
    assert not mb_answers.waiting("discography", "artist-1")
    assert asks.need("recording", first) is not None


def test_ask_now_returns_as_soon_as_the_answer_is_kept(node, mb, monkeypatch):
    import mb_answers
    artist = mbid()
    mb.artist(artist, "Talk Talk")
    queued = threading.Event()
    enqueue = mb_answers.enqueue

    def recording_enqueue(*a, **k):
        enqueue(*a, **k)
        queued.set()
    monkeypatch.setattr(mb_answers, "enqueue", recording_enqueue)
    result = {}
    t = threading.Thread(target=lambda: result.setdefault(
        "ok", mb_answers.ask_now([("artist", artist)], timeout=30)))
    t.start()
    assert queued.wait(10)
    drain()
    t.join(10)
    assert result == {"ok": True}


def test_a_refuted_binding_cannot_be_written_again_by_any_tier(node):
    from canon import confirm
    from uuid_utils import artist_uuid
    aid, m = str(artist_uuid("Mouse on Mars")), mbid()
    with node.cursor() as cur:
        cur.execute("INSERT INTO artists (id, name) VALUES (%s, 'Mouse on Mars')", (aid,))
        cur.execute("INSERT INTO artist_mbids (mbid, artist_id, confidence) VALUES (%s, %s, 'phantom')",
                    (m, aid))
    confirm.withdraw(aid, m)
    with node.cursor() as cur:
        cur.execute("INSERT INTO artist_mbids (mbid, artist_id, confidence) "
                    "VALUES (%s, %s, 'overlap_verified') ON CONFLICT (mbid) DO NOTHING", (m, aid))
        cur.execute("SELECT count(*) FROM artist_mbids WHERE mbid = %s", (m,))
        assert cur.fetchone() == (0,)
        assert confirm.refuted(aid, m)


def test_only_a_node_with_slices_and_no_dump_asks(node, monkeypatch):
    import mb_answers
    import mb_backend
    assert mb_answers.required()
    with node.cursor() as cur:
        cur.execute("INSERT INTO user_settings (key, value) VALUES ('musicbrainz.db_version', '\"x\"')")
    assert not mb_answers.required()
    with node.cursor() as cur:
        cur.execute("DELETE FROM user_settings WHERE key = 'musicbrainz.db_version'")
    monkeypatch.setattr(mb_backend, "LOCAL_DUMP", False)
    assert not mb_answers.required()


def test_a_node_that_stops_asking_wakes_whatever_waited(node, mb, monkeypatch):
    import mb_answers
    import mb_backend
    woken = []
    mb_answers.on_ready("owned_artist", woken.append)
    asks = mb_answers.Asks("owned_artist", "artist-2", mb_answers.PRIORITY_OWNED)
    asks.need("artist", mbid())
    asks.flush()
    monkeypatch.setattr(mb_backend, "LOCAL_DUMP", False)
    mb_answers._Worker()._drop_all()
    assert woken == [["artist-2"]]
    assert mb.asked == []


def _next_step():
    """One step of the consumer on the item it would take next."""
    import mb_answers
    from db_pool import db_query_one
    row = db_query_one(mb_answers._NEXT_SQL, {"last": ""})
    return (row["kind"], row["mbid"]) + mb_answers._step(row["kind"], row["mbid"])


def _queued(cur, mbid_):
    cur.execute("""SELECT attempts, not_before IS NOT NULL AND not_before > now(), browse_offset
                     FROM mbws_queue WHERE mbid = %s""", (mbid_,))
    return cur.fetchone()


def test_one_items_failure_backs_it_off_and_the_queue_goes_on(node, mb):
    import mb_answers
    import musicbrainz
    stuck, other = mbid(), mbid()
    for m in (stuck, other):
        mb.recording(m, "x", 1000, credit(("y", mbid())))
    mb.fail[("recording", stuck)] = [musicbrainz.Failed("HTTP 500")]
    mb_answers.enqueue([("recording", stuck)], mb_answers.PRIORITY_OWNED)
    mb_answers.enqueue([("recording", other)], mb_answers.PRIORITY_OWNED)

    assert drain() == 1
    assert mb_answers.answer("recording", other).status == "found"
    assert mb_answers.answer("recording", stuck) is None
    with node.cursor() as cur:
        cur.execute("""SELECT attempts, not_before > now() + interval '30 seconds',
                              not_before < now() + interval '2 minutes'
                         FROM mbws_queue WHERE mbid = %s""", (stuck,))
        assert cur.fetchone() == (1, True, True)
        cur.execute("UPDATE mbws_queue SET not_before = now() WHERE mbid = %s", (stuck,))
    assert drain() == 1
    assert mb_answers.answer("recording", stuck).status == "found"


def test_the_pace_rule_pauses_the_queue_and_holds_no_item_back(node, mb):
    import mb_answers
    import musicbrainz
    m = mbid()
    mb.recording(m, "x", 1000, credit(("y", mbid())))
    mb.fail[("recording", m)] = [musicbrainz.RateLimited("HTTP 503", 4.0)]
    mb_answers.enqueue([("recording", m)], mb_answers.PRIORITY_OWNED)
    worker = mb_answers._Worker()
    before = time.monotonic()
    worker._one("recording", m, "")
    assert 3.0 < worker._resume_at - before <= 4.5
    with node.cursor() as cur:
        assert _queued(cur, m) == (0, False, 0)
    worker._one("recording", m, "")
    assert mb_answers.answer("recording", m).status == "found"


def test_different_items_failing_in_a_row_pause_the_whole_queue(node, mb):
    import mb_answers
    import musicbrainz
    items = [mbid() for _ in range(mb_answers.FAILED_IN_A_ROW)]
    for m in items:
        mb.fail[("recording", m)] = [musicbrainz.Failed("HTTP 502")]
    mb_answers.enqueue([("recording", m) for m in items], mb_answers.PRIORITY_OWNED)
    worker = mb_answers._Worker()
    for m in items[:-1]:
        worker._one("recording", m, "")
        assert worker._resume_at == 0.0
    worker._one("recording", items[-1], "")
    assert worker._resume_at > time.monotonic() + mb_answers.QUEUE_PAUSE_S / 3


def test_an_answer_that_cannot_be_kept_holds_its_item_back_and_wakes_no_one(node, mb, monkeypatch):
    import mb_answers
    woken = []
    mb_answers.on_ready("discography", woken.append)
    m = mbid()
    mb.recording(m, "x", 1000, credit(("y", mbid())))
    asks = mb_answers.Asks("discography", "artist-4", mb_answers.PRIORITY_DISCOVERY)
    asks.need("recording", m)
    asks.flush()

    def cannot_keep(kind, mbid_, data):
        raise RuntimeError("cannot keep it")
    monkeypatch.setattr(mb_answers, "store", cannot_keep)
    for attempt in range(1, 4):
        assert drain() == 0
        with node.cursor() as cur:
            assert _queued(cur, m) == (attempt, True, 0)
            cur.execute("UPDATE mbws_queue SET not_before = now() WHERE mbid = %s", (m,))
    assert woken == []
    assert mb_answers.waiting("discography", "artist-4")


def test_what_a_release_settled_leaves_the_queue_unasked_and_wakes_its_waiters(node, mb):
    import mb_answers
    woken = []
    mb_answers.on_ready("credit_head", woken.append)
    artist, group, rel, rec = (mbid() for _ in range(4))
    by = credit(("Low", artist))
    mb.release_group(group, "Secret Name", by)
    mb.release(rel, "Secret Name", group, by, [{"title": "Starfire", "length": 300000, "recording": rec}])
    asks = mb_answers.Asks("credit_head", "artist-5", mb_answers.PRIORITY_LISTENING)
    asks.need("recording", rec)
    asks.flush()
    mb_answers.enqueue([("release", rel)], mb_answers.PRIORITY_OWNED)

    assert drain() == 2
    assert mb.asked == [("release", rel)]
    assert woken == [["artist-5"]]
    with node.cursor() as cur:
        cur.execute("SELECT count(*) FROM mbws_queue")
        assert cur.fetchone() == (0,)


def test_a_long_browse_goes_a_page_a_step_lets_a_person_first_and_resumes(node, mb):
    import mb_answers
    import musicbrainz
    artist, person = mbid(), mbid()
    mb.artist(artist, "Johann Sebastian Bach")
    mb.recording(person, "Air", 300000, credit(("Bach", artist)))
    mb.page = 2
    groups = [mb.release_group(mbid(), f"Cantata {i}", credit(("Bach", artist))) for i in range(5)]
    mb.fail[("artist_release_groups", artist, 4)] = [musicbrainz.Failed("HTTP 504")]
    mb_answers.enqueue([("artist_release_groups", artist)], mb_answers.PRIORITY_DISCOVERY)

    assert _next_step() == ("artist_release_groups", artist, "progress", 0)   # the artist first
    assert _next_step()[2:] == ("progress", 0)                           # groups 0-1
    assert mb_answers.answer("artist_release_groups", artist) is None
    mb_answers.enqueue([("recording", person)], mb_answers.PRIORITY_PERSON)
    assert _next_step() == ("recording", person, "answered", 1)          # the person's, between pages
    assert _next_step()[2:] == ("progress", 0)                           # groups 2-3
    assert _next_step()[2:] == ("failed", 0)                             # groups 4-: failed
    with node.cursor() as cur:
        assert _queued(cur, artist) == (1, True, 4)
        cur.execute("UPDATE mbws_queue SET not_before = now() WHERE mbid = %s", (artist,))
    assert _next_step()[2:] == ("answered", 1)                           # resumed at 4
    assert [k for k, _ in mb.asked] == ["artist", "artist_release_groups", "recording",
                                        "artist_release_groups", "artist_release_groups",
                                        "artist_release_groups"]
    a = mb_answers.answer("artist_release_groups", artist)
    assert (a.status, a.target) == ("found", artist)
    assert sorted(g["mbid"] for g in mb_answers.artist_release_groups(artist)) == sorted(groups)


def test_a_refreshed_browse_drops_what_musicbrainz_stopped_listing_only_at_its_end(node, mb):
    import mb_answers
    artist = mbid()
    mb.artist(artist, "Can")
    mb.page = 2
    first, *_ = [mb.release_group(mbid(), t, credit(("Can", artist)))
                 for t in ("Monster Movie", "Tago Mago", "Ege Bamyasi")]
    mb_answers.enqueue([("artist_release_groups", artist)], mb_answers.PRIORITY_DISCOVERY)
    assert drain() == 1
    assert len(mb_answers.artist_release_groups(artist)) == 3

    with node.cursor() as cur:
        cur.execute("UPDATE mbws_answers SET answered_at = now() - interval '31 days'")
    mb.credited[artist].remove(first)
    mb.release_group(mbid(), "Future Days", credit(("Can", artist)))
    mb_answers.enqueue([("artist_release_groups", artist)], mb_answers.PRIORITY_DISCOVERY)

    def listed(cur):
        cur.execute("""SELECT g.title FROM mbws_artist_release_groups a
                         JOIN mbws_release_groups g ON g.mbid = a.release_group
                        WHERE a.artist = %s ORDER BY 1""", (artist,))
        return [r[0] for r in cur.fetchall()]
    assert _next_step()[2] == "progress"                                 # the artist again
    assert _next_step()[2] == "progress"                                 # its first page
    with node.cursor() as cur:
        assert listed(cur) == ["Ege Bamyasi", "Monster Movie", "Tago Mago"]
    assert _next_step()[2] == "answered"
    with node.cursor() as cur:
        assert listed(cur) == ["Ege Bamyasi", "Future Days", "Tago Mago"]


def test_a_question_is_counted_under_the_author_whose_kept_slice_raised_it(node):
    import mb_answers
    author, suspect = secrets.token_hex(32), secrets.token_hex(32)
    artist, group, rec, other = mbid(), mbid(), mbid(), mbid()
    with node.cursor() as cur:
        for key, by, held, artists, groups in ((f"kept-{author[:8]}", author, False, [artist], [group]),
                                               (f"held-{suspect[:8]}", suspect, True, [other], [])):
            cur.execute("""INSERT INTO mb_slice_blobs (name_key, dump_version, author_pubkey, sig,
                                                       blob_gz, suspect)
                           VALUES (%s, '20261104-002222', %s, %s, %s, %s)""",
                        (key, by, "0" * 128, b"blob", held))
            cur.execute("""INSERT INTO slice_index (family, key, author, relay, payload_sha256,
                                                    dump_version, subject_artists, subject_groups)
                           VALUES ('mb', %s, %s, %s, %s, '20261104-002222', %s::uuid[], %s::uuid[])""",
                        (key, by, by, "0" * 64, artists, groups))
    mb_answers.enqueue([("artist", artist), ("artist_release_groups", artist),
                        ("release_group", group), ("recording", rec), ("artist", other)],
                       mb_answers.PRIORITY_OWNED)
    with node.cursor() as cur:
        cur.execute("SELECT kind::text, mbid::text, author FROM mbws_queue")
        authors = {(k, m): a for k, m, a in cur.fetchall()}
    assert authors == {("artist", artist): author, ("artist_release_groups", artist): author,
                       ("release_group", group): author, ("recording", rec): None,
                       ("artist", other): None}


def _blocked_on_a_lock(conn, timeout: float = 10.0) -> None:
    """Until a session of this database waits on a lock — the one test that
    must see another session block, and PostgreSQL tells it no other way."""
    deadline = time.monotonic() + timeout
    with conn.cursor() as cur:
        while time.monotonic() < deadline:
            cur.execute("""SELECT 1 FROM pg_stat_activity
                            WHERE datname = current_database() AND wait_event_type = 'Lock'""")
            if cur.fetchone():
                return
            time.sleep(0.02)
    raise AssertionError("no session blocked")


def test_a_subject_queuing_while_its_item_is_answered_is_woken(node, mb, scratch_dsn):
    import mb_answers
    from desktop.p2p import mb_asks
    woken = []
    mb_answers.on_ready("phantom", woken.extend)
    m = mbid()
    mb.recording(m, "x", 1000, credit(("y", mbid())))
    asks = mb_answers.Asks("phantom", "first", mb_answers.PRIORITY_DISCOVERY)
    asks.need("recording", m)
    asks.flush()
    mb_answers.store("recording", m, mb.lookup("recording", m))
    late = psycopg2.connect(scratch_dsn)
    mb_asks.enqueue(late, [("recording", m)], mb_answers.PRIORITY_DISCOVERY, "phantom", "second")
    answering = threading.Thread(target=mb_answers._answered_item, args=("recording", m))
    answering.start()
    _blocked_on_a_lock(node)
    late.commit()
    late.close()
    answering.join(10)
    assert sorted(woken) == ["first", "second"]


def _verified(cur, pubkey):
    cur.execute("""INSERT INTO p2p_identities (pubkey, cert_v, method, issued_at, difficulty,
                                               params_version, issuer, cert_sig, status)
                   VALUES (%s, 4, 'pow', now(), 1, 1, 'test', 'sig', 'verified')""", (pubkey,))


def test_only_a_node_that_asks_checks_a_slice_or_passes_on_another_authors(node):
    from desktop.p2p import slice_checks
    author, relay, own = (secrets.token_hex(32) for _ in range(3))
    artist, rec = mbid(), mbid()
    core = {"artist_mbid": artist, "dump_version": "20261101-000000", "recordings": [[rec, 40, 3, [artist]]]}
    with node.cursor() as cur:
        _verified(cur, author)
        cur.execute("""INSERT INTO lb_slice_blobs (artist_mbid, dump_version, author_pubkey, sig,
                                                   blob_gz, relay)
                       VALUES (%s, '20261101-000000', %s, %s, %s, %s)""",
                    (artist, author, "0" * 128, gzip.compress(json.dumps(core).encode()), relay))
        cur.execute("""INSERT INTO lb_slice_blobs (artist_mbid, dump_version, author_pubkey, sig, blob_gz)
                       VALUES (%s, '20261101-000000', %s, %s, %s)""",
                    (mbid(), own, "0" * 128, gzip.compress(json.dumps(core).encode())))

    def queued(cur):
        cur.execute("SELECT kind::text, mbid::text FROM mbws_queue")
        return cur.fetchall()
    # no catalogue here: nothing to check against
    with node.cursor() as cur:
        assert slice_checks.servable(node, "lb", artist, own) is False
        slice_checks.request_check(node, "lb", artist)
        assert queued(cur) == []
        assert slice_checks.checkable_count(node, "lb", own) == 1
        # the full dump here: its own builds only
        Slice(cur).artist(mbid(), "Someone")
        cur.execute("INSERT INTO user_settings (key, value) VALUES ('musicbrainz.db_version', '\"x\"')")
        assert slice_checks.servable(node, "lb", artist, own) is False
        slice_checks.request_check(node, "lb", artist)
        assert slice_checks.evaluate(node, "lb", artist) is None
        assert queued(cur) == []
        assert slice_checks.checkable_count(node, "lb", own) == 1
        # catalogue slices and no dump: it checks
        cur.execute("DELETE FROM user_settings WHERE key = 'musicbrainz.db_version'")
        cur.execute("INSERT INTO mb_slice_fetches (name_key, matched_ids) VALUES ('someone', 1)")
        assert slice_checks.servable(node, "lb", artist, own) is None
        slice_checks.request_check(node, "lb", artist)
        assert sorted(queued(cur)) == sorted([("artist", artist), ("recording", rec)])
        assert slice_checks.checkable_count(node, "lb", own) == 2


def test_a_node_that_stops_asking_drops_its_queue_once_and_wakes_no_check(node, monkeypatch):
    import mb_answers
    import mb_backend
    from desktop.p2p import mb_asks
    woken = {"owned_artist": [], "slice_check": []}
    mb_answers.on_ready("owned_artist", woken["owned_artist"].append)

    def check_asks_again(keys):
        woken["slice_check"].append(keys)
        if len(woken["slice_check"]) < 3:
            mb_asks.enqueue(node, [("recording", mbid())], 5, "slice_check", "lb:again")
    mb_answers.on_ready("slice_check", check_asks_again)
    asks = mb_answers.Asks("owned_artist", "artist-3", mb_answers.PRIORITY_OWNED)
    asks.need("artist", mbid())
    asks.flush()
    mb_asks.enqueue(node, [("recording", mbid())], 5, "slice_check", "lb:first")
    monkeypatch.setattr(mb_backend, "LOCAL_DUMP", False)

    mb_answers._Worker()._drop_all()
    assert woken == {"owned_artist": [["artist-3"]], "slice_check": []}
    with node.cursor() as cur:
        cur.execute("SELECT count(*) FROM mbws_queue")
        assert cur.fetchone() == (0,)
    # and it queues nothing more
    mb_answers.enqueue([("artist", mbid())], mb_answers.PRIORITY_OWNED)
    assert mb_answers.ask_now([("artist", mbid())], timeout=30) is False
    with node.cursor() as cur:
        cur.execute("SELECT count(*) FROM mbws_queue")
        assert cur.fetchone() == (0,)


def test_a_completed_dump_load_forgets_the_refutations_and_the_pause(node, monkeypatch, tmp_path,
                                                                     scratch_dsn):
    import api_cooldown
    import mb_dump_load
    import musicbrainz
    from canon import confirm, genres
    from uuid_utils import artist_uuid
    aid, m = str(artist_uuid("Broadcast")), mbid()
    with node.cursor() as cur:
        cur.execute("INSERT INTO artists (id, name) VALUES (%s, 'Broadcast')", (aid,))
    confirm.withdraw(aid, m)
    api_cooldown.arm(musicbrainz.COOLDOWN_SOURCE, "test")
    monkeypatch.setattr(mb_dump_load, "latest_version", lambda: "20261104-002222")
    monkeypatch.setattr(mb_dump_load, "loaded_version", lambda: None)
    monkeypatch.setattr(mb_dump_load, "download", lambda version, archive, cb: str(tmp_path / archive))
    monkeypatch.setattr(mb_dump_load, "stream_load", lambda archive, version, cb, **kw: None)
    monkeypatch.setattr(mb_dump_load, "load_genre_list", lambda cb: 0)
    monkeypatch.setattr(genres, "refresh_album_mb_genres", lambda cb: None)
    monkeypatch.setattr(mb_dump_load, "VERSION_PATH", str(tmp_path / "VERSION"))
    listen = psycopg2.connect(scratch_dsn)
    listen.autocommit = True
    with listen.cursor() as cur:
        cur.execute("LISTEN sautium_mbws")

    mb_dump_load.download_and_load()
    assert not confirm.refuted(aid, m)
    assert api_cooldown.cooling_down(musicbrainz.COOLDOWN_SOURCE) is None
    with node.cursor() as cur:
        cur.execute("SELECT count(*) FROM external_api_cooldown WHERE source = 'musicbrainz'")
        assert cur.fetchone() == (0,)
    listen.poll()
    assert [n.channel for n in listen.notifies] == ["sautium_mbws"]
    listen.close()


def test_a_persons_ask_clears_the_backoff_whatever_its_earlier_priority(node, mb):
    """A person asks again, at the person's priority, for what failed when they
    asked before: it is asked now, not after the backoff its failure earned."""
    import mb_answers
    import musicbrainz
    item = mbid()
    mb.recording(item, "x", 1000, credit(("y", mbid())))
    mb.fail[("recording", item)] = [musicbrainz.Failed("HTTP 500")]
    mb_answers.enqueue([("recording", item)], mb_answers.PRIORITY_PERSON)
    assert drain() == 0
    with node.cursor() as cur:
        assert _queued(cur, item)[:2] == (1, True)

    mb_answers.enqueue([("recording", item)], mb_answers.PRIORITY_PERSON)

    with node.cursor() as cur:
        assert _queued(cur, item)[:2] == (1, False)
    assert drain() == 1
    assert mb_answers.answer("recording", item).status == "found"


def test_the_consumer_tries_again_after_a_failure_it_did_not_expect(monkeypatch):
    """A dropped connection wakes no one: after an unexpected failure the
    consumer waits a growing delay, not for a wake that may never come."""
    import mb_answers

    class Stop(Exception):
        pass

    waits = []

    def wait(timeout=None):
        waits.append(timeout)
        if len(waits) == 2:
            raise Stop

    def required():
        raise RuntimeError("the connection went away")

    worker = mb_answers._Worker()
    monkeypatch.setattr(worker._event, "wait", wait)
    monkeypatch.setattr(mb_answers, "required", required)
    with pytest.raises(Stop):
        worker._run()
    assert waits == [mb_answers.QUEUE_PAUSE_S, 2 * mb_answers.QUEUE_PAUSE_S]
