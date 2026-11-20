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


def test_a_barcode_naming_its_own_release_binds_it_beside_a_box_that_fits_closer(node):
    """§12.9 Q18, the harness's case 1215: the copy's barcode names its own
    release; a box's first medium carrying the same recordings fits a little
    closer. A smaller error among fitting releases is no preference — the
    witness binds its release."""
    conn, _fake = node
    with conn.cursor() as cur:
        _dump_node(cur)
        album = _polarity(cur)
        s, leader, ens = _mb(cur)
        own = str(uuid.uuid4())
        _release(s, cur, own, "Polarity", [(ens, "Hoff Ensemble")], fmt=CD, barcode="7041888520023")
        box = str(uuid.uuid4())
        _box(s, cur, box, "The Box", [(ens, "Hoff Ensemble")], disc=1)
        cur.execute("UPDATE mb_track t SET length = t.length + 500 FROM mb_medium m JOIN mb_release r "
                    "ON r.id = m.release JOIN mb_release_group g ON g.id = r.release_group "
                    "WHERE t.medium = m.id AND g.gid = %s AND m.position = 1", (box,))
        cur.execute("UPDATE mb_medium SET format = %s", (CD,))
        cur.execute("UPDATE media_files SET barcode = '7041888520023'")
        assert _run()["bound_albums"] == 1
        cur.execute("SELECT musicbrainz_id::text FROM albums WHERE id = %s", (album,))
        assert cur.fetchone() == (own,)


@pytest.mark.parametrize("same_group, also_the_fit", ((False, False), (True, False), (False, True)))
def test_a_witness_naming_a_release_that_does_not_fit_contradicts_another_group_s(node, same_group, also_the_fit):
    """§3.3 as §12.9 Q18 reads it: the copy's barcode names a candidate
    release that does not fit (five positions against four files) while a
    release of another group does — no bind. Editions in one group (the
    barcode a deluxe's, the positions the standard's) are no contradiction,
    and nor is a barcode that names the fitting release as well (the
    harness's case 1129: one barcode on two releases of the artist's)."""
    conn, _fake = node
    with conn.cursor() as cur:
        _dump_node(cur)
        album = _polarity(cur)
        s, leader, ens = _mb(cur)
        fits_rg = str(uuid.uuid4())
        _release(s, cur, fits_rg, "Polarity", [(ens, "Hoff Ensemble")])
        named_rg = fits_rg if same_group else str(uuid.uuid4())
        c = s.credit((ens, "Hoff Ensemble"))
        cur.execute("SELECT id FROM mb_release_group WHERE gid = %s", (named_rg,))
        row = cur.fetchone()
        g = row[0] if row else s.release_group(named_rg, "Polarity (Deluxe)", c)
        deluxe = s.release(str(uuid.uuid4()), "Polarity (Deluxe)", g, c,
                           [(f"Movement {i}", ms, str(uuid.uuid4())) for i, ms in enumerate(LENGTHS + (300000,), 1)])
        cur.execute("UPDATE mb_release SET barcode = '7041888520047' WHERE id = %s", (deluxe,))
        if also_the_fit:
            cur.execute("UPDATE mb_release r SET barcode = '7041888520047' FROM mb_release_group g "
                        "WHERE g.id = r.release_group AND g.gid = %s", (fits_rg,))
        cur.execute("UPDATE mb_medium SET format = %s", (DM,))
        cur.execute("UPDATE media_files SET barcode = '7041888520047'")
        bound = same_group or also_the_fit
        st = _run()
        assert st["bound_albums"] == (1 if bound else 0)
        cur.execute("SELECT musicbrainz_id::text FROM albums WHERE id = %s", (album,))
        assert cur.fetchone() == ((fits_rg if bound else None),)


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


