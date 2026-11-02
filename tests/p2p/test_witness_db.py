"""Witness records against a real PostgreSQL built from the migrations: the
table's guards (a sighting never changes, its seal fills once, no row is ever
deleted), its wake, the 044 delta on a node that ran up to 043, and
witness.Recorder's rules — one row per (subject, certificate, channel), the
refusals, the per-channel ceilings and the memo. Skipped without a cluster."""

import logging
import select
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

psycopg2 = pytest.importorskip("psycopg2")
import psycopg2.errors  # noqa: E402
import psycopg2.pool  # noqa: E402

from desktop.p2p import birth_cert, identity_registry, witness  # noqa: E402
from tests.p2p.test_witness import AUTHORITY, ISSUER, certificate  # noqa: E402


def _key_hex() -> str:
    return Ed25519PrivateKey.generate().public_key().public_bytes_raw().hex()


OWN = _key_hex()


@pytest.fixture(autouse=True)
def trusted(monkeypatch):
    monkeypatch.setattr(birth_cert, "TRUSTED_AUTHORITIES", [ISSUER])
    monkeypatch.setattr(witness, "TRUSTED_AUTHORITIES", [ISSUER])


@pytest.fixture(scope="module")
def pool(scratch_dsn):
    p = psycopg2.pool.ThreadedConnectionPool(1, 4, scratch_dsn)

    @contextmanager
    def connect():
        conn = p.getconn()
        try:
            conn.autocommit = True
            yield conn
        finally:
            p.putconn(conn)
    yield connect
    p.closeall()


@pytest.fixture
def conn(pool):
    with pool() as c:
        yield c


@pytest.fixture
def recorder(pool):
    return witness.Recorder(pool, own_keys=lambda: frozenset({OWN}))


def _rows(conn, subject):
    with conn.cursor() as cur:
        cur.execute("SELECT channel::text, cert_sig, via, seen_at FROM p2p_witness "
                    "WHERE subject = %s ORDER BY id", (subject,))
        return cur.fetchall()


