"""Clashes MusicBrainz settles, and the catalogue rebuilt from the kept
slices, on a real PostgreSQL built from the migrations — skipped without a
cluster (desktop/mb_slice_client.py, desktop/p2p/slice_checks.py,
slice_rebuild.py, lb_slice_cycle.py):

- a clash decides which rows land and blames no one: a held blob that loses
  is kept out of every rebuild whoever served it — its author itself
  included — its name asked again, and the rebuild the settlement asks for
  lands the winner; a release clash finds its carrier by the row; a forger's
  clash against an honest blob whose release MusicBrainz removed since
  convicts no one; a forged dictionary row is a stale row, never a block;
- a dropped blob's clashes go with it: a later honest blob of the name is
  judged on its own rows;
- a rebuild that waited on a full load finds the dump and leaves it alone; a
  rebuild that fails leaves the catalogue and the request as they were;
- an answer whose relay or whose author is convicted by an entry of it lands
  nothing of them, whatever came first in it;
- a rebuild keeps the shop pages only, as the import does;
- the LB exchange takes nothing from a source penalized since its list of
  sources was made."""

import asyncio
import sys
import threading
import uuid
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

psycopg2 = pytest.importorskip("psycopg2")

REPO = Path(__file__).resolve().parents[2]
for _p in (REPO, REPO / "backend"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from desktop import mb_slice_client  # noqa: E402
from desktop.mb_slice_client import MBSliceClient  # noqa: E402
from desktop.p2p import (lb_slice_cycle, mb_slice_cycle, mb_slice_queries, slice_checks,  # noqa: E402
                         slice_rebuild, slice_trust)
from desktop.p2p.sync_walk import DeadAddresses  # noqa: E402
from tests.slice_certs import Peer, Subtree, lb_entry, mb_entry, pub, trust  # noqa: E402

_STATE = ("mb_slice_blobs", "mb_slice_fetches", "lb_slice_blobs", "lb_slice_fetches", "slice_index",
          "slice_evidence", "slice_tally", "slice_penalties", "slice_check_plans", "slice_clashes",
          "slice_clash_wins", "slice_burned_mailboxes", "p2p_identities", "p2p_node_bans", "mbws_queue",
          "mbws_answers", "mbws_artists", "mbws_artist_aliases", "mbws_release_groups",
          "mbws_artist_release_groups", "mbws_releases", "mbws_recordings",
          "mbws_credits", "lb_recording", "lb_artist", "artists") + tuple(mb_slice_queries.SLICE_TABLES)
_SETTINGS = ["musicbrainz.db_version", "listenbrainz.db_version", "mb_slice.published", "lb_slice.published",
             slice_rebuild.PENDING_KEY]
SHOP = "https://heldname.bandcamp.com/album/first"


@pytest.fixture(autouse=True)
def _authority(monkeypatch):
    trust(monkeypatch)


@pytest.fixture
def conn(scratch_dsn):
    c = psycopg2.connect(scratch_dsn, options="-c timezone=UTC")
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute("TRUNCATE " + ", ".join(_STATE) + " CASCADE")
        cur.execute("DELETE FROM user_settings WHERE key = ANY(%s)", (_SETTINGS,))
    yield c
    c.close()


def _key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def _offer_many(dsn, entries, relay):
    """One answer of `relay` carrying `entries` ({name: entry}), names in order."""
    client = MBSliceClient(Peer(entries, pub(relay)), db_dsn=dsn)
    try:
        return client.run(list(entries))
    finally:
        client.close()


def _offer(dsn, key, subtree, relay=None):
    return _offer_many(dsn, {subtree.name: mb_entry(key, subtree.name, subtree.one())}, relay or key)


def _honest(name, **kw):
    s = Subtree(name, **kw)
    s.group()
    return s


def _outside(name):
    s = _honest(name)
    s.outside()
    return s


def _rows(conn, sql, *params):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _count(conn, table):
    return _rows(conn, f"SELECT count(*) FROM {table}")[0][0]


def _gids(conn, table):
    return {r[0] for r in _rows(conn, f"SELECT gid::text FROM {table}")}


def _answer(conn, kind, mbid, status="found"):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO mbws_answers (kind, mbid, status, target) VALUES (%s, %s, %s, %s)",
                    (kind, mbid, status, None if status == "unknown" else mbid))


def _flags(conn):
    return {r[0]: (r[1], r[2]) for r in _rows(conn, "SELECT name_key, suspect, contested FROM mb_slice_blobs")}


