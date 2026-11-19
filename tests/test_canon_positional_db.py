"""The positional release tier (CANON-CLASSICAL-DESIGN §3, §12.5–§12.6): a
copy the cascade left without a release group is bound by its positions and
their lengths to the release of an artist bound on its tracks or its album's
credit.

On a real PostgreSQL built from the migrations, MusicBrainz's rows as a dump
or a slice holds them (mb_fakes.Slice), MusicBrainz's answers faked —
skipped without a cluster. Before this slice the owner's "Polarity" (an
ensemble's album, the leader on the tracks, the ensemble's release credit,
MusicBrainz's lengths in whole seconds) reached no release group: the cascade
never sees an album whose credit and tracks name different artists."""

import sys
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
for p in (REPO, REPO / "backend", REPO / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

pytest.importorskip("psycopg2")
pytest.importorskip("sqlalchemy")

import mb_fakes  # noqa: E402
from mb_fakes import FakeMusicBrainz, Slice, credit, drain  # noqa: E402

LEADER, ENSEMBLE, OTHER = (f"7a1c0000-0000-4000-8000-00000000000{i}" for i in (1, 2, 3))
VA = "89ad4ac3-39f7-470e-963a-56509c546377"
DM, CD = 12, 1                                   # MB's medium format ids for Digital Media and CD
LENGTHS = (312000, 245000, 401000, 198000)        # whole seconds, as a store's
FILES = (312.99, 245.40, 401.02, 198.61)          # the floor's file: within [0, 1)


@pytest.fixture
def node(scratch_dsn, monkeypatch):
    conn, pool, engine = mb_fakes.slice_fed(scratch_dsn, monkeypatch)
    fake = FakeMusicBrainz().install(monkeypatch)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO mb_medium_format (id, name) VALUES (1, 'CD'), (12, 'Digital Media') "
                    "ON CONFLICT DO NOTHING")
    yield conn, fake
    conn.close()
    pool.closeall()
    engine.dispose()


def _dump_node(cur):
    from desktop.p2p import mb_asks
    cur.execute("INSERT INTO user_settings (key, value) VALUES (%s, '\"20261111-002335\"') "
                "ON CONFLICT DO NOTHING", (mb_asks.DUMP_MARKER,))


def _bind_artist(cur, name, mbid):
    from uuid_utils import artist_uuid
    aid = str(artist_uuid(name))
    cur.execute("INSERT INTO artists (id, name) VALUES (%s, %s) ON CONFLICT DO NOTHING", (aid, name))
    cur.execute("INSERT INTO artist_mbids (mbid, artist_id, confidence) VALUES (%s, %s, 'overlap_verified')",
                (mbid, aid))
    return aid


def _polarity(cur, files=FILES, album_artist="Hoff Ensemble", title="Polarity"):
    """The ensemble's album, its tracks credited to the leader (the owner's
    files), the copy's lengths `files`."""
    from mb_fakes import Library
    lib = Library(cur, "Jan Gunnar Hoff")
    album = lib.album(title, [(f"Movement {i}", s) for i, s in enumerate(files, 1)])
    ens = _bind_artist(cur, album_artist, ENSEMBLE)
    cur.execute("DELETE FROM album_artists WHERE album_id = %s", (album,))
    cur.execute("INSERT INTO album_artists (album_id, artist_id, role) VALUES (%s, %s, 'primary')", (album, ens))
    cur.execute("INSERT INTO artist_mbids (mbid, artist_id, confidence) VALUES (%s, %s, 'overlap_verified')",
                (LEADER, lib.id))
    return album


def _release(s, cur, group_gid, title, artists, lengths=LENGTHS, fmt=DM, barcode=None):
    c = s.credit(*artists)
    g = s.release_group(group_gid, title, c)
    rel_gid = str(uuid.uuid4())
    recs = [str(uuid.uuid4()) for _ in lengths]
    rid = s.release(rel_gid, title, g, c, [(f"Movement {i}", ms, r) for i, (ms, r) in
                                           enumerate(zip(lengths, recs), 1)])
    cur.execute("UPDATE mb_medium SET format = %s WHERE release = %s", (fmt, rid))
    if barcode:
        cur.execute("UPDATE mb_release SET barcode = %s WHERE id = %s", (barcode, rid))
    return rel_gid, recs


def _mb(cur):
    s = Slice(cur)
    return s, s.artist(LEADER, "Jan Gunnar Hoff"), s.artist(ENSEMBLE, "Hoff Ensemble")