def test_an_album_still_arriving_waits_for_its_files_unless_the_scan_is_explicit(node):
    """Round 1 H1: four of six declared files bound the copy to the release
    its first four fit, for good — the same-titled single a partial album
    fills (LIBRARY-WATCH-DESIGN §3.5). A watcher's pass leaves it."""
    conn, _fake = node
    from canon import positional
    with conn.cursor() as cur:
        _dump_node(cur)
        _polarity(cur)
        cur.execute("UPDATE media_files SET track_total = 6")
        s, leader, ens = _mb(cur)
        _release(s, cur, str(uuid.uuid4()), "Polarity", [(ens, "Hoff Ensemble")])
    st = positional.run(take_arriving=False)
    assert (st["copies"], st["bound_albums"]) == (0, 0)
    assert positional.run()["bound_albums"] == 1


def test_a_copy_that_changes_is_asked_with_its_album_s_other_copies(node):
    """Round 1 H2: two copies fitting two groups bound neither; when one of
    them changed, it was asked alone and bound the album to its group."""
    conn, _fake = node
    with conn.cursor() as cur:
        _dump_node(cur)
        album = _polarity(cur)
        v2 = _second_copy(cur, album, shift=30)
        s, leader, ens = _mb(cur)
        _release(s, cur, str(uuid.uuid4()), "Polarity", [(ens, "Hoff Ensemble")])
        _release(s, cur, str(uuid.uuid4()), "Polarity", [(ens, "Hoff Ensemble")],
                 lengths=tuple(ms + 30000 for ms in LENGTHS))
        assert _run()["album_conflict"] == 1
        cur.execute("UPDATE media_files SET barcode = '7041888520030' WHERE album_variant_id = %s", (v2,))
        st = _run()
        assert (st["asked"], st["bound_albums"], st["album_conflict"]) == (2, 0, 1)
        cur.execute("SELECT musicbrainz_id FROM albums WHERE id = %s", (album,))
        assert cur.fetchone() == (None,)


def _second_copy(cur, album, shift=0.0):
    """Another copy of `album`: its first copy's files, `shift` seconds longer."""
    cur.execute("INSERT INTO album_variants (album_id, directory_path, raw_title) "
                "VALUES (%s, %s, 'Polarity') RETURNING id", (album, f"/music/Hoff/Polarity +{shift}"))
    v = cur.fetchone()[0]
    cur.execute("""INSERT INTO media_files (track_id, album_variant_id, file_path, sample_rate, bit_depth,
                                            duration_seconds, track_number, disc_number)
                   SELECT DISTINCT ON (track_number) track_id, %s, file_path || %s, 96000, 24,
                          duration_seconds + %s, track_number, 1
                     FROM media_files ORDER BY track_number, id""", (v, f".{v}", shift))
    return v


def test_a_later_copy_of_an_album_the_tier_bound_binds_to_its_group_or_not_at_all(node):
    """Round 1 M1: a copy of an album the tier had bound — one that waited
    while a sibling bound it, or one added since — was never asked again.
    It binds to the album's group, or stays unbound; the album keeps one."""
    conn, _fake = node
    with conn.cursor() as cur:
        _dump_node(cur)
        album = _polarity(cur)
        s, leader, ens = _mb(cur)
        rg = str(uuid.uuid4())
        rel, recs = _release(s, cur, rg, "Polarity", [(ens, "Hoff Ensemble")])
        _release(s, cur, str(uuid.uuid4()), "Polarity", [(ens, "Hoff Ensemble")],
                 lengths=tuple(ms + 30000 for ms in LENGTHS))
        assert _run()["bound_albums"] == 1
        v2 = _second_copy(cur, album)
        v3 = _second_copy(cur, album, shift=30)
        st = _run()
        assert (st["bound_albums"], st["bound_copies"], st["abstained"]) == (0, 1, 1)
        cur.execute("SELECT musicbrainz_id::text FROM albums WHERE id = %s", (album,))
        assert cur.fetchone() == (rg,)
        cur.execute("SELECT id, release_mbid::text FROM album_variants WHERE id IN (%s, %s) ORDER BY id",
                    (v2, v3))
        assert cur.fetchall() == [(v2, rel), (v3, None)]
        cur.execute("SELECT recording_mbid::text FROM media_files WHERE album_variant_id = %s "
                    "ORDER BY track_number", (v2,))
        assert [r[0] for r in cur.fetchall()] == recs
        assert _run()["asked"] == 1                   # its album's unbound copies changed: asked once more
        assert _run()["asked"] == 0                   # then the abstention is kept