# --------------------------------------------------------------------------
# Clashes
# --------------------------------------------------------------------------

def _void(conn):
    return {r[0] for r in _rows(conn, "SELECT name_key FROM mb_slice_fetches WHERE void")}


def test_a_held_blob_its_author_served_itself_loses_a_clash_blamed_on_no_one_and_the_winner_lands(
        conn, scratch_dsn):
    held_author, incoming_author = _key(), _key()
    held = _honest("Held Name")
    _offer(scratch_dsn, held_author, held)                         # straight from its author
    incoming = Subtree("Incoming Name", artist_id=held.artist_id)  # the held artist's id, another MBID
    incoming_group, _ = incoming.group()
    assert _offer(scratch_dsn, incoming_author, incoming)["contested"] == 1
    _answer(conn, "artist", incoming.gid)
    _answer(conn, "artist", held.gid, "unknown")
    slice_checks.evaluate(conn, "mb", "incoming name")
    assert _count(conn, "slice_evidence") == 0 and _count(conn, "slice_penalties") == 0
    assert _flags(conn)["held name"] == (True, True)                # out of every rebuild
    assert _void(conn) == {"held name"}                             # asked again, of another author first
    assert mb_slice_cycle.reopened_authors(scratch_dsn, ["Held Name"]) == {"Held Name": pub(held_author)}
    assert slice_rebuild.run_pending(conn)["landed"] == 1
    assert _flags(conn)["incoming name"] == (False, False)
    assert _gids(conn, "mb_artist") == {incoming.gid} and _gids(conn, "mb_release_group") == {incoming_group}
    assert slice_rebuild.run_pending(conn) is None                  # settled for good


def test_a_release_clash_finds_the_blob_that_carried_the_false_row(conn, scratch_dsn):
    held_author, incoming_author = _key(), _key()
    held = Subtree("Held Release")
    _group, (held_release,) = held.group()
    _offer(scratch_dsn, held_author, held)
    release_id = held.tables["mb_release"][0][0]
    incoming = Subtree("Incoming Release")
    incoming.group(releases=0)
    group_id = incoming.tables["mb_release_group"][0][0]
    other = str(uuid.uuid4())
    incoming._add("mb_release", id=release_id, gid=other, name="Another Album",
                  artist_credit=incoming.credit, release_group=group_id)
    assert _offer(scratch_dsn, incoming_author, incoming)["contested"] == 1
    assert _rows(conn, "SELECT entity::text, incoming::text, held::text FROM slice_clashes") == [
        ("mb_release", other, held_release)]
    _answer(conn, "release", other)
    _answer(conn, "release", held_release, "unknown")
    slice_checks.evaluate(conn, "mb", "incoming release")
    assert _count(conn, "slice_evidence") == 0
    assert _flags(conn)["held release"] == (True, True)
    slice_rebuild.run_pending(conn)
    assert _gids(conn, "mb_release") == {other}


def test_a_forgers_clash_against_a_release_musicbrainz_removed_since_convicts_no_one(conn, scratch_dsn):
    """An honest fresh blob carries a release MusicBrainz has removed since;
    a forger serves a slice that puts any real MBID on that release's id. The
    clash tells which row lands — never who lied: the honest author and the
    node that passed its blob on carry nothing, however many such rows."""
    master, relay, forger = _key(), _key(), _key()
    honest = Subtree("Honest Name")
    honest.group(releases=3)
    _offer(scratch_dsn, master, honest, relay=relay)
    for n, row in enumerate(honest.tables["mb_release"]):
        release_id, removed = row[0], row[1]
        _answer(conn, "release", removed, "unknown")                 # MusicBrainz removed it
        forged = Subtree(f"Forged {n}")
        forged.group(releases=0)
        group_id = forged.tables["mb_release_group"][0][0]
        real = str(uuid.uuid4())
        _answer(conn, "release", real)                                # any real MBID it chose
        forged._add("mb_release", id=release_id, gid=real, name="Something Real",
                    artist_credit=forged.credit, release_group=group_id)
        assert _offer(scratch_dsn, forger, forged)["contested"] == 1
        slice_checks.evaluate(conn, "mb", f"forged {n}")
    assert _count(conn, "slice_evidence") == 0
    assert _count(conn, "slice_penalties") == 0
    assert slice_trust.judge(conn, "mb", pub(master), pub(relay)) == []
    assert _flags(conn)["honest name"] == (True, True)               # its rows do not land meanwhile
    assert "honest name" in _void(conn)                               # asked again, of another author first