def _run():
    from canon import positional
    return positional.run()


def test_an_ensembles_album_binds_by_its_positions_to_the_ensembles_release(node):
    conn, _fake = node
    with conn.cursor() as cur:
        _dump_node(cur)
        album = _polarity(cur)
        s, leader, ens = _mb(cur)
        rg = str(uuid.uuid4())
        rel, recs = _release(s, cur, rg, "Polarity", [(ens, "Hoff Ensemble"), (leader, "Jan Gunnar Hoff")])
        st = _run()
        assert (st["bound_albums"], st["bound_copies"]) == (1, 1)
        cur.execute("SELECT musicbrainz_id::text, mb_match_confidence::text FROM albums WHERE id = %s", (album,))
        assert cur.fetchone() == (rg, "positional")
        cur.execute("SELECT release_mbid::text FROM album_variants WHERE album_id = %s", (album,))
        assert cur.fetchone() == (rel,)
        cur.execute("SELECT recording_mbid::text FROM media_files ORDER BY track_number")
        assert [r[0] for r in cur.fetchall()] == recs
        cur.execute("SELECT count(*), min(confidence::text), min(matcher_version) FROM track_mbids")
        assert cur.fetchone() == (4, "positional", 2)
        assert _run()["copies"] == 0                  # bound: no copy left to ask


def test_a_store_length_is_a_floor_and_a_millisecond_length_is_kept_to_half_a_second(node):
    conn, _fake = node
    with conn.cursor() as cur:
        _dump_node(cur)
        _polarity(cur, files=(313.6, 245.4, 401.0, 198.3))   # 1.6 s past a whole-second length
        s, leader, ens = _mb(cur)
        _release(s, cur, str(uuid.uuid4()), "Polarity", [(ens, "Hoff Ensemble")])
        assert _run()["bound_albums"] == 0
        cur.execute("UPDATE mb_track SET length = length + 1")    # MB keeps milliseconds now
        cur.execute("UPDATE user_settings SET value = '\"20261118-000000\"' "
                    "WHERE key = 'musicbrainz.db_version'")     # in the next dump
        cur.execute("UPDATE media_files SET duration_seconds = duration_seconds - 0.6 "
                    "WHERE track_number = 1")                     # 313.0 against 312.001
        assert _run()["bound_albums"] == 0                        # 0.999 s: past half a second
        cur.execute("UPDATE media_files SET duration_seconds = 312.4 WHERE track_number = 1")
        assert _run()["bound_albums"] == 1


def test_a_cd_is_kept_to_two_seconds_and_a_lengthless_position_fits_nothing(node):
    conn, _fake = node
    with conn.cursor() as cur:
        _dump_node(cur)
        _polarity(cur, files=(313.9, 243.1, 401.0, 198.0))
        s, leader, ens = _mb(cur)
        _release(s, cur, str(uuid.uuid4()), "Polarity", [(ens, "Hoff Ensemble")], fmt=CD)
        cur.execute("UPDATE mb_track SET length = NULL WHERE position = 2")
        cur.execute("UPDATE mb_recording SET length = NULL WHERE id IN "
                    "(SELECT recording FROM mb_track WHERE position = 2)")
        assert _run()["bound_albums"] == 0
        cur.execute("UPDATE mb_track SET length = 245000 WHERE position = 2")
        cur.execute("UPDATE user_settings SET value = '\"20261118-000000\"' "
                    "WHERE key = 'musicbrainz.db_version'")     # the next dump has the length
        assert _run()["bound_albums"] == 1


def test_two_groups_that_fit_abstain_unless_the_barcode_names_one(node):
    conn, _fake = node
    with conn.cursor() as cur:
        _dump_node(cur)
        album = _polarity(cur)
        s, leader, ens = _mb(cur)
        _release(s, cur, str(uuid.uuid4()), "Polarity", [(ens, "Hoff Ensemble")], barcode="7041888520023")
        rg2 = str(uuid.uuid4())
        _release(s, cur, rg2, "Polarity (Live)", [(ens, "Hoff Ensemble")], barcode="7041888520030")
        st = _run()
        assert (st["bound_albums"], st["abstained"]) == (0, 1)
        assert _run()["asked"] == 0                   # the same candidates are not asked again
        cur.execute("UPDATE media_files SET barcode = '7041888520030'")
        assert _run()["bound_albums"] == 1            # the copy's witness changed: asked again
        cur.execute("SELECT musicbrainz_id::text FROM albums WHERE id = %s", (album,))
        assert cur.fetchone() == (rg2,)