def _total(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM p2p_witness")
        return cur.fetchone()[0]


def _one(conn, subject, channel):
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM p2p_witness WHERE subject = %s AND channel = %s", (subject, channel))
        return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------

def test_a_sighting_is_recorded_once_with_its_certificate_as_signed(recorder, pool, conn):
    subject = _key_hex()
    cert = certificate(subject, method="email", email_token="ab" * 32, email_class="major",
                       email_domain_token="cd" * 32, predecessor=_key_hex())
    assert recorder.record(cert, "introduced", subject)
    assert recorder.record(cert, "introduced", subject)                     # the memo
    assert witness.Recorder(pool, own_keys=frozenset).record(cert, "introduced", subject)
    with conn.cursor() as cur:
        cur.execute("""SELECT subject, cert_payload, cert_sig, issuer, cert_v, issued_at, method::text,
                              difficulty, params_version, email_token, email_class::text,
                              email_domain_token, predecessor, channel::text, via, seen_at,
                              author_pubkey, signature, batch_root, witness_v
                         FROM p2p_witness WHERE subject = %s""", (subject,))
        rows = cur.fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row[:15] == (subject, birth_cert.canonical_payload(cert).decode(), cert["sig"], ISSUER, 4,
                        datetime(2026, 11, 1, 9, tzinfo=timezone.utc), "email", 32, 1, "ab" * 32,
                        "major", "cd" * 32, cert["predecessor"], "introduced", None)
    assert abs(row[15] - datetime.now(timezone.utc)) < timedelta(seconds=30)
    assert row[15].microsecond == 0
    assert row[16:] == (None, None, None, 1)
    assert witness.certificate_holds({"subject": row[0], "cert_payload": row[1], "cert_sig": row[2],
                                      "issuer": row[3]})


def test_a_recorded_sighting_never_changes_and_its_seal_fills_once(recorder, conn):
    subject = _key_hex()
    assert recorder.record(certificate(subject), "probe", subject)
    rid = _one(conn, subject, "probe")

    def update(sets: str, params=()):
        with conn.cursor() as cur:
            cur.execute(f"UPDATE p2p_witness SET {sets} WHERE id = %s", (*params, rid))

    for sets, params in (("subject = %s", ("ab" * 32,)), ("seen_at = seen_at - interval '1 day'", ()),
                         ("cert_payload = cert_payload || 'x'", ()), ("channel = 'introduced'", ()),
                         ("witness_v = 2", ())):
        with pytest.raises(psycopg2.errors.RaiseException, match="never changes"):
            update(sets, params)
    with pytest.raises(psycopg2.errors.CheckViolation):          # a stamp needs a signature first
        update("batch_root = %s, merkle_proof = '[]', worker_date = now(), worker_ip_hash = %s, "
               "worker_sig = %s, worker_authority = %s, timestamp_version = 2",
               ("aa" * 32, "00000000-0000-0000-0000-000000000001", "bb" * 64, ISSUER))
    update("author_pubkey = %s, signature = %s", (OWN, "cc" * 64))
    with pytest.raises(psycopg2.errors.RaiseException, match="only its seal fills, once"):
        update("signature = %s", ("dd" * 64,))
    with pytest.raises(psycopg2.errors.CheckViolation):          # a stamp is copied whole
        update("batch_root = %s", ("aa" * 32,))
    update("batch_root = %s, merkle_proof = '[]', worker_date = now(), worker_ip_hash = %s, "
           "worker_sig = %s, worker_authority = %s, timestamp_version = 2",
           ("aa" * 32, "00000000-0000-0000-0000-000000000001", "bb" * 64, ISSUER))
    update("channel = channel")                                  # no change is no change
    with pytest.raises(psycopg2.errors.RaiseException, match="never changes"):
        update("worker_date = worker_date + interval '1 second'")
    with pytest.raises(psycopg2.errors.RaiseException, match="never changes"):
        update("merkle_proof = '[[\"ab\", \"L\"]]'")


def test_no_witness_record_is_ever_deleted(recorder, conn):
    subject = _key_hex()
    assert recorder.record(certificate(subject), "probe", subject)
    before = _total(conn)
    with conn.cursor() as cur:
        with pytest.raises(psycopg2.errors.RaiseException, match=r"never deleted \(DELETE\)") as e:
            cur.execute("DELETE FROM p2p_witness WHERE subject = %s", (subject,))
        assert "Witness records" in e.value.diag.message_hint
        with pytest.raises(psycopg2.errors.RaiseException, match="never deleted"):
            cur.execute("DELETE FROM p2p_witness WHERE false")
        with pytest.raises(psycopg2.errors.RaiseException, match=r"never deleted \(TRUNCATE\)"):
            cur.execute("TRUNCATE p2p_witness")
    assert _total(conn) == before


def test_a_wake_comes_only_with_rows(recorder, pool, conn):
    subject = _key_hex()
    with pool() as listener:
        with listener.cursor() as cur:
            cur.execute("LISTEN sautium_witness")

        def heard() -> int:
            n = 0
            while select.select([listener], [], [], 0.5)[0]:
                listener.poll()
                n += len(listener.notifies)
                listener.notifies.clear()
            return n

        assert recorder.record(certificate(subject), "introduced", subject)
        assert heard() == 1
        rid = _one(conn, subject, "introduced")
        with conn.cursor() as cur:              # the same sighting again: no row, no wake
            cur.execute("""INSERT INTO p2p_witness (subject, cert_payload, cert_sig, issuer, cert_v,
                                                    issued_at, method, difficulty, params_version, channel)
                           SELECT subject, cert_payload, cert_sig, issuer, cert_v, issued_at, method,
                                  difficulty, params_version, channel
                             FROM p2p_witness WHERE id = %s
                           ON CONFLICT (subject, cert_sig, channel) DO NOTHING""", (rid,))
            assert cur.rowcount == 0
        assert heard() == 0
        with listener.cursor() as cur:
            cur.execute("UNLISTEN sautium_witness")


def test_the_delta_brings_a_node_at_043_to_the_table(scratch_dsn):
    from desktop import db_init
    conn = psycopg2.connect(scratch_dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM _schema_migrations WHERE filename = '044_p2p_witness.sql'")
            assert cur.fetchone() == (1,)                # a no-op after 001 on a fresh node
            # The node that ran everything up to 043: none of the witness objects.
            cur.execute("""DROP TABLE p2p_witness;
                           DROP TYPE p2p_witness_channel;
                           DROP FUNCTION notify_witness_recorded(), p2p_witness_guard_update(),
                                         p2p_witness_guard_delete();
                           DELETE FROM _schema_migrations WHERE filename = '044_p2p_witness.sql'""")
        conn.commit()
        assert db_init.apply_migrations(conn) == 1
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("""SELECT tgname FROM pg_trigger
                            WHERE tgrelid = 'p2p_witness'::regclass AND NOT tgisinternal ORDER BY 1""")
            assert [r[0] for r in cur.fetchall()] == [
                "trg_p2p_witness_guard_delete", "trg_p2p_witness_guard_truncate",
                "trg_p2p_witness_guard_update", "trg_p2p_witness_notify"]
            cur.execute("""SELECT conname FROM pg_constraint
                            WHERE conrelid = 'p2p_witness'::regclass AND contype = 'c' ORDER BY 1""")
            assert [r[0] for r in cur.fetchall()] == [
                "p2p_witness_hex", "p2p_witness_signed_whole", "p2p_witness_stamp_whole",
                "p2p_witness_stamped_signed", "p2p_witness_via_carried"]
        assert db_init.apply_migrations(conn) == 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# What is recorded, and what is not
# ---------------------------------------------------------------------------

def test_an_anomalous_certificate_is_a_row_of_its_own_while_the_registry_refuses_it(recorder, conn):
    subject = _key_hex()
    first = certificate(subject, issued_at="2026-11-01T09:00:00Z")
    other = certificate(subject, issued_at="2026-10-01T09:00:00Z")      # another anchor, same key
    assert recorder.record(first, "introduced", subject)
    assert recorder.record(other, "introduced", subject)
    assert sorted(r[1] for r in _rows(conn, subject)) == sorted([first["sig"], other["sig"]])
    assert identity_registry.observe(conn, first).get("anomaly") is not True
    assert identity_registry.observe(conn, other)["anomaly"] is True


def test_banned_and_own_keys_are_recorded_on_no_channel(recorder, conn):
    banned = _key_hex()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO p2p_node_bans (pubkey, reason) VALUES (%s, 'test')", (banned,))
    anyone = _key_hex()
    for channel in witness.CHANNELS:
        via = _key_hex() if channel == "carried" else None
        assert recorder.record(certificate(banned), channel, banned, via) is False
        assert recorder.record(certificate(OWN), channel, OWN, via) is False
        assert recorder.record(certificate(anyone), channel, anyone, via)
    assert _rows(conn, banned) == [] and _rows(conn, OWN) == []
    assert sorted(r[0] for r in _rows(conn, anyone)) == sorted(witness.CHANNELS)


def test_a_carried_sighting_names_its_deliverer(recorder, conn):
    subject, via = _key_hex(), _key_hex()
    assert recorder.record(certificate(subject), "carried", subject, via.upper())
    assert [(r[0], r[2]) for r in _rows(conn, subject)] == [("carried", via)]
    with conn.cursor() as cur, pytest.raises(psycopg2.errors.CheckViolation, match="via_carried"):
        cur.execute("""INSERT INTO p2p_witness (subject, cert_payload, cert_sig, issuer, cert_v, issued_at,
                                                method, difficulty, params_version, channel)
                       VALUES (%s, 'x', %s, %s, 4, now(), 'pow', 32, 1, 'carried')""",
                    (subject, "ab" * 64, ISSUER))


def test_a_probe_does_not_suppress_the_introduction_of_the_same_certificate(recorder, conn):
    subject = _key_hex()
    cert = certificate(subject)
    assert recorder.record(cert, "probe", subject)
    assert recorder.record(cert, "introduced", subject)
    assert sorted(r[0] for r in _rows(conn, subject)) == ["introduced", "probe"]


# ---------------------------------------------------------------------------
# The ceilings
# ---------------------------------------------------------------------------

def _counting_today(conn, channel) -> int:
    with conn.cursor() as cur:
        cur.execute(witness._TODAY_SQL, {"channel": channel, "since": datetime.combine(
            witness._utc_today(), datetime.min.time(), tzinfo=timezone.utc)})
        return cur.fetchone()[0]


def test_the_first_sighting_past_a_ceiling_is_refused_and_said(recorder, conn, monkeypatch, caplog):
    monkeypatch.setattr(witness, "CEILINGS", {"probe": _counting_today(conn, "probe") + 2})
    total = _total(conn)
    subjects = [_key_hex() for _ in range(4)]
    with caplog.at_level(logging.WARNING, logger="desktop.p2p.witness"):
        assert recorder.record(certificate(subjects[0]), "probe", subjects[0])
        assert recorder.record(certificate(subjects[1]), "probe", subjects[1])
        assert recorder.record(certificate(subjects[2]), "probe", subjects[2]) is False
        assert recorder.record(certificate(subjects[3]), "probe", subjects[3]) is False
    assert [r.getMessage() for r in caplog.records if "ceiling" in r.getMessage()] == [
        "p2p_witness: the probe ceiling (%d new rows a day) is met — further probe sightings "
        "today are not recorded; a later day records them" % witness.CEILINGS["probe"]]
    assert _rows(conn, subjects[2]) == [] and _rows(conn, subjects[3]) == []
    # Another channel is not this one's ceiling; a known sighting is no new row.
    assert recorder.record(certificate(subjects[2]), "introduced", subjects[2])
    assert recorder.record(certificate(subjects[0]), "probe", subjects[0])
    assert _total(conn) == total + 3


def test_a_re_sign_of_a_known_subject_does_not_count(recorder, conn, monkeypatch):
    known = _key_hex()
    with conn.cursor() as cur:             # recorded under the previous certificate format
        cur.execute("""INSERT INTO p2p_witness (subject, cert_payload, cert_sig, issuer, cert_v, issued_at,
                                                method, difficulty, params_version, channel)
                       VALUES (%s, 'sautium-birth:v3:x', %s, %s, 3, '2026-08-17T12:00:00Z', 'pow', 32, 1,
                               'probe')""", (known, "ee" * 64, ISSUER))
    full = _counting_today(conn, "probe")
    monkeypatch.setattr(witness, "CEILINGS", {"probe": full})
    stranger = _key_hex()
    assert recorder.record(certificate(stranger), "probe", stranger) is False
    assert recorder.record(certificate(known), "probe", known)                # exempt
    assert len(_rows(conn, known)) == 2
    assert _counting_today(conn, "probe") == full


def test_a_sighting_past_the_ceiling_is_recorded_once_the_day_turns(recorder, conn, monkeypatch, caplog):
    first = _key_hex()
    assert recorder.record(certificate(first), "introduced", first)
    monkeypatch.setattr(witness, "CEILINGS", {"introduced": _counting_today(conn, "introduced")})
    subject = _key_hex()
    assert recorder.record(certificate(subject), "introduced", subject) is False
    tomorrow = witness._utc_today() + timedelta(days=1)
    monkeypatch.setattr(witness, "_utc_today", lambda: tomorrow)
    with caplog.at_level(logging.WARNING, logger="desktop.p2p.witness"):
        assert recorder.record(certificate(subject), "introduced", subject)
    assert [r[0] for r in _rows(conn, subject)] == ["introduced"]
    assert any("1 introduced sighting(s) past the ceiling on" in r.getMessage() for r in caplog.records)


def test_one_certificate_is_one_row_however_its_signature_is_spelled(recorder, pool, conn):
    subject = _key_hex()
    cert = certificate(subject)
    assert birth_cert.verify_certificate(dict(cert, sig=cert["sig"].upper()))      # the parser takes these…
    spaced = " ".join(cert["sig"][i:i + 2] for i in range(0, 128, 2))
    assert birth_cert.verify_certificate(dict(cert, sig=spaced))
    for channel in witness.CHANNELS:
        via = _key_hex() if channel == "carried" else None
        assert recorder.record(cert, channel, subject, via)
        fresh = witness.Recorder(pool, own_keys=frozenset)                         # no memo to hide behind
        for sig in (cert["sig"].upper(), spaced, " " + cert["sig"], cert["sig"] + "\n"):
            assert fresh.record(dict(cert, sig=sig), channel, subject, via) is False  # …the record does not
    assert sorted(r[0] for r in _rows(conn, subject)) == sorted(witness.CHANNELS)
    with conn.cursor() as cur, pytest.raises(psycopg2.errors.CheckViolation, match="p2p_witness_hex"):
        cur.execute("""INSERT INTO p2p_witness (subject, cert_payload, cert_sig, issuer, cert_v, issued_at,
                                                method, difficulty, params_version, channel)
                       VALUES (%s, 'x', %s, %s, 4, now(), 'pow', 32, 1, 'probe')""",
                    (subject, cert["sig"].upper(), ISSUER))


def test_a_second_certificate_under_a_new_format_counts(recorder, conn, monkeypatch):
    """The re-sign exemption is for a known subject's certificate under a
    format newer than ANY recorded for it: a bump re-presents every subject
    once. A second certificate under that format (an upgrade, a new email)
    is an ordinary sighting and meets the ceiling."""
    subject = _key_hex()
    assert recorder.record(certificate(subject), "probe", subject)                  # known at v4
    monkeypatch.setattr(birth_cert, "CERT_VERSION", 5)
    monkeypatch.setattr(witness, "CEILINGS", {"probe": _counting_today(conn, "probe")})
    stranger = _key_hex()
    assert recorder.record(certificate(stranger), "probe", stranger) is False       # the day is full
    assert recorder.record(certificate(subject), "probe", subject)                  # its re-sign: exempt
    upgraded = certificate(subject, method="email", email_token="ab" * 32, email_class="major")
    assert recorder.record(upgraded, "probe", subject) is False                     # a v5 like the last
    assert len(_rows(conn, subject)) == 2


def test_a_format_bump_re_presents_each_known_subject_once_per_channel(recorder, conn, monkeypatch):
    """The bump-day wave: a subject known under the old format re-presented on
    each channel it is met on does not count; one known only under the new
    format is no re-sign anywhere."""
    old_timer, newcomer = _key_hex(), _key_hex()
    for channel in ("probe", "introduced"):
        assert recorder.record(certificate(old_timer), channel, old_timer)
    monkeypatch.setattr(birth_cert, "CERT_VERSION", 5)
    assert recorder.record(certificate(newcomer), "probe", newcomer)                # first met at v5
    monkeypatch.setattr(witness, "CEILINGS", {"probe": _counting_today(conn, "probe"),
                                              "introduced": _counting_today(conn, "introduced")})
    for channel in ("probe", "introduced"):
        assert recorder.record(certificate(old_timer), channel, old_timer)          # the wave: exempt
    assert recorder.record(certificate(newcomer), "introduced", newcomer) is False  # counts: the day is full
    assert sorted(r[0] for r in _rows(conn, old_timer)) == ["introduced", "introduced", "probe", "probe"]
