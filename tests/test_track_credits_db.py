"""track_credits against every sweep and merge of artists (CREDITS-1 step 2a,
T7), on a real database.

A composer lives in track_credits, never in track_artists, and its artist_id
RESTRICTs: every DELETE FROM artists in the tree is a sweep or a merge
written against track_artists, and one that forgot the table would fail
rather than drop a credit silently. Each test runs one of them with an
artist that only composes present:

- the scanner's prune, the HQPlayer library's forget, the phantom discard
  and the un-split's member GC keep him while a credit names him, and sweep
  him once the track his credit was on is gone;
- a track merge (_update_track_uuid), an artist merge (recanonicalize_artist),
  the clean pass's merge, the identity renormalisation and a compound split
  carry his credits to the survivor;
- a bare delete fails.
Skipped without a cluster."""

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BACKEND = REPO / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

psycopg2 = pytest.importorskip("psycopg2")
pytest.importorskip("sqlalchemy")

from transliterate import latinize  # noqa: E402
from uuid_utils import artist_uuid, track_uuid  # noqa: E402


@pytest.fixture
def db(scratch_dsn, monkeypatch):
    """A raw cursor for setup and checks, and the node's sessions and pool on
    the module's database; every test starts from an empty catalogue."""
    import database
    import db_pool
    import psycopg2.pool
    from sqlalchemy.orm import sessionmaker
    pool = psycopg2.pool.ThreadedConnectionPool(1, 2, dsn=scratch_dsn)
    monkeypatch.setattr(db_pool, "_pool", pool)
    engine = database.make_engine(scratch_dsn)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    conn = psycopg2.connect(scratch_dsn)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("TRUNCATE track_credits, media_files, hqp_library_files, album_variants, "
                "hqp_endpoints, albums, tracks, artists CASCADE")
    yield cur, factory
    conn.close()
    engine.dispose()
    pool.closeall()


def _artist(cur, name, *, id_=None, created_at=None) -> str:
    aid = id_ or str(artist_uuid(name))
    cur.execute("INSERT INTO artists (id, name, name_latin, created_at) "
                "VALUES (%s, %s, %s, COALESCE(%s, now()))",
                (aid, name, latinize(name), created_at))
    return aid


def _track(cur, title, performer_name, performer_id) -> str:
    tid = str(track_uuid(title, performer_name))
    cur.execute("INSERT INTO tracks (id, title, title_latin) VALUES (%s, %s, %s)",
                (tid, title, latinize(title)))
    cur.execute("INSERT INTO track_artists (track_id, artist_id, role) VALUES (%s, %s, 'primary')",
                (tid, performer_id))
    return tid


def _credit(cur, track, artist, position=0, source="tag"):
    cur.execute("INSERT INTO track_credits (track_id, role, artist_id, source, position) "
                "VALUES (%s, 'composer', %s, %s, %s)", (track, artist, source, position))


def _credits(cur) -> set:
    cur.execute("SELECT track_id::text, artist_id::text, source::text, position FROM track_credits")
    return set(cur.fetchall())


def _artists(cur, *ids) -> set:
    cur.execute("SELECT id::text FROM artists WHERE id = ANY(CAST(%s AS uuid[]))", (list(ids),))
    return {r[0] for r in cur.fetchall()}