def test_under_three_positions_a_second_witness_is_needed(node):
    conn, _fake = node
    with conn.cursor() as cur:
        _dump_node(cur)
        _polarity(cur, files=FILES[:2], title="Single")
        s, leader, ens = _mb(cur)
        _release(s, cur, str(uuid.uuid4()), "Something Else Entirely", [(ens, "Hoff Ensemble")],
                 lengths=LENGTHS[:2])
        assert _run()["bound_albums"] == 0
        cur.execute("UPDATE mb_release_group SET name = 'Single'")
        cur.execute("UPDATE album_variants SET positional_asked = NULL")
        assert _run()["bound_albums"] == 1


def test_various_artists_is_no_candidate(node):
    conn, _fake = node
    with conn.cursor() as cur:
        _dump_node(cur)
        from mb_fakes import Library
        lib = Library(cur, "Various Artists")
        lib.album("Jazz Sampler", [(f"T{i}", s) for i, s in enumerate(FILES, 1)])
        cur.execute("INSERT INTO artist_mbids (mbid, artist_id, confidence) VALUES (%s, %s, 'overlap_verified')",
                    (VA, lib.id))
        s = Slice(cur)
        va = s.artist(VA, "Various Artists")
        _release(s, cur, str(uuid.uuid4()), "Jazz Sampler", [(va, "Various Artists")])
        assert _run()["copies"] == 0


def test_copies_of_one_album_fitting_two_groups_bind_neither(node):
    conn, _fake = node
    with conn.cursor() as cur:
        _dump_node(cur)
        album = _polarity(cur)
        cur.execute("INSERT INTO album_variants (album_id, directory_path, raw_title) "
                    "VALUES (%s, '/music/Hoff/Polarity [24-96]', 'Polarity') RETURNING id", (album,))
        v2 = cur.fetchone()[0]
        cur.execute("""INSERT INTO media_files (track_id, album_variant_id, file_path, sample_rate, bit_depth,
                                                duration_seconds, track_number, disc_number)
                       SELECT track_id, %s, file_path || '.hr', 96000, 24, duration_seconds + 30, track_number, 1
                         FROM media_files""", (v2,))
        s, leader, ens = _mb(cur)
        _release(s, cur, str(uuid.uuid4()), "Polarity", [(ens, "Hoff Ensemble")])
        _release(s, cur, str(uuid.uuid4()), "Polarity", [(ens, "Hoff Ensemble")],
                 lengths=tuple(ms + 30000 for ms in LENGTHS))
        st = _run()
        assert (st["bound_albums"], st["album_conflict"]) == (0, 1)


def _box(s, cur, group_gid, title, artists, disc=2, other=(180000, 220000, 260000)):
    """A release of two media: `disc` holds LENGTHS (the copy's), the other
    medium `other`. Returns (release gid, release id, the recordings of `disc`)."""
    c = s.credit(*artists)
    g = s.release_group(group_gid, title, c)
    first = LENGTHS if disc == 1 else other
    rel_gid = str(uuid.uuid4())
    recs1 = [str(uuid.uuid4()) for _ in first]
    rid = s.release(rel_gid, title, g, c, [(f"Piece {i}", ms, r) for i, (ms, r) in
                                           enumerate(zip(first, recs1), 1)])
    second = other if disc == 1 else LENGTHS
    recs2 = [str(uuid.uuid4()) for _ in second]
    mid = 9_000_000 + rid
    cur.execute("INSERT INTO mb_medium (id, release, position, track_count) VALUES (%s, %s, 2, %s)",
                (mid, rid, len(second)))
    for pos, (ms, r) in enumerate(zip(second, recs2), 1):
        cur.execute("INSERT INTO mb_recording (id, gid, name, artist_credit, length) VALUES (%s, %s, %s, %s, %s)",
                    (mid * 100 + pos, r, f"Movement {pos}", c, ms))
        cur.execute("INSERT INTO mb_track (id, gid, recording, medium, position, number, name, artist_credit, "
                    "length) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (mid * 100 + pos, str(uuid.uuid4()), mid * 100 + pos, mid, pos, str(pos),
                     f"Movement {pos}", c, ms))
    cur.execute("UPDATE mb_medium SET format = %s WHERE release = %s", (DM, rid))
    return rel_gid, rid, recs1 if disc == 1 else recs2


