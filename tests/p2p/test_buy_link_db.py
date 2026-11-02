"""The Buy button of a phantom album opens a Bandcamp shop's own page or
nothing (backend/routers/albums._buy_link), on a real PostgreSQL built from
the migrations — skipped without a cluster: a url row that is no shop page
is never the link, whatever slice brought it, and a shop page is opened
over https."""

import sys
import uuid
from pathlib import Path
from urllib.parse import urlparse

import psycopg2
import pytest

REPO = Path(__file__).resolve().parents[2]
for p in (REPO, REPO / "backend"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

RG = str(uuid.uuid4())


@pytest.fixture
def albums(scratch_dsn, monkeypatch):
    from config import settings
    import db_pool
    url = urlparse(scratch_dsn)
    for key, value in dict(postgres_host=url.hostname, postgres_port=url.port,
                           postgres_user=url.username, postgres_password=url.password,
                           postgres_db=url.path.lstrip("/")).items():
        monkeypatch.setattr(settings, key, value)
    db_pool.close_pool()
    c = psycopg2.connect(scratch_dsn)
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute("TRUNCATE mb_release_group, mb_release, mb_l_release_url, mb_url, mb_artist_credit_name")
        cur.execute("INSERT INTO mb_release_group (id, gid, name, artist_credit) VALUES (1, %s, 'LP', 1)", (RG,))
        cur.execute("""INSERT INTO mb_release (id, gid, name, artist_credit, release_group)
                       VALUES (1, %s, 'LP', 1, 1), (2, %s, 'LP', 1, 1)""",
                    (str(uuid.uuid4()), str(uuid.uuid4())))
    from routers import albums as module
    yield module, c
    c.close()
    db_pool.close_pool()


def _page(c, release, url_id, url):
    with c.cursor() as cur:
        cur.execute("INSERT INTO mb_url (id, gid, url) VALUES (%s, %s, %s)", (url_id, str(uuid.uuid4()), url))
        cur.execute("""INSERT INTO mb_l_release_url (id, link, entity0, entity1)
                       VALUES (%s, 1, %s, %s)""", (url_id, release, url_id))


def test_only_a_shop_page_is_ever_the_buy_link(albums):
    module, c = albums
    _page(c, 1, 10, "https://elsewhere.example/album/lp")
    assert module._buy_link(RG, "Artist")["url"] is None
    _page(c, 2, 11, "http://artist.bandcamp.com/album/lp")
    assert module._buy_link(RG, "Artist") == {"state": "album",
                                              "url": "https://artist.bandcamp.com/album/lp"}
