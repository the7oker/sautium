"""The owned canon of a node with catalogue slices and no dump re-made on
MusicBrainz's answers (backend/canon/content.py, backend/canon/confirm.py):
the slice nominates, MusicBrainz decides. A rename, a merge, an edition's
title, a collaboration's split and a name-only binding happen only on what
MusicBrainz's own answers for the nominated MBIDs confirm, and write
MusicBrainz's values; a premise they refute binds nothing, ever again; a
decision whose answers are not here waits with its artist unstamped. A node
holding the dump asks nothing and behaves as before.

On a scratch PostgreSQL built from the migrations; the slice is rows in the
mb_* tables (tests/mb_fakes.Slice), MusicBrainz is FakeMusicBrainz."""

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

psycopg2 = pytest.importorskip("psycopg2")
pytest.importorskip("sqlalchemy")

from tests.mb_fakes import FakeMusicBrainz, Library, Slice, credit, drain, mbid, slice_fed  # noqa: E402

SONGS = [("Wouldn't It Be Nice", 153), ("You Still Believe in Me", 157),
         ("That's Not Me", 149), ("Don't Talk", 173)]


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


def _catalogue(cur, artist_gid, artist_name, aliases, group_gid, group_title, release_gid,
               songs=SONGS):
    """A slice: the artist, its credit, a group with one release of `songs`."""
    s = Slice(cur)
    a = s.artist(artist_gid, artist_name, aliases=aliases)
    c = s.credit((a, artist_name))
    g = s.release_group(group_gid, group_title, c)
    recs = [mbid() for _ in songs]
    s.release(release_gid, group_title, g, c,
              [(t, sec * 1000, r) for (t, sec), r in zip(songs, recs)])
    return recs


def _musicbrainz(mb, artist_gid, name, aliases, group_gid, group_title, release_gid, recs,
                 songs=SONGS):
    by = credit((name, artist_gid))
    mb.artist(artist_gid, name, aliases=aliases)
    mb.release_group(group_gid, group_title, by)
    mb.release(release_gid, group_title, group_gid, by,
               [{"title": t, "length": sec * 1000, "recording": r} for (t, sec), r in zip(songs, recs)])


def _settle(run, rounds=6):
    """The canon and MusicBrainz's answers in turn, as the consumer's wakes
    run them, until nothing waits; returns the last run's result."""
    for _ in range(rounds):
        out = run()
        if not (out.get("waiting") if isinstance(out, dict) else out):
            return out
        drain()
    raise AssertionError("still waiting after %d rounds" % rounds)


def _artist_rows(cur):
    cur.execute("""SELECT a.name, am.mbid::text FROM artists a
                     LEFT JOIN artist_mbids am ON am.artist_id = a.id
                    WHERE EXISTS (SELECT 1 FROM track_artists ta WHERE ta.artist_id = a.id)
                    ORDER BY a.name""")
    return cur.fetchall()


def test_a_rename_waits_for_musicbrainz_then_takes_its_name(node, mb):
    from canon.content import canonicalize_pending
    band, group, rel = mbid(), mbid(), mbid()
    with node.cursor() as cur:
        Library(cur, "Beach Boys").album("Pet Sounds", SONGS)
        # The slice's spelling is not MusicBrainz's current one.
        recs = _catalogue(cur, band, "Beach Boys, The", ["Beach Boys"], group, "Pet Sounds", rel)
    _musicbrainz(mb, band, "The Beach Boys", ["Beach Boys"], group, "Pet Sounds", rel, recs)

    first = canonicalize_pending()
    assert first["waiting"] == 1
    with node.cursor() as cur:
        assert [r[0] for r in _artist_rows(cur)] == ["Beach Boys"]
        cur.execute("SELECT last_mb_sync FROM artists WHERE name = 'Beach Boys'")
        assert cur.fetchone() == (None,)
        cur.execute("SELECT DISTINCT subject::text FROM mbws_waiters")
        assert cur.fetchall() == [("owned_artist",)]

    drain()
    _settle(canonicalize_pending)

    with node.cursor() as cur:
        assert _artist_rows(cur) == [("The Beach Boys", band)]
    assert {k for k, _ in mb.asked} <= {"artist", "release", "release_group", "recording"}
    assert ("artist", band) in mb.asked and ("release", rel) in mb.asked