def test_one_disc_of_a_box_fits_the_box_s_group_too_and_abstains(node):
    """§12.7 (the 50-bind gate, 2026-11-19): HitStory (Disc 1) bound to the
    one-disc Elv1s, whose recordings MusicBrainz's three-disc Hitstory carries
    as its first medium. A medium of a release of several is a candidate as
    well, so the copy fits two groups and no witness names one."""
    conn, _fake = node
    with conn.cursor() as cur:
        _dump_node(cur)
        album = _polarity(cur, title="Polarity (Disc 1)")
        s, leader, ens = _mb(cur)
        _release(s, cur, str(uuid.uuid4()), "Polarity", [(ens, "Hoff Ensemble")])
        _box(s, cur, str(uuid.uuid4()), "The Box", [(ens, "Hoff Ensemble")], disc=1)
        st = _run()
        assert (st["bound_albums"], st["abstained"]) == (0, 1)
        cur.execute("SELECT musicbrainz_id FROM albums WHERE id = %s", (album,))
        assert cur.fetchone() == (None,)


def test_a_copy_of_one_disc_of_a_box_binds_to_the_box_by_that_medium(node):
    """Works, 1965-1995's Disc 10 without City Life / Proverb beside it: the
    medium's positions fit, the box's group binds, its release is the copy's,
    and each position takes the medium's recording, not the first disc's."""
    conn, _fake = node
    with conn.cursor() as cur:
        _dump_node(cur)
        album = _polarity(cur, title="Disc 2")
        s, leader, ens = _mb(cur)
        rg = str(uuid.uuid4())
        rel, _rid, recs = _box(s, cur, rg, "The Box", [(ens, "Hoff Ensemble")], disc=2)
        st = _run()
        assert (st["bound_albums"], st["bound_copies"]) == (1, 1)
        cur.execute("SELECT musicbrainz_id::text FROM albums WHERE id = %s", (album,))
        assert cur.fetchone() == (rg,)
        cur.execute("SELECT release_mbid::text FROM album_variants WHERE album_id = %s", (album,))
        assert cur.fetchone() == (rel,)
        cur.execute("SELECT recording_mbid::text FROM media_files ORDER BY track_number")
        assert [r[0] for r in cur.fetchall()] == recs


def test_on_a_slice_fed_node_a_medium_s_bind_checks_that_medium_s_tracklist(node):
    conn, fake = node
    with conn.cursor() as cur:
        album = _polarity(cur, title="Disc 2")
        s, leader, ens = _mb(cur)
        rg = str(uuid.uuid4())
        rel, _rid, recs = _box(s, cur, rg, "The Box", [(ens, "Hoff Ensemble")], disc=2)
        cur.execute("INSERT INTO mb_slice_fetches (name_key, matched_ids) VALUES ('hoff ensemble', 1) "
                    "ON CONFLICT DO NOTHING")
    fake.artist(ENSEMBLE, "Hoff Ensemble")
    fake.release_group(rg, "The Box", credit(("Hoff Ensemble", ENSEMBLE)))
    fake.release(rel, "The Box", rg, credit(("Hoff Ensemble", ENSEMBLE)),
                 [{"title": f"Piece {i}", "length": ms, "recording": str(uuid.uuid4()), "disc": 1}
                  for i, ms in enumerate((180000, 220000, 260000), 1)]
                 + [{"title": f"Movement {i}", "length": ms, "recording": r, "disc": 2}
                    for i, (ms, r) in enumerate(zip(LENGTHS, recs), 1)], media_format="Digital Media")
    assert _run()["waiting"] == 1
    drain()
    assert _run()["bound_albums"] == 1
    with conn.cursor() as cur:
        cur.execute("SELECT musicbrainz_id::text FROM albums WHERE id = %s", (album,))
        assert cur.fetchone() == (rg,)
        cur.execute("SELECT recording_mbid::text FROM media_files ORDER BY track_number")
        assert [r[0] for r in cur.fetchall()] == recs


def test_the_cascade_leaves_an_album_the_positional_tier_bound(node):
    conn, _fake = node
    with conn.cursor() as cur:
        _dump_node(cur)
        album = _polarity(cur)
        s, leader, ens = _mb(cur)
        _release(s, cur, str(uuid.uuid4()), "Polarity", [(ens, "Hoff Ensemble")])
        _run()
        from canon import content
        from uuid_utils import artist_uuid
        plan = content.resolve_artist(str(artist_uuid("Hoff Ensemble")), "Hoff Ensemble")
        assert album not in {a["album_id"] for a in plan["albums"]}