def test_a_forged_dictionary_row_is_a_stale_row_never_a_block(conn, scratch_dsn):
    """The type dictionaries ride in every slice and MusicBrainz is asked
    about none of their rows: the held one stays, the rest of a slice lands."""
    forger, honest = _key(), _key()
    forged, real = str(uuid.uuid4()), str(uuid.uuid4())
    first = _honest("First Here")
    first._add("mb_release_group_primary_type", id=1, gid=forged, name="Album")
    _offer(scratch_dsn, forger, first)
    later = _honest("Honest Later")
    later._add("mb_release_group_primary_type", id=1, gid=real, name="Album")
    stats = _offer(scratch_dsn, honest, later)
    assert stats["contested"] == 0 and stats["rows_inserted"] > 0
    assert later.gid in _gids(conn, "mb_artist")
    assert _gids(conn, "mb_release_group_primary_type") == {forged}
    assert _count(conn, "slice_clashes") == 0
    assert _flags(conn)["honest later"] == (False, False)


def test_a_dropped_blobs_clashes_go_with_it(conn, scratch_dsn):
    """A later honest blob of the name is judged on its own rows: the clash the
    convicted author's blob raised blames no one after it."""
    keeper, forger, honest, relay = _key(), _key(), _key(), _key()
    held = _honest("Held Here")
    _offer(scratch_dsn, keeper, held)
    false = Subtree("Shared Name", artist_id=held.artist_id)      # another MBID on the held id
    false.group()
    assert _offer(scratch_dsn, forger, false)["contested"] == 1
    assert _count(conn, "slice_clashes") == 1
    # The P2 convicts the forger, the answer's own source: refused whole.
    assert _offer(scratch_dsn, forger, _outside("Elsewhere")) == {"error": "source carries a penalty"}
    assert _count(conn, "slice_clashes") == 0 and _count(conn, "slice_check_plans") == 0
    slice_rebuild.run_pending(conn)
    again = Subtree("Shared Name", artist_id=held.artist_id, gid=held.gid)   # the held row itself
    _group, (release,) = again.group()
    assert _offer(scratch_dsn, honest, again, relay=relay)["contested"] == 0
    _answer(conn, "artist", false.gid, "unknown")
    _answer(conn, "artist", held.gid)
    _answer(conn, "artist_release_groups", held.gid)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO mbws_artist_release_groups (artist, release_group) VALUES (%s, %s)",
                    (held.gid, _group))
    _answer(conn, "release", release)
    slice_checks.request_check(conn, "mb", "shared name")
    assert slice_checks.evaluate(conn, "mb", "shared name") is True
    # The forger's own P2 is on record; nothing is held against the honest author.
    assert _rows(conn, "SELECT 1 FROM slice_evidence WHERE btrim(author) = %s", pub(honest)) == []
    assert slice_trust.penalty(conn, pub(honest)) is None


# --------------------------------------------------------------------------
# The rebuild
# --------------------------------------------------------------------------

def test_a_rebuild_that_waited_on_a_full_load_leaves_the_dump_alone(conn, scratch_dsn, monkeypatch):
    """The loader holds its lock from the first COPY until the completion
    marker; a rebuild that waited on it finds the marker and truncates
    nothing."""
    _offer(scratch_dsn, _key(), _honest("Kept Name"))
    monkeypatch.setattr(mb_slice_queries, "local_dump_available", lambda c: _marker(c))
    loader = psycopg2.connect(scratch_dsn)
    loader.autocommit = True
    loaded = str(uuid.uuid4())
    out: dict = {}
    try:
        with loader.cursor() as cur:                      # a full load under way, its COPY done
            cur.execute("SELECT pg_advisory_lock(%s)", (mb_slice_queries.MB_LOAD_LOCK_KEY,))
            cur.execute("INSERT INTO mb_artist (id, gid, name) VALUES (9000001, %s, 'From The Dump')",
                        (loaded,))

        def rebuild():
            c = psycopg2.connect(scratch_dsn)
            c.autocommit = True
            try:
                out["result"] = slice_rebuild.rebuild(c)
            finally:
                c.close()
        t = threading.Thread(target=rebuild)
        t.start()
        t.join(1.0)
        assert t.is_alive()                               # it waits on the load
        with loader.cursor() as cur:                      # the marker, then the lock let go
            cur.execute("""INSERT INTO user_settings (key, value) VALUES ('musicbrainz.db_version', '"20261104-002222"')
                           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""")
            cur.execute("SELECT pg_advisory_unlock(%s)", (mb_slice_queries.MB_LOAD_LOCK_KEY,))
        t.join(30.0)
    finally:
        loader.close()
    assert out["result"] == {"rebuilt": 0}
    assert loaded in _gids(conn, "mb_artist")