def test_a_forged_alias_on_a_real_mbid_renames_nothing_and_binds_it_never_again(node, mb):
    from canon.content import canonicalize_pending
    stranger, group, rel = mbid(), mbid(), mbid()
    with node.cursor() as cur:
        Library(cur, "Beach Boys").album("Pet Sounds", SONGS)
        # A real MBID whose slice row claims the owned name as an alias.
        recs = _catalogue(cur, stranger, "Brian Wilson", ["Beach Boys"], group, "Pet Sounds", rel)
    _musicbrainz(mb, stranger, "Brian Wilson", [], group, "Pet Sounds", rel, recs)

    _settle(canonicalize_pending)

    with node.cursor() as cur:
        assert _artist_rows(cur) == [("Beach Boys", None)]
        cur.execute("SELECT mbid::text FROM mb_refuted_bindings")
        assert cur.fetchall() == [(stranger,)]
        # the slice still says it; a new canon run cannot bind it again
        cur.execute("UPDATE artists SET last_mb_sync = NULL")
    _settle(canonicalize_pending)
    with node.cursor() as cur:
        assert _artist_rows(cur) == [("Beach Boys", None)]
        cur.execute("SELECT count(*) FROM album_artists WHERE mbid IS NOT NULL")
        assert cur.fetchone() == (0,)


def test_recordings_forged_to_the_owned_titles_on_a_real_group_rename_nothing(node, mb):
    """…and refute nothing: the pointers were the slice's to write, the artist
    is MusicBrainz's own — the binding stays, unproven, and nothing rests on it."""
    from canon import confirm
    from canon.content import canonicalize_pending
    import mb_answers
    band, group, forged_release = mbid(), mbid(), mbid()
    with node.cursor() as cur:
        Library(cur, "Beach Boys").album("Pet Sounds", SONGS)
        recs = _catalogue(cur, band, "The Beach Boys", ["Beach Boys"], group, "Pet Sounds",
                          forged_release)
    # MusicBrainz knows the artist and the group, not that release nor those recordings.
    by = credit(("The Beach Boys", band))
    mb.artist(band, "The Beach Boys", aliases=["Beach Boys"])
    mb.release_group(group, "Pet Sounds", by)

    _settle(canonicalize_pending)

    with node.cursor() as cur:
        assert _artist_rows(cur) == [("Beach Boys", band)]
        cur.execute("SELECT count(*) FROM mb_refuted_bindings")
        assert cur.fetchone() == (0,)
        cur.execute("SELECT id::text FROM artists")
        artist_id = cur.fetchone()[0]
    assert ("release", forged_release) in mb.asked
    assert len({m for k, m in mb.asked if k == "recording"} & set(recs)) == 3
    asks = mb_answers.Asks("owned_artist", artist_id, mb_answers.PRIORITY_OWNED)
    assert confirm.binding(artist_id, "Beach Boys", band, asks) is confirm.UNPROVEN


def test_while_musicbrainz_is_silent_nothing_is_renamed_and_the_artist_waits(node, mb):
    from canon.content import canonicalize_pending
    band, group, rel = mbid(), mbid(), mbid()
    with node.cursor() as cur:
        Library(cur, "Beach Boys").album("Pet Sounds", SONGS)
        recs = _catalogue(cur, band, "The Beach Boys", ["Beach Boys"], group, "Pet Sounds", rel)
    _musicbrainz(mb, band, "The Beach Boys", ["Beach Boys"], group, "Pet Sounds", rel, recs)
    mb.down = True

    canonicalize_pending()
    assert drain() == 0
    again = canonicalize_pending()

    assert again["pending"] == 0   # waiting, not re-planned meanwhile
    with node.cursor() as cur:
        assert [r[0] for r in _artist_rows(cur)] == ["Beach Boys"]
    mb.down = False
    import api_cooldown
    api_cooldown._cache.clear()
    drain()
    _settle(canonicalize_pending)
    with node.cursor() as cur:
        assert _artist_rows(cur) == [("The Beach Boys", band)]