def test_on_a_slice_fed_node_the_bind_waits_for_musicbrainz_s_release(node):
    conn, fake = node
    with conn.cursor() as cur:
        album = _polarity(cur)
        s, leader, ens = _mb(cur)
        rg = str(uuid.uuid4())
        rel, recs = _release(s, cur, rg, "Polarity", [(ens, "Hoff Ensemble")])
        cur.execute("INSERT INTO mb_slice_fetches (name_key, matched_ids) VALUES ('hoff ensemble', 1) "
                    "ON CONFLICT DO NOTHING")
    fake.artist(ENSEMBLE, "Hoff Ensemble")
    fake.release_group(rg, "Polarity", credit(("Hoff Ensemble", ENSEMBLE)))
    fake.release(rel, "Polarity", rg, credit(("Hoff Ensemble", ENSEMBLE)),
                 [{"title": f"Movement {i}", "length": ms, "recording": r}
                  for i, (ms, r) in enumerate(zip(LENGTHS, recs), 1)], media_format="Digital Media")
    st = _run()
    assert (st["bound_albums"], st["waiting"]) == (0, 1)
    drain()
    assert _run()["bound_albums"] == 1
    with conn.cursor() as cur:
        cur.execute("SELECT musicbrainz_id::text FROM albums WHERE id = %s", (album,))
        assert cur.fetchone() == (rg,)


def test_the_apply_prunes_its_own_bindings_no_file_carries(node):
    """CANON-CLASSICAL-DESIGN §5.2: an earlier pass's binding a later pass
    moved goes; a recording another file of the track carries stays, and
    an imported binding is never touched."""
    conn, _fake = node
    from canon import content
    from mb_fakes import Library
    with conn.cursor() as cur:
        lib = Library(cur, "Aurora")
        album = lib.album("LP", [("Song", 200.0)])
        live = lib.album("Live", [("Song", 260.0)])               # the same song, another album
        cur.execute("SELECT DISTINCT track_id::text FROM media_files")
        (track,), = cur.fetchall()
        cur.execute("""UPDATE media_files mf SET recording_mbid = 'a0000000-0000-4000-8000-00000000000b'
                         FROM album_variants av WHERE av.id = mf.album_variant_id AND av.album_id = %s""",
                    (live,))
        cur.execute("""INSERT INTO track_mbids (recording_mbid, track_id, confidence, imported) VALUES
                       ('a0000000-0000-4000-8000-00000000000a', %(t)s, 'overlap_verified', FALSE),
                       ('a0000000-0000-4000-8000-00000000000b', %(t)s, 'overlap_verified', FALSE),
                       ('a0000000-0000-4000-8000-00000000000c', %(t)s, 'overlap_verified', TRUE)""",
                    {"t": track})
    plan = {"albums": [{"album_id": album, "verified": True, "rg_mbid": None, "release_mbid": None,
                        "artist_mbid": None,
                        "recordings": {track: "a0000000-0000-4000-8000-00000000000d"}}],
            "artist_mbids": []}
    st = content.apply_artist(lib.id, "Aurora", plan)
    assert st["track_mbids_pruned"] == 1
    with conn.cursor() as cur:
        cur.execute("SELECT right(recording_mbid::text, 1), matcher_version, imported FROM track_mbids "
                    "ORDER BY 1")
        # a: no file carries it — gone; b: the live file carries it; c: imported; d: the new binding
        assert cur.fetchall() == [("b", 1, False), ("c", 1, True), ("d", 2, False)]


def test_a_performer_list_joined_by_semicolons_splits_into_its_members(node):
    """D3 (DESIGN §4.1): `; ` is MusicBrainz's separator of a classical
    release's performers; every part an MB artist, the whole not one."""
    conn, _fake = node
    from canon import content
    with conn.cursor() as cur:
        _dump_node(cur)
        s = Slice(cur)
        for gid, name in ((LEADER, "Trio Mediæval"), (ENSEMBLE, "Arve Henriksen"), (OTHER, "Anders Jormin")):
            s.artist(gid, name)
    name = "Trio Mediæval; Arve Henriksen; Anders Jormin"
    members = content._collab_members("00000000-0000-4000-8000-000000000000", name,
                                      {"albums": []})
    assert members == [("Trio Mediæval", LEADER), ("Arve Henriksen", ENSEMBLE), ("Anders Jormin", OTHER)]
    assert content._collab_members("00000000-0000-4000-8000-000000000000",
                                   "Hoff Ensemble: Jan Gunnar Hoff, Audun Kleive & Anders Jormin",
                                   {"albums": []}) == []