def test_on_a_slice_fed_node_a_data_track_is_no_position(node):
    """Round 1 M3: the slice's fit leaves an enhanced CD's data track out;
    MusicBrainz's answer counted it, so the bind never confirmed."""
    conn, fake = node
    with conn.cursor() as cur:
        album = _polarity(cur)
        s, leader, ens = _mb(cur)
        rg = str(uuid.uuid4())
        rel, recs = _release(s, cur, rg, "Polarity", [(ens, "Hoff Ensemble")], fmt=CD)
        cur.execute("""INSERT INTO mb_track (id, gid, recording, medium, position, number, name, artist_credit,
                                             length, is_data_track)
                       SELECT t.id + 1000000, gen_random_uuid(), t.recording, t.medium, 5, '5', 'Data',
                              t.artist_credit, 60000, true
                         FROM mb_track t JOIN mb_medium m ON m.id = t.medium
                         JOIN mb_release r ON r.id = m.release AND r.gid = %s WHERE t.position = 4""", (rel,))
        cur.execute("INSERT INTO mb_slice_fetches (name_key, matched_ids) VALUES ('hoff ensemble', 1) "
                    "ON CONFLICT DO NOTHING")
    fake.artist(ENSEMBLE, "Hoff Ensemble")
    fake.release_group(rg, "Polarity", credit(("Hoff Ensemble", ENSEMBLE)))
    fake.release(rel, "Polarity", rg, credit(("Hoff Ensemble", ENSEMBLE)),
                 [{"title": f"Movement {i}", "length": ms, "recording": r}
                  for i, (ms, r) in enumerate(zip(LENGTHS, recs), 1)]
                 + [{"title": "Data", "length": 60000, "recording": str(uuid.uuid4()), "data": True}])
    assert _run()["waiting"] == 1
    drain()
    assert _run()["bound_albums"] == 1
    with conn.cursor() as cur:
        cur.execute("SELECT musicbrainz_id::text FROM albums WHERE id = %s", (album,))
        assert cur.fetchone() == (rg,)


def test_a_file_of_another_album_on_a_shared_track_keeps_its_own_recording(node):
    """T19 (§12.1 Q3): "The Elder" and "Within" are one track each with a
    file on Polarity (the ensemble's 405 s and 360 s) and one on the leader's
    solo Stories (289 s and 276 s). The positional bind is per file: Stories'
    files and their binding stay as they were."""
    conn, _fake = node
    from mb_fakes import Library
    titles = ("The Elder", "Within", "Movement 3", "Movement 4")
    with conn.cursor() as cur:
        _dump_node(cur)
        lib = Library(cur, "Jan Gunnar Hoff")
        polarity = lib.album("Polarity", list(zip(titles, FILES)))
        ens = _bind_artist(cur, "Hoff Ensemble", ENSEMBLE)
        cur.execute("UPDATE album_artists SET artist_id = %s WHERE album_id = %s", (ens, polarity))
        cur.execute("INSERT INTO artist_mbids (mbid, artist_id, confidence) VALUES (%s, %s, 'overlap_verified')",
                    (LEADER, lib.id))
        stories = lib.album("Stories", [("The Elder", 288.7), ("Within", 275.6), ("Prelude", 190.0)])
        own = str(uuid.uuid4())
        cur.execute("""UPDATE media_files mf SET recording_mbid = %s FROM album_variants av
                        WHERE av.id = mf.album_variant_id AND av.album_id = %s AND mf.track_number = 1""",
                    (own, stories))
        cur.execute("""INSERT INTO track_mbids (recording_mbid, track_id, confidence, matcher_version)
                       SELECT %s, track_id, 'overlap_verified', 2 FROM media_files WHERE recording_mbid = %s""",
                    (own, own))
        s, leader, ensemble = _mb(cur)
        rg = str(uuid.uuid4())
        _rel, recs = _release(s, cur, rg, "Polarity", [(ensemble, "Hoff Ensemble")])
        assert _run()["bound_albums"] == 1
        cur.execute("""SELECT al.id::text, mf.track_number, mf.recording_mbid::text
                         FROM media_files mf JOIN album_variants av ON av.id = mf.album_variant_id
                         JOIN albums al ON al.id = av.album_id ORDER BY al.title, mf.track_number""")
        rows = cur.fetchall()
        assert [r[2] for r in rows if r[0] == polarity] == recs
        assert [r[2] for r in rows if r[0] == stories] == [own, None, None]
        cur.execute("SELECT musicbrainz_id FROM albums WHERE id = %s", (stories,))
        assert cur.fetchone() == (None,)
        cur.execute("SELECT recording_mbid::text FROM track_mbids WHERE track_id = "
                    "(SELECT track_id FROM media_files WHERE recording_mbid = %s)", (own,))
        assert sorted(r[0] for r in cur.fetchall()) == sorted([own, recs[0]])


