"""The phantom canon and the credit heads on a node with catalogue slices and
no MusicBrainz database (backend/canon/phantom.py, canon/confirm.py): a
similar-artist stub is bound only to an artist MusicBrainz knows under the
stub's name, namesakes are told apart on MusicBrainz's own genres, nothing is
deleted for want of an answer, a stub kept unbound is taken again only once
something could change its answer (or when its answers wake it), and a credit
head is anchored on MusicBrainz's credit of its recording. A node with the
dump keeps discarding the stubs it cannot bind.

On the module's scratch database; MusicBrainz is tests/mb_fakes.py's fake."""

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import mb_fakes  # noqa: E402
from mb_fakes import FakeMusicBrainz, Slice, credit, drain, mbid  # noqa: E402

SEED_MBID = mbid()


@pytest.fixture
def node(scratch_dsn, monkeypatch):
    conn, pool, engine = mb_fakes.slice_fed(scratch_dsn, monkeypatch)
    fake = FakeMusicBrainz().install(monkeypatch)
    with conn.cursor() as cur:
        cur.execute("TRUNCATE listening_history, similar_artists CASCADE")
    yield conn, fake
    conn.close()
    pool.closeall()
    engine.dispose()


def _stub(cur, name: str) -> str:
    """A similar-artist stub of a library artist; the library artist's MBID
    is SEED_MBID."""
    seed = str(uuid.uuid4())
    stub = str(uuid.uuid4())
    cur.execute("INSERT INTO artists (id, name) VALUES (%s, %s), (%s, %s)",
                (seed, f"Seed {seed[:6]}", stub, name))
    cur.execute("INSERT INTO artist_mbids (mbid, artist_id, confidence) VALUES (%s, %s, 'user') "
                "ON CONFLICT DO NOTHING", (SEED_MBID, seed))
    cur.execute("INSERT INTO similar_artists (artist_id, similar_artist_id, match_score, source) "
                "VALUES (%s, %s, 0.9, 'lastfm')", (seed, stub))
    return stub


def _binding(cur, stub):
    cur.execute("SELECT mbid::text, confidence::text FROM artist_mbids WHERE artist_id = %s", (stub,))
    return cur.fetchall()


def _exists(cur, artist_id) -> bool:
    cur.execute("SELECT 1 FROM artists WHERE id = %s", (artist_id,))
    return cur.fetchone() is not None


def test_a_stub_waits_for_musicbrainz_then_binds_the_artist_it_names(node):
    from canon.phantom import canonize_phantom_similars
    conn, fake = node
    gid = mbid()
    fake.artist(gid, "Basil O'Glue")
    with conn.cursor() as cur:
        Slice(cur).artist(gid, "Basil O'Glue")
        stub = _stub(cur, "Basil O'Glue")

    first = canonize_phantom_similars()

    assert first["canonized"] == 0 and first.get("waiting") == 1
    with conn.cursor() as cur:
        assert _exists(cur, stub) and _binding(cur, stub) == []
    drain()
    assert canonize_phantom_similars()["canonized"] == 1
    with conn.cursor() as cur:
        assert _binding(cur, stub) == [(gid, "phantom")]


def test_a_candidate_musicbrainz_does_not_know_binds_nothing_and_the_stub_stays(node):
    from canon.phantom import canonize_phantom_similars
    conn, fake = node
    with conn.cursor() as cur:
        Slice(cur).artist(mbid(), "Basil O'Glue")
        stub = _stub(cur, "Basil O'Glue")

    canonize_phantom_similars()
    drain()
    stats = canonize_phantom_similars()

    assert stats["not_in_mb"] == 1 and stats["discarded"] == 0
    with conn.cursor() as cur:
        assert _exists(cur, stub) and _binding(cur, stub) == []


def test_an_alias_only_the_slice_carries_binds_nothing(node):
    from canon.phantom import canonize_phantom_similars
    conn, fake = node
    gid = mbid()
    fake.artist(gid, "Someone Else")
    with conn.cursor() as cur:
        Slice(cur).artist(gid, "Someone Else", aliases=["Basil O'Glue"])
        stub = _stub(cur, "Basil O'Glue")

    canonize_phantom_similars()
    drain()
    canonize_phantom_similars()

    with conn.cursor() as cur:
        assert _binding(cur, stub) == []


