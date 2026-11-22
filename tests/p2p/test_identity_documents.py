"""No certificate or proof file is written over unsaid (MASTER-FAILCLOSED §4,
BACKLOG B-113): every writer of either goes through
node_identity.write_identity_document, which moves a document of another key
aside whole and says so at `error`, and writes this key's in place."""

import json
import logging
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from desktop import node_backup, node_identity  # noqa: E402
from desktop.p2p import birth_cert, identity_proof  # noqa: E402
from desktop.p2p.identity_pow import PowParams  # noqa: E402
from tests.slice_certs import certificate, proof, trust  # noqa: E402

CERT = node_identity.CERT_FILENAME
PROOF = identity_proof.PROOF_FILENAME


def _key() -> str:
    return os.urandom(32).hex()


@pytest.fixture(autouse=True)
def _trusted(monkeypatch):
    trust(monkeypatch)


def _write(d: Path, name: str, doc) -> bytes:
    data = (json.dumps(doc, indent=2) if not isinstance(doc, str) else doc).encode("utf-8")
    (d / name).write_bytes(data)
    return data


def _aside(d: Path):
    """The documents moved aside, by name."""
    return {p.name: p.read_bytes() for p in d.glob("replaced-*/*")}


def _moved_said(caplog, name: str, old: str, new: str) -> bool:
    return any(r.levelno == logging.ERROR and name in r.getMessage() and old[:16] in r.getMessage()
               and new[:16] in r.getMessage() for r in caplog.records)


def test_a_document_of_another_key_is_moved_aside_whole_and_said(tmp_path, caplog):
    theirs, mine = _key(), _key()
    for name, old, new in ((CERT, certificate(theirs), certificate(mine)),
                           (PROOF, proof(certificate(theirs)), proof(certificate(mine)))):
        d = tmp_path / name
        d.mkdir()
        kept = _write(d, name, old)
        data = json.dumps(new).encode("utf-8")
        node_identity.write_identity_document(d, name, data, mine)
        assert _aside(d) == {name: kept}
        assert (d / name).read_bytes() == data
        assert _moved_said(caplog, name, theirs, mine)


def test_this_key_s_document_or_none_at_all_is_written_in_place(tmp_path, caplog):
    """The email upgrade of a pow certificate replaces it; a file that is no
    verified document of anyone (garbage, an unknown issuer's) is no document
    to keep."""
    mine = _key()
    caplog.set_level(logging.ERROR)
    for held in (certificate(mine, method="pow"), "{not json", {"pubkey": _key(), "sig": "00"}):
        _write(tmp_path, CERT, held)
        upgraded = json.dumps(certificate(mine)).encode("utf-8")
        node_identity.write_identity_document(tmp_path, CERT, upgraded, mine)
        assert (tmp_path / CERT).read_bytes() == upgraded
    assert _aside(tmp_path) == {} and not caplog.records


def test_the_docker_writer_keeps_the_master_s_certificate(tmp_path, caplog):
    """B-113 itself: a start under another key stored its own certificate over
    the one the folder held."""
    import p2p_identity
    key = Ed25519PrivateKey.generate()
    mine = key.public_key().public_bytes_raw().hex()
    (tmp_path / "node_ed25519.key").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    (tmp_path / "node_info.json").write_text(json.dumps({
        "node_id": mine, "public_key_hex": mine, "username": "Sautium", "invite_code": "Sautium#X"}))
    masters = _write(tmp_path, CERT, certificate(_key()))
    settings = SimpleNamespace(p2p_identity_dir=str(tmp_path), p2p_username="", p2p_password="",
                               p2p_email="")
    p2p_identity.forget_cached()
    try:
        assert p2p_identity.save_certificate(settings, certificate(mine, method="pow"))
    finally:
        p2p_identity.forget_cached()
    assert _aside(tmp_path) == {CERT: masters}
    assert json.loads((tmp_path / CERT).read_text())["pubkey"] == mine


@pytest.fixture
def launcher(tmp_path, monkeypatch):
    """The launcher's identity folder, holding an account of `mine`."""
    mine = _key()
    monkeypatch.setattr(node_identity, "_identity_dir", lambda: tmp_path)
    (tmp_path / "node_info.json").write_text(json.dumps({
        "node_id": mine, "public_key_hex": mine, "username": "someone", "invite_code": "someone#X"}))
    return mine


def test_the_launcher_s_writers_keep_another_key_s_documents(launcher, tmp_path, caplog, monkeypatch):
    mine = launcher
    theirs = certificate(_key())
    kept = _write(tmp_path, CERT, theirs)
    assert birth_cert.save_certificate(certificate(mine, method="pow"))
    assert _aside(tmp_path) == {CERT: kept}

    # The wizard's request for a key not saved yet writes through the same door
    for p in tmp_path.glob("replaced-*"):
        for f in p.iterdir():
            f.unlink()
        p.rmdir()
    kept = _write(tmp_path, CERT, theirs)
    other = _key()
    from desktop.p2p import email_verify
    monkeypatch.setattr(email_verify, "_post_worker", lambda path, body: certificate(other))
    assert birth_cert.request_certificate(other, lambda message: b"\0" * 64) is not None
    assert _aside(tmp_path) == {CERT: kept}
    assert _moved_said(caplog, CERT, theirs["pubkey"], other)


def test_an_imported_bundle_keeps_the_documents_it_replaces(launcher, tmp_path):
    mine = launcher
    theirs = certificate(_key(), method="pow")
    kept = {CERT: _write(tmp_path, CERT, theirs), PROOF: _write(tmp_path, PROOF, proof(theirs))}
    cert = certificate(mine, method="pow")
    bundle = tmp_path / "bundle.json"
    bundle.write_text(json.dumps({"certificate": cert, "proof": proof(cert)}))
    assert birth_cert.import_certificate(str(bundle))
    assert _aside(tmp_path) == kept


def test_the_mining_worker_keeps_another_key_s_proof(tmp_path):
    mine = _key()
    cert = dict(certificate(mine, method="pow"), difficulty=2)
    kept = _write(tmp_path, PROOF, proof(certificate(_key(), method="pow")))
    mined = identity_proof.ensure_identity_proof(
        cert, tmp_path / PROOF, stop=threading.Event(), on_state=lambda state: None,
        params=PowParams(version=0, memory_kib=8 * 1024, time_cost=1, parallelism=1),
        mem_available_kib=lambda: 10 ** 9, pause_seconds=0.01)
    assert mined is not None and mined["pubkey"] == mine
    assert _aside(tmp_path) == {PROOF: kept}


def test_a_restore_onto_a_node_with_no_info_keeps_another_key_s_documents(tmp_path):
    """An env-derived Docker node keeps no node_info.json: nothing named the
    live identity, so the restore wrote the backup's certificate over it."""
    live = certificate(_key())
    kept = _write(tmp_path, CERT, live)
    incoming = certificate(_key())
    data = json.dumps(incoming).encode("utf-8")
    node_backup.write_identity(tmp_path, {CERT: data}, username="someone", password=None)
    assert _aside(tmp_path) == {CERT: kept}
    assert (tmp_path / CERT).read_bytes() == data
