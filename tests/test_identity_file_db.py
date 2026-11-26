"""The database's word before an identity file is placed
(node_identity.another_identity_at — the launcher's and the Docker command's
question, desktop/identity_file's caller's duty): another identity of record,
or of the node's own seals, asks the owner; the same identity, or a database
that names none, does not. On a real PostgreSQL built from the migrations;
skipped without a cluster."""

import json

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from desktop import node_identity  # noqa: E402

MINE, THEIRS = "ab" * 32, "cd" * 32


@pytest.fixture
def conn(scratch_dsn):
    c = psycopg2.connect(scratch_dsn)
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute("TRUNCATE tracks, albums CASCADE")
        cur.execute("DELETE FROM user_settings WHERE key IN ('p2p.bound_identity', 'identity.signing_key')")
    yield c
    c.close()


def test_a_database_that_names_no_identity_asks_nothing(conn, scratch_dsn):
    assert node_identity.another_identity_at(scratch_dsn, MINE) is False


def test_the_signing_key_of_record_is_the_identity_it_names(conn, scratch_dsn):
    with conn.cursor() as cur:
        cur.execute(node_identity.RECORD_SIGNING_KEY_SQL, {"key": json.dumps(THEIRS)})
    assert node_identity.another_identity_at(scratch_dsn, MINE) is True
    assert node_identity.another_identity_at(scratch_dsn, THEIRS) is False


def test_the_last_p2p_run_s_key_names_it_where_none_is_on_record(conn, scratch_dsn):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO user_settings (key, value) VALUES ('p2p.bound_identity', %s)",
                    (json.dumps(THEIRS),))
    assert node_identity.another_identity_at(scratch_dsn, MINE) is True
    assert node_identity.another_identity_at(scratch_dsn, THEIRS) is False


def test_the_node_s_own_seals_name_their_author(conn, scratch_dsn):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO albums (id, title, author_pubkey, signature, imported) "
                    "VALUES (gen_random_uuid(), 'a', %s, %s, false)", (THEIRS, "5e" * 64))
    assert node_identity.another_identity_at(scratch_dsn, MINE) is True
    assert node_identity.another_identity_at(scratch_dsn, THEIRS) is False
