"""The life-data merge (backend/life_merge.py, BACKUP.md Product C).

The pure parts run anywhere: pg_restore's script split into COPY blocks, the
same-account rule, the rotation-archive union. The merge itself is the
acceptance case from the design — two clones with divergent histories merge
to the union in either order and `local_play_stats` recomputes to the same
numbers — and runs against a real PostgreSQL (the backend container's: two
throwaway databases built from the migrations), skipped where there is none.
"""

import io
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from desktop import node_backup as nb

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

life_merge = pytest.importorskip("life_merge")
psycopg2 = pytest.importorskip("psycopg2")

SCRIPT = (
    b"--\n-- PostgreSQL database dump\n--\n\n\\restrict abc\n\nSET client_encoding = 'UTF8';\n"
    b"SELECT pg_catalog.set_config('search_path', '', false);\n\n"
    b"--\n-- Data for Name: friend_rights; Type: TABLE DATA; Schema: public; Owner: musicai\n--\n\n"
    b"COPY public.friend_rights (friend_id, p2p_right) FROM stdin;\n"
    b"1\tcan_message\n2\tcan_search\n\\.\n\n\n"
    b'COPY public.session_tracks (session_id, "position", media_file_id, track_id, album_id) FROM stdin;\n'
    b"\\.\n\n"
    b"SELECT pg_catalog.setval('public.friends_id_seq', 2, true);\n\n"
    b"COPY public.user_settings (key, value, updated_at) FROM stdin;\n"
    b'a.b\t"x\\\\.y"\t2026-01-01 00:00:00+00\n\\.\n\n\\unrestrict abc\n'
)


def test_copy_blocks_split_the_script_and_drain_what_is_skipped():
    seen = []
    for table, columns, data in life_merge.copy_blocks(io.BytesIO(SCRIPT)):
        if table == "friend_rights":
            body = data.read()
            assert body == b"1\tcan_message\n2\tcan_search\n" and data.read() == b""
        seen.append((table, columns, data.rows if table == "friend_rights" else None))
    assert seen == [("friend_rights", ["friend_id", "p2p_right"], 2),
                    ("session_tracks", ["session_id", "position", "media_file_id", "track_id", "album_id"], None),
                    ("user_settings", ["key", "value", "updated_at"], None)]


def test_copy_blocks_accept_crlf_and_refuse_a_cut_block():
    crlf = SCRIPT.replace(b"\n", b"\r\n")
    rows = {t: d.read() for t, _c, d in life_merge.copy_blocks(io.BytesIO(crlf))}
    assert rows["friend_rights"] == b"1\tcan_message\n2\tcan_search\n"
    assert rows["user_settings"] == b'a.b\t"x\\\\.y"\t2026-01-01 00:00:00+00\n'
    cut = SCRIPT[:SCRIPT.index(b"2\tcan_search")]
    with pytest.raises(nb.BackupError, match="inside a COPY block"):
        for _t, _c, data in life_merge.copy_blocks(io.BytesIO(cut)):
            data.read()


def test_same_account_rule():
    own, old, other = "ab" * 32, "cd" * 32, "ef" * 32
    life_merge.check_same_account({"node": {"pubkey": own.upper()}}, pubkey=own, previous=[])
    life_merge.check_same_account({"node": {"pubkey": old}}, pubkey=own, previous=[old])
    with pytest.raises(nb.Refused, match="another identity"):
        life_merge.check_same_account({"node": {"pubkey": other, "username": "x"}}, pubkey=own, previous=[old])
    with pytest.raises(nb.Refused):
        life_merge.check_same_account({"node": {}}, pubkey=own, previous=[])


def test_rotation_archive_union(tmp_path):
    live = {"public_key_hex": "ab" * 32, "username": "vale", "previous": [
        {"public_key_hex": "11" * 32, "dir": "d1", "retired_at": "2026-02-01T00:00:00Z"}]}
    (tmp_path / "node_info.json").write_text(json.dumps(live))
    theirs = {"public_key_hex": "ab" * 32, "previous": [
        {"public_key_hex": "11" * 32, "dir": "d1", "retired_at": "2026-02-01T00:00:00Z"},
        {"public_key_hex": "00" * 32, "dir": "d0", "retired_at": "2026-01-01T00:00:00Z"}]}
    files = {"node_info.json": json.dumps(theirs).encode(),
             "previous/d0/rotation.json": b'{"v":1}', "previous/d0/birth_certificate.json": b"{}",
             "previous/d1/rotation.json": b"stale"}
    assert life_merge.merge_identity(tmp_path, files) == 1
    info = json.loads((tmp_path / "node_info.json").read_text())
    assert [e["dir"] for e in info["previous"]] == ["d0", "d1"]          # oldest first
    assert (tmp_path / "previous" / "d0" / "rotation.json").read_bytes() == b'{"v":1}'
    assert not (tmp_path / "previous" / "d1").exists()                   # known: nothing written
    assert life_merge.merge_identity(tmp_path, files) == 0
    assert life_merge.merge_identity(None, files) == 0
    assert life_merge.merge_identity(tmp_path / "nowhere", files) == 0