def _marker(c):
    with c.cursor() as cur:
        cur.execute("SELECT value FROM user_settings WHERE key = 'musicbrainz.db_version'")
        row = cur.fetchone()
    return row[0] if row else None


def test_a_rebuild_that_fails_leaves_the_catalogue_and_the_request_as_they_were(conn, scratch_dsn, monkeypatch):
    first, second = _honest("First Name"), _honest("Second Name")
    _offer(scratch_dsn, _key(), first)
    _offer(scratch_dsn, _key(), second)
    before = _gids(conn, "mb_artist")
    slice_rebuild.request(conn, "test")
    real = mb_slice_client.insert_rows

    def failing(c, nk, tables):
        if nk == "second name":
            raise RuntimeError("the database went away")
        return real(c, nk, tables)
    monkeypatch.setattr(mb_slice_client, "insert_rows", failing)
    with pytest.raises(RuntimeError):
        slice_rebuild.rebuild(conn)
    assert _gids(conn, "mb_artist") == before             # the deletes went back with it
    with pytest.raises(RuntimeError):
        slice_rebuild.run_pending(conn)
    assert _rows(conn, "SELECT 1 FROM user_settings WHERE key = %s", slice_rebuild.PENDING_KEY) == [(1,)]
    monkeypatch.setattr(mb_slice_client, "insert_rows", real)
    assert slice_rebuild.run_pending(conn)["rebuilt"] == 2
    assert _gids(conn, "mb_artist") == before
    assert _rows(conn, "SELECT 1 FROM user_settings WHERE key = %s", slice_rebuild.PENDING_KEY) == []


def test_a_rebuild_keeps_the_shop_pages_only_as_the_import_does(conn, scratch_dsn):
    s = _honest("Held Name")
    for i, url in enumerate((SHOP, "https://example.org/not-a-shop")):
        url_id = 7000 + i
        s._add("mb_url", id=url_id, gid=str(uuid.uuid4()), url=url)
        s._add("mb_l_artist_url", id=7100 + i, link=1, entity0=s.artist_id, entity1=url_id)
    _offer(scratch_dsn, _key(), s)
    assert {r[0] for r in _rows(conn, "SELECT url FROM mb_url")} == {SHOP}
    slice_rebuild.request(conn, "test")
    slice_rebuild.run_pending(conn)
    assert {r[0] for r in _rows(conn, "SELECT url FROM mb_url")} == {SHOP}
    assert _count(conn, "mb_l_artist_url") == 1


# --------------------------------------------------------------------------
# One answer, one verdict
# --------------------------------------------------------------------------

def test_an_author_convicted_by_an_entry_lands_nothing_of_its_answer(conn, scratch_dsn):
    author = _key()
    honest_looking, reaching = _honest("Signed First"), _outside("Signed Second")
    out = _offer_many(scratch_dsn, {s.name: mb_entry(author, s.name, s.one())
                                    for s in (honest_looking, reaching)}, author)
    assert out == {"error": "source carries a penalty"}            # it served its own answer
    assert slice_trust.penalty(conn, pub(author))["party"] == "author"
    assert honest_looking.gid not in _gids(conn, "mb_artist") and _count(conn, "mb_slice_blobs") == 0


def test_an_answer_whose_source_one_of_its_entries_quarantines_lands_nothing(conn, scratch_dsn):
    honest, forger, relay = _key(), _key(), _key()
    first, reaching = _honest("Honest First"), _outside("Forged Second")
    out = _offer_many(scratch_dsn, {first.name: mb_entry(honest, first.name, first.one()),
                                    reaching.name: mb_entry(forger, reaching.name, reaching.one())}, relay)
    assert out == {"error": "source carries a penalty"}
    assert first.gid not in _gids(conn, "mb_artist") and _count(conn, "mb_slice_blobs") == 0
    assert slice_trust.penalty(conn, pub(honest)) is None