def _album_with_files(cur, title, performer, tracks, *, hqp_endpoint=None) -> list:
    """An album of `tracks`, one file each — local, or at the HQPlayer
    `hqp_endpoint` — returning the file ids."""
    album = str(uuid.uuid4())
    cur.execute("INSERT INTO albums (id, title) VALUES (%s, %s)", (album, title))
    cur.execute("INSERT INTO album_artists (album_id, artist_id, role) VALUES (%s, %s, 'primary')",
                (album, performer))
    cur.execute("INSERT INTO album_variants (album_id, directory_path, location, hqp_endpoint_id) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                (album, f"/lib/{title}", "hqplayer" if hqp_endpoint else "local", hqp_endpoint))
    variant = cur.fetchone()[0]
    files = []
    for n, tid in enumerate(tracks, 1):
        if hqp_endpoint:
            cur.execute("INSERT INTO hqp_library_files (track_id, album_variant_id, hqp_path, "
                        "file_format, duration_seconds) VALUES (%s, %s, %s, 'FLAC', 200) "
                        "RETURNING id", (tid, variant, f"/hqp/{title}/{n}.flac"))
        else:
            cur.execute("INSERT INTO media_files (track_id, album_variant_id, file_path, "
                        "file_format, track_number, duration_seconds) "
                        "VALUES (%s, %s, %s, 'FLAC', %s, 200) RETURNING id",
                        (tid, variant, f"/lib/{title}/{n}.flac", n))
        files.append(cur.fetchone()[0])
    return files


def test_a_bare_delete_of_a_credited_artist_fails(db):
    cur, _ = db
    performer = _artist(cur, "Akademie fur Alte Musik Berlin")
    composer = _artist(cur, "Johann Sebastian Bach")
    _credit(cur, _track(cur, "Brandenburg Concerto No. 3", "Akademie fur Alte Musik Berlin",
                        performer), composer)
    # the sweeps as they were written before the table: track_artists alone
    with pytest.raises(psycopg2.errors.RestrictViolation):
        cur.execute("""DELETE FROM artists a WHERE a.id = %s
                         AND NOT EXISTS (SELECT 1 FROM track_artists ta WHERE ta.artist_id = a.id)""",
                    (composer,))
    assert _artists(cur, composer) == {composer}


def _prune_case(cur):
    """Two tracks of one performer P. A is credited to C (only there) and to
    K; B, which stays, to K and to D — and D also performs A's sibling album,
    whose file goes. Returns the ids and the file of A and of D's track."""
    p = _artist(cur, "Performer P")
    c, k = _artist(cur, "Composer C"), _artist(cur, "Composer K")
    d = _artist(cur, "Performer D")
    a = _track(cur, "Track A", "Performer P", p)
    b = _track(cur, "Track B", "Performer P", p)
    dt = _track(cur, "Track by D", "Performer D", d)
    _credit(cur, a, c, 0)
    _credit(cur, a, k, 1)
    _credit(cur, b, k, 0)
    _credit(cur, b, d, 1)
    return dict(p=p, c=c, k=k, d=d, a=a, b=b, dt=dt)


def test_the_scanner_prune_keeps_a_credited_artist_and_sweeps_an_uncredited_one(db):
    import scanner
    cur, _ = db
    ids = _prune_case(cur)
    gone_a, _kept_b = _album_with_files(cur, "Album P", ids["p"], [ids["a"], ids["b"]])
    (gone_d,) = _album_with_files(cur, "Album D", ids["d"], [ids["dt"]])
    stats = scanner._prune_files([gone_a, gone_d], scanner._prune_stats(), dry_run=False,
                                 progress_cb=None, cancel_check=None)
    assert stats["orphan_tracks"] == 2
    # C's only credit went with track A: he is swept. D no longer performs
    # anything here but composes B: RESTRICT would have failed the whole sweep.
    assert _artists(cur, *ids.values()) == {ids["p"], ids["k"], ids["d"]}
    assert _credits(cur) == {(ids["b"], ids["k"], "tag", 0), (ids["b"], ids["d"], "tag", 1)}


def test_the_hqplayer_forget_keeps_a_credited_artist_and_sweeps_an_uncredited_one(db):
    import hqp_library
    cur, factory = db
    since = datetime.now(timezone.utc) - timedelta(minutes=1)
    ids = _prune_case(cur)
    cur.execute("INSERT INTO hqp_endpoints (name) VALUES ('box') RETURNING id")
    endpoint = cur.fetchone()[0]
    gone_a, _kept_b = _album_with_files(cur, "Album P", ids["p"], [ids["a"], ids["b"]],
                                        hqp_endpoint=endpoint)
    (gone_d,) = _album_with_files(cur, "Album D", ids["d"], [ids["dt"]], hqp_endpoint=endpoint)
    with factory() as s:
        stats = hqp_library._forget(s, [gone_a, gone_d], since=since)
        s.commit()
    assert stats["orphan_tracks"] == 2
    assert _artists(cur, *ids.values()) == {ids["p"], ids["k"], ids["d"]}
    assert _credits(cur) == {(ids["b"], ids["k"], "tag", 0), (ids["b"], ids["d"], "tag", 1)}


def test_the_phantom_discard_keeps_a_stub_a_credit_names(db):
    from canon import phantom
    cur, _ = db
    p = _artist(cur, "Performer P")
    credited = _artist(cur, "Credited Stub")
    bare = _artist(cur, "Bare Stub")
    _credit(cur, _track(cur, "Track A", "Performer P", p), credited)
    stats = {"not_in_mb": 2, "inconclusive": 0}
    phantom._bind_chosen({}, [credited, bare], stats, dry_run=False, discard=True)
    assert stats["discarded"] == 1
    assert _artists(cur, credited, bare) == {credited}


def test_the_unsplit_gc_keeps_a_member_a_credit_names(db):
    from canon import migrations
    cur, factory = db
    whole = "Beth Hart & Joe Bonamassa"
    compound = _artist(cur, whole)
    cur.execute("UPDATE artists SET verification_status = 'verified_split' WHERE id = %s",
                (compound,))
    beth, joe = _artist(cur, "Beth Hart"), _artist(cur, "Joe Bonamassa")
    for member in (beth, joe):
        cur.execute("INSERT INTO artist_members (compound_artist_id, member_artist_id, role) "
                    "VALUES (%s, %s, 'featured')", (compound, member))
    _credit(cur, _track(cur, "Song", whole, compound), joe)
    with factory() as s:
        stats = migrations.unsplit_pass2_compounds(s)
    assert stats["members_removed"] == 1
    assert _artists(cur, beth, joe) == {joe}


def test_a_track_merge_carries_the_credits_to_the_survivor(db):
    from canon import identity
    cur, factory = db
    p = _artist(cur, "Performer P")
    c, k = _artist(cur, "Composer C"), _artist(cur, "Composer K")
    old = _track(cur, "Old Title", "Performer P", p)
    new = _track(cur, "New Title", "Performer P", p)
    _credit(cur, old, c, 0)
    _credit(cur, old, k, 1)
    _credit(cur, new, k, 0)
    with factory() as s:
        assert identity._update_track_uuid(s, old, new) == new
        s.commit()
    # K was already the survivor's: one row, at the survivor's own position;
    # C comes after the survivor's own credits
    assert _credits(cur) == {(new, k, "tag", 0), (new, c, "tag", 1)}


def test_a_track_renamed_takes_its_credits_by_cascade(db):
    from canon import identity
    cur, factory = db
    p = _artist(cur, "Performer P")
    c = _artist(cur, "Composer C")
    old = _track(cur, "Old Title", "Performer P", p)
    _credit(cur, old, c)
    new = str(uuid.uuid4())
    with factory() as s:
        identity._update_track_uuid(s, old, new)
        s.commit()
    assert _credits(cur) == {(new, c, "tag", 0)}


def test_an_artist_merge_repoints_the_credits(db):
    from canon import identity
    cur, factory = db
    p = _artist(cur, "Performer P")
    source = _artist(cur, "J. S. Bach")
    canonical = _artist(cur, "Johann Sebastian Bach")
    one = _track(cur, "One", "Performer P", p)
    two = _track(cur, "Two", "Performer P", p)
    _credit(cur, one, source, 0)
    _credit(cur, two, source, 1)
    _credit(cur, two, canonical, 0)      # the same credit under the canonical name
    _credit(cur, two, source, 2, source="mb")
    with factory() as s:
        assert identity.recanonicalize_artist(s, source, "Johann Sebastian Bach") == canonical
        s.commit()
    assert _artists(cur, source, canonical) == {canonical}
    assert _credits(cur) == {(one, canonical, "tag", 0), (two, canonical, "tag", 0),
                             (two, canonical, "mb", 2)}


def test_the_clean_pass_merge_repoints_the_credits(db):
    from canon import migrations
    cur, factory = db
    p = _artist(cur, "Performer P")
    dirty = _artist(cur, "Johann Sebastian Bach /", id_=str(uuid.uuid4()))
    clean = _artist(cur, "Johann Sebastian Bach")
    t = _track(cur, "One", "Performer P", p)
    _credit(cur, t, dirty)
    with factory() as s:
        # the pass leaves its commit to normalize_artists' later passes
        stats = migrations._clean_all_artist_names(s)
        s.commit()
    assert stats["merged"] == 1
    assert _artists(cur, dirty, clean) == {clean}
    assert _credits(cur) == {(t, clean, "tag", 0)}


def test_the_identity_renormalisation_carries_a_merged_artists_credits(db):
    from canon import migrations
    cur, factory = db
    p = _artist(cur, "Performer P")
    # Two rows one name folds to (an apostrophe is dropped): the one with an
    # owned file survives, the other merges into it.
    survivor = _artist(cur, "Don't Stop Composer", id_=str(uuid.uuid4()))
    merged = _artist(cur, "Dont Stop Composer", id_=str(uuid.uuid4()),
                     created_at=datetime(2020, 1, 1, tzinfo=timezone.utc))
    owned = _track(cur, "Owned", "Don't Stop Composer", survivor)
    _album_with_files(cur, "Owned Album", survivor, [owned])
    credited = _track(cur, "Credited", "Performer P", p)
    _credit(cur, credited, merged)
    with factory() as s:
        stats = migrations._renormalize_artists(s, dry_run=False)
    final = str(artist_uuid("Don't Stop Composer"))
    assert stats["merged"] == 1
    assert _artists(cur, survivor, merged, final) == {final}
    assert _credits(cur) == {(credited, final, "tag", 0)}


def test_the_renormalisation_keeps_the_credited_row_when_nothing_else_decides(db):
    from canon import migrations
    cur, factory = db
    p = _artist(cur, "Performer P")
    # Neither row has a file, an embedding, a listen, an MBID or a performing
    # credit; the older would win on age alone, the credited one wins first.
    _artist(cur, "Don't Stop Composer", id_=str(uuid.uuid4()),
            created_at=datetime(2020, 1, 1, tzinfo=timezone.utc))
    credited = _artist(cur, "Dont Stop Composer", id_=str(uuid.uuid4()))
    _credit(cur, _track(cur, "Credited", "Performer P", p), credited)
    with factory() as s:
        migrations._renormalize_artists(s, dry_run=False)
    cur.execute("SELECT name FROM artists WHERE id = %s", (str(artist_uuid("Dont Stop Composer")),))
    assert cur.fetchone() == ("Dont Stop Composer",)


def test_a_compound_split_carries_the_credits(db):
    from canon import split
    cur, factory = db
    whole = "Beth Hart feat. Joe Bonamassa"
    compound = _artist(cur, whole)
    c = _artist(cur, "Composer C")
    t = _track(cur, "Song", whole, compound)
    _credit(cur, t, c)
    with factory() as s:
        split.normalize_compound_artist(s, compound, whole, ["Beth Hart", "Joe Bonamassa"])
        s.commit()
    moved = str(track_uuid("Song", "Beth Hart"))
    assert _credits(cur) == {(moved, c, "tag", 0)}
    cur.execute("SELECT artist_id::text, role::text FROM track_artists WHERE track_id = %s",
                (moved,))
    assert set(cur.fetchall()) == {(str(artist_uuid("Beth Hart")), "primary"),
                                   (str(artist_uuid("Joe Bonamassa")), "featured")}
