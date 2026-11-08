"""The Buy button of a phantom album on a node with catalogue slices and no
MusicBrainz database opens a page only MusicBrainz lists for that album
(backend/routers/albums._buy_link): the slice proposes the release's shop
page or the credited artist's shop; MusicBrainz's answer for that release
must carry the page and file the release under the album's group, or its
answer for that artist must carry the shop and its credit of the group name
the artist; anything else falls back to the search. On a real PostgreSQL
built from the migrations, MusicBrainz faked — skipped without a cluster."""

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

A = "7c3f0000-0000-4000-8000-000000000001"
RG = "7c3f0000-0000-4000-8000-000000000011"
REL = "7c3f0000-0000-4000-8000-000000000021"
REC = "7c3f0000-0000-4000-8000-000000000031"
ALBUM_PAGE = "http://hiddenorchestra.bandcamp.com/album/night-walks"
SHOP = "https://hiddenorchestra.bandcamp.com/"
# Someone else's real entities, which a slice can name for this album.
OTHER = "7c3f0000-0000-4000-8000-000000000002"
OTHER_RG = "7c3f0000-0000-4000-8000-000000000012"
OTHER_REL = "7c3f0000-0000-4000-8000-000000000022"
OTHER_REC = "7c3f0000-0000-4000-8000-000000000032"
OTHER_PAGE = "https://someone-else.bandcamp.com/album/night-walks"
OTHER_SHOP = "https://someone-else.bandcamp.com/"
OLD_RG = "7c3f0000-0000-4000-8000-000000000013"


@pytest.fixture
def node(scratch_dsn, monkeypatch):
    conn, pool, engine = mb_fakes.slice_fed(scratch_dsn, monkeypatch)
    with conn.cursor() as cur:
        cur.execute("TRUNCATE mb_url, mb_l_release_url, mb_l_artist_url")
        s = Slice(cur)
        a = s.artist(A, "Hidden Orchestra")
        c = s.credit((a, "Hidden Orchestra"))
        g = s.release_group(RG, "Night Walks", c)
        rel = s.release(REL, "Night Walks", g, c, [("Antiphon", 300000, REC)])
        cur.execute("INSERT INTO mb_url (id, gid, url) VALUES (1, %s, %s), (2, %s, %s)",
                    (str(uuid.uuid4()), ALBUM_PAGE, str(uuid.uuid4()), SHOP))
        cur.execute("INSERT INTO mb_l_release_url (id, link, entity0, entity1) VALUES (1, 1, %s, 1)",
                    (rel,))
        cur.execute("INSERT INTO mb_l_artist_url (id, link, entity0, entity1) VALUES (2, 1, %s, 2)",
                    (a,))
    fake = FakeMusicBrainz().install(monkeypatch)
    import mb_answers

    def ask_now(items, priority=mb_answers.PRIORITY_PERSON, timeout=2.5):
        items = list(items)
        mb_answers.enqueue(items, priority)
        drain()
        return all((x := mb_answers.answer(*i)) is not None and x.fresh for i in items)
    monkeypatch.setattr(mb_answers, "ask_now", ask_now)
    from routers import albums
    yield albums, fake
    conn.close()
    pool.closeall()
    engine.dispose()


def _musicbrainz(fake, *, release_urls=(), artist_urls=()):
    fake.artist(A, "Hidden Orchestra", urls=artist_urls)
    fake.release_group(RG, "Night Walks", credit(("Hidden Orchestra", A)))
    fake.release(REL, "Night Walks", RG, credit(("Hidden Orchestra", A)),
                 [{"title": "Antiphon", "length": 300000, "recording": REC}], urls=release_urls)


def test_buy_opens_the_album_page_only_when_musicbrainz_lists_it_for_the_release(node):
    albums, fake = node
    _musicbrainz(fake, release_urls=["https://hiddenorchestra.bandcamp.com/album/night-walks"])
    assert albums._buy_link(RG, "Hidden Orchestra") == {
        "state": "album", "url": "https://hiddenorchestra.bandcamp.com/album/night-walks"}
    assert ("release", REL) in fake.asked


def test_buy_opens_the_shop_only_when_musicbrainz_lists_it_for_the_credited_artist(node):
    albums, fake = node
    _musicbrainz(fake, artist_urls=[SHOP])
    assert albums._buy_link(RG, "Hidden Orchestra") == {
        "state": "artist", "url": "https://hiddenorchestra.bandcamp.com/music"}