def test_namesakes_are_told_apart_on_musicbrainz_genres(node):
    from canon.phantom import canonize_phantom_similars
    conn, fake = node
    jazz, metal = mbid(), mbid()
    fake.artist(jazz, "Basil", genres=["jazz"])
    fake.artist(metal, "Basil", genres=["metal"])
    fake.artist(SEED_MBID, "Seed", genres=["jazz", "soul"])
    with conn.cursor() as cur:
        s = Slice(cur)
        s.artist(jazz, "Basil")
        s.artist(metal, "Basil")
        stub = _stub(cur, "Basil")

    for _ in range(3):
        canonize_phantom_similars()
        drain()

    with conn.cursor() as cur:
        assert _binding(cur, stub) == [(jazz, "phantom")]


def test_a_node_with_the_dump_still_discards_what_it_cannot_bind(node):
    from canon.phantom import canonize_phantom_similars
    conn, fake = node
    with conn.cursor() as cur:
        cur.execute("INSERT INTO user_settings (key, value) VALUES ('musicbrainz.db_version', '\"x\"')")
        stub = _stub(cur, "Nobody Of That Name")

    stats = canonize_phantom_similars()

    assert stats["discarded"] == 1 and fake.asked == []
    with conn.cursor() as cur:
        assert not _exists(cur, stub)


def test_a_stub_kept_unbound_is_taken_again_only_once_something_could_change_its_answer(node):
    from canon.phantom import canonize_phantom_similars
    conn, fake = node
    with conn.cursor() as cur:
        cur.execute("TRUNCATE mb_slice_fetches")
        stub = _stub(cur, "Kept Stub Of No Slice")

    first = canonize_phantom_similars(limit=50)
    assert first["phantoms"] == 1 and first["not_in_mb"] == 1
    assert canonize_phantom_similars(limit=50)["phantoms"] == 0

    with conn.cursor() as cur:
        # A slice for its name is fetched after the try (a zero-match too).
        cur.execute("UPDATE artists SET last_mb_sync = now() - interval '1 hour' WHERE id = %s", (stub,))
        cur.execute("INSERT INTO mb_slice_fetches (name_key, matched_ids) "
                    "VALUES ('kept stub of no slice', 0)")
    assert canonize_phantom_similars(limit=50)["phantoms"] == 1
    assert canonize_phantom_similars(limit=50)["phantoms"] == 0

    with conn.cursor() as cur:
        # Its answers have gone stale since.
        cur.execute("TRUNCATE mb_slice_fetches")
        cur.execute("UPDATE artists SET last_mb_sync = now() - interval '31 days' WHERE id = %s", (stub,))
    assert canonize_phantom_similars(limit=50)["phantoms"] == 1
    with conn.cursor() as cur:
        assert _exists(cur, stub) and _binding(cur, stub) == []


def test_the_background_canon_step_rests_once_every_kept_stub_was_tried(node):
    import background_enrichment
    conn, fake = node
    with conn.cursor() as cur:
        cur.execute("TRUNCATE mb_slice_fetches")
        _stub(cur, "Kept One")
        _stub(cur, "Kept Two")

    first = background_enrichment._step_canonize(2)
    assert "errors" not in first and first.get("retry_soon")
    second = background_enrichment._step_canonize(2)
    assert "errors" not in second and not second.get("retry_soon")


def test_a_stub_its_answers_woke_is_taken_whatever_its_watermark_says(node):
    from canon.phantom import canonize_phantom_similars
    conn, fake = node
    gid = mbid()
    fake.artist(gid, "Basil O'Glue")
    with conn.cursor() as cur:
        cur.execute("TRUNCATE mb_slice_fetches")
        Slice(cur).artist(gid, "Basil O'Glue")
        stub = _stub(cur, "Basil O'Glue")
        other = _stub(cur, "Not Woken")
        cur.execute("UPDATE artists SET last_mb_sync = now() WHERE id = ANY(%s::uuid[])", ([stub, other],))

    assert canonize_phantom_similars()["phantoms"] == 0
    asked = canonize_phantom_similars(artist_ids=[stub])
    assert asked["phantoms"] == 1 and asked.get("waiting") == 1
    drain()
    woken = canonize_phantom_similars(artist_ids=[stub])

    assert woken["phantoms"] == 1 and woken["canonized"] == 1
    with conn.cursor() as cur:
        assert _binding(cur, stub) == [(gid, "phantom")] and _binding(cur, other) == []