def test_the_track_layer_leaves_the_files_the_positional_pass_bound(node):
    """Round 2 H1: an artist whose one album the positional pass bound has no
    album the cascade resolves, so the track layer took it and stamped every
    file of each title-matched track — the positional copy's included."""
    conn, _fake = node
    from canon import content
    with conn.cursor() as cur:
        _dump_node(cur)
        _polarity(cur)
        s, leader, ens = _mb(cur)
        _rel, recs = _release(s, cur, str(uuid.uuid4()), "Polarity", [(ens, "Hoff Ensemble")])
        # the leader's own recordings of the same names, lengths within the track layer's reach
        _release(s, cur, str(uuid.uuid4()), "Live", [(leader, "Jan Gunnar Hoff")],
                 lengths=tuple(ms + 3000 for ms in LENGTHS))
        assert _run()["bound_albums"] == 1
    from uuid_utils import artist_uuid
    content.canonicalize_trackonly(str(artist_uuid("Jan Gunnar Hoff")), "Jan Gunnar Hoff")
    with conn.cursor() as cur:
        cur.execute("SELECT recording_mbid::text FROM media_files ORDER BY track_number")
        assert [r[0] for r in cur.fetchall()] == recs


def test_digital_media_s_window_holds_without_the_format_names(node):
    """Round 2 M1: a slice carries a medium's format id, not the formats'
    names; read by name, every medium on a slice-fed node fell to 2 s."""
    conn, _fake = node
    with conn.cursor() as cur:
        _dump_node(cur)
        _polarity(cur, files=(313.8, 245.4, 401.0, 198.6))   # 1.8 s past a whole-second length
        cur.execute("DELETE FROM mb_medium_format")
        s, leader, ens = _mb(cur)
        _release(s, cur, str(uuid.uuid4()), "Polarity", [(ens, "Hoff Ensemble")])
        assert _run()["bound_albums"] == 0


def test_a_broadcast_group_is_no_candidate(node):
    """Round 2 M2 (§3.4): 'Other' and 'Broadcast' stay out, as in _RG_SQL."""
    conn, _fake = node
    with conn.cursor() as cur:
        _dump_node(cur)
        _polarity(cur)
        s, leader, ens = _mb(cur)
        c = s.credit((ens, "Hoff Ensemble"))
        g = s.release_group(str(uuid.uuid4()), "Polarity", c, primary="Broadcast")
        s.release(str(uuid.uuid4()), "Polarity", g, c,
                  [(f"Movement {i}", ms, str(uuid.uuid4())) for i, ms in enumerate(LENGTHS, 1)])
        cur.execute("UPDATE mb_medium SET format = %s", (DM,))
        st = _run()
        assert (st["bound_albums"], st["abstained"]) == (0, 1)