def test_the_lb_exchange_takes_nothing_from_a_source_penalized_since_its_sources_were_found(conn, scratch_dsn):
    author, relay = _key(), _key()
    artist, recording = str(uuid.uuid4()), str(uuid.uuid4())
    one = {"dump_version": "20261101-001500", "artist": [10, 3], "recordings": [[recording, 5, 2, [artist]]],
           "truncated": False}
    with conn.cursor() as cur:                            # convicted since: a relay penalty
        cur.execute("INSERT INTO slice_penalties (pubkey, party) VALUES (%s, 'relay')", (pub(relay),))
    peer = Peer({artist: lb_entry(author, artist, one)}, pub(relay))
    cycle = lb_slice_cycle.LbSliceCycle(scratch_dsn, connect=None, dead=DeadAddresses())
    assert asyncio.run(cycle._exchange(peer, pub(relay), None, [artist], None)) == {
        "error": "source carries a penalty"}
    assert _count(conn, "lb_slice_blobs") == 0 and _count(conn, "lb_recording") == 0


# --------------------------------------------------------------------------
# Asked again, never closed on nothing
# --------------------------------------------------------------------------

def test_a_penalized_key_that_brought_nothing_kept_costs_no_rebuild(conn, scratch_dsn):
    with slice_rebuild.atomic(conn):
        assert slice_rebuild.drop(conn, [pub(_key())]) == {
            "names": 0, "blobs": 0, "lb_names": 0, "lb_blobs": 0, "indexed": 0}
    assert _rows(conn, "SELECT 1 FROM user_settings WHERE key = %s", slice_rebuild.PENDING_KEY) == []


def test_a_name_whose_blob_a_rebuild_leaves_out_is_asked_again_of_another_author_first(conn, scratch_dsn):
    kept_author, banned_author = _key(), _key()
    _offer(scratch_dsn, kept_author, _honest("Kept Name"))
    _offer(scratch_dsn, banned_author, _honest("Banned Name"))
    with conn.cursor() as cur:
        cur.execute("INSERT INTO p2p_node_bans (pubkey) VALUES (%s)", (pub(banned_author),))
    slice_rebuild.request(conn, "test")
    assert slice_rebuild.run_pending(conn)["left_out"] == 1
    assert _void(conn) == {"banned name"}
    assert mb_slice_cycle.reopened_authors(scratch_dsn, ["Banned Name", "Kept Name"]) == {
        "Banned Name": pub(banned_author)}
    assert mb_slice_queries.name_key("Banned Name") not in {
        r[0] for r in _rows(conn, """SELECT name_key FROM mb_slice_fetches
                                      WHERE NOT void AND matched_ids > 0""")}


def test_a_clashing_blob_too_large_to_keep_closes_its_name_for_a_month_only(conn, scratch_dsn, monkeypatch):
    held = _honest("Held Name")
    _offer(scratch_dsn, _key(), held)
    incoming = Subtree("Large Name", artist_id=held.artist_id)
    incoming.group()
    monkeypatch.setattr(mb_slice_client, "store_slice_blob", lambda *a, **k: False)
    assert _offer(scratch_dsn, _key(), incoming)["contested"] == 1
    # Nothing landed and nothing is kept to settle: a zero-match's closing,
    # re-asked after a month of another author first — not a closed name.
    assert _rows(conn, "SELECT matched_ids, void FROM mb_slice_fetches WHERE name_key = 'large name'") == [
        (0, False)]


def test_the_blob_that_lost_a_clash_served_again_closes_its_name_like_a_zero_match(conn, scratch_dsn):
    held_author, loser = _key(), _key()
    held = _honest("Held Name")
    _offer(scratch_dsn, held_author, held)
    incoming = Subtree("Lost Name", artist_id=held.artist_id)
    incoming.group()
    entry = mb_entry(loser, incoming.name, incoming.one())
    assert _offer_many(scratch_dsn, {incoming.name: entry}, loser)["contested"] == 1
    _answer(conn, "artist", incoming.gid, "unknown")
    _answer(conn, "artist", held.gid)
    slice_checks.evaluate(conn, "mb", "lost name")
    assert _void(conn) == {"lost name"}
    stats = _offer_many(scratch_dsn, {incoming.name: entry}, loser)  # the last source left
    assert stats["contested"] == 0 and stats["rows_inserted"] == 0
    assert _rows(conn, "SELECT matched_ids, void FROM mb_slice_fetches WHERE name_key = 'lost name'") == [
        (0, False)]
    assert _count(conn, "slice_clashes") == 0