def _head(cur, name: str, recording: str) -> str:
    """A credit head: a phantom slot credited to it, listened to, no files."""
    head = str(uuid.uuid4())
    track = str(uuid.uuid4())
    album = str(uuid.uuid4())
    cur.execute("INSERT INTO artists (id, name) VALUES (%s, %s)", (head, name))
    cur.execute("INSERT INTO tracks (id, title) VALUES (%s, 'Tune')", (track,))
    cur.execute("INSERT INTO track_artists (track_id, artist_id, role) VALUES (%s, %s, 'primary')",
                (track, head))
    cur.execute("INSERT INTO albums (id, title) VALUES (%s, 'Comp')", (album,))
    cur.execute("INSERT INTO album_tracks (album_id, track_id, position, recording_mbid, length_ms) "
                "VALUES (%s, %s, 1, %s, 200000)", (album, track, recording))
    cur.execute("INSERT INTO listening_history (track_id, started_at, ended_at, duration_listened, "
                "completed, skipped) VALUES (%s, %s, %s, 200, TRUE, FALSE)",
                (track, datetime(2026, 9, 1, tzinfo=timezone.utc),
                 datetime(2026, 9, 1, 0, 4, tzinfo=timezone.utc)))
    return head


def test_a_credit_head_is_anchored_on_musicbrainz_credit_of_its_recording(node):
    from canon.phantom import canonize_engaged_credit_heads
    conn, fake = node
    rec, mike = mbid(), mbid()
    fake.recording(rec, "Tune", 200000, credit(("Mike", mike)))
    with conn.cursor() as cur:
        head = _head(cur, "Mike", rec)

    first = canonize_engaged_credit_heads()

    assert first["canonized"] == 0 and first.get("waiting") == 1
    with conn.cursor() as cur:
        cur.execute("SELECT last_mb_sync FROM artists WHERE id = %s", (head,))
        assert cur.fetchone() == (None,)
    drain()
    assert canonize_engaged_credit_heads()["canonized"] == 1
    with conn.cursor() as cur:
        assert _binding(cur, head) == [(mike, "name_exact")]


def test_a_credit_naming_someone_else_anchors_nothing(node):
    from canon.phantom import canonize_engaged_credit_heads
    conn, fake = node
    rec = mbid()
    fake.recording(rec, "Tune", 200000, credit(("Somebody", mbid())))
    with conn.cursor() as cur:
        head = _head(cur, "Mike", rec)
        # The slice credits the recording to a Mike of its own making.
        s = Slice(cur)
        a = s.artist(mbid(), "Mike")
        c = s.credit((a, "Mike"))
        g = s.release_group(mbid(), "Comp", c)
        s.release(mbid(), "Comp", g, c, [("Tune", 200000, rec)])

    canonize_engaged_credit_heads()
    drain()
    stats = canonize_engaged_credit_heads()

    assert stats["canonized"] == 0 and stats["unresolved"] == 1
    with conn.cursor() as cur:
        assert _binding(cur, head) == []


def test_a_compound_stub_is_not_bound_to_a_member_by_the_members_alias(node):
    """A collaboration's "X and Y" is an alias of member X on MusicBrainz: the
    phantom canon takes no alias for a compound, and neither does its check."""
    from canon.phantom import canonize_phantom_similars
    conn, fake = node
    vangelis = mbid()
    fake.artist(vangelis, "Vangelis", aliases=["Vangelis and Irene Papas"])
    with conn.cursor() as cur:
        # The slice adds a spelling the nomination probes ("The …").
        Slice(cur).artist(vangelis, "Vangelis", aliases=["The Vangelis and Irene Papas"])
        stub = _stub(cur, "Vangelis and Irene Papas")

    canonize_phantom_similars()
    drain()
    canonize_phantom_similars()

    with conn.cursor() as cur:
        assert _binding(cur, stub) == []