def test_buy_falls_back_to_the_search_when_musicbrainz_lists_neither_page(node):
    albums, fake = node
    _musicbrainz(fake, release_urls=["https://someone-else.bandcamp.com/album/night-walks"],
                 artist_urls=["https://someone-else.bandcamp.com/"])
    assert albums._buy_link(RG, "Hidden Orchestra") == {"state": "unknown", "url": None}


def test_a_page_musicbrainz_lists_for_a_release_of_another_group_opens_nothing(node):
    albums, fake = node
    _musicbrainz(fake)
    fake.artist(OTHER, "Someone Else")
    fake.release_group(OTHER_RG, "Other Walks", credit(("Someone Else", OTHER)))
    fake.release(OTHER_REL, "Night Walks", OTHER_RG, credit(("Someone Else", OTHER)),
                 [{"title": "Antiphon", "length": 300000, "recording": OTHER_REC}],
                 urls=[OTHER_PAGE])
    from db_pool import transaction
    with transaction() as cur:
        # The slice files that real release under this album's group, dated
        # first so its page is the one picked.
        cur.execute("SELECT id, artist_credit FROM mb_release_group WHERE gid = %s", (RG,))
        group, credit_id = cur.fetchone()
        rel = Slice(cur).release(OTHER_REL, "Night Walks", group, credit_id,
                                 [("Antiphon", 300000, OTHER_REC)])
        cur.execute("INSERT INTO mb_release_country (release, country, date_year) VALUES (%s, 1, 1999)",
                    (rel,))
        cur.execute("INSERT INTO mb_url (id, gid, url) VALUES (3, %s, %s)", (str(uuid.uuid4()), OTHER_PAGE))
        cur.execute("INSERT INTO mb_l_release_url (id, link, entity0, entity1) VALUES (3, 1, %s, 3)",
                    (rel,))

    assert albums._buy_link(RG, "Hidden Orchestra") == {"state": "unknown", "url": None}
    assert ("release", OTHER_REL) in fake.asked and ("release_group", RG) in fake.asked


def test_a_shop_of_an_artist_only_the_slice_credits_on_the_group_opens_nothing(node):
    albums, fake = node
    _musicbrainz(fake)
    fake.artist(OTHER, "Someone Else", urls=[OTHER_SHOP])
    from db_pool import transaction
    with transaction() as cur:
        # The slice puts a real artist at the head of the group's credit.
        s = Slice(cur)
        other = s.artist(OTHER, "Someone Else")
        cur.execute("SELECT id FROM mb_artist WHERE gid = %s", (A,))
        credit_id = s.credit((other, "Someone Else"), (cur.fetchone()[0], "Hidden Orchestra"))
        cur.execute("UPDATE mb_release_group SET artist_credit = %s WHERE gid = %s", (credit_id, RG))
        cur.execute("INSERT INTO mb_url (id, gid, url) VALUES (3, %s, %s)", (str(uuid.uuid4()), OTHER_SHOP))
        cur.execute("INSERT INTO mb_l_artist_url (id, link, entity0, entity1) VALUES (3, 1, %s, 3)",
                    (other,))

    assert albums._buy_link(RG, "Hidden Orchestra") == {"state": "unknown", "url": None}
    # The artist asked; the group's credit came with the release's own answer
    # (or a lookup of its own) — MusicBrainz's, not the slice's.
    import mb_answers
    assert ("artist", OTHER) in fake.asked
    assert mb_answers.answer("release_group", RG).status == "found"


def test_a_release_filed_under_the_group_this_one_was_merged_into_opens_its_page(node):
    albums, fake = node
    _musicbrainz(fake, release_urls=["https://hiddenorchestra.bandcamp.com/album/night-walks"])
    fake.merge("release_group", OLD_RG, RG)
    from db_pool import transaction
    with transaction() as cur:
        # The slice still knows the group under the MBID MusicBrainz merged.
        cur.execute("UPDATE mb_release_group SET gid = %s WHERE gid = %s", (OLD_RG, RG))

    assert albums._buy_link(OLD_RG, "Hidden Orchestra") == {
        "state": "album", "url": "https://hiddenorchestra.bandcamp.com/album/night-walks"}
