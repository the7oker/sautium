"""Passing on only what this node checked, and what MusicBrainz's answers and
MetaBrainz's listings prove about a kept catalogue slice, on a real
PostgreSQL built from the migrations — skipped without a cluster
(desktop/p2p/slice_checks.py, slice_trust.judge, published.py):

- another author's kept slice is passed on only once this node checked it
  against MusicBrainz: until then it is `missing` and its check is queued,
  drawn once while it waits; a suspect or contested slice never is; this
  node's own build always is; /health advertises what may be passed on;
- an LB slice is passed on once MusicBrainz credits its recordings to its
  artist;
- a stale but honest slice — a renamed artist under a merged MBID, a group
  MusicBrainz now credits to someone else, a release and a recording it no
  longer has — is blamed on no one and passed on; an old dump's slice counts
  no evidence;
- the thresholds of a suspected fabrication: an author at three events over
  at least 1% of its checked facts — three among a thousand convict no one —
  and a node passing slices on at three events of at least two authors over
  at least 1% of at least a hundred facts, while none of those authors
  reaches its own;
- a slice from a dump MetaBrainz has not published, kept six hours before
  its listing was read, proves its author and the node that passed it on
  false; one that arrived minutes before the listing proves nothing; the
  listing is read at most once a day.

MusicBrainz is tests/mb_fakes.FakeMusicBrainz where its answers' semantics
matter (a merge, a browse), its stored answers otherwise."""

import asyncio
import hashlib
import json
import random
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