def test_an_abstention_is_asked_again_when_its_album_loses_a_copy(node):
    """Round 2 M3: two copies fitting two groups bound neither and kept their
    digests; with one of them gone, the other alone fits one group."""
    conn, _fake = node
    with conn.cursor() as cur:
        _dump_node(cur)
        album = _polarity(cur)
        v2 = _second_copy(cur, album, shift=30)
        s, leader, ens = _mb(cur)
        rg = str(uuid.uuid4())
        _release(s, cur, rg, "Polarity", [(ens, "Hoff Ensemble")])
        _release(s, cur, str(uuid.uuid4()), "Polarity", [(ens, "Hoff Ensemble")],
                 lengths=tuple(ms + 30000 for ms in LENGTHS))
        assert _run()["album_conflict"] == 1
        cur.execute("DELETE FROM media_files WHERE album_variant_id = %s", (v2,))
        cur.execute("DELETE FROM album_variants WHERE id = %s", (v2,))
        assert _run()["bound_albums"] == 1
        cur.execute("SELECT musicbrainz_id::text FROM albums WHERE id = %s", (album,))
        assert cur.fetchone() == (rg,)


def test_any_slice_asks_an_abstained_copy_again(node):
    """Round 2 M4: a slice of any name can bring a release credited to the
    copy's artists (one whose group credits only the composer arrives with
    the composer's); the digest followed only the copy's own artists' slices."""
    conn, _fake = node
    with conn.cursor() as cur:
        _dump_node(cur)
        album = _polarity(cur)
        s, leader, ens = _mb(cur)
        cur.execute("INSERT INTO mb_slice_fetches (name_key, matched_ids, payload_sha256) "
                    "VALUES ('arvo pärt', 1, repeat('a', 64))")
        assert _run()["abstained"] == 1                 # nothing fits yet
        # round 3 M-B: the same slice again, or a name with no match, is no change
        cur.execute("UPDATE mb_slice_fetches SET fetched_at = now() + interval '1 hour'")
        cur.execute("INSERT INTO mb_slice_fetches (name_key, matched_ids) VALUES ('nobody', 0)")
        assert _run()["asked"] == 0
        _release(s, cur, str(uuid.uuid4()), "Polarity", [(ens, "Hoff Ensemble")])
        cur.execute("UPDATE mb_slice_fetches SET payload_sha256 = repeat('b', 64) WHERE name_key = 'arvo pärt'")
        st = _run()
        assert (st["asked"], st["bound_albums"]) == (1, 1)


def test_a_binding_from_the_files_never_restates_this_node_s_own(node):
    """Round 2 M5 (round 1 L2): bind_owned_recordings binds from what the
    files carry, which says nothing of the matcher that stamped them: an own
    row of an older matcher stays; a peer's is replaced."""
    conn, _fake = node
    from canon import content
    from mb_fakes import Library
    rec = str(uuid.uuid4())
    with conn.cursor() as cur:
        lib = Library(cur, "Anne Akiko Meyers")
        lib.album("The Four Seasons", [("Spring: I. Allegro", 210.0)])
        cur.execute("UPDATE media_files SET recording_mbid = %s", (rec,))
        cur.execute("INSERT INTO tracks (id, title) VALUES (gen_random_uuid(), 'elsewhere') RETURNING id::text")
        elsewhere = cur.fetchone()[0]
        cur.execute("INSERT INTO track_mbids (recording_mbid, track_id, confidence, matcher_version) "
                    "VALUES (%s, %s, 'overlap_verified', 1)", (rec, elsewhere))
    content.bind_owned_recordings(lib.id)
    with conn.cursor() as cur:
        cur.execute("SELECT track_id::text, matcher_version, imported FROM track_mbids WHERE recording_mbid = %s",
                    (rec,))
        assert cur.fetchone() == (elsewhere, 1, False)
        cur.execute("UPDATE track_mbids SET imported = TRUE WHERE recording_mbid = %s", (rec,))
    content.bind_owned_recordings(lib.id)
    with conn.cursor() as cur:
        cur.execute("SELECT track_id::text <> %s, matcher_version, imported FROM track_mbids "
                    "WHERE recording_mbid = %s", (elsewhere, rec))
        assert cur.fetchone() == (True, 2, False)