def test_artists_merge_only_where_musicbrainz_confirms_each_of_them(node, mb):
    from canon.content import merge_collisions
    band, group, rel = mbid(), mbid(), mbid()
    with node.cursor() as cur:
        honest = Library(cur, "Beach Boys")
        al = honest.album("Pet Sounds", SONGS)
        other = Library(cur, "Sunflower Kids")
        al2 = other.album("Pet Sounds", SONGS[:3])
        recs = _catalogue(cur, band, "The Beach Boys", ["Beach Boys", "Sunflower Kids"], group,
                          "Pet Sounds", rel)
        for artist, album in ((honest, al), (other, al2)):
            cur.execute("UPDATE album_artists SET mbid = %s WHERE album_id = %s", (band, album))
            cur.execute("UPDATE album_variants SET release_mbid = %s WHERE album_id = %s", (rel, album))
        cur.execute("INSERT INTO artist_mbids (mbid, artist_id, confidence) "
                    "VALUES (%s, %s, 'overlap_verified')", (band, honest.id))
    # MusicBrainz carries the first spelling, not the second.
    _musicbrainz(mb, band, "The Beach Boys", ["Beach Boys"], group, "Pet Sounds", rel, recs)

    def run():
        waiting = set()
        merge_collisions(dry_run=False, waiting=waiting)
        return bool(waiting)
    assert run()
    drain()
    _settle(run)

    with node.cursor() as cur:
        cur.execute("SELECT name FROM artists ORDER BY name")
        assert [r[0] for r in cur.fetchall()] == ["Beach Boys", "Sunflower Kids"]
        cur.execute("SELECT artist_id::text, mbid::text FROM mb_refuted_bindings")
        assert cur.fetchall() == [(other.id, band)]
        cur.execute("SELECT artist_id::text FROM artist_mbids WHERE mbid = %s", (band,))
        assert cur.fetchall() == [(honest.id,)]


def test_an_edition_takes_the_title_musicbrainz_gives_its_group(node, mb):
    from canon.content import canonicalize_pending
    band, group, rel = mbid(), mbid(), mbid()
    with node.cursor() as cur:
        Library(cur, "The Beach Boys").album("Pet Sounds (Remastered)", SONGS)
        recs = _catalogue(cur, band, "The Beach Boys", [], group, "Pet Sound", rel)
    _musicbrainz(mb, band, "The Beach Boys", [], group, "Pet Sounds", rel, recs)

    _settle(canonicalize_pending)

    with node.cursor() as cur:
        cur.execute("SELECT title, musicbrainz_id::text FROM albums")
        assert cur.fetchall() == [("Pet Sounds", group)]


def test_an_edition_whose_release_musicbrainz_files_elsewhere_keeps_its_title(node, mb):
    from canon.content import canonicalize_pending
    band, group, rel, elsewhere = mbid(), mbid(), mbid(), mbid()
    with node.cursor() as cur:
        Library(cur, "The Beach Boys").album("Pet Sounds (Remastered)", SONGS)
        recs = _catalogue(cur, band, "The Beach Boys", [], group, "Pet Sounds", rel)
    by = credit(("The Beach Boys", band))
    mb.artist(band, "The Beach Boys")
    mb.release_group(group, "Pet Sounds", by)
    mb.release_group(elsewhere, "Smiley Smile", by)
    mb.release(rel, "Smiley Smile", elsewhere, by,
               [{"title": t, "length": s * 1000, "recording": r} for (t, s), r in zip(SONGS, recs)])

    _settle(canonicalize_pending)

    with node.cursor() as cur:
        cur.execute("SELECT title FROM albums")
        assert cur.fetchall() == [("Pet Sounds (Remastered)",)]