def test_an_author_convicted_while_its_answer_waited_on_the_lock_lands_nothing(conn, scratch_dsn, monkeypatch):
    """The verdict commits between the answer's verification and its import
    (a rebuild held the lock meanwhile): read again under the lock, it keeps
    every row of the convicted author out."""
    author = _key()
    late = _honest("Late Name")
    real = MBSliceClient._get_conn

    def convicted_first(self):
        c = psycopg2.connect(scratch_dsn)
        try:
            with c.cursor() as cur:
                cur.execute("INSERT INTO slice_penalties (pubkey, party) VALUES (%s, 'author')",
                            (pub(author),))
            c.commit()
        finally:
            c.close()
        return real(self)
    monkeypatch.setattr(MBSliceClient, "_get_conn", convicted_first)
    # Passed on by another node: the source carries no penalty, the author does.
    stats = _offer(scratch_dsn, author, late, relay=_key())
    assert late.name in stats["missing"]
    assert late.gid not in _gids(conn, "mb_artist")
    assert _count(conn, "mb_slice_blobs") == 0 and _count(conn, "mb_slice_fetches") == 0


def test_an_lb_author_convicted_before_the_import_transaction_lands_nothing(conn, scratch_dsn):
    author, relay = _key(), _key()
    artist, recording = str(uuid.uuid4()), str(uuid.uuid4())
    one = {"dump_version": "20261101-001500", "artist": [10, 3], "recordings": [[recording, 5, 2, [artist]]],
           "truncated": False}
    entry = lb_entry(author, artist, one)
    from desktop.p2p import lb_slice_queries
    core, blob_gz, cert, proof = lb_slice_queries.verify_slice_entry(artist, entry, None)
    with conn.cursor() as cur:                            # the verdict, after the batch was verified
        cur.execute("INSERT INTO slice_penalties (pubkey, party) VALUES (%s, 'author')", (pub(author),))
    assert lb_slice_cycle.import_batch(scratch_dsn, "peer", None,
                                       [(artist, core, blob_gz, entry, cert, proof)], pub(relay)) == 0
    assert _count(conn, "lb_slice_blobs") == 0 and _count(conn, "lb_slice_fetches") == 0


def test_one_name_the_database_refuses_loses_that_name_alone(conn, scratch_dsn, monkeypatch):
    author = _key()
    good, bad = _honest("Good Name"), _honest("Bad Name")
    real = mb_slice_client.insert_rows

    def refusing(c, nk, tables):
        if nk == "bad name":
            with c.cursor() as cur:
                cur.execute("SELECT 1/0")
        return real(c, nk, tables)
    monkeypatch.setattr(mb_slice_client, "insert_rows", refusing)
    stats = _offer_many(scratch_dsn, {s.name: mb_entry(author, s.name, s.one()) for s in (good, bad)}, author)
    assert bad.name in stats["missing"]
    assert good.gid in _gids(conn, "mb_artist") and bad.gid not in _gids(conn, "mb_artist")
    assert {r[0] for r in _rows(conn, "SELECT name_key FROM mb_slice_fetches")} == {"good name"}


def test_a_convicted_key_whose_blob_another_answer_replaced_still_costs_a_rebuild(conn, scratch_dsn):
    """Its rows may still be in the catalogue: the subject index, append-only,
    remembers that it brought them."""
    first, later = _key(), _key()
    s = _honest("Replaced Name")
    _offer(scratch_dsn, first, s)
    again = Subtree("Replaced Name", artist_id=s.artist_id, gid=s.gid)   # the same rows, another author
    again.group()
    _offer(scratch_dsn, later, again)
    assert _rows(conn, "SELECT btrim(author_pubkey) FROM mb_slice_blobs") == [(pub(later),)]
    with slice_rebuild.atomic(conn):
        slice_rebuild.drop(conn, [pub(first)])
    assert _rows(conn, "SELECT 1 FROM user_settings WHERE key = %s", slice_rebuild.PENDING_KEY) == [(1,)]


def test_a_name_whose_matched_artist_did_not_land_closes_only_like_a_zero_match(conn, scratch_dsn, monkeypatch):
    s = _honest("Unlanded Name")
    real = mb_slice_client.insert_rows

    def without_the_artist(c, nk, tables):
        return real(c, nk, {t: rows for t, rows in tables.items() if t != "mb_artist"})
    monkeypatch.setattr(mb_slice_client, "insert_rows", without_the_artist)
    _offer(scratch_dsn, _key(), s)
    assert _rows(conn, "SELECT matched_ids FROM mb_slice_fetches WHERE name_key = 'unlanded name'") == [(0,)]