def test_the_collaboration_sweep_finds_a_list_joined_by_semicolons(node, monkeypatch):
    """Round 2 M5 (round 1 L6): split_collaborations' candidates missed D3's
    form, which only the inline path after a wake split."""
    conn, _fake = node
    import mb_backend
    from canon import content
    from mb_fakes import Library
    monkeypatch.setattr(mb_backend, "LOCAL_DUMP", True)
    with conn.cursor() as cur:
        _dump_node(cur)
        Library(cur, "Trio Mediæval; Arve Henriksen; Anders Jormin").album("Rímur", [("Om Kvelden", 300.0)])
    assert content.split_collaborations(dry_run=True)["candidates"] == 1


def test_a_canon_trigger_leaves_an_album_still_arriving(node, monkeypatch):
    """Round 3 H-A: the canon a slice or MusicBrainz's answers trigger took an
    album still arriving as an explicit scan does, and the positional pass
    bound it for good to the release its first files fit."""
    from tests.test_scan_unreachable import BACKEND
    monkeypatch.chdir(BACKEND)                     # main mounts static/ relative to its directory
    import canon
    import main
    import mb_backend
    import mb_dump_load
    from canon import content
    seen = []
    monkeypatch.setattr(mb_backend, "refresh", lambda: True)
    monkeypatch.setattr(mb_backend, "LOCAL_DUMP", True)
    monkeypatch.setattr(mb_dump_load, "load_genre_list", lambda: None)
    monkeypatch.setattr(content, "canonicalize_pending", lambda **kw: seen.append(kw) or {})
    monkeypatch.setattr(content, "distill_album_residue", lambda **kw: {})
    monkeypatch.setattr(content, "distill_album_coverage", lambda **kw: {})
    monkeypatch.setattr(canon, "canonize_phantom_similars", lambda **kw: {})
    main._canon_run(False, ["00000000-0000-4000-8000-000000000001"], [])
    main._canon_run(True, [], [])
    assert [k.get("take_arriving") for k in seen] == [False, False]


def test_an_album_only_labelled_positional_is_the_cascade_s(node):
    """Round 3 M-A: the exclusion was the album's label, which a seed or a
    share carries: a node whose own copy no position bound left the album to
    no tier. A copy the pass bound here is what keeps the cascade off."""
    conn, _fake = node
    from canon import content
    names = ("Spring: I. Allegro", "Spring: II. Largo", "Spring: III. Allegro")
    with conn.cursor() as cur:
        _dump_node(cur)
        lib, album, s, c = _meyers(cur, [(n, sec) for n, sec in zip(names, (210.0, 160.0, 250.0))])
        g = s.release_group(str(uuid.uuid4()), "The Four Seasons", c)
        s.release(str(uuid.uuid4()), "The Four Seasons", g, c,
                  [(n, ms, str(uuid.uuid4())) for n, ms in zip(names, (210400, 159800, 250300))])
        cur.execute("UPDATE albums SET musicbrainz_id = gen_random_uuid(), mb_match_confidence = 'positional' "
                    "WHERE id = %s", (album,))
    assert album in {a["album_id"] for a in content.resolve_artist(lib.id, "Anne Akiko Meyers")["albums"]}
    with conn.cursor() as cur:
        cur.execute("UPDATE album_variants SET positional_bound = TRUE WHERE album_id = %s", (album,))
    assert album not in {a["album_id"] for a in content.resolve_artist(lib.id, "Anne Akiko Meyers")["albums"]}


def test_a_later_copy_fitting_its_album_s_group_and_another_binds_to_its_album_s(node):
    """Round 3 L-A: decided before the album's group was applied, such a copy
    abstained across the two groups although one of them is its album's."""
    conn, _fake = node
    with conn.cursor() as cur:
        _dump_node(cur)
        album = _polarity(cur)
        s, leader, ens = _mb(cur)
        rg = str(uuid.uuid4())
        rel, recs = _release(s, cur, rg, "Polarity", [(ens, "Hoff Ensemble")])
        assert _run()["bound_albums"] == 1
        _release(s, cur, str(uuid.uuid4()), "Polarity (Live)", [(ens, "Hoff Ensemble")])
        v2 = _second_copy(cur, album)
        assert _run()["bound_copies"] == 1
        cur.execute("SELECT release_mbid::text FROM album_variants WHERE id = %s", (v2,))
        assert cur.fetchone() == (rel,)