def test_a_collaboration_splits_into_musicbrainzs_credit_and_not_on_a_single_one(node, mb):
    from canon.content import _collab_members
    bei, shawn, rec = mbid(), mbid(), mbid()
    with node.cursor() as cur:
        lib = Library(cur, "Bei Bei & Shawn Lee")
        lib.album("Into the Wind", [("Into the Wind", 200)])
        s = Slice(cur)
        a1, a2 = s.artist(bei, "Bei Bei"), s.artist(shawn, "Shawn Lee")
        c = s.credit((a1, "Bei Bei"), (a2, "Shawn Lee"))
        g = s.release_group(mbid(), "Into the Wind", c)
        s.release(mbid(), "Into the Wind", g, c, [("Into the Wind", 200000, rec)])
        cur.execute("SELECT id::text FROM tracks")
        track = cur.fetchone()[0]
    plan = {"albums": [{"recordings": {track: rec}}]}
    mb.recording(rec, "Into the Wind", 200000, credit(("Bei Bei", bei, " & "), ("Shawn Lee", shawn)))

    from canon import confirm
    assert _collab_members(lib.id, lib.name, plan) is confirm.WAIT
    drain()
    assert _collab_members(lib.id, lib.name, plan) == [("Bei Bei", bei), ("Shawn Lee", shawn)]

    import mb_answers
    with node.cursor() as cur:
        cur.execute("DELETE FROM mbws_answers")
    mb.recording(rec, "Into the Wind", 200000, credit(("Bei Bei", bei)))
    assert _collab_members(lib.id, lib.name, plan) is confirm.WAIT
    drain()
    assert _collab_members(lib.id, lib.name, plan) == []
    assert mb_answers.answer("recording", rec).known


def test_a_name_only_binding_takes_musicbrainzs_word_for_the_name(node, mb):
    from canon.content import assign_name_exact
    real, forged = mbid(), mbid()
    with node.cursor() as cur:
        a = Library(cur, "Autechre")
        b = Library(cur, "Seefeel")
        s = Slice(cur)
        s.artist(real, "Autechre")
        s.artist(forged, "Mark Clifford", aliases=["Seefeel"])
    mb.artist(real, "Autechre")
    mb.artist(forged, "Mark Clifford")

    waiting = set()
    assert assign_name_exact([a.id, b.id], waiting=waiting) == 0
    assert waiting == {a.id, b.id}
    drain()
    assert assign_name_exact([a.id, b.id]) == 1
    with node.cursor() as cur:
        cur.execute("SELECT a.name FROM artist_mbids am JOIN artists a ON a.id = am.artist_id")
        assert cur.fetchall() == [("Autechre",)]
        cur.execute("SELECT mbid::text FROM mb_refuted_bindings")
        assert cur.fetchall() == [(forged,)]


def test_various_artists_takes_musicbrainzs_constant_name_whatever_a_slice_says(node, mb):
    from canon.content import _canonicalize_va
    with node.cursor() as cur:
        Library(cur, "VA").album("Café del Mar", [("Lazy Lounge", 300)])
        Slice(cur).artist("89ad4ac3-39f7-470e-963a-56509c546377", "Various Artistz")
    assert _canonicalize_va() == 1
    with node.cursor() as cur:
        cur.execute("""SELECT a.name FROM artists a JOIN artist_mbids am ON am.artist_id = a.id""")
        assert cur.fetchall() == [("Various Artists",)]
    assert mb.asked == []


def test_a_node_with_the_dump_renames_from_its_own_data_and_asks_nothing(node, mb):
    from canon.content import canonicalize_pending
    band, group, rel = mbid(), mbid(), mbid()
    with node.cursor() as cur:
        cur.execute("INSERT INTO user_settings (key, value) VALUES ('musicbrainz.db_version', '\"x\"')")
        Library(cur, "Beach Boys").album("Pet Sounds", SONGS)
        _catalogue(cur, band, "The Beach Boys", ["Beach Boys"], group, "Pet Sounds", rel)

    canonicalize_pending()

    with node.cursor() as cur:
        assert _artist_rows(cur) == [("The Beach Boys", band)]
    assert mb.asked == []


# ---- review round 1 of the MusicBrainz side

