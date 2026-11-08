"""A node with catalogue slices and no MusicBrainz database mints its phantom
shelves on MusicBrainz's answers (backend/discography.py, canon/confirm.py):
only the groups MusicBrainz's own browse credits to the artist, under its
titles, each with the tracklist MusicBrainz gives for a release it files
under that group. A shelf waits, unstamped, while an answer is missing; a
binding MusicBrainz refutes is withdrawn and stays so; a node with neither the
dump nor slices has no source. On a real PostgreSQL built from the
migrations, MusicBrainz faked (tests/mb_fakes.py) — skipped without a
cluster."""

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
for p in (REPO, REPO / "backend", REPO / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

pytest.importorskip("psycopg2")
pytest.importorskip("sqlalchemy")

import mb_fakes  # noqa: E402
from mb_fakes import FakeMusicBrainz, Library, Slice, credit, drain, mbid  # noqa: E402

ARTIST = "Nadia Reid"
A = "5a1d0000-0000-4000-8000-000000000001"
RG1 = "5a1d0000-0000-4000-8000-000000000011"
RG2 = "5a1d0000-0000-4000-8000-000000000012"
OTHER_RG = "5a1d0000-0000-4000-8000-000000000013"
# Lexically first, so the slice's pick asks it first.
R_ELSEWHERE = "00000000-0000-4000-8000-000000000021"
R1 = "5a1d0000-0000-4000-8000-000000000022"
R_FORGED = "5a1d0000-0000-4000-8000-000000000023"
REC1 = "5a1d0000-0000-4000-8000-000000000031"
REC2 = "5a1d0000-0000-4000-8000-000000000032"
REC3 = "5a1d0000-0000-4000-8000-000000000033"


@pytest.fixture
def node(scratch_dsn, monkeypatch):
    conn, pool, engine = mb_fakes.slice_fed(scratch_dsn, monkeypatch)
    with conn.cursor() as cur:
        cur.execute("TRUNCATE mb_slice_fetches, mbws_waiters")
    fake = FakeMusicBrainz().install(monkeypatch)
    yield conn, fake
    conn.close()
    pool.closeall()
    engine.dispose()


def _phantom(cur, name: str, gid: str) -> str:
    """A similar-artist stub bound to `gid`, its name answered by a slice."""
    from uuid_utils import artist_uuid
    aid = str(artist_uuid(name))
    cur.execute("INSERT INTO artists (id, name) VALUES (%s, %s)", (aid, name))
    cur.execute("INSERT INTO artist_mbids (mbid, artist_id, confidence) VALUES (%s, %s, 'phantom')",
                (gid, aid))
    cur.execute("INSERT INTO mb_slice_fetches (name_key, matched_ids) VALUES (lower(%s), 1)", (name,))
    return aid


def _sync_until_settled(aid: str, name: str, rounds: int = 8) -> list:
    """The shelf's sync, MusicBrainz's answers delivered between rounds the
    way the consumer delivers them; returns every round's status."""
    from discography import sync_artist_discography
    statuses = []
    for _ in range(rounds):
        statuses.append(sync_artist_discography(aid, name)["status"])
        if statuses[-1] != "waiting":
            return statuses
        assert drain() > 0
    return statuses


def _shelf(cur, aid: str) -> list:
    cur.execute("""
        SELECT al.musicbrainz_id::text, al.title,
               array_agg(t.title ORDER BY atr.position), array_agg(atr.length_ms ORDER BY atr.position)
          FROM albums al
          JOIN album_artists aa ON aa.album_id = al.id AND aa.artist_id = %s
          JOIN album_tracks atr ON atr.album_id = al.id
          JOIN tracks t ON t.id = atr.track_id
         GROUP BY al.id ORDER BY al.title""", (aid,))
    return cur.fetchall()


def test_a_shelf_holds_only_the_groups_musicbrainz_lists_under_its_titles(node):
    conn, fake = node
    with conn.cursor() as cur:
        aid = _phantom(cur, ARTIST, A)
        s = Slice(cur)
        a = s.artist(A, ARTIST)
        c = s.credit((a, ARTIST))
        g1 = s.release_group(RG1, "Out of my Province (2020)", c)
        s.release(R1, "Out of my Province", g1, c,
                  [("Get the Devil Out", 200000, REC1), ("Other Side", 180000, REC2)])
        g2 = s.release_group(RG2, "An Album MusicBrainz Never Had", c)
        s.release(R_FORGED, "An Album MusicBrainz Never Had", g2, c, [("Filler", 100000, REC3)])
    fake.artist(A, ARTIST)
    fake.release_group(RG1, "Out of My Province", credit((ARTIST, A)))
    fake.release(R1, "Out of My Province", RG1, credit((ARTIST, A)),
                 [{"title": "Get The Devil Out", "length": 201000, "recording": REC1},
                  {"title": "Other Side", "length": 181000, "recording": REC2}])

    from discography import sync_artist_discography
    assert sync_artist_discography(aid, ARTIST)["status"] == "waiting"
    with conn.cursor() as cur:
        cur.execute("SELECT last_album_sync FROM artists WHERE id = %s", (aid,))
        assert cur.fetchone() == (None,)
        cur.execute("SELECT subject::text, subject_key FROM mbws_waiters")
        assert set(cur.fetchall()) == {("discography", aid)}
    assert drain() > 0

    assert _sync_until_settled(aid, ARTIST)[-1] == "success"
    with conn.cursor() as cur:
        assert _shelf(cur, aid) == [(RG1, "Out of My Province",
                                     ["Get The Devil Out", "Other Side"], [201000, 181000])]
        cur.execute("SELECT count(*) FROM albums WHERE musicbrainz_id = %s", (RG2,))
        assert cur.fetchone() == (0,)
        cur.execute("SELECT last_album_sync IS NOT NULL FROM artists WHERE id = %s", (aid,))
        assert cur.fetchone() == (True,)
    assert ("release", R_FORGED) not in fake.asked


def test_a_tracklist_comes_from_a_release_musicbrainz_files_under_the_group(node):
    conn, fake = node
    with conn.cursor() as cur:
        aid = _phantom(cur, ARTIST, A)
        s = Slice(cur)
        a = s.artist(A, ARTIST)
        c = s.credit((a, ARTIST))
        g1 = s.release_group(RG1, "Out of My Province", c)
        # The slice files a release of another group under this one, and it
        # sorts first in the pick.
        s.release(R_ELSEWHERE, "Stranger Things", g1, c,
                  [("Intruder", 100000, REC3), ("Second", 120000, REC2)])
        s.release(R1, "Out of My Province", g1, c,
                  [("Get the Devil Out", 200000, REC1), ("Other Side", 180000, REC2)])
    fake.artist(A, ARTIST)
    fake.release_group(RG1, "Out of My Province", credit((ARTIST, A)))
    fake.release_group(OTHER_RG, "Stranger Things", credit((ARTIST, A)), primary="Single")
    fake.release(R_ELSEWHERE, "Stranger Things", OTHER_RG, credit((ARTIST, A)),
                 [{"title": "Intruder", "length": 100000, "recording": REC3},
                  {"title": "Second", "length": 120000, "recording": REC2}])
    fake.release(R1, "Out of My Province", RG1, credit((ARTIST, A)),
                 [{"title": "Get The Devil Out", "length": 201000, "recording": REC1},
                  {"title": "Other Side", "length": 181000, "recording": REC2}])

    assert _sync_until_settled(aid, ARTIST)[-1] == "success"

    with conn.cursor() as cur:
        assert _shelf(cur, aid) == [(RG1, "Out of My Province",
                                     ["Get The Devil Out", "Other Side"], [201000, 181000])]
    assert ("release", R_ELSEWHERE) in fake.asked and ("release", R1) in fake.asked


def test_a_binding_musicbrainz_refutes_is_withdrawn_and_stays_withdrawn(node):
    conn, fake = node
    with conn.cursor() as cur:
        aid = _phantom(cur, ARTIST, A)
        s = Slice(cur)
        a = s.artist(A, ARTIST)
        c = s.credit((a, ARTIST))
        g1 = s.release_group(RG1, "Out of My Province", c)
        s.release(R1, "Out of My Province", g1, c, [("Get the Devil Out", 200000, REC1)])
    # MusicBrainz's artist under that MBID goes by another name entirely.
    fake.artist(A, "Somebody Else Entirely")

    assert _sync_until_settled(aid, ARTIST)[-1] == "not_canonized"

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM artist_mbids WHERE artist_id = %s", (aid,))
        assert cur.fetchone() == (0,)
        cur.execute("SELECT artist_id::text, mbid::text FROM mb_refuted_bindings")
        assert cur.fetchall() == [(aid, A)]
        cur.execute("INSERT INTO artist_mbids (mbid, artist_id, confidence) VALUES (%s, %s, 'phantom')",
                    (A, aid))
        assert cur.rowcount == 0
        cur.execute("SELECT count(*) FROM albums WHERE musicbrainz_id = %s", (RG1,))
        assert cur.fetchone() == (0,)


def test_a_node_with_neither_dump_nor_slices_has_no_source_and_asks_nothing(node, monkeypatch):
    conn, fake = node
    import mb_backend
    monkeypatch.setattr(mb_backend, "LOCAL_DUMP", False)
    with conn.cursor() as cur:
        aid = _phantom(cur, ARTIST, A)

    from discography import stale_canonized_artists, sync_artist_discography
    assert sync_artist_discography(aid, ARTIST)["status"] == "no_source"
    assert stale_canonized_artists() == []
    assert fake.asked == []
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM mbws_queue")
        assert cur.fetchone() == (0,)


# ---- review round 3 of the MusicBrainz side

def _owned_artist(cur, albums: dict) -> str:
    """An owned artist bound to A, each album bound to A and naming its
    covering release (title → (tracks, release MBID))."""
    lib = Library(cur, ARTIST)
    for title, (tracks, rel) in albums.items():
        al = lib.album(title, tracks)
        cur.execute("UPDATE album_artists SET mbid = %s WHERE album_id = %s", (A, al))
        cur.execute("UPDATE album_variants SET release_mbid = %s WHERE album_id = %s", (rel, al))
    cur.execute("INSERT INTO artist_mbids (mbid, artist_id, confidence) "
                "VALUES (%s, %s, 'overlap_verified')", (A, lib.id))
    cur.execute("INSERT INTO mb_slice_fetches (name_key, matched_ids) VALUES (lower(%s), 1)", (ARTIST,))
    return lib.id


def _musicbrainz_has_the_group(fake):
    by = credit((ARTIST, A))
    fake.artist(A, ARTIST)
    fake.release_group(RG1, "Out of My Province", by)
    fake.release(R1, "Out of My Province", RG1, by,
                 [{"title": "Get The Devil Out", "length": 201000, "recording": REC1},
                  {"title": "Other Side", "length": 181000, "recording": REC2}])


def test_a_shelf_its_binding_proves_by_one_album_does_not_wait_on_another(node):
    """One album proves the binding: the question about another album's
    release does not hold the shelf back."""
    import mb_answers
    from discography import _confirmed_groups
    conn, fake = node
    with conn.cursor() as cur:
        aid = _owned_artist(cur, {
            "Preservation": ([(f"Song {i}", 200 + i) for i in range(6)], R_FORGED),
            "Out of My Province": ([("Get The Devil Out", 201), ("Other Side", 181)], R1)})
    _musicbrainz_has_the_group(fake)
    mb_answers.enqueue([("artist", A), ("artist_release_groups", A), ("release", R1)],
                       mb_answers.PRIORITY_OWNED)
    drain()

    asks = mb_answers.Asks("discography", aid, mb_answers.PRIORITY_DISCOVERY)
    assert _confirmed_groups(aid, ARTIST, [A], asks) is not None
    assert not asks.waiting


def test_a_shelf_whose_binding_is_unproven_keeps_its_albums_and_mints_nothing(node):
    """MusicBrainz knows the artist but not the release the slice stamped on
    the owned album: nothing rests on the binding — no mint, and no reconcile
    that would empty the shelf an earlier sync minted."""
    conn, fake = node
    with conn.cursor() as cur:
        aid = _owned_artist(cur, {"Preservation": ([("Reaching Through", 210), ("Richard", 190)],
                                                    R_FORGED)})
        cur.execute("INSERT INTO albums (id, title, musicbrainz_id) "
                    "VALUES (gen_random_uuid(), 'Out of My Province', %s) RETURNING id", (RG1,))
        phantom = cur.fetchone()[0]
        cur.execute("INSERT INTO album_artists (album_id, artist_id, role) VALUES (%s, %s, 'primary')",
                    (phantom, aid))
    fake.artist(A, ARTIST)

    assert _sync_until_settled(aid, ARTIST)[-1] == "unproven"
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM album_artists WHERE album_id = %s", (phantom,))
        assert cur.fetchone() == (1,)
        cur.execute("SELECT count(*) FROM mb_refuted_bindings")
        assert cur.fetchone() == (0,)


def test_another_artists_album_naming_the_group_unlinks_no_phantom_of_it(node):
    """Another owned album names this shelf's group on a slice's word alone:
    the shelf does not mint the group again, and does not unlink its phantom."""
    conn, fake = node
    with conn.cursor() as cur:
        aid = _phantom(cur, ARTIST, A)
        s = Slice(cur)
        a = s.artist(A, ARTIST)
        c = s.credit((a, ARTIST))
        g1 = s.release_group(RG1, "Out of My Province", c)
        s.release(R1, "Out of My Province", g1, c, [("Get The Devil Out", 201000, REC1),
                                                     ("Other Side", 181000, REC2)])
    _musicbrainz_has_the_group(fake)
    assert _sync_until_settled(aid, ARTIST)[-1] == "success"
    with conn.cursor() as cur:
        assert [r[0] for r in _shelf(cur, aid)] == [RG1]
        other = Library(cur, "Somebody Else")
        al = other.album("Something", [("A Song", 200)])
        cur.execute("UPDATE albums SET musicbrainz_id = %s WHERE id = %s", (RG1, al))
        cur.execute("UPDATE artists SET last_album_sync = NULL WHERE id = %s", (aid,))

    assert _sync_until_settled(aid, ARTIST)[-1] == "success"
    with conn.cursor() as cur:
        assert [r[0] for r in _shelf(cur, aid)] == [RG1]


def test_a_credit_head_named_as_musicbrainz_credits_it_keeps_its_binding(node):
    """A head minted under its credited name before heads took MusicBrainz's
    own: MusicBrainz credits the artist so on a recording, and that is the
    rule the head was bound on — the owned tiers' rule does not refute it."""
    import mb_answers
    from canon import confirm
    conn, fake = node
    head, rec = mbid(), mbid()
    with conn.cursor() as cur:
        aid = _phantom(cur, "Biggie Smalls", head)
    fake.artist(head, "The Notorious B.I.G.")
    fake.recording(rec, "Juicy", 300000, [{"name": "Biggie Smalls", "joinphrase": "",
                                           "artist": {"id": head, "name": "The Notorious B.I.G."}}])
    mb_answers.enqueue([("artist", head), ("recording", rec)], mb_answers.PRIORITY_OWNED)
    drain()

    asks = mb_answers.Asks("discography", aid, mb_answers.PRIORITY_DISCOVERY)
    assert confirm.binding(aid, "Biggie Smalls", head, asks) is True
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM mb_refuted_bindings")
        assert cur.fetchone() == (0,)
