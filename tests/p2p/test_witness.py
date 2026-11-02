"""Witness records (desktop/p2p/witness.py) — the parts that need no
database: the statement's v1 format, its signature, the evidence check on a
stored certificate, and the refusals record() makes before it touches one.
The database half is test_witness_db.py."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from desktop.p2p import birth_cert, record_sig, witness

AUTHORITY = Ed25519PrivateKey.from_private_bytes(b"\x31" * 32)
ISSUER = AUTHORITY.public_key().public_bytes_raw().hex()


def _pub(key) -> str:
    return key.public_key().public_bytes_raw().hex()


def certificate(subject: str, issued_at="2026-11-01T09:00:00Z", **over) -> dict:
    cert = {"v": birth_cert.CERT_VERSION, "pubkey": subject, "issued_at": issued_at,
            "method": "pow", "difficulty": 32, "params_version": 1, "email_token": None,
            "email_class": None, "email_domain_token": None, "predecessor": None,
            "issuer": ISSUER}
    cert.update(over)
    cert["sig"] = AUTHORITY.sign(birth_cert.canonical_payload(cert)).hex()
    return cert


@pytest.fixture(autouse=True)
def trusted(monkeypatch):
    monkeypatch.setattr(birth_cert, "TRUSTED_AUTHORITIES", [ISSUER])
    monkeypatch.setattr(witness, "TRUSTED_AUTHORITIES", [ISSUER])


def test_the_v1_statement_is_frozen():
    seen = datetime(2026, 11, 2, 12, 0, 1, tzinfo=timezone.utc)
    assert witness.statement_v1("11" * 32, "22" * 32, "33" * 64, "probe", None, seen) == (
        b"sautium-witness:v1:" + b"11" * 32 + b":" + b"22" * 32 + b":" + b"33" * 64
        + b":probe:-:2026-11-02T12:00:01Z")
    assert witness.statement_v1("11" * 32, "22" * 32, "33" * 64, "carried", "44" * 32, seen) == (
        b"sautium-witness:v1:" + b"11" * 32 + b":" + b"22" * 32 + b":" + b"33" * 64
        + b":carried:" + b"44" * 32 + b":2026-11-02T12:00:01Z")
    # Another zone and a fraction of a second: the same second, in UTC.
    kyiv = datetime(2026, 11, 2, 14, 0, 1, 999999, tzinfo=timezone(timedelta(hours=2)))
    assert witness.statement_v1("11" * 32, "22" * 32, "33" * 64, "probe", None, kyiv) == \
        witness.statement_v1("11" * 32, "22" * 32, "33" * 64, "probe", None, seen)
    row = {"witness_v": 1, "subject": "22" * 32, "cert_sig": "33" * 64, "channel": "probe",
           "via": None, "seen_at": seen}
    assert witness.statement(row, "11" * 32) == witness.statement_v1(
        "11" * 32, "22" * 32, "33" * 64, "probe", None, seen)


def test_a_statement_signs_and_verifies_for_its_witness_only():
    me, other = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    row = {"witness_v": 1, "subject": _pub(other), "cert_sig": "ab" * 64, "channel": "introduced",
           "via": None, "seen_at": datetime(2026, 11, 2, 8, 30, tzinfo=timezone.utc)}
    payload = witness.statement(row, _pub(me))
    sig = me.sign(payload).hex()
    assert record_sig.verify(payload, sig, _pub(me))
    assert not record_sig.verify(payload, sig, _pub(other))
    # The witness is inside the statement: another key's statement differs.
    assert not record_sig.verify(witness.statement(row, _pub(other)), sig, _pub(me))


def _row(cert: dict) -> dict:
    return {"subject": cert["pubkey"], "cert_payload": birth_cert.canonical_payload(cert).decode(),
            "cert_sig": cert["sig"], "issuer": cert["issuer"]}


def test_a_stored_certificate_holds_by_ed25519_alone():
    cert = certificate(_pub(Ed25519PrivateKey.generate()))
    row = _row(cert)
    assert witness.certificate_holds(row)
    assert witness.certificate_holds(dict(row, cert_payload=row["cert_payload"].replace(
        "2026-11-01T09:00:00Z", "2026-01-01T09:00:00Z"))) is False       # another issued_at
    assert witness.certificate_holds(dict(row, subject="cd" * 32)) is False
    assert witness.certificate_holds(row, trusted=["ef" * 32]) is False   # not a trusted authority
    assert witness.certificate_holds(dict(row, cert_sig="00" * 64)) is False


class _NoDatabase:
    """A connect() that fails the test: the refusals below never need one."""

    @contextmanager
    def __call__(self):
        raise AssertionError("record() touched the database")
        yield


def test_a_certificate_for_another_key_is_refused_before_the_database():
    subject, other = _pub(Ed25519PrivateKey.generate()), _pub(Ed25519PrivateKey.generate())
    rec = witness.Recorder(_NoDatabase(), own_keys=lambda: frozenset())
    cert = certificate(subject)
    assert rec.record(cert, "probe", other) is False
    forged = dict(cert, difficulty=31)                 # the authority never signed this
    assert rec.record(forged, "probe", subject) is False
    assert rec.record(certificate(subject, v=3), "probe", subject) is False


def test_this_nodes_own_and_retired_keys_are_never_witnessed():
    own, retired = _pub(Ed25519PrivateKey.generate()), _pub(Ed25519PrivateKey.generate())
    rec = witness.Recorder(_NoDatabase(), own_keys=lambda: frozenset({own, retired}))
    for channel in witness.CHANNELS:
        assert rec.record(certificate(own), channel, own.upper()) is False
        assert rec.record(certificate(retired), channel, retired) is False


def test_own_keys_reads_the_retired_ones_from_node_info(tmp_path):
    import json
    assert witness.own_keys("AB" * 32, None) == frozenset({"ab" * 32})
    assert witness.own_keys("ab" * 32, tmp_path) == frozenset({"ab" * 32})     # no node_info.json
    (tmp_path / "node_info.json").write_text(json.dumps({
        "public_key_hex": "ab" * 32,
        "previous": [{"public_key_hex": "CD" * 32, "dir": "x"}, {"dir": "y"}]}))
    assert witness.own_keys("ab" * 32, tmp_path) == frozenset({"ab" * 32, "cd" * 32})
    assert witness.own_keys("", tmp_path) == frozenset({"cd" * 32})