def test_an_album_keeps_its_title_under_a_binding_musicbrainz_does_not_confirm(node, mb):
    """The edition's title is taken only for a binding MusicBrainz confirms: a
    real cover act's real group carries the owned titles, and the slice hangs
    the owned name on it as an alias."""
    from canon.content import canonicalize_pending
    covers, group, rel = mbid(), mbid(), mbid()
    with node.cursor() as cur:
        Library(cur, "Beach Boys").album("Pet Sounds", SONGS)
        recs = _catalogue(cur, covers, "The Karaoke Crew", ["Beach Boys"], group,
                          "Karaoke Hits of the Sixties", rel)
    _musicbrainz(mb, covers, "The Karaoke Crew", [], group, "Karaoke Hits of the Sixties", rel, recs)

    _settle(canonicalize_pending)

    with node.cursor() as cur:
        cur.execute("SELECT title FROM albums")
        assert cur.fetchall() == [("Pet Sounds",)]
        assert _artist_rows(cur) == [("Beach Boys", None)]


def test_an_album_is_not_retitled_after_a_box_set_that_merely_holds_its_tracks(node, mb):
    """The release covers the album both ways: MusicBrainz's box set of forty
    tracks holds the four owned ones, the slice's copy of it only those four."""
    from canon.content import canonicalize_pending
    band, group, rel = mbid(), mbid(), mbid()
    with node.cursor() as cur:
        Library(cur, "The Beach Boys").album("Pet Sounds", SONGS)
        recs = _catalogue(cur, band, "The Beach Boys", [], group, "The Complete Sessions", rel)
    by = credit(("The Beach Boys", band))
    mb.artist(band, "The Beach Boys")
    mb.release_group(group, "The Complete Sessions", by)
    takes = [{"title": f"Session Take {i}", "length": 120000 + i * 1000, "recording": mbid()}
             for i in range(36)]
    mb.release(rel, "The Complete Sessions", group, by,
               [{"title": t, "length": s * 1000, "recording": r} for (t, s), r in zip(SONGS, recs)]
               + takes)

    _settle(canonicalize_pending)

    with node.cursor() as cur:
        cur.execute("SELECT title FROM albums")
        assert cur.fetchall() == [("Pet Sounds",)]


def test_an_artist_whose_decision_waits_keeps_its_name_until_it_is_decided(node, mb):
    """A rename re-keys the artist; the answers another of its decisions waits
    on are keyed by its id, so the rename waits for that decision."""
    import mb_answers
    from canon.content import rename_to_canonical
    band, group, rel, edition = mbid(), mbid(), mbid(), mbid()
    with node.cursor() as cur:
        lib = Library(cur, "Beach Boys")
        al = lib.album("Pet Sounds", SONGS)
        recs = _catalogue(cur, band, "The Beach Boys", ["Beach Boys"], group, "Pet Sounds", rel)
        cur.execute("UPDATE album_artists SET mbid = %s WHERE album_id = %s", (band, al))
        cur.execute("UPDATE album_variants SET release_mbid = %s WHERE album_id = %s", (rel, al))
        cur.execute("INSERT INTO artist_mbids (mbid, artist_id, confidence) "
                    "VALUES (%s, %s, 'overlap_verified')", (band, lib.id))
    _musicbrainz(mb, band, "The Beach Boys", ["Beach Boys"], group, "Pet Sounds", rel, recs)
    mb_answers.enqueue([("artist", band), ("release", rel)], mb_answers.PRIORITY_OWNED)
    drain()
    # The rename's own answers are in; an edition's release is still asked.
    mb_answers.enqueue([("release", edition)], mb_answers.PRIORITY_OWNED, "owned_artist", lib.id)

    assert rename_to_canonical(artist_ids=[lib.id], waiting=set())["renamed"] == 0
    with node.cursor() as cur:
        assert [r[0] for r in _artist_rows(cur)] == ["Beach Boys"]

    drain()
    assert rename_to_canonical(artist_ids=[lib.id], waiting=set())["renamed"] == 1
    with node.cursor() as cur:
        assert _artist_rows(cur) == [("The Beach Boys", band)]