def _meyers(cur, tracks):
    from mb_fakes import Library
    lib = Library(cur, "Anne Akiko Meyers")
    album = lib.album("The Four Seasons", tracks)
    s = Slice(cur)
    a = s.artist(LEADER, "Anne Akiko Meyers")
    return lib, album, s, s.credit((a, "Anne Akiko Meyers"))


def test_a_lengthless_recording_loses_to_a_timed_one_and_anchors_no_duration_pass(node):
    """T4 (DESIGN §2.1): a recording with no length scored a perfect
    duration (dd = 0) and won; now it ranks after every timed candidate."""
    conn, _fake = node
    from canon import content
    with conn.cursor() as cur:
        _dump_node(cur)
        lib, album, s, c = _meyers(cur, [("Spring: I. Allegro", 210.0), ("Spring: II. Largo", 160.0),
                                         ("Spring: III. Allegro", 250.0)])
        g = s.release_group(str(uuid.uuid4()), "Live in Milton Keynes", c)
        live = [str(uuid.uuid4()) for _ in range(3)]
        s.release(str(uuid.uuid4()), "Live in Milton Keynes", g, c,
                  [(t, None, r) for t, r in zip(("Spring: I. Allegro", "Spring: II. Largo",
                                                 "Spring: III. Allegro"), live)])
        g2 = s.release_group(str(uuid.uuid4()), "The Four Seasons", c)
        studio = [str(uuid.uuid4()) for _ in range(3)]
        s.release(str(uuid.uuid4()), "The Four Seasons", g2, c,
                  [(t, ms, r) for t, ms, r in zip(("Spring: I. Allegro", "Spring: II. Largo",
                                                   "Spring: III. Allegro"), (210400, 159800, 250300), studio)])
    plan = content.resolve_artist(lib.id, "Anne Akiko Meyers")
    (a,) = plan["albums"]
    assert set(a["recordings"].values()) == set(studio) and a["pass"] == "strict"
    with conn.cursor() as cur:
        cur.execute("UPDATE mb_recording SET length = NULL")
        cur.execute("UPDATE mb_track SET length = NULL")
    (a,) = content.resolve_artist(lib.id, "Anne Akiko Meyers")["albums"]
    assert a["pass"] == "title"                       # no duration pass verified it on no lengths


def test_the_albums_own_group_ranks_before_a_timed_recording_off_it(node):
    """§12.7 (the 50-move sample, "Случай в ресторане"): a bound album's
    recording on its own group, lengthless, gave way to a timed one of the
    same name off the group. The group the resolve binds ranks first; a timed
    recording still anchors the duration pass that verifies the album."""
    conn, _fake = node
    from canon import content
    names = ("Spring: I. Allegro", "Spring: II. Largo", "Spring: III. Allegro")
    with conn.cursor() as cur:
        _dump_node(cur)
        lib, album, s, c = _meyers(cur, [(n, sec) for n, sec in zip(names, (210.0, 160.0, 250.0))])
        cur.execute("UPDATE albums SET title = 'Live in Milton Keynes' WHERE id = %s", (album,))
        g = s.release_group(str(uuid.uuid4()), "Live in Milton Keynes", c)
        live = [str(uuid.uuid4()) for _ in names]
        s.release(str(uuid.uuid4()), "Live in Milton Keynes", g, c,
                  [(n, ms, r) for n, ms, r in zip(names, (210200, None, None), live)])
        g2 = s.release_group(str(uuid.uuid4()), "The Four Seasons", c)
        studio = [str(uuid.uuid4()) for _ in names]
        s.release(str(uuid.uuid4()), "The Four Seasons", g2, c,
                  [(n, ms, r) for n, ms, r in zip(names, (210400, 159800, 250300), studio)])
        cur.execute("SELECT gid::text FROM mb_release_group WHERE id = %s", (g,))
        live_rg = cur.fetchone()[0]
    (a,) = content.resolve_artist(lib.id, "Anne Akiko Meyers")["albums"]
    assert (a["rg_mbid"], a["pass"]) == (live_rg, "strict")
    assert set(a["recordings"].values()) == set(live)