def test_on_a_slice_fed_node_the_confirm_refuses_a_group_of_another_type(node):
    """Round 3 L-E: the slice's group an album, MusicBrainz's answer a
    broadcast: no bind."""
    conn, fake = node
    with conn.cursor() as cur:
        _polarity(cur)
        s, leader, ens = _mb(cur)
        rg = str(uuid.uuid4())
        rel, recs = _release(s, cur, rg, "Polarity", [(ens, "Hoff Ensemble")])
        cur.execute("INSERT INTO mb_slice_fetches (name_key, matched_ids) VALUES ('hoff ensemble', 1) "
                    "ON CONFLICT DO NOTHING")
    fake.artist(ENSEMBLE, "Hoff Ensemble")
    fake.release_group(rg, "Polarity", credit(("Hoff Ensemble", ENSEMBLE)), primary="Broadcast")
    fake.release(rel, "Polarity", rg, credit(("Hoff Ensemble", ENSEMBLE)),
                 [{"title": f"Movement {i}", "length": ms, "recording": r}
                  for i, (ms, r) in enumerate(zip(LENGTHS, recs), 1)], media_format="Digital Media")
    assert _run()["waiting"] == 1
    drain()
    assert _run()["bound_albums"] == 0


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


@pytest.mark.parametrize("closer_first", (True, False))
def test_two_groups_equal_on_the_four_keys_bind_the_closer_by_length(node, closer_first):
    """§12.8 (the harness's case 2252: Kreisler's 1926 concerto bound to
    another performance's group of the same name): two groups equal on name,
    coverage, size and type were left to the plan's order. The closer by the
    sum of |track − file| binds, whichever MusicBrainz holds first."""
    conn, _fake = node
    from canon import content
    names = ("Violin Concerto: I. Allegro ma non troppo", "Violin Concerto: II. Larghetto",
             "Violin Concerto: III. Rondo. Allegro")
    title = "Violin Concerto in D major, op. 61"
    with conn.cursor() as cur:
        _dump_node(cur)
        lib, album, s, c = _meyers(cur, [(n, sec) for n, sec in zip(names, (1340.0, 560.0, 600.0))])
        cur.execute("UPDATE albums SET title = %s WHERE id = %s", (title, album))
        made = {}
        for which in (("closer", "farther") if closer_first else ("farther", "closer")):
            g = s.release_group(str(uuid.uuid4()), title, c)
            recs = [str(uuid.uuid4()) for _ in names]
            lengths = (1341200, 560400, 600900) if which == "closer" else (1254000, 563000, 596000)
            s.release(str(uuid.uuid4()), title, g, c,
                      [(n, ms, r) for n, ms, r in zip(names, lengths, recs)])
            cur.execute("SELECT gid::text FROM mb_release_group WHERE id = %s", (g,))
            made[which] = (cur.fetchone()[0], recs)
    (a,) = content.resolve_artist(lib.id, "Anne Akiko Meyers")["albums"]
    assert a["rg_mbid"] == made["closer"][0]
    assert set(a["recordings"].values()) == set(made["closer"][1])


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
    plan = content.resolve_artist(lib.id, "Anne Akiko Meyers")
    recs = {r for a in plan["albums"] for r in a["recordings"].values()}
    assert wrong not in recs


def test_the_data_step_wakes_every_owned_artist_once_and_the_start_queues_the_re_read(node):
    """T14 (DESIGN §5.1): every owned artist's canon is due again (the
    matcher changed library-wide), an artist with no file is not; a row under
    the reader's revision is a changed file to the next scan (the whole
    walk's marker: test_canon_classical_reader_db)."""
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