def test_artists_whose_decision_waits_are_not_merged_until_it_is_decided(node, mb):
    import mb_answers
    from canon.content import merge_collisions
    band, group, rel, edition = mbid(), mbid(), mbid(), mbid()
    with node.cursor() as cur:
        one = Library(cur, "Beach Boys")
        two = Library(cur, "Beach Boys, The")
        recs = _catalogue(cur, band, "The Beach Boys", ["Beach Boys", "Beach Boys, The"], group,
                          "Pet Sounds", rel)
        for lib in (one, two):
            al = lib.album("Pet Sounds", SONGS, directory=f"/music/{lib.name}")
            cur.execute("UPDATE album_artists SET mbid = %s WHERE album_id = %s", (band, al))
            cur.execute("UPDATE album_variants SET release_mbid = %s WHERE album_id = %s", (rel, al))
        cur.execute("INSERT INTO artist_mbids (mbid, artist_id, confidence) "
                    "VALUES (%s, %s, 'overlap_verified')", (band, one.id))
    _musicbrainz(mb, band, "The Beach Boys", ["Beach Boys", "Beach Boys, The"], group, "Pet Sounds",
                 rel, recs)
    mb_answers.enqueue([("artist", band), ("release", rel)], mb_answers.PRIORITY_OWNED)
    drain()
    mb_answers.enqueue([("release", edition)], mb_answers.PRIORITY_OWNED, "owned_artist", two.id)

    merge_collisions(dry_run=False, waiting=set())
    with node.cursor() as cur:
        cur.execute("SELECT count(*) FROM artists")
        assert cur.fetchone() == (2,)

    drain()
    merge_collisions(dry_run=False, waiting=set())
    with node.cursor() as cur:
        cur.execute("SELECT name FROM artists")
        assert cur.fetchall() == [("The Beach Boys",)]


def test_a_binding_with_nothing_owned_to_weigh_renames_nothing_and_stays(node, mb):
    """A rename rests on owned evidence: a binding no stamped release or
    recording of the artist's files backs (an imported one) is kept, unused."""
    from canon.content import rename_to_canonical
    band = mbid()
    with node.cursor() as cur:
        lib = Library(cur, "Beach Boys")
        lib.album("Pet Sounds", SONGS)
        cur.execute("INSERT INTO artist_mbids (mbid, artist_id, confidence) "
                    "VALUES (%s, %s, 'alias_exact')", (band, lib.id))
    mb.artist(band, "The Beach Boys", aliases=["Beach Boys"])

    def run():
        waiting = set()
        out = rename_to_canonical(artist_ids=[lib.id], waiting=waiting)
        return {**out, "waiting": bool(waiting)}
    assert _settle(run)["renamed"] == 0
    with node.cursor() as cur:
        assert _artist_rows(cur) == [("Beach Boys", band)]
        cur.execute("SELECT count(*) FROM mb_refuted_bindings")
        assert cur.fetchone() == (0,)


def test_a_compound_whose_part_musicbrainz_leaves_ambiguous_is_not_split(node, mb):
    """The name path's parts are each one artist MusicBrainz confirms: a slice
    that leaves the band out and offers namesakes for a part splits nothing."""
    from canon import confirm
    from canon.content import _collab_members
    simon, other_simon, garfunkel = mbid(), mbid(), mbid()
    with node.cursor() as cur:
        lib = Library(cur, "Simon & Garfunkel")
        lib.album("Bookends", [("Mrs. Robinson", 244)])
        s = Slice(cur)
        s.artist(simon, "Simon")
        s.artist(other_simon, "Simon")
        s.artist(garfunkel, "Garfunkel")
    mb.artist(simon, "Simon")
    mb.artist(other_simon, "Simon")
    mb.artist(garfunkel, "Garfunkel")

    assert _collab_members(lib.id, lib.name, {"albums": []}) is confirm.WAIT
    drain()
    assert _collab_members(lib.id, lib.name, {"albums": []}) == []
    assert {m for k, m in mb.asked if k == "artist"} == {simon, other_simon, garfunkel}


