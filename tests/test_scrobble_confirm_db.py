"""Imported scrobbles on a node with catalogue slices and no MusicBrainz
database (backend/canon/scrobbles.py, canon/confirm.py): a name is anchored
only on recordings MusicBrainz itself credits to the artist under a heard
title, the anchor is named by MusicBrainz, and a listen placed through the
catalogue rests on MusicBrainz's answer for its recording — the slice only
nominates. A node with the dump decides as before, asking nothing.

On the module's scratch database; MusicBrainz is tests/mb_fakes.py's fake."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import mb_fakes  # noqa: E402
from mb_fakes import FakeMusicBrainz, Slice, credit, drain, mbid  # noqa: E402

T0 = datetime(2026, 9, 20, 18, 0, tzinfo=timezone.utc)
SADE, KISS, SMOOTH, FORGED = mbid(), mbid(), mbid(), mbid()


@pytest.fixture
def node(scratch_dsn, monkeypatch):
    conn, pool, engine = mb_fakes.slice_fed(scratch_dsn, monkeypatch)
    fake = FakeMusicBrainz().install(monkeypatch)
    with conn.cursor() as cur:
        cur.execute("TRUNCATE pending_scrobbles, pending_scrobble_artists, listening_history, "
                    "mb_slice_fetches CASCADE")
        # The slice the node holds for the name: Sade's songs on Love Deluxe,
        # and a third the slice credits to her that MusicBrainz does not have.
        s = Slice(cur)
        a = s.artist(SADE, "Sade")
        c = s.credit((a, "Sade"))
        g = s.release_group(mbid(), "Love Deluxe", c)
        s.release(mbid(), "Love Deluxe", g, c, [("Kiss of Life", 330000, KISS),
                                                ("Smooth operator", 298000, SMOOTH),
                                                ("Nothing Like Hers", 200000, FORGED)])
        cur.execute("INSERT INTO mb_slice_fetches (name_key, matched_ids) VALUES ('sade', 1)")
    yield conn, fake
    conn.close()
    pool.closeall()
    engine.dispose()


def _wait(cur, minutes, title, artist="Sade"):
    import lastfm_history
    nk = lastfm_history.name_key(artist)
    cur.execute("INSERT INTO pending_scrobble_artists (name_key) VALUES (%s) ON CONFLICT DO NOTHING",
                (nk,))
    cur.execute("INSERT INTO pending_scrobbles (played_at, artist, title, name_key) "
                "VALUES (%s, %s, %s, %s)", (T0 + timedelta(minutes=minutes), artist, title, nk))


def _musicbrainz_has_sade(fake, name="Sade"):
    fake.artist(SADE, name)
    fake.recording(KISS, "Kiss of Life", 330000, credit((name, SADE)))
    fake.recording(SMOOTH, "Smooth Operator", 298000, credit((name, SADE)))


def _anchored(cur):
    cur.execute("""SELECT a.name, am.mbid::text FROM pending_scrobble_artists n
                     JOIN artists a ON a.id = n.artist_id
                     JOIN artist_mbids am ON am.artist_id = a.id""")
    return cur.fetchall()


def test_a_name_waits_for_musicbrainz_then_anchors_on_the_recordings_it_confirms(node):
    from canon import scrobbles
    conn, fake = node
    _musicbrainz_has_sade(fake)
    with conn.cursor() as cur:
        _wait(cur, 0, "Kiss of Life")
        _wait(cur, 5, "Smooth Operator")

    first = scrobbles.resolve_names()

    assert first["resolved"] == 0 and first["waiting"] == 1
    with conn.cursor() as cur:
        cur.execute("SELECT checked_at, artist_id FROM pending_scrobble_artists")
        assert cur.fetchall() == [(None, None)]
        assert _anchored(cur) == []
    assert drain() >= 2
    assert scrobbles.resolve_names()["resolved"] == 1
    with conn.cursor() as cur:
        assert _anchored(cur) == [("Sade", SADE)]


def test_the_anchor_row_is_named_by_musicbrainz_not_by_the_slice(node):
    from canon import scrobbles
    conn, fake = node
    _musicbrainz_has_sade(fake, name="Sadé")
    with conn.cursor() as cur:
        _wait(cur, 0, "Kiss of Life")

    scrobbles.resolve_names()
    drain()
    scrobbles.resolve_names()

    with conn.cursor() as cur:
        assert _anchored(cur) == [("Sadé", SADE)]


def test_recordings_musicbrainz_does_not_have_anchor_nothing(node):
    from canon import scrobbles
    conn, fake = node
    fake.artist(SADE, "Sade")        # the artist is real; the heard song is not hers on MusicBrainz
    with conn.cursor() as cur:
        _wait(cur, 0, "Nothing Like Hers")

    scrobbles.resolve_names()
    drain()
    stats = scrobbles.resolve_names()

    assert stats["resolved"] == 0
    with conn.cursor() as cur:
        assert _anchored(cur) == []
        cur.execute("SELECT checked_at IS NOT NULL FROM pending_scrobble_artists")
        assert cur.fetchone() == (True,)


def _resolved_artist(cur) -> str:
    from uuid_utils import artist_uuid
    artist = str(artist_uuid("Sade"))
    cur.execute("INSERT INTO artists (id, name) VALUES (%s, 'Sade') ON CONFLICT DO NOTHING", (artist,))
    cur.execute("INSERT INTO artist_mbids (mbid, artist_id, confidence) VALUES (%s, %s, 'phantom')",
                (SADE, artist))
    cur.execute("UPDATE pending_scrobble_artists SET artist_id = %s, checked_at = NULL", (artist,))
    return artist


def test_listens_are_placed_only_on_recordings_musicbrainz_confirms(node):
    from canon import scrobbles
    from uuid_utils import track_uuid
    conn, fake = node
    _musicbrainz_has_sade(fake)
    with conn.cursor() as cur:
        _wait(cur, 0, "Smooth Operator - Single Version")
        _wait(cur, 5, "Nothing Like Hers")
        artist = _resolved_artist(cur)

    first = scrobbles.bind_catalogue([artist])

    assert first["bound"] == 0 and first["waiting"] == 2
    drain()
    second = scrobbles.bind_catalogue([artist])
    assert second["bound"] == 1 and second["tracks_minted"] == 1
    assert second["not_in_catalogue"] == 1
    smooth = str(track_uuid("Smooth Operator", "Sade"))
    with conn.cursor() as cur:
        # The recording's own track, under MusicBrainz's title and length.
        cur.execute("SELECT title FROM tracks WHERE id = %s", (smooth,))
        assert cur.fetchone() == ("Smooth Operator",)
        cur.execute("SELECT track_id::text, duration_listened FROM listening_history")
        assert cur.fetchall() == [(smooth, 298)]
        cur.execute("SELECT title FROM pending_scrobbles")
        assert cur.fetchall() == [("Nothing Like Hers",)]


def test_a_recording_answered_inside_a_release_needs_no_request_of_its_own(node):
    import mb_answers
    from canon import scrobbles
    conn, fake = node
    rg = fake.release_group(mbid(), "Love Deluxe", credit(("Sade", SADE)))
    rel = fake.release(mbid(), "Love Deluxe", rg, credit(("Sade", SADE)),
                       [{"title": "Kiss of Life", "length": 330000, "recording": KISS}])
    mb_answers.store("release", rel, fake.lookup("release", rel))
    fake.asked.clear()
    with conn.cursor() as cur:
        _wait(cur, 0, "Kiss of Life")
        artist = _resolved_artist(cur)

    stats = scrobbles.bind_catalogue([artist])

    assert stats["bound"] == 1
    assert ("recording", KISS) not in fake.asked


def test_a_node_with_the_dump_decides_without_asking(node):
    from canon import scrobbles
    conn, fake = node
    with conn.cursor() as cur:
        cur.execute("INSERT INTO user_settings (key, value) VALUES ('musicbrainz.db_version', '\"x\"')")
        _wait(cur, 0, "Kiss of Life")

    assert scrobbles.resolve_names()["resolved"] == 1
    assert fake.asked == []
    with conn.cursor() as cur:
        assert _anchored(cur) == [("Sade", SADE)]


def test_a_heard_name_the_slice_hangs_on_another_real_artist_anchors_nothing(node):
    """The heard credit's head must name the artist on MusicBrainz's own
    strings: a real cover act's real recordings carry the heard titles, and
    the slice gives it the heard name as an alias."""
    from canon import scrobbles
    conn, fake = node
    covers, creep, karma = mbid(), mbid(), mbid()
    with conn.cursor() as cur:
        s = Slice(cur)
        a = s.artist(covers, "Karaoke Kings", aliases=["Radiohead"])
        c = s.credit((a, "Karaoke Kings"))
        g = s.release_group(mbid(), "Karaoke Hits", c)
        s.release(mbid(), "Karaoke Hits", g, c, [("Creep", 238000, creep),
                                                ("Karma Police", 264000, karma)])
        cur.execute("INSERT INTO mb_slice_fetches (name_key, matched_ids) VALUES ('radiohead', 1)")
        _wait(cur, 0, "Creep", artist="Radiohead")
        _wait(cur, 5, "Karma Police", artist="Radiohead")
    fake.artist(covers, "Karaoke Kings")
    fake.recording(creep, "Creep", 238000, credit(("Karaoke Kings", covers)))
    fake.recording(karma, "Karma Police", 264000, credit(("Karaoke Kings", covers)))

    scrobbles.resolve_names()
    drain()
    scrobbles.resolve_names()

    with conn.cursor() as cur:
        assert _anchored(cur) == []


def test_a_listen_is_not_placed_on_a_track_a_wrong_binding_names(node):
    """A local track bound to the recording (a peer's sealed binding, a
    slice's stamp) takes the listen only when it is MusicBrainz's recording by
    title: a binding of "Kiss of Life" onto another song places nothing there."""
    from canon import scrobbles
    from mb_fakes import Library
    from uuid_utils import track_uuid
    conn, fake = node
    _musicbrainz_has_sade(fake)
    with conn.cursor() as cur:
        Library(cur, "Sade").album("Promise", [("Is It a Crime", 380)])
        other = str(track_uuid("Is It a Crime", "Sade"))
        cur.execute("INSERT INTO track_mbids (recording_mbid, track_id, confidence) "
                    "VALUES (%s, %s, 'overlap_verified')", (KISS, other))
        _wait(cur, 0, "Kiss of Life")
        artist = _resolved_artist(cur)

    scrobbles.bind_catalogue([artist])
    drain()
    scrobbles.bind_catalogue([artist])

    with conn.cursor() as cur:
        cur.execute("SELECT track_id::text FROM listening_history")
        assert cur.fetchall() == [(str(track_uuid("Kiss of Life", "Sade")),)]


def test_a_listen_waiting_on_its_recording_is_placed_when_the_answer_wakes_its_name(node, monkeypatch):
    """The waker names the scrobble name, not its artist: a placement that
    waited is taken again by that pass, though the name's attempt was stamped
    before (stage B decided it, its shelf was minted) and no stage hands the
    pass its artist."""
    import lastfm_history
    from canon import scrobbles
    conn, fake = node
    monkeypatch.setattr(scrobbles, "_after_binding", lambda **kw: None)
    monkeypatch.setattr(lastfm_history, "resume_below_budget", lambda: None)
    _musicbrainz_has_sade(fake)
    with conn.cursor() as cur:
        _wait(cur, 0, "Kiss of Life")
        artist = _resolved_artist(cur)
        cur.execute("UPDATE artists SET last_album_sync = now() - interval '1 day' WHERE id = %s",
                    (artist,))
        cur.execute("UPDATE pending_scrobble_artists SET checked_at = now()")

    assert scrobbles.bind_catalogue([artist])["waiting"] == 1
    drain()
    # What the answer's waker runs: a pass over the woken name.
    scrobbles.run_pass({"full": False, "names": ["sade"], "artists": []})

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM listening_history")
        assert cur.fetchone() == (1,)