psycopg2 = pytest.importorskip("psycopg2")
import psycopg2.pool  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
for _p in (REPO, REPO / "backend"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from desktop.mb_slice_client import MBSliceClient  # noqa: E402
from desktop.p2p import (lb_slice_cycle, lb_slice_queries, mb_asks, mb_slice_queries,  # noqa: E402
                         published, slice_checks, slice_rebuild, slice_trust)
from desktop.p2p.sync_walk import DeadAddresses  # noqa: E402
from tests.mb_fakes import FakeMusicBrainz, credit, drain  # noqa: E402
from tests.slice_certs import Peer, Subtree, certificate, dump_version, lb_entry, mb_entry, pub, trust  # noqa: E402

OWN_KEY = Ed25519PrivateKey.generate()
OWN = pub(OWN_KEY)
_STATE = ("mb_slice_blobs", "mb_slice_fetches", "lb_slice_blobs", "lb_slice_fetches", "slice_index",
          "slice_evidence", "slice_tally", "slice_penalties", "slice_check_plans", "slice_clashes",
          "slice_clash_wins", "slice_burned_mailboxes", "p2p_identities", "p2p_node_bans", "mbws_queue",
          "mbws_answers", "mbws_artists", "mbws_release_groups", "mbws_artist_release_groups", "mbws_releases",
          "mbws_recordings", "mbws_credits", "lb_recording", "lb_artist", "artists"
          ) + tuple(mb_slice_queries.SLICE_TABLES)
_SETTINGS = ["musicbrainz.db_version", "listenbrainz.db_version", "mb_slice.published", "lb_slice.published"]


@pytest.fixture(autouse=True)
def _authority(monkeypatch):
    trust(monkeypatch)


@pytest.fixture
def conn(scratch_dsn):
    """A node without either dump, holding no slice yet."""
    c = psycopg2.connect(scratch_dsn, options="-c timezone=UTC")
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute("TRUNCATE " + ", ".join(_STATE) + " CASCADE")
        cur.execute("DELETE FROM user_settings WHERE key = ANY(%s)", (_SETTINGS,))
    yield c
    c.close()


@pytest.fixture
def musicbrainz(scratch_dsn, monkeypatch):
    """MusicBrainz's web service as FakeMusicBrainz; what it answers is kept
    in the scratch database by backend/mb_answers.py (tests/mb_fakes.drain)."""
    import db_pool
    import mb_answers
    pool = psycopg2.pool.ThreadedConnectionPool(1, 4, dsn=scratch_dsn, options="-c timezone=UTC")
    monkeypatch.setattr(db_pool, "_pool", pool)
    monkeypatch.setattr(mb_answers, "_wakers", {})
    yield FakeMusicBrainz().install(monkeypatch)
    pool.closeall()


def _key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def _offer(dsn, key, subtree, relay=None):
    """`key`'s slice of `subtree` taken through the requester, passed on by
    `relay` (its author itself by default)."""
    peer = Peer({subtree.name: mb_entry(key, subtree.name, subtree.one())}, pub(relay or key))
    client = MBSliceClient(peer, db_dsn=dsn)
    try:
        return client.run([subtree.name])
    finally:
        client.close()


def _offer_lb(dsn, key, entries, relay):
    """`key`'s LB slices ({artist MBID: slice}) taken through the LB cycle's exchange."""
    peer = Peer({m: lb_entry(key, m, one) for m, one in entries.items()}, pub(relay))
    cycle = lb_slice_cycle.LbSliceCycle(dsn, connect=None, dead=DeadAddresses())
    return asyncio.run(cycle._exchange(peer, pub(relay), None, list(entries), None))


def _rows(conn, sql, *params):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _count(conn, table):
    return _rows(conn, f"SELECT count(*) FROM {table}")[0][0]


def _penalties(conn):
    return dict(_rows(conn, "SELECT btrim(pubkey), party::text FROM slice_penalties"))


def _answer(conn, kind, mbid, status="found"):
    """MusicBrainz's answer for (kind, mbid), as backend/mb_answers.py keeps it."""
    with conn.cursor() as cur:
        cur.execute("INSERT INTO mbws_answers (kind, mbid, status, target) VALUES (%s, %s, %s, %s)",
                    (kind, mbid, status, None if status == "unknown" else mbid))


def _browse(conn, artist, groups):
    """MusicBrainz's list of the groups it credits to `artist`."""
    _answer(conn, "artist_release_groups", artist)
    with conn.cursor() as cur:
        for g in groups:
            cur.execute("INSERT INTO mbws_artist_release_groups (artist, release_group) VALUES (%s, %s)",
                        (artist, g))


def _credited(conn, recording, *artists):
    """MusicBrainz knows `recording`, credited to `artists`."""
    _answer(conn, "recording", recording)
    with conn.cursor() as cur:
        for position, artist in enumerate(artists):
            cur.execute("""INSERT INTO mbws_credits (owner_kind, owner, position, artist, artist_name,
                                                     credited_as)
                           VALUES ('recording', %s, %s, %s, '', '')""", (recording, position, artist))


def _slice_fed(conn) -> None:
    """A node that holds catalogue slices (and no dump): one that checks."""
    with conn.cursor() as cur:
        cur.execute("INSERT INTO mb_slice_fetches (name_key, matched_ids) VALUES ('a fed name', 1) "
                    "ON CONFLICT DO NOTHING")


def _waiting(conn, family, key):
    return set(_rows(conn, """SELECT kind::text, mbid::text FROM mbws_waiters
                               WHERE subject = 'slice_check' AND subject_key = %s""",
                     slice_checks.subject_key(family, key)))


def _unknown_artist(name, more_groups=0):
    """A slice of an artist MusicBrainz does not know, with `more_groups`
    groups of no release before its one released group: (it, that group,
    the group's release)."""
    s = Subtree(name)
    for _ in range(more_groups):
        s.group(releases=0)
    group, (release,) = s.group()
    return s, group, release


# --------------------------------------------------------------------------
# Check before passing on
# --------------------------------------------------------------------------

def test_a_kept_slice_is_passed_on_only_once_this_node_checked_it(conn, scratch_dsn):
    author, relay = _key(), _key()
    s = Subtree("Kept Name")
    group, (release,) = s.group()
    _offer(scratch_dsn, author, s, relay=relay)
    assert mb_slice_queries.serve_slices(conn, [s.name], own_pubkey=OWN) == {
        "v": mb_slice_queries.PROTOCOL_VERSION, "slices": {}, "missing": [s.name]}
    assert _waiting(conn, "mb", "kept name") == {("artist", s.gid), ("artist_release_groups", s.gid),
                                                 ("release", release)}
    assert {r[0] for r in _rows(conn, "SELECT priority FROM mbws_queue")} == {slice_checks.PRIORITY}
    planned = _rows(conn, "SELECT planned_at FROM slice_check_plans")
    mb_slice_queries.serve_slices(conn, [s.name], own_pubkey=OWN)       # asked again meanwhile
    assert _rows(conn, "SELECT planned_at FROM slice_check_plans") == planned
    # MusicBrainz knows the artist and credits the group to it.
    _answer(conn, "artist", s.gid)
    _browse(conn, s.gid, [group])
    _answer(conn, "release", release)
    assert slice_checks.evaluate(conn, "mb", "kept name") is True
    entry = mb_slice_queries.serve_slices(conn, [s.name], own_pubkey=OWN)["slices"][s.name]
    assert entry["author_pubkey"] == pub(author)
    core, _, cert, proof_ = mb_slice_queries.verify_slice_entry(s.name, entry)    # as the next hop does
    assert (cert["pubkey"], proof_, core["name_key"]) == (pub(author), None, "kept name")
    assert _count(conn, "slice_evidence") == 0 and _count(conn, "slice_check_plans") == 0


def test_a_suspect_or_contested_slice_is_never_passed_on_and_this_nodes_own_build_always_is(conn, scratch_dsn):
    author, relay = _key(), _key()
    suspect, group, release = _unknown_artist("Not In MusicBrainz")
    _offer(scratch_dsn, author, suspect, relay=relay)
    slice_checks.request_check(conn, "mb", "not in musicbrainz")
    _answer(conn, "artist", suspect.gid, "unknown")
    _answer(conn, "release", release, "unknown")
    _answer(conn, "release_group", group, "unknown")
    assert slice_checks.evaluate(conn, "mb", "not in musicbrainz") is False
    held = Subtree("Held Name")
    held.group()
    _offer(scratch_dsn, author, held)
    clashing = Subtree("Clashing Name", artist_id=held.artist_id)
    clashing.group()
    assert _offer(scratch_dsn, author, clashing, relay=relay)["contested"] == 1
    own = Subtree("Own Build")
    own.group()
    entry = mb_entry(OWN_KEY, own.name, own.one())
    _, blob_gz, *_ = mb_slice_queries.verify_slice_entry(own.name, entry)
    mb_slice_queries.store_slice_blob(conn, "own build", own.version, OWN, entry["sig"], blob_gz, cap=False)
    own_cert = certificate(OWN)
    out = mb_slice_queries.serve_slices(conn, ["Not In MusicBrainz", "Clashing Name", "Own Build"],
                                        own_cert=(own_cert, None), own_pubkey=OWN)
    assert list(out["slices"]) == ["Own Build"] and out["missing"] == ["Not In MusicBrainz", "Clashing Name"]
    assert (out["slices"]["Own Build"]["author_cert"], out["slices"]["Own Build"]["author_proof"]) == (own_cert, None)
    assert _count(conn, "slice_check_plans") == 0                   # neither is checked again
    # /health advertises what may be passed on: this node's build and the held slice.
    assert mb_slice_queries.count_slice_blobs(conn, OWN) == slice_checks.checkable_count(conn, "mb", OWN) == 2


def test_an_lb_slice_is_passed_on_once_musicbrainz_credits_its_recordings_to_its_artist(conn, scratch_dsn):
    author, relay = _key(), _key()
    artist, elsewhere, someone = (str(uuid.uuid4()) for _ in range(3))
    heard, misplaced = str(uuid.uuid4()), str(uuid.uuid4())
    version = dump_version(3)
    out = _offer_lb(scratch_dsn, author, {
        artist: {"dump_version": version, "artist": [10, 3], "recordings": [[heard, 5, 2, [artist]]],
                 "truncated": False},
        elsewhere: {"dump_version": version, "artist": [4, 1], "recordings": [[misplaced, 4, 1, [elsewhere]]],
                    "truncated": False}}, relay)
    assert out["imported"] == 2
    asked = [artist, elsewhere]
    _slice_fed(conn)
    assert lb_slice_queries.serve_slices(conn, asked, own_pubkey=OWN)["missing"] == asked
    assert _waiting(conn, "lb", artist) == {("recording", heard), ("artist", artist)}
    _answer(conn, "artist", artist)
    _answer(conn, "artist", elsewhere)
    _credited(conn, heard, artist)
    _credited(conn, misplaced, someone)                            # MusicBrainz credits it to another artist
    assert slice_checks.evaluate(conn, "lb", artist) is True
    assert slice_checks.evaluate(conn, "lb", elsewhere) is False
    out = lb_slice_queries.serve_slices(conn, asked, own_pubkey=OWN)
    assert list(out["slices"]) == [artist] and out["missing"] == [elsewhere]
    assert lb_slice_queries.verify_slice_entry(artist, out["slices"][artist])[2]["pubkey"] == pub(author)


def _checked(conn, s, group, release):
    """`s`'s kept slice checked here against MusicBrainz's answers (the
    artist known, the group credited to it), and passed on."""
    key = s.name.lower()
    assert mb_slice_queries.serve_slices(conn, [s.name], own_pubkey=OWN)["missing"] == [s.name]
    _answer(conn, "artist", s.gid)
    _browse(conn, s.gid, [group])
    _answer(conn, "release", release)
    assert slice_checks.evaluate(conn, "mb", key) is True
    assert slice_checks.servable(conn, "mb", key, OWN) is True


def test_an_answer_that_musicbrainz_does_not_know_a_subject_un_checks_the_slice_about_it(
        conn, scratch_dsn, musicbrainz, monkeypatch):
    """Rule 0's condition 4 in its narrowest form: whatever asked — the
    canon's own confirmation, another slice's check — an answer stored
    unknown for an artist or a group a kept slice is indexed about takes
    that slice's check away, so it is passed on again only after a check
    that read the answer."""
    import mb_answers
    told = []

    def uncheck(kind, mbid):
        # As the backend's slice checker is told: after the answer's commit.
        assert mb_asks.answer(conn, kind, mbid) == ("unknown", None)
        told.append(kind)
        slice_checks.uncheck_subject(conn, kind, mbid)
    monkeypatch.setattr(mb_answers, "_unknown_waker", uncheck)
    author, relay = _key(), _key()
    s, other, third = Subtree("Unknown Since"), Subtree("Still Known"), Subtree("Another Version")
    groups = {x.name: x.group() for x in (s, other, third)}
    for x in (s, other, third):
        _offer(scratch_dsn, author, x, relay=relay)
        _checked(conn, x, groups[x.name][0], groups[x.name][1][0])
    # A known answer takes nothing away; nor an unknown one of a kind that is
    # no subject.
    musicbrainz.artist(other.gid, other.name)
    mb_answers.store("artist", other.gid, musicbrainz.lookup("artist", other.gid))
    mb_answers.store("recording", str(uuid.uuid4()), None)
    assert slice_checks.servable(conn, "mb", "still known", OWN) is True
    # The artist unknown: its slice is checked no more — `missing`, its check
    # queued — while the other is passed on as before.
    mb_answers.store("artist", s.gid, None)
    assert slice_checks.servable(conn, "mb", "unknown since", OWN) is None
    assert slice_checks.servable(conn, "mb", "still known", OWN) is True
    out = mb_slice_queries.serve_slices(conn, [s.name, other.name], own_pubkey=OWN)
    assert out["missing"] == [s.name] and list(out["slices"]) == [other.name]
    assert _rows(conn, "SELECT key FROM slice_check_plans") == [("unknown since",)]
    # The check that follows reads the answer: an artist MusicBrainz does not
    # know whose release it still knows is an honest older dump's — passed on.
    assert slice_checks.evaluate(conn, "mb", "unknown since") is True
    assert slice_checks.servable(conn, "mb", "unknown since", OWN) is True
    # A group answered unknown takes its slice's check away too.
    mb_answers.store("release_group", groups[other.name][0], None)
    assert slice_checks.servable(conn, "mb", "still known", OWN) is None
    # By the version of the blob its index row names: the name's blob since
    # replaced keeps its check.
    with conn.cursor() as cur:
        cur.execute("UPDATE slice_index SET payload_sha256 = %s WHERE key = 'another version'",
                    ("ab" * 32,))
    mb_answers.store("artist", third.gid, None)
    assert slice_checks.servable(conn, "mb", "another version", OWN) is True
    # An LB slice of an artist answered unknown.
    artist, heard = str(uuid.uuid4()), str(uuid.uuid4())
    assert _offer_lb(scratch_dsn, author, {artist: {
        "dump_version": dump_version(3), "artist": [10, 3], "recordings": [[heard, 5, 2, [artist]]],
        "truncated": False}}, relay)["imported"] == 1
    assert lb_slice_queries.serve_slices(conn, [artist], own_pubkey=OWN)["missing"] == [artist]
    _answer(conn, "artist", artist)
    _credited(conn, heard, artist)
    assert slice_checks.evaluate(conn, "lb", artist) is True
    assert slice_checks.servable(conn, "lb", artist, OWN) is True
    mb_answers.store("artist", artist, None)
    assert slice_checks.servable(conn, "lb", artist, OWN) is None
    assert told == ["recording", "artist", "release_group", "artist", "artist"]


def test_at_start_a_slice_whose_subject_was_answered_unknown_after_its_check_is_checked_again(
        conn, scratch_dsn):
    """What a restart took from the slice checker before it ran: an answer
    kept unknown after a kept slice's check takes that check away; one kept
    before it was read by it."""
    author, relay = _key(), _key()
    later, earlier = Subtree("Answered Later"), Subtree("Answered Earlier")
    artist_later, replaced = Subtree("Artist Answered Later"), Subtree("Since Replaced")
    groups = {x.name: x.group() for x in (later, earlier, artist_later, replaced)}
    for x in (later, earlier, artist_later, replaced):
        _offer(scratch_dsn, author, x, relay=relay)
        _checked(conn, x, groups[x.name][0], groups[x.name][1][0])
    artist, heard = str(uuid.uuid4()), str(uuid.uuid4())
    _offer_lb(scratch_dsn, author, {artist: {
        "dump_version": dump_version(3), "artist": [10, 3], "recordings": [[heard, 5, 2, [artist]]],
        "truncated": False}}, relay)
    lb_slice_queries.serve_slices(conn, [artist], own_pubkey=OWN)
    _answer(conn, "artist", artist)
    _credited(conn, heard, artist)
    assert slice_checks.evaluate(conn, "lb", artist) is True
    _answer(conn, "release_group", groups[later.name][0], "unknown")
    with conn.cursor() as cur:
        cur.execute("""UPDATE mbws_answers SET status = 'unknown', target = NULL,
                              answered_at = now() + interval '1 minute'
                        WHERE (kind = 'release_group' AND mbid = %s)
                           OR (kind = 'artist' AND mbid = ANY(%s::uuid[]))""",
                    (groups[later.name][0], [artist, artist_later.gid, replaced.gid]))
        # The index row of "since replaced" names another version of its blob.
        cur.execute("UPDATE slice_index SET payload_sha256 = %s WHERE key = 'since replaced'", ("cd" * 32,))
        cur.execute("""UPDATE mbws_answers SET status = 'unknown', target = NULL,
                              answered_at = now() - interval '1 minute'
                        WHERE kind = 'artist' AND mbid = %s""", (earlier.gid,))
    assert slice_checks.uncheck_answered(conn) == 3
    assert slice_checks.servable(conn, "mb", "answered later", OWN) is None
    assert slice_checks.servable(conn, "mb", "artist answered later", OWN) is None
    assert slice_checks.servable(conn, "lb", artist, OWN) is None
    assert slice_checks.servable(conn, "mb", "answered earlier", OWN) is True
    assert slice_checks.servable(conn, "mb", "since replaced", OWN) is True
    assert slice_checks.uncheck_answered(conn) == 0


def test_a_verified_author_s_row_of_an_older_format_is_passed_on_by_no_slice_lane(
        conn, scratch_dsn, monkeypatch):
    """After a certificate format bump, a registry row not observed since is
    no certificate the next hop takes: neither family passes its author's
    slices on under it."""
    from desktop.p2p import birth_cert
    author, relay = _key(), _key()
    s = Subtree("Old Format")
    group, (release,) = s.group()
    _offer(scratch_dsn, author, s, relay=relay)
    _checked(conn, s, group, release)
    artist, heard = str(uuid.uuid4()), str(uuid.uuid4())
    _offer_lb(scratch_dsn, author, {artist: {
        "dump_version": dump_version(3), "artist": [10, 3], "recordings": [[heard, 5, 2, [artist]]],
        "truncated": False}}, relay)
    lb_slice_queries.serve_slices(conn, [artist], own_pubkey=OWN)
    _answer(conn, "artist", artist)
    _credited(conn, heard, artist)
    assert slice_checks.evaluate(conn, "lb", artist) is True
    assert list(mb_slice_queries.serve_slices(conn, [s.name], own_pubkey=OWN)["slices"]) == [s.name]
    assert list(lb_slice_queries.serve_slices(conn, [artist], own_pubkey=OWN)["slices"]) == [artist]
    _slice_fed(conn)
    assert slice_checks.checkable_count(conn, "mb", OWN) == 1
    monkeypatch.setattr(birth_cert, "CERT_VERSION", birth_cert.CERT_VERSION + 1)
    assert slice_trust.author_certificate(conn, pub(author)) is None
    queued = _count(conn, "mbws_queue")
    assert mb_slice_queries.serve_slices(conn, [s.name], own_pubkey=OWN)["missing"] == [s.name]
    assert lb_slice_queries.serve_slices(conn, [artist], own_pubkey=OWN)["missing"] == [artist]
    # Nor advertised, nor checked again for nothing: no check is queued.
    assert slice_checks.servable(conn, "mb", "old format", OWN) is False
    assert slice_checks.checkable_count(conn, "mb", OWN) == 0
    assert slice_checks.checkable_count(conn, "lb", OWN) == 0
    assert _count(conn, "mbws_queue") == queued


def test_a_source_whose_proof_failed_here_is_refused_on_the_slice_lanes_once_its_ban_row_is_gone(
        conn, scratch_dsn):
    """A key whose proof failed here is refused as a banned one is, after its
    ban row was evicted past the cap: not asked for slices, what it passed on
    neither passed on nor advertised, and left out of a rebuild."""
    from desktop.p2p import identity_registry
    author, failed, honest = _key(), _key(), _key()
    s, other = Subtree("Through A Liar"), Subtree("Through An Honest Relay")
    groups = {x.name: x.group() for x in (s, other)}
    _offer(scratch_dsn, author, s, relay=failed)
    _offer(scratch_dsn, author, other, relay=honest)
    for x in (s, other):
        _checked(conn, x, groups[x.name][0], groups[x.name][1][0])
    assert slice_checks.checkable_count(conn, "mb", OWN) == 2
    identity_registry.observe(conn, certificate(pub(failed), method="pow"))
    identity_registry.mark_failed(conn, pub(failed), None, "pow_failed")
    with conn.cursor() as cur:
        cur.execute("DELETE FROM p2p_node_bans WHERE pubkey = %s", (pub(failed),))
    assert slice_checks.source_refused(conn, pub(failed)) and not slice_checks.source_refused(conn, pub(honest))
    assert pub(failed) in lb_slice_cycle.load_bans(scratch_dsn)[0]
    assert pub(honest) not in lb_slice_cycle.load_bans(scratch_dsn)[0]
    assert slice_checks.servable(conn, "mb", "through a liar", OWN) is False
    assert slice_checks.servable(conn, "mb", "through an honest relay", OWN) is True
    assert slice_checks.checkable_count(conn, "mb", OWN) == 1
    slice_rebuild.rebuild(conn)
    gids = {r[0] for r in _rows(conn, "SELECT gid::text FROM mb_artist")}
    assert other.gid in gids and s.gid not in gids
    # An author whose proof failed here since its blobs were kept: left out too.
    identity_registry.observe(conn, certificate(pub(author), method="pow"))
    with conn.cursor() as cur:
        cur.execute("UPDATE p2p_identities SET status = 'failed' WHERE pubkey = %s", (pub(author),))
    slice_rebuild.rebuild(conn)
    assert other.gid not in {r[0] for r in _rows(conn, "SELECT gid::text FROM mb_artist")}


# --------------------------------------------------------------------------
# What an honest dump explains
# --------------------------------------------------------------------------

def test_a_stale_but_honest_slice_is_blamed_on_no_one(conn, scratch_dsn, musicbrainz):
    author, relay = _key(), _key()
    old, new, someone = (str(uuid.uuid4()) for _ in range(3))
    s = Subtree("Old Name", gid=old)
    recredited, (kept,) = s.group()                  # MusicBrainz now credits it to someone else
    listed, (gone,) = s.group()                      # MusicBrainz lists it, without this release
    lost = s.recording()                             # nor does it have this recording
    _offer(scratch_dsn, author, s, relay=relay)
    musicbrainz.artist(new, "New Name")
    musicbrainz.merge("artist", old, new)            # the artist renamed under a merged MBID
    musicbrainz.release_group(recredited, "First", credit(("Someone Else", someone)))
    musicbrainz.release(kept, "First", recredited, credit(("Someone Else", someone)), [])
    musicbrainz.release_group(listed, "Second", credit(("New Name", new)))
    assert mb_slice_queries.serve_slices(conn, [s.name], own_pubkey=OWN)["missing"] == [s.name]
    verdict = None
    while verdict is None:                           # each round asks what the last one lacked
        assert drain() > 0
        verdict = slice_checks.evaluate(conn, "mb", "old name")
    assert verdict is True
    assert ("release", gone) in musicbrainz.asked and ("recording", lost) in musicbrainz.asked
    assert _count(conn, "slice_evidence") == 0 and _count(conn, "slice_penalties") == 0
    assert sorted(_rows(conn, "SELECT party::text, facts, events FROM slice_tally")) == [
        ("author", 3, 0), ("relay", 3, 0)]
    assert list(mb_slice_queries.serve_slices(conn, [s.name], own_pubkey=OWN)["slices"]) == [s.name]


def test_an_old_dumps_slice_counts_no_evidence(conn, scratch_dsn):
    author, relay = _key(), _key()
    s = Subtree("Long Gone", version=dump_version(slice_trust.FRESH_DAYS + 30))
    group, (release,) = s.group()
    _offer(scratch_dsn, author, s, relay=relay)
    slice_checks.request_check(conn, "mb", "long gone")
    _answer(conn, "artist", s.gid, "unknown")
    _answer(conn, "release", release, "unknown")
    _answer(conn, "release_group", group, "unknown")
    assert slice_checks.evaluate(conn, "mb", "long gone") is False          # held back all the same
    assert slice_checks.servable(conn, "mb", "long gone", OWN) is False
    for table in ("slice_evidence", "slice_tally", "slice_penalties"):
        assert _count(conn, table) == 0, table


# --------------------------------------------------------------------------
# The thresholds
# --------------------------------------------------------------------------

def _suspect_check(conn, dsn, author, name, relay=None, more_groups=0, group_known=True):
    """One of `author`'s slices taken and checked: an artist MusicBrainz does
    not know, nor the first release of its group — one S1 event, one per
    subject — over two checked facts (the artist, its first release; an
    unknown artist's groups are not looked up). `more_groups` are further
    groups credited to it; `group_known` what MusicBrainz says of the group."""
    s, group, release = _unknown_artist(name, more_groups)
    _offer(dsn, author, s, relay=relay)
    slice_checks.request_check(conn, "mb", name.lower())
    _answer(conn, "artist", s.gid, "unknown")
    _answer(conn, "release", release, "unknown")
    _answer(conn, "release_group", group, "found" if group_known else "unknown")
    return slice_checks.evaluate(conn, "mb", name.lower())


def test_an_author_is_convicted_at_three_events_over_one_percent_of_its_checked_facts(conn, scratch_dsn):
    author = _key()
    before = []
    for i in range(3):
        before.append(slice_trust.penalty(conn, pub(author)))
        assert _suspect_check(conn, scratch_dsn, author, f"Unknown {i}") is False
    assert before == [None, None, None]
    p = slice_trust.penalty(conn, pub(author))
    assert (p["party"], p["kind"], p["entity"]) == ("author", "S1", "artist")
    assert _rows(conn, "SELECT sum(facts), sum(events) FROM slice_tally") == [(6, 3)]
    # Its slices go with the conviction, and its rows with the rebuild it asked for.
    assert _count(conn, "mb_slice_blobs") == 0
    slice_rebuild.run_pending(conn)
    assert _count(conn, "mb_artist") == 0


def test_three_events_among_a_thousand_checked_facts_convict_no_one(conn, scratch_dsn):
    author = _key()
    slice_trust.tally(conn, "mb", pub(author), pub(author), 994, 0)       # its earlier checks, all true
    for i in range(3):
        assert _suspect_check(conn, scratch_dsn, author, f"Unknown {i}") is False
    assert _rows(conn, "SELECT sum(facts), sum(events) FROM slice_tally") == [(1000, 3)]
    assert _count(conn, "slice_penalties") == 0
    assert _count(conn, "mb_slice_blobs") == 3


def test_a_node_passing_suspect_slices_of_many_authors_on_is_quarantined_while_no_author_is(conn, scratch_dsn):
    relay = _key()
    authors = [_key() for _ in range(3)]
    # What the node passed on before, all true: the hundred facts the rate needs.
    slice_trust.tally(conn, "mb", pub(_key()), pub(relay), 102, 0)
    before = []
    for i, author in enumerate(authors):
        before.append(slice_trust.penalty(conn, pub(relay)))
        assert _suspect_check(conn, scratch_dsn, author, f"Unknown {i}", relay=relay, more_groups=34,
                              group_known=False) is False
    assert before == [None, None, None]                  # not at one author, nor at two
    assert _penalties(conn) == {pub(relay): "relay"}
    assert dict(_rows(conn, """SELECT btrim(author), count(*) FROM slice_evidence GROUP BY 1""")) == {
        pub(a): 1 for a in authors}
    assert dict(_rows(conn, "SELECT party::text, sum(facts) FROM slice_tally GROUP BY 1")) == {
        "author": 108, "relay": 108}
    assert _count(conn, "mb_slice_blobs") == 0           # what it passed on is dropped


def test_a_node_that_passed_on_the_event_its_author_was_convicted_on_is_quarantined_with_it(conn, scratch_dsn):
    author, relay = _key(), _key()
    for i in range(3):                                   # one author: the source thresholds never apply
        assert _suspect_check(conn, scratch_dsn, author, f"Unknown {i}", relay=relay) is False
    assert _penalties(conn) == {pub(author): "author", pub(relay): "relay"}
    assert len(_rows(conn, "SELECT DISTINCT evidence FROM slice_penalties")) == 1


# --------------------------------------------------------------------------
# P1: a dump version MetaBrainz has not published
# --------------------------------------------------------------------------

def _version(at: datetime) -> str:
    return at.strftime("%Y%m%d-%H%M%S")


def test_a_dump_metabrainz_has_not_published_proves_its_author_false(conn, scratch_dsn):
    """And the node that passed it on too — but only when the version was past
    what that node's own check bounds when it served it, a day after it reached
    this node: one that never read the listing could not have seen it sooner."""
    read_at = datetime.now(timezone.utc)
    newest = _version(read_at - timedelta(days=10))
    past, past_relay, within, within_relay = _key(), _key(), _key(), _key()
    for name, key, relay, version, age in (
            ("Past Its Bound", past, past_relay, read_at - timedelta(days=1), "3 days"),
            ("Within Its Bound", within, within_relay, read_at - timedelta(hours=1), "7 hours"),
            ("Arrived Late", _key(), _key(), read_at - timedelta(hours=1), "10 minutes"),
            ("Published", _key(), _key(), read_at - timedelta(days=20), "7 hours")):
        s = Subtree(name, version=_version(version))
        s.group()
        assert "error" not in _offer(scratch_dsn, key, s, relay=relay)
        with conn.cursor() as cur:
            cur.execute("UPDATE mb_slice_blobs SET created_at = %s - %s::interval WHERE name_key = %s",
                        (read_at, age, name.lower()))
    out = slice_checks.published_check(conn, "mb", newest, read_at)
    assert sorted(out) == sorted([pub(past), pub(past_relay), pub(within)])
    assert _penalties(conn) == {pub(past): "author", pub(past_relay): "relay", pub(within): "author"}
    assert sorted(_rows(conn, "SELECT kind::text, entity::text, mbid, key FROM slice_evidence")) == [
        ("P1", "dump_version", None, "past its bound"), ("P1", "dump_version", None, "within its bound")]
    # What the convicted keys brought is dropped; a dump published minutes
    # before the listing was read is not yet on every mirror.
    assert sorted(_rows(conn, "SELECT name_key FROM mb_slice_blobs")) == [("arrived late",), ("published",)]
    assert slice_checks.published_check(conn, "mb", newest, read_at) == []


def test_a_version_past_the_newest_published_is_neither_taken_nor_passed_on(conn, scratch_dsn):
    """The relay's own check sees what P1 convicts for: a node passes on no
    slice of a version later than MetaBrainz's newest as it last read the
    listing — a day ahead while it has never read it — and takes none."""
    author, relay = _key(), _key()
    kept = Subtree("Kept Before", version=dump_version(1))
    kept.group()
    _offer(scratch_dsn, author, kept, relay=relay)
    _answer(conn, "artist", kept.gid)
    _browse(conn, kept.gid, [g[1] for g in kept.tables["mb_release_group"]])
    for r in kept.tables["mb_release"]:
        _answer(conn, "release", r[1])
    slice_checks.request_check(conn, "mb", "kept before")
    assert slice_checks.evaluate(conn, "mb", "kept before") is True
    assert slice_checks.servable(conn, "mb", "kept before", OWN) is True
    with conn.cursor() as cur:                            # the listing read since: an older newest
        cur.execute("INSERT INTO user_settings (key, value) VALUES ('mb_slice.published', %s::jsonb)",
                    (json.dumps({"version": dump_version(5),
                                 "read_at": datetime.now(timezone.utc).isoformat()}),))
    assert slice_checks.servable(conn, "mb", "kept before", OWN) is False
    assert slice_checks.checkable_count(conn, "mb", OWN) == 0
    later = Subtree("Taken After", version=dump_version(2))
    later.group()
    out = _offer(scratch_dsn, author, later, relay=relay)
    assert later.name in out["missing"] and _count(conn, "mb_slice_blobs") == 1
    with conn.cursor() as cur:                            # never read: a day ahead at most
        cur.execute("DELETE FROM user_settings WHERE key = 'mb_slice.published'")
    ahead = Subtree("From The Future", version=_version(datetime.now(timezone.utc) + timedelta(days=3)))
    ahead.group()
    assert ahead.name in _offer(scratch_dsn, author, ahead, relay=relay)["missing"]
    assert _count(conn, "mb_slice_blobs") == 1


def test_an_lb_slice_from_an_unpublished_dump_proves_its_author_false_once_the_listing_is_read(conn, scratch_dsn):
    author, relay = _key(), _key()
    artist, recording = str(uuid.uuid4()), str(uuid.uuid4())
    one = {"dump_version": "20261106-000000", "artist": [10, 3], "recordings": [[recording, 5, 2, [artist]]],
           "truncated": False}
    peer = Peer({artist: lb_entry(author, artist, one)}, pub(relay))
    cycle = lb_slice_cycle.LbSliceCycle(scratch_dsn, connect=None, dead=DeadAddresses())
    cycle._published = "20261101-000000"                    # the listing read: refused at the door
    assert asyncio.run(cycle._exchange(peer, pub(relay), None, [artist], None)) == {
        "error": "all slices failed verification"}
    cycle._published = None                                 # not read yet: a day ahead at most
    assert asyncio.run(cycle._exchange(peer, pub(relay), None, [artist], None))["imported"] == 1
    with conn.cursor() as cur:
        cur.execute("UPDATE lb_slice_blobs SET created_at = now() - interval '7 hours'")
    out = slice_checks.published_check(conn, "lb", "20261101-000000", datetime.now(timezone.utc))
    # The relay took it within what its own check bounds: only the author answers for it.
    assert out == [pub(author)]
    assert _penalties(conn) == {pub(author): "author"}
    for table in ("lb_slice_blobs", "lb_slice_fetches", "lb_artist", "lb_recording"):
        assert _count(conn, table) == 0, table


def test_an_lb_slice_of_a_merged_artist_is_honest_under_the_artist_it_was_merged_into(conn, scratch_dsn):
    """An older ListenBrainz dump counts a recording for the artist's old
    MBID; MusicBrainz credits it to the artist that MBID was merged into."""
    author, relay = _key(), _key()
    old, merged_into, recording = (str(uuid.uuid4()) for _ in range(3))
    out = _offer_lb(scratch_dsn, author, {old: {"dump_version": dump_version(3), "artist": [10, 3],
                                                "recordings": [[recording, 5, 2, [old]]],
                                                "truncated": False}}, relay)
    assert out["imported"] == 1
    _slice_fed(conn)
    lb_slice_queries.serve_slices(conn, [old], own_pubkey=OWN)
    assert _waiting(conn, "lb", old) == {("recording", recording), ("artist", old)}
    with conn.cursor() as cur:
        cur.execute("INSERT INTO mbws_answers (kind, mbid, status, target) VALUES ('artist', %s, 'forwarded', %s)",
                    (old, merged_into))
    _credited(conn, recording, merged_into)
    assert slice_checks.evaluate(conn, "lb", old) is True
    assert slice_checks.servable(conn, "lb", old, OWN) is True


def test_a_node_is_quarantined_on_one_event_only_where_its_own_check_met_it(conn, scratch_dsn):
    """Rule 2 (b) — one event of an author convicted on it — binds a node
    that passed the blob on only when its own check necessarily drew the same
    fact: a subject artist of a blob with no more subjects than a check
    draws. Of a blob with more, the subject it skipped convicts no one but
    the author."""
    author, relay = _key(), _key()
    s = Subtree("Many Namesakes")
    for _ in range(slice_checks.SUBJECTS_PER_CHECK + 2):  # more subjects than a check draws
        extra = Subtree("Many Namesakes")
        s._add("mb_artist", id=extra.artist_id, gid=extra.gid, name=s.name, sort_name=s.name)
        s.tables["mb_artist_credit_name"].append(
            [s.credit, len(s.tables["mb_artist_credit_name"]), extra.artist_id, s.name, ""])
    group, (release,) = s.group()
    one = s.one()
    one["artists_matched"] = {s.name: [r[0] for r in s.tables["mb_artist"]]}
    peer = Peer({s.name: mb_entry(author, s.name, one)}, pub(relay))
    MBSliceClient(peer, db_dsn=scratch_dsn).run([s.name])
    gids = [r[1] for r in s.tables["mb_artist"]]
    for g in gids:
        _answer(conn, "artist", g, "unknown")
    _answer(conn, "release", release, "unknown")
    _answer(conn, "release_group", group, "unknown")
    slice_checks.request_check(conn, "mb", "many namesakes")
    for g in gids:                                        # whatever the draw, every answer is here
        _browse(conn, g, [])
    assert slice_checks.evaluate(conn, "mb", "many namesakes") is False
    # The author is convicted on one of the blob's own events, which its
    # source's check need not have drawn.
    assert _penalties(conn) == {pub(author): "author"}
    assert _rows(conn, "SELECT DISTINCT covered FROM slice_evidence") == [(False,)]
    assert _count(conn, "slice_evidence") == len(gids)


def test_metabrainz_listing_is_read_at_most_once_a_day(conn, monkeypatch):
    reads = []
    monkeypatch.setattr(published, "fetch", lambda family: reads.append(family) or "20261101-000000")
    version, read_at = published.newest(conn, "mb")
    assert (version, reads) == ("20261101-000000", ["mb"])
    assert published.newest(conn, "mb") == (version, read_at) and reads == ["mb"]
    with conn.cursor() as cur:                              # a day later
        cur.execute("UPDATE user_settings SET value = %s::jsonb WHERE key = 'mb_slice.published'",
                    (json.dumps({"version": version, "read_at": (read_at - timedelta(hours=25)).isoformat()}),))
    monkeypatch.setattr(published, "fetch", lambda family: reads.append(family) or None)
    assert published.newest(conn, "mb")[0] == "20261101-000000"     # unreadable: the record stands
    assert reads == ["mb", "mb"]
    assert published.newest(conn, "lb") is None


def test_an_unreadable_listing_is_asked_again_only_after_an_hour(conn, monkeypatch):
    reads = []
    monkeypatch.setattr(published, "fetch", lambda family: reads.append(family) or None)
    assert published.newest(conn, "mb") is None and reads == ["mb"]
    assert published.newest(conn, "mb") is None and reads == ["mb"]     # however often the cycles run
    with conn.cursor() as cur:                              # an hour later
        cur.execute("UPDATE user_settings SET value = %s::jsonb WHERE key = 'mb_slice.published'",
                    (json.dumps({"failed_at": (datetime.now(timezone.utc) - published.RETRY
                                               - timedelta(minutes=1)).isoformat()}),))
    monkeypatch.setattr(published, "fetch", lambda family: reads.append(family) or "20261101-000000")
    assert published.newest(conn, "mb")[0] == "20261101-000000" and reads == ["mb", "mb"]
    # A record read once keeps its version through a failure and its backoff.
    with conn.cursor() as cur:
        cur.execute("UPDATE user_settings SET value = value || %s::jsonb WHERE key = 'mb_slice.published'",
                    (json.dumps({"read_at": (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()}),))
    monkeypatch.setattr(published, "fetch", lambda family: reads.append(family) or None)
    assert published.newest(conn, "mb")[0] == "20261101-000000"
    assert published.newest(conn, "mb")[0] == "20261101-000000" and reads == ["mb", "mb", "mb"]


def test_a_payload_is_one_fact_however_often_it_is_checked(conn):
    author, relay = pub(_key()), pub(_key())
    payload, mbid = hashlib.sha256(b"one blob").hexdigest(), str(uuid.uuid4())
    first = slice_trust.record(conn, "mb", "a name", author, relay, "S1", "artist", mbid, dump_version(3), payload)
    assert first is not None
    assert slice_trust.record(conn, "mb", "a name", author, relay, "S1", "artist", mbid, dump_version(3),
                              payload) is None
    assert _count(conn, "slice_evidence") == 1


# --------------------------------------------------------------------------
# Review round 2
# --------------------------------------------------------------------------

def test_a_node_that_passed_on_any_event_its_author_was_convicted_on_is_quarantined(conn, scratch_dsn):
    """Rule 2 (b) reads every event that convicted the author, not only the
    one its penalty names — and at the conviction it judges every node that
    passed one of them on: the last of three, and the one that passed the
    first two."""
    author, first, last = _key(), _key(), _key()
    assert _suspect_check(conn, scratch_dsn, author, "Unknown 0", relay=first) is False
    assert _suspect_check(conn, scratch_dsn, author, "Unknown 1", relay=first) is False
    assert _suspect_check(conn, scratch_dsn, author, "Unknown 2", relay=last) is False
    assert _penalties(conn) == {pub(author): "author", pub(first): "relay", pub(last): "relay"}


def test_whether_a_node_checks_never_waits_on_the_catalogue_tables(conn, scratch_dsn):
    """A rebuild or a full load holds mb_artist while it rewrites it; the
    peer surface's health asks on every probe whether this node checks."""
    _offer(scratch_dsn, _key(), Subtree("Any Name"))
    locker = psycopg2.connect(scratch_dsn)
    reader = psycopg2.connect(scratch_dsn)
    reader.autocommit = True
    try:
        with locker.cursor() as cur:
            cur.execute("LOCK TABLE mb_artist IN ACCESS EXCLUSIVE MODE")
        with reader.cursor() as cur:
            cur.execute("SET statement_timeout = '2s'")
        assert mb_asks.node_asks(reader) is True
    finally:
        locker.rollback()
        locker.close()
        reader.close()


def test_a_check_that_ends_after_its_blob_was_replaced_stamps_nothing(conn, scratch_dsn):
    author = _key()
    first = Subtree("Replaced Name")
    first.group()
    _offer(scratch_dsn, author, first, relay=_key())
    read = slice_checks._blob(conn, "mb", "replaced name")
    newer = Subtree("Replaced Name", gid=first.gid, artist_id=first.artist_id)
    newer.group()
    newer.group()
    _offer(scratch_dsn, author, newer, relay=_key())              # a newer blob of the name, meanwhile
    slice_checks._finish(conn, "mb", "replaced name", read, facts=3, events=[])
    assert _rows(conn, "SELECT checked_at FROM mb_slice_blobs WHERE name_key = 'replaced name'") == [(None,)]
    assert _count(conn, "slice_tally") == 0


def test_a_check_asks_a_bounded_number_of_groups_outside_musicbrainzs_list(conn, scratch_dsn):
    s = Subtree("Many Groups")
    groups = {s.group()[0] for _ in range(slice_checks.GROUP_LOOKUPS + 4)}
    _offer(scratch_dsn, _key(), s, relay=_key())
    _answer(conn, "artist", s.gid)
    _browse(conn, s.gid, [])                                      # MusicBrainz lists none of them
    slice_checks.request_check(conn, "mb", "many groups", random.Random(1))
    assert slice_checks.evaluate(conn, "mb", "many groups") is None
    asked = {r[0] for r in _rows(conn, "SELECT mbid::text FROM mbws_queue WHERE kind = 'release_group'")}
    assert len(asked) == slice_checks.GROUP_LOOKUPS and asked <= groups
    # The groups never looked up are no facts this node checked.
    for g in asked:
        _answer(conn, "release_group", g)
    rel = mb_slice_queries.SLICE_TABLES["mb_release"].index("gid")
    for row in s.tables["mb_release"]:
        _answer(conn, "release", row[rel])
    assert slice_checks.evaluate(conn, "mb", "many groups") is True
    assert _rows(conn, "SELECT facts FROM slice_tally WHERE party = 'author'") == [
        (1 + slice_checks.GROUP_LOOKUPS,)]


def test_the_newest_published_version_never_moves_back(conn, monkeypatch):
    """A dump MetaBrainz withdrew after publishing it was published: the
    nodes that loaded it are no forgers."""
    monkeypatch.setattr(published, "fetch", lambda family: "20261104-000000")
    assert published.newest(conn, "mb")[0] == "20261104-000000"
    with conn.cursor() as cur:                                    # a day later
        cur.execute("UPDATE user_settings SET value = value || %s::jsonb WHERE key = 'mb_slice.published'",
                    (json.dumps({"read_at": (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()}),))
    monkeypatch.setattr(published, "fetch", lambda family: "20261101-000000")
    assert published.newest(conn, "mb")[0] == "20261104-000000"


def test_an_event_past_the_lookup_window_is_not_one_every_check_met(conn, scratch_dsn):
    """A group past the first GROUP_LOOKUPS the subject is credited with may
    not be among another node's lookups — its MusicBrainz list may have held
    more of the groups before it — so rule 2 (b) does not count it; and the
    facts counted are those this node checked: the subject, the groups
    MusicBrainz lists, the one looked up outside them."""
    relay = _key()
    s = Subtree("Window Name")
    groups = [s.group() for _ in range(slice_checks.GROUP_LOOKUPS + 1)]
    _offer(scratch_dsn, _key(), s, relay=relay)
    _answer(conn, "artist", s.gid)
    _browse(conn, s.gid, [g for g, _ in groups[:-1]])            # MusicBrainz lists all but the last
    for g, (release,) in groups[:-1]:
        _answer(conn, "release", release)
    last, (last_release,) = groups[-1]
    _answer(conn, "release_group", last, "unknown")
    _answer(conn, "release", last_release, "unknown")
    slice_checks.request_check(conn, "mb", "window name", random.Random(1))
    assert slice_checks.evaluate(conn, "mb", "window name") is False
    assert _rows(conn, "SELECT kind::text, covered FROM slice_evidence") == [("S2", False)]
    assert _rows(conn, "SELECT facts FROM slice_tally WHERE party = 'relay'") == [
        (1 + slice_checks.GROUP_LOOKUPS + 1,)]


def test_a_banned_author_s_kept_slices_are_not_passed_on(conn, scratch_dsn):
    author = _key()
    s = Subtree("Banned Name")
    s.group()
    _offer(scratch_dsn, author, s, relay=_key())
    with conn.cursor() as cur:
        cur.execute("UPDATE mb_slice_blobs SET checked_at = now()")
    assert slice_checks.servable(conn, "mb", "banned name", OWN) is True
    before = slice_checks.checkable_count(conn, "mb", OWN)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO p2p_node_bans (pubkey, reason) VALUES (%s, 'test')", (pub(author),))
    assert slice_checks.servable(conn, "mb", "banned name", OWN) is False
    assert slice_checks.checkable_count(conn, "mb", OWN) == before - 1


def test_a_verdict_waits_for_the_loader_lock_a_rebuild_holds(conn, scratch_dsn):
    """A verdict takes the loader's lock shared first: it waits for a rebuild
    (or a load) instead of meeting its rows half-way — no deadlock either way."""
    s = Subtree("Waiting Name")
    s.group()
    _offer(scratch_dsn, _key(), s, relay=_key())
    b = slice_checks._blob(conn, "mb", "waiting name")
    rebuilding = psycopg2.connect(scratch_dsn)
    verdict = psycopg2.connect(scratch_dsn)
    verdict.autocommit = True
    try:
        with rebuilding.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (mb_slice_queries.MB_LOAD_LOCK_KEY,))
        with verdict.cursor() as cur:
            cur.execute("SET lock_timeout = '200ms'")
        with pytest.raises(psycopg2.errors.LockNotAvailable):
            slice_checks._finish(verdict, "mb", "waiting name", b, facts=1, events=[])
        assert _rows(conn, "SELECT checked_at FROM mb_slice_blobs") == [(None,)]
    finally:
        rebuilding.rollback()
        rebuilding.close()
        verdict.close()


def test_at_a_conviction_every_node_that_passed_a_convicting_event_on_is_quarantined(conn, scratch_dsn):
    author = _key()
    relays = [_key() for _ in range(3)]
    for i, relay in enumerate(relays):                   # each event through another node
        assert _suspect_check(conn, scratch_dsn, author, f"Unknown {i}", relay=relay) is False
    assert _penalties(conn) == {pub(author): "author", **{pub(r): "relay" for r in relays}}


def test_a_name_is_keyed_one_way_in_the_ledger_and_the_blob(conn, scratch_dsn):
    """A non-breaking space PostgreSQL's btrim leaves: the ledger is keyed as
    the blob is, and the name is not asked again every cycle."""
    name = "Spaced Name "
    s = Subtree(name)
    s.group()
    _offer(scratch_dsn, _key(), s, relay=_key())
    key = mb_slice_queries.name_key(name)
    assert _rows(conn, "SELECT name_key FROM mb_slice_fetches") == [(key,)]
    assert _rows(conn, "SELECT name_key FROM mb_slice_blobs") == [(key,)]
    with conn.cursor() as cur:
        cur.execute("INSERT INTO artists (id, name) VALUES (%s, %s)", (str(uuid.uuid4()), name))
        cur.execute("INSERT INTO artist_mbids (mbid, artist_id, confidence) "
                    "SELECT %s, id, 'user' FROM artists WHERE name = %s", (s.gid, name))
    assert name not in [n for n, _tier in mb_slice_queries.pending_slice_names(conn)]