def test_a_collaboration_credit_the_slice_hangs_on_a_solo_artist_splits_nothing(node, mb):
    """The content path's credit must be the owned compound: a real recording
    of a duet, matched to a solo artist's track, says nothing about who the
    solo artist is."""
    from canon import confirm
    from canon.content import _collab_members
    duo, singer, rec = mbid(), mbid(), mbid()
    with node.cursor() as cur:
        lib = Library(cur, "Beth Orton")
        lib.album("Central Reservation", [("Where Do I Begin", 400)])
        s = Slice(cur)
        a1, a2 = s.artist(duo, "The Chemical Brothers"), s.artist(singer, "B. Orton")
        c = s.credit((a1, "The Chemical Brothers"), (a2, "B. Orton"))
        g = s.release_group(mbid(), "Dig Your Own Hole", c)
        s.release(mbid(), "Dig Your Own Hole", g, c, [("Where Do I Begin", 400000, rec)])
        cur.execute("SELECT id::text FROM tracks")
        track = cur.fetchone()[0]
    mb.recording(rec, "Where Do I Begin", 400000,
                 credit(("The Chemical Brothers", duo, " & "), ("Beth Orton", singer)))
    plan = {"albums": [{"recordings": {track: rec}}]}

    assert _collab_members(lib.id, lib.name, plan) is confirm.WAIT
    drain()
    assert _collab_members(lib.id, lib.name, plan) == []


def test_a_node_without_the_dump_nominates_no_artist_by_the_searchs_fallback(node, mb):
    """The search's top candidate below its score floor is no nomination on a
    node without the dump: no name of MusicBrainz's could confirm it."""
    from canon.match import _candidates
    with node.cursor() as cur:
        Slice(cur).artist(mbid(), "The Beatles")
    assert _candidates("The Beatels") == []


def test_two_members_of_one_group_are_not_split_as_a_collaboration(node, mb):
    """The name path's parts must not be one band's lineup: MusicBrainz's
    "member of band" links of both parts name one group (the slice left the
    duo itself out)."""
    from canon import confirm
    from canon.content import _collab_members
    sonny, cher, duo = mbid(), mbid(), mbid()
    with node.cursor() as cur:
        lib = Library(cur, "Sonny & Cher")
        lib.album("Look at Us", [("I Got You Babe", 190)])
        s = Slice(cur)
        s.artist(sonny, "Sonny")
        s.artist(cher, "Cher")
    mb.artist(sonny, "Sonny", member_of=[duo])
    mb.artist(cher, "Cher", member_of=[duo])

    assert _collab_members(lib.id, lib.name, {"albums": []}) is confirm.WAIT
    drain()
    assert _collab_members(lib.id, lib.name, {"albums": []}) == []


# ---- review round 3 of the MusicBrainz side

def test_the_unique_name_tier_refutes_nothing_the_owned_tiers_rule_would_confirm(node, mb):
    """The slice gives a real artist the owned spelling as an exact alias it
    does not carry on MusicBrainz: the unique-name tier binds nothing, and the
    pair stays open to a content tier, whose rule ("The …") carries it."""
    from canon.content import assign_name_exact
    band = mbid()
    with node.cursor() as cur:
        lib = Library(cur, "Beatles")
        Slice(cur).artist(band, "The Beatles", aliases=["Beatles"])
    mb.artist(band, "The Beatles")

    assert assign_name_exact([lib.id], waiting=set()) == 0
    drain()
    assert assign_name_exact([lib.id]) == 0
    with node.cursor() as cur:
        cur.execute("SELECT count(*) FROM mb_refuted_bindings")
        assert cur.fetchone() == (0,)
        cur.execute("SELECT count(*) FROM artist_mbids")
        assert cur.fetchone() == (0,)


def test_a_whole_pass_takes_an_artist_a_wake_named_whatever_its_watermark(node, mb):
    """Waiters a CLI run queued leave the artist stamped: a whole pass that a
    wake joined still takes it."""
    import mb_answers
    from canon.content import canonicalize_pending
    band, group, rel = mbid(), mbid(), mbid()
    with node.cursor() as cur:
        lib = Library(cur, "Beach Boys")
        lib.album("Pet Sounds", SONGS)
        recs = _catalogue(cur, band, "Beach Boys, The", ["Beach Boys"], group, "Pet Sounds", rel)
    _musicbrainz(mb, band, "The Beach Boys", ["Beach Boys"], group, "Pet Sounds", rel, recs)
    _settle(canonicalize_pending)
    with node.cursor() as cur:
        assert _artist_rows(cur) == [("The Beach Boys", band)]
        cur.execute("SELECT id::text FROM artists WHERE name = 'The Beach Boys'")
        aid = cur.fetchone()[0]
        # Stamped now; a decision of it was queued outside a pass meanwhile.
        cur.execute("UPDATE artists SET last_mb_sync = now() + interval '1 minute'")
    mb_answers.enqueue([("release", mbid())], mb_answers.PRIORITY_OWNED, "owned_artist", aid)
    drain()

    assert canonicalize_pending()["pending"] == 0
    assert canonicalize_pending(woken=[aid])["pending"] == 1