def test_a_track_timed_apart_from_its_recording_fits_by_the_track_s_length(node):
    """§12.7 (Polarity's "The Elder": 405 s as a track, 396.5 s as a
    recording): the strict pass's 8 s reach the file by the track's length."""
    conn, _fake = node
    from canon import content
    names = ("Spring: I. Allegro", "Spring: II. Largo", "Spring: III. Allegro")
    with conn.cursor() as cur:
        _dump_node(cur)
        lib, album, s, c = _meyers(cur, [(n, sec) for n, sec in zip(names, (210.0, 405.1, 250.0))])
        g = s.release_group(str(uuid.uuid4()), "The Four Seasons", c)
        recs = [str(uuid.uuid4()) for _ in names]
        s.release(str(uuid.uuid4()), "The Four Seasons", g, c,
                  [(n, ms, r) for n, ms, r in zip(names, (210400, 405000, 250300), recs)])
        cur.execute("UPDATE mb_recording SET length = 396500 WHERE gid = %s", (recs[1],))
    (a,) = content.resolve_artist(lib.id, "Anne Akiko Meyers")["albums"]
    assert a["pass"] == "strict"
    assert set(a["recordings"].values()) == set(recs)


def test_a_fuzzy_title_that_disagrees_on_its_movement_is_no_candidate(node):
    """T5 (DESIGN §2.2): Meyers' Spring I bound to "… No. 1: II. Largo"."""
    conn, _fake = node
    from canon import content
    with conn.cursor() as cur:
        _dump_node(cur)
        lib, album, s, c = _meyers(cur, [("Concerto No. 1 in E major, RV 269: I. Allegro", 210.0),
                                         ("Concerto No. 1 in E major, RV 269: III. Allegro", 250.0),
                                         ("Sonata No. 14, Op. 27 No. 2", 300.0)])
        g = s.release_group(str(uuid.uuid4()), "Seasons", c)
        wrong, moon = str(uuid.uuid4()), str(uuid.uuid4())
        s.release(str(uuid.uuid4()), "Seasons", g, c,
                  [("Concerto No. 1 in E major, RV 269 “La primavera”: II. Largo", 210500, wrong),
                   ("Piano Sonata No. 14 in C-sharp minor, Op. 27 No. 2", 300200, moon)])
        cur.execute("SELECT title_numbers_disagree(title_numbers(%s), title_numbers(%s)),"
                    "       title_numbers_disagree(title_numbers(%s), title_numbers(%s))",
                    ("Concerto No. 1 in E major, RV 269: I. Allegro",
                     "Concerto No. 1 in E major, RV 269 “La primavera”: II. Largo",
                     "Sonata No. 14, Op. 27 No. 2", "Piano Sonata No. 14 in C-sharp minor, Op. 27 No. 2"))
        assert cur.fetchone() == (True, False)
    cur = conn.cursor()
    cur.execute("SET pg_trgm.similarity_threshold = 0.3")
    plan = content.resolve_artist(lib.id, "Anne Akiko Meyers")
    recs = {r for a in plan["albums"] for r in a["recordings"].values()}
    assert wrong not in recs


def test_the_data_step_wakes_every_owned_artist_once_and_the_start_queues_the_re_read(node):
    """T14 (DESIGN §5.1): every owned artist's canon is due again (the
    matcher changed library-wide), an artist with no file is not; the
    re-read is state, not the marker — a row under the reader's revision is
    a changed file to the next scan."""
    conn, _fake = node
    import db_migrate
    import scanner
    from mb_fakes import Library
    with conn.cursor() as cur:
        Library(cur, "Owned").album("LP", [("Song", 200.0)])
        cur.execute("INSERT INTO artists (id, name, last_mb_sync) VALUES (gen_random_uuid(), 'Phantom', now())")
        cur.execute("UPDATE artists SET last_mb_sync = now()")
        cur.execute("UPDATE media_files SET tags_read_rev = 0")
    assert db_migrate._wake_owned_canon(conn) == 1
    assert db_migrate._wake_owned_canon(conn) == 0                 # a second run does nothing
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM artists WHERE last_mb_sync IS NULL")
        assert cur.fetchall() == [("Owned",)]
        cur.execute("SELECT count(*) FROM media_files WHERE tags_read_rev < %s", (scanner.TAGS_READ_REV,))
        assert cur.fetchone() == (1,)