# ---------------------------------------------------------------------------
# The acceptance case, on a real cluster
# ---------------------------------------------------------------------------

PG = dict(host=os.environ.get("SAUTIUM_TEST_PGHOST", "postgres"),
          port=int(os.environ.get("SAUTIUM_TEST_PGPORT", "5432")),
          user=os.environ.get("SAUTIUM_TEST_PGUSER", "musicai"),
          password=os.environ.get("SAUTIUM_TEST_PGPASSWORD", "supervisor"))
KDF = nb.KdfParams(time_cost=1, memory_cost=8 * 1024, parallelism=1)
PW, USER, PUB = "merge me", "vale", "ab" * 32
T0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
T1, T2, T3, T4 = (uuid.UUID(int=n) for n in (1, 2, 3, 4))
S1, S2, S3, S4 = (uuid.UUID(int=n) for n in (11, 12, 13, 14))
F1, F2, F3 = "11" * 32, "22" * 32, "33" * 32
TK1, GB1, GM1 = uuid.UUID(int=21), uuid.UUID(int=31), uuid.UUID(int=32)
M0, M1, M2 = (uuid.UUID(int=n) for n in (41, 42, 43))


def _target(dbname: str) -> nb.PgTarget:
    return nb.PgTarget(dbname=dbname, pg_bin=Path(os.environ["PG_BIN"]) if os.environ.get("PG_BIN") else None,
                       **PG)


@pytest.fixture(scope="module")
def clones():
    """Two databases built from the migrations — 'a' and 'b' — dropped after."""
    try:
        admin = psycopg2.connect(dbname="postgres", **PG)
    except psycopg2.OperationalError as e:
        pytest.skip(f"no PostgreSQL for the merge test: {e}")
    admin.autocommit = True
    from desktop import db_init
    names = {"a": "sautium_merge_test_a", "b": "sautium_merge_test_b"}
    targets = {}
    for key, name in names.items():
        nb._drop_database(admin, name)
        with admin.cursor() as cur:
            cur.execute(f"CREATE DATABASE {name}")
        conn = psycopg2.connect(dbname=name, **PG)
        db_init.apply_migrations(conn)
        conn.commit()
        conn.close()
        targets[key] = _target(name)
    yield targets
    for name in names.values():
        nb._drop_database(admin, name)
    admin.close()


def _exec(target, statements):
    conn = target.connect()
    try:
        with conn.cursor() as cur:
            for stmt, params in statements:
                cur.execute(stmt, params)
        conn.commit()
    finally:
        conn.close()


def _rows(target, query, params=None):
    conn = target.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchall()
    finally:
        conn.close()