def test_the_content_premise_weighs_albums_that_name_their_release_whatever_their_size(node, mb):
    """The strongest bound albums that carry a covering release are weighed —
    three bigger albums without one do not crowd it out."""
    import mb_answers
    from canon import confirm
    band, group, rel = mbid(), mbid(), mbid()
    with node.cursor() as cur:
        lib = Library(cur, "The Beach Boys")
        for title in ("Smiley Smile", "Wild Honey", "Friends"):
            al = lib.album(title, [(f"{title} {i}", 150 + i) for i in range(6)])
            cur.execute("UPDATE album_artists SET mbid = %s WHERE album_id = %s", (band, al))
        al = lib.album("Pet Sounds", SONGS)
        recs = _catalogue(cur, band, "The Beach Boys", [], group, "Pet Sounds", rel)
        cur.execute("UPDATE album_artists SET mbid = %s WHERE album_id = %s", (band, al))
        cur.execute("UPDATE album_variants SET release_mbid = %s WHERE album_id = %s", (rel, al))
    _musicbrainz(mb, band, "The Beach Boys", [], group, "Pet Sounds", rel, recs)
    asks = mb_answers.Asks("owned_artist", lib.id, mb_answers.PRIORITY_OWNED)
    assert confirm.owned_content(lib.id, [band], asks) is confirm.WAIT
    asks.flush()
    drain()

    asks = mb_answers.Asks("owned_artist", lib.id, mb_answers.PRIORITY_OWNED)
    assert confirm.owned_content(lib.id, [band], asks) is True


def test_a_recording_with_the_owned_title_is_one_within_the_cascades_wide_band(node, mb):
    """The cascade stamps an equal title ±25 s apart (its wide tier); the
    premise that re-checks the stamp takes the same band."""
    from canon import confirm
    band = mbid()
    with node.cursor() as cur:
        lib = Library(cur, "The Beach Boys")
        lib.album("Pet Sounds", [("God Only Knows", 175)])
        cur.execute("SELECT id::text FROM tracks")
        track = cur.fetchone()[0]
    rec = {"title": "God Only Knows", "length_ms": 195000, "credit": [{"artist": band}]}
    assert confirm.recording_premise(track, rec, [band]) is True
    fuzzy = {"title": "God Only Know", "length_ms": 195000, "credit": [{"artist": band}]}
    assert confirm.recording_premise(track, fuzzy, [band]) is False


def test_a_woken_run_judges_only_the_collisions_of_the_artists_it_woke(node, mb):
    from canon.content import merge_collisions
    one, two = mbid(), mbid()
    with node.cursor() as cur:
        libs = [Library(cur, n) for n in ("Beach Boys", "The Beach Boys", "Byrds", "The Byrds")]
        for lib, gid in zip(libs, (one, one, two, two)):
            al = lib.album("An Album", SONGS, directory=f"/music/{lib.name}")
            cur.execute("UPDATE album_artists SET mbid = %s WHERE album_id = %s", (gid, al))
        cur.execute("INSERT INTO artist_mbids (mbid, artist_id, confidence) "
                    "VALUES (%s, %s, 'overlap_verified'), (%s, %s, 'overlap_verified')",
                    (one, libs[0].id, two, libs[2].id))
    merge_collisions(dry_run=True, waiting=set(), artist_ids=[libs[2].id])
    with node.cursor() as cur:
        cur.execute("SELECT DISTINCT mbid::text FROM mbws_queue WHERE kind = 'artist'")
        assert cur.fetchall() == [(two,)]