def _listen(track, at, *, completed, dur, pct):
    return ("INSERT INTO listening_history (track_id, started_at, ended_at, duration_listened, "
            "percent_listened, completed, skipped) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (str(track), at, at + timedelta(seconds=dur), dur, pct, completed, not completed))


def _demo(track, at, provider="youtube"):
    return ("INSERT INTO demo_plays (track_id, provider, played_at) VALUES (%s, %s, %s)",
            (str(track), provider, at))


def _session(sid, *, ended, tracks, origin="album", title="X"):
    out = [("INSERT INTO listening_sessions (id, origin, title, track_count, started_at, ended_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (str(sid), origin, title, len(tracks), T0, T0 + timedelta(hours=1) if ended else None))]
    for pos, track in enumerate(tracks):
        out.append(("INSERT INTO session_tracks (session_id, position, track_id) VALUES (%s, %s, %s)",
                    (str(sid), pos, str(track))))
    return out


def _friend(pub, code, *, blocked=False, token=None):
    return ("INSERT INTO friends (username, public_key_hex, invite_code, is_blocked, source_token_id) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id", (code, pub, code, blocked, str(token) if token else None))


def _seed(clones):
    a, b = clones["a"], clones["b"]
    tracks = lambda ids: [("INSERT INTO tracks (id, title) VALUES (%s, %s)", (str(t), f"t{t.int}")) for t in ids]
    stats = ("INSERT INTO local_play_stats (track_id) SELECT DISTINCT track_id FROM listening_history "
             "ON CONFLICT DO NOTHING", None)
    from play_stats import PLAY_STATS_SQL

    def refresh(target):
        ids = [r[0] for r in _rows(target, "SELECT DISTINCT track_id::text FROM listening_history")]
        _exec(target, [(PLAY_STATS_SQL, {"ids": ids})])

    _exec(a, tracks([T1, T2, T3]) + [
        _listen(T1, T0, completed=True, dur=200, pct=100),
        _listen(T2, T0 + timedelta(minutes=5), completed=False, dur=10, pct=5),
        _listen(T1, T0 + timedelta(hours=2), completed=True, dur=180, pct=90),
        _demo(T1, T0 + timedelta(hours=2)),
    ] + _session(S4, ended=True, tracks=[T2]) + [
        _friend(F1, "inv1"), _friend(F2, "inv2", blocked=True),
        ("INSERT INTO friend_rights (friend_id, p2p_right) SELECT id, 'can_message' FROM friends "
         "WHERE public_key_hex = %s", (F1,)),
        ("INSERT INTO p2p_messages (friend_id, direction, content, timestamp, message_uuid) "
         "SELECT id, 'in', 'hey', %s, %s FROM friends WHERE public_key_hex = %s", (T0, str(M0), F1)),
        ("INSERT INTO user_settings (key, value) VALUES ('sync.carry_limit', '100'), "
         "('discovery.phantom_layer', 'true')", None),
    ])
    _exec(b, tracks([T1, T2, T3, T4]) + [
        _listen(T2, T0 + timedelta(minutes=5), completed=False, dur=10, pct=5),
        _listen(T3, T0 + timedelta(hours=1), completed=True, dur=300, pct=100),
        _listen(T4, T0 + timedelta(hours=1, minutes=10), completed=True, dur=100, pct=100),
        _listen(T1, T0 + timedelta(hours=3), completed=False, dur=20, pct=10),
        _demo(T1, T0 + timedelta(hours=1)), _demo(T3, T0 + timedelta(hours=1)), _demo(T4, T0),
    ] + _session(S1, ended=True, tracks=[T1, T3]) + _session(S2, ended=False, tracks=[T2], origin="mix")
      + _session(S3, ended=True, tracks=[T4]) + [
        ("INSERT INTO invite_tokens (id, label, max_uses, use_count, created_at) VALUES (%s, 'friends', 5, 1, %s)",
         (str(TK1), T0)),
        ("INSERT INTO sent_invites (to_email, sent_at) VALUES ('a@b.c', %s)", (T0,)),
        _friend(F1, "inv1"), _friend(F2, "inv2"), _friend(F3, "inv3", token=TK1),
        ("INSERT INTO friend_rights (friend_id, p2p_right) SELECT id, r FROM friends, "
         "unnest(ARRAY['can_message', 'can_search']::p2p_right[]) r WHERE public_key_hex = %s", (F1,)),
        ("INSERT INTO p2p_messages (friend_id, direction, content, timestamp, message_uuid) "
         "SELECT id, 'in', 'hey', %s, %s FROM friends WHERE public_key_hex = %s", (T0, str(M0), F1)),
        ("INSERT INTO p2p_messages (friend_id, direction, content, timestamp, message_uuid) "
         "SELECT id, 'in', 'hi', %s, %s FROM friends WHERE public_key_hex = %s", (T0, str(M1), F3)),
        ("INSERT INTO p2p_messages (friend_id, direction, content, timestamp, message_uuid) "
         "SELECT id, 'out', 'hello', %s, %s FROM friends WHERE public_key_hex = %s",
         (T0 + timedelta(minutes=1), str(M2), F3)),
        ("INSERT INTO chat_sessions (title, created_at, claude_session_id) VALUES ('chat', %s, 'sess-on-b') "
         "RETURNING id", (T0,)),
        ("INSERT INTO chat_messages (session_id, role, content, created_at) "
         "SELECT id, 'user', 'hi', %s FROM chat_sessions", (T0,)),
        ("INSERT INTO chat_messages (session_id, role, content, created_at) "
         "SELECT id, 'assistant', 'hello', %s FROM chat_sessions", (T0 + timedelta(seconds=5),)),
        ("INSERT INTO gear_brands (id, name) VALUES (%s, 'Meze')", (str(GB1),)),
        ("INSERT INTO gear_models (id, brand_id, model, category) VALUES (%s, %s, 'Elite', 'headphones')",
         (str(GM1), str(GB1))),
        ("INSERT INTO user_gear (gear_model_id, notes) VALUES (%s, 'daily')", (str(GM1),)),
        ("UPDATE user_profile SET display_name = 'Vale', city = 'Kyiv' WHERE id = 1", None),
        ("INSERT INTO p2p_identities (pubkey, cert_v, method, issued_at, difficulty, params_version, "
         "issuer, cert_sig, status, first_seen_at, last_seen_at, contacts) "
         "VALUES (%s, 1, 'pow', %s, 10, 1, 'worker', 'sig', 'verified', %s, %s, 3)",
         ("44" * 32, T0, T0, T0)),
        ("INSERT INTO p2p_node_bans (pubkey, reason) VALUES (%s, 'spam')", ("55" * 32,)),
        ("INSERT INTO user_settings (key, value) VALUES ('discovery.phantom_layer', 'false'), "
         "('hqplayer.host', '\"x\"'), ('sync.last_at', '\"2026-09-01\"')", None),
    ])
    for t in (a, b):
        _exec(t, [stats])
        refresh(t)


def _backup(target, out_dir: Path) -> Path:
    kek = nb.derive_kek(PW, USER, KDF)
    result = nb.create_backup(out_dir, target=target, kek=kek, username=USER, pubkey=PUB, kdf=KDF)
    return result["path"]


def _merge(path: Path, target, **kw) -> dict:
    return life_merge.merge_backup(path, PW, target=target, pubkey=PUB, previous=[], kdf=KDF, **kw)


def _stats(target):
    return {r[0]: r[1:] for r in _rows(
        target, "SELECT track_id::text, play_count, skip_count, total_listen_time, avg_percent_listened, "
                "last_played_at FROM local_play_stats ORDER BY 1")}


def _friends(target):
    return {r[0]: r[1:] for r in _rows(target, """
        SELECT f.public_key_hex, f.is_blocked, f.source_token_id::text,
               (SELECT array_agg(p2p_right::text ORDER BY p2p_right) FROM friend_rights r WHERE r.friend_id = f.id),
               (SELECT array_agg(message_uuid::text ORDER BY message_uuid) FROM p2p_messages m WHERE m.friend_id = f.id)
          FROM friends f ORDER BY 1""")}


def _settings(target):
    return {r[0]: r[1] for r in _rows(target, "SELECT key, value FROM user_settings")}


def test_two_clones_merge_to_the_union_in_either_order(clones, tmp_path):
    a, b = clones["a"], clones["b"]
    _seed(clones)
    file_a, file_b = _backup(a, tmp_path / "a"), _backup(b, tmp_path / "b")

    # Not this account: refused before anything is read into the database.
    with pytest.raises(nb.Refused, match="another identity"):
        life_merge.merge_backup(file_b, PW, target=a, pubkey="99" * 32, previous=[], kdf=KDF)
    with pytest.raises(nb.WrongPassword):
        life_merge.merge_backup(file_b, "nope", target=a, pubkey=PUB, previous=[], kdf=KDF)

    # A dry run reports the merge and leaves the database as it was.
    plan = _merge(file_b, a, dry_run=True)
    assert plan["dry_run"] and _rows(a, "SELECT count(*) FROM listening_history")[0][0] == 3
    assert _rows(a, "SELECT count(*) FROM pg_namespace WHERE nspname = %s", (life_merge.SCRATCH,))[0][0] == 0

    events = []
    got = _merge(file_b, a, progress=lambda phase, **f: events.append(phase))
    assert {e for e in events} >= {"unlocking", "reading", "loading", "merging"}
    assert got["merged"] == plan["merged"]
    m = got["merged"]
    assert (m["listens"], m["sessions"], m["friends"], m["rights"], m["messages"]) == (2, 1, 1, 1, 2)
    assert (m["chats"], m["chat_messages"], m["gear"], m["identities"], m["bans"]) == (1, 2, 1, 1, 1)
    assert (m["tokens"], m["invites"], m["settings"], m["profile"], m["rotations"]) == (1, 1, 0, 1, 0)
    assert m["demo_plays"] == 2                          # T3 new, T1 spent earlier on B
    assert got["waiting"] == {"listens": 1, "tracks": 1, "sessions": 1, "demo_plays": 1}  # T4 is unknown to A

    got_b = _merge(file_a, b)
    mb = got_b["merged"]
    assert (mb["listens"], mb["sessions"], mb["friends"], mb["rights"], mb["messages"]) == (2, 1, 0, 0, 0)
    assert (mb["settings"], mb["profile"]) == (1, 0)                          # carry_limit; profile was set
    assert mb["demo_plays"] == 0                         # A's T1 demo is later: B keeps its own
    assert got_b["waiting"] == {"listens": 0, "tracks": 0, "sessions": 0, "demo_plays": 0}

    # The union, on what both know; stats recomputed to the same numbers.
    hist = lambda t: set(_rows(t, "SELECT track_id::text, started_at, completed FROM listening_history"))
    both = {str(t) for t in (T1, T2, T3)}
    assert {h for h in hist(a)} == {h for h in hist(b) if h[0] in both}
    assert len(hist(a)) == 5 and len(hist(b)) == 6
    sa, sb = _stats(a), _stats(b)
    assert set(sa) == both and set(sb) == both | {str(T4)}
    assert all(sa[t] == sb[t] for t in both)
    assert sa[str(T1)][:2] == (2, 1) and sa[str(T1)][2] == 380 and sa[str(T2)][:2] == (0, 1)
    assert sa[str(T1)][4] == T0 + timedelta(hours=2, seconds=180)             # last COMPLETED listen
    demos = lambda t: dict(_rows(t, "SELECT track_id::text, played_at FROM demo_plays"))
    assert demos(a) == {str(T1): T0 + timedelta(hours=1), str(T3): T0 + timedelta(hours=1)}
    assert demos(b) == {**demos(a), str(T4): T0}

    # Sessions: the closed, fully known one came over; the open one and the
    # one naming T4 did not; the other way every session of A is in B.
    sess = lambda t: {r[0] for r in _rows(t, "SELECT id::text FROM listening_sessions")}
    assert sess(a) == {str(S4), str(S1)} and sess(b) == {str(S1), str(S2), str(S3), str(S4)}
    assert _rows(a, "SELECT array_agg(track_id::text ORDER BY position) FROM session_tracks WHERE session_id = %s",
                 (str(S1),))[0][0] == [str(T1), str(T3)]

    # Friends: rights unioned, blocked on either side, F3 with its token and messages, M0 once.
    fa, fb = _friends(a), _friends(b)
    assert fa == fb
    assert fa[F1] == (False, None, ["can_message", "can_search"], [str(M0)])
    assert fa[F2][0] is True
    assert fa[F3] == (False, str(TK1), None, sorted([str(M1), str(M2)]))
    assert _rows(a, "SELECT use_count FROM invite_tokens WHERE id = %s", (str(TK1),))[0][0] == 1

    # Chats: copied without the other machine's agent session id.
    assert _rows(a, "SELECT title, claude_session_id, (SELECT count(*) FROM chat_messages) FROM chat_sessions") \
        == [("chat", None, 2)]
    assert _rows(a, "SELECT g.notes, b.name FROM user_gear g JOIN gear_models m ON m.id = g.gear_model_id "
                    "JOIN gear_brands b ON b.id = m.brand_id") == [("daily", "Meze")]
    assert _rows(a, "SELECT display_name, city FROM user_profile") == [("Vale", "Kyiv")]
    assert _rows(a, "SELECT count(*) FROM p2p_identities")[0][0] == 1
    assert _rows(a, "SELECT reason FROM p2p_node_bans") == [("spam",)]

    # Settings: the allowlist only, and a key already set here wins.
    assert _settings(a) == {"sync.carry_limit": 100, "discovery.phantom_layer": True}
    assert _settings(b) == {"sync.carry_limit": 100, "discovery.phantom_layer": False,
                            "hqplayer.host": "x", "sync.last_at": "2026-09-01"}

    # A second merge of the same file changes nothing.
    again = _merge(file_b, a)
    assert not any(again["merged"].values()), again["merged"]
    assert again["waiting"] == got["waiting"]
    assert _stats(a) == sa and _friends(a) == fa and len(hist(a)) == 5 and len(demos(a)) == 2
