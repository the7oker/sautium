"""Node backup format v1 (desktop/node_backup.py) — the pure parts: the KDF
domain, the envelope, the chunked AEAD stream and its member framing. Every
tamper case must fail closed. The pg_dump / pg_restore half runs against the
live database as `python -m backup selftest` (backend) — see CLAUDE.md
"Testing Expectations"."""

import hashlib
import io
import json
import os
import struct

import pytest

from desktop import node_backup as nb

# Small parameters: the point here is the domain and the framing, not 256 MiB
# of Argon2 per case. The reader is told the same parameters explicitly — it
# refuses a header whose parameters differ from what it was given.
T = nb.KdfParams(time_cost=1, memory_cost=8 * 1024, parallelism=1)
PW = "correct horse battery"
USER = "vale"
PUB = "ab" * 32
CHUNK = 64


MEMBERS = [
    (nb.MANIFEST_MEMBER, json.dumps({"format_version": 1, "migrations": []}).encode()),
    (nb.DB_MEMBER, os.urandom(CHUNK * 7 + 13)),
    ("identity/empty.json", b""),
    ("identity/previous/x/rotation.json", b'{"v":1}'),
]


def _members():
    return list(MEMBERS)


def _write(members=None, chunk_size=CHUNK, password=PW):
    buf = io.BytesIO()
    w = nb.BackupWriter(buf, kek=nb.derive_kek(password, USER, T), username=USER,
                        pubkey=PUB, kdf=T, chunk_size=chunk_size)
    for name, data in (members or _members()):
        w.add_member(name, data)
    summary = w.finish()
    return buf.getvalue(), summary


def _open(blob, password=PW):
    r = nb.BackupReader(io.BytesIO(blob))
    r.unlock(password, T)
    return r


def test_kdf_vector_and_domain():
    kek = nb.derive_kek(PW, USER, T)
    assert kek.hex() == "6f3e17b8f502a87284410a70bee291b305ad4d65bd4769137a74c43a0a422ca4"
    assert nb.derive_kek(PW, USER, T) == kek
    # The identity seed uses "<username>:sautium"; same password, other key.
    from argon2.low_level import Type, hash_secret_raw
    seed = hash_secret_raw(PW.encode(), f"{USER}:sautium".encode(), time_cost=T.time_cost,
                           memory_cost=T.memory_cost, parallelism=T.parallelism,
                           hash_len=32, type=Type.ID)
    assert seed != kek
    assert T.header(USER)["salt"] == "sautium-backup:v1:vale"


def test_round_trip_members_sizes_and_digests():
    blob, summary = _write()
    assert blob.startswith(nb.MAGIC)
    assert summary["sha256"] == hashlib.sha256(blob).hexdigest()
    assert summary["size"] == len(blob)
    r = _open(blob)
    assert r.node == {"pubkey": PUB, "username": USER}
    seen = []
    for m in r.members():
        data = m.read()
        seen.append((m.name, data))
        assert m.size == len(data)
        assert m.sha256 == hashlib.sha256(data).hexdigest()
    assert seen == _members()
    assert [m["name"] for m in summary["members"]] == [n for n, _ in _members()]


def test_skipped_member_is_drained_and_verified():
    blob, _ = _write()
    r = _open(blob)
    names = [m.name for m in r.members()]          # never reads a byte of any member
    assert names == [n for n, _ in _members()]


def test_manifest_first_then_the_rest():
    blob, _ = _write()
    r = _open(blob)
    assert r.read_manifest() == {"format_version": 1, "migrations": []}
    rest = [(m.name, m.read()) for m in r.remaining_members()]
    assert rest == _members()[1:]
    # a file whose first member is not the manifest is refused as such
    blob2, _ = _write(members=_members()[1:])
    with pytest.raises(nb.Tampered):
        _open(blob2).read_manifest()


def test_ciphertext_never_repeats_and_data_key_is_per_file():
    a, _ = _write()
    b, _ = _write()
    ha, hb = nb.read_header(io.BytesIO(a))[0], nb.read_header(io.BytesIO(b))[0]
    assert ha["nonce_prefix"] != hb["nonce_prefix"]
    assert ha["wrapped_keys"][0]["box"] != hb["wrapped_keys"][0]["box"]
    assert a[len(nb.encode_header(ha)):] != b[len(nb.encode_header(hb)):]


def test_wrong_password_fails_before_any_chunk():
    blob, _ = _write()
    r = nb.BackupReader(io.BytesIO(blob))
    with pytest.raises(nb.WrongPassword):
        r.unlock("wrong horse", T)
    with pytest.raises(nb.BackupError):
        list(r.records())                         # still locked


def test_header_kdf_parameters_are_pinned():
    blob, _ = _write()
    header, raw = nb.read_header(io.BytesIO(blob))
    header["kdf"]["m"] = 1 << 24                  # 16 GiB, if anyone derived it
    forged = nb.encode_header(header) + blob[len(raw):]
    with pytest.raises(nb.Refused):
        nb.BackupReader(io.BytesIO(forged)).unlock(PW, T)
    with pytest.raises(nb.Refused):
        _open(blob).unlock(PW, nb.KdfParams(time_cost=2, memory_cost=8 * 1024, parallelism=1))


def test_edited_header_fails_the_first_chunk():
    blob, _ = _write()
    header, raw = nb.read_header(io.BytesIO(blob))
    header["created_at"] = "1999-12-31T23:59:59Z"   # outside the wrap AAD, so unlock passes
    forged = nb.encode_header(header) + blob[len(raw):]
    r = _open(forged)
    with pytest.raises(nb.Tampered):
        next(r.members())


def test_flipped_ciphertext_byte_is_detected():
    blob, _ = _write()
    _, raw = nb.read_header(io.BytesIO(blob))
    pos = len(raw) + 4 + 3                        # inside the first chunk's ciphertext
    bad = bytearray(blob)
    bad[pos] ^= 0x01
    with pytest.raises(nb.Tampered):
        list(_open(bytes(bad)).members())


def _frames(blob):
    """(offset, length) of every chunk frame after the header."""
    _, raw = nb.read_header(io.BytesIO(blob))
    pos, out = len(raw), []
    while pos < len(blob):
        (n,) = struct.unpack(">I", blob[pos:pos + 4])
        out.append((pos, 4 + n))
        pos += 4 + n
    return out


def test_truncation_anywhere_is_detected():
    blob, _ = _write()
    frames = _frames(blob)
    cut_points = [
        frames[3][0] + 2,                 # inside a length prefix
        frames[3][0] + 10,                # inside a ciphertext
        frames[-1][0],                    # exactly before the END record
        frames[5][0],                     # at a chunk boundary mid-member
    ]
    for cut in cut_points:
        r = _open(blob[:cut])
        with pytest.raises(nb.Tampered):
            for m in r.members():
                m.read()


def test_reordered_and_duplicated_chunks_are_detected():
    blob, _ = _write()
    frames = _frames(blob)
    # two DATA chunks of the db member (frames 3 and 4: after the manifest's
    # START/DATA/END, the dump's START is frame 3, DATA chunks follow)
    a, b = frames[4], frames[5]
    swapped = (blob[:a[0]] + blob[b[0]:b[0] + b[1]] + blob[a[0]:a[0] + a[1]] + blob[b[0] + b[1]:])
    with pytest.raises(nb.Tampered):
        for m in _open(swapped).members():
            m.read()
    dup = blob[:a[0] + a[1]] + blob[a[0]:a[0] + a[1]] + blob[a[0] + a[1]:]
    with pytest.raises(nb.Tampered):
        for m in _open(dup).members():
            m.read()
    dropped = blob[:a[0]] + blob[a[0] + a[1]:]
    with pytest.raises(nb.Tampered):
        for m in _open(dropped).members():
            m.read()


def test_trailing_bytes_after_end_are_rejected():
    blob, _ = _write()
    with pytest.raises(nb.Tampered):
        list(_open(blob + b"\0").members())


def test_not_a_backup_and_future_version():
    with pytest.raises(nb.BackupError):
        nb.read_header(io.BytesIO(b"PGDMP..."))
    blob, _ = _write()
    future = bytearray(blob)
    future[len(nb.MAGIC) + 1] = 2                   # version u16 → 2
    with pytest.raises(nb.Refused):
        nb.read_header(io.BytesIO(bytes(future)))


def test_check_compatible_refuses_newer_schema_and_rule():
    known = ["001_initial.sql", "002_x.sql"]
    nb.check_compatible({"migrations": ["001_initial.sql", "identity_rule_v2", "seed_v1"]},
                        migrations=known, identity_rule=2)
    with pytest.raises(nb.Refused):
        nb.check_compatible({"migrations": ["001_initial.sql", "003_new.sql"]},
                            migrations=known, identity_rule=2)
    with pytest.raises(nb.Refused):
        nb.check_compatible({"migrations": ["001_initial.sql", "identity_rule_v3"]},
                            migrations=known, identity_rule=2)
    # older data under newer code is the normal upgrade path
    nb.check_compatible({"migrations": ["001_initial.sql", "identity_rule_v1"]},
                        migrations=known, identity_rule=2)


def test_identity_files_selection(tmp_path):
    for name in ("node_info.json", "node_ed25519.key", "node_ed25519.pub", "tls_cert.pem",
                 "tls_key.pem", "birth_certificate.json", "identity_proof.json", ".api_secret"):
        (tmp_path / name).write_text(name)
    prev = tmp_path / "previous" / "20260101T000000Z-abc"
    prev.mkdir(parents=True)
    for name in ("node_info.json", "node_ed25519.key", "rotation.json"):
        (prev / name).write_text(name)
    (tmp_path / "replaced-20260102T000000Z").mkdir()
    (tmp_path / "replaced-20260102T000000Z" / "node_info.json").write_text("old")
    names = [n for n, _ in nb.identity_files(tmp_path)]
    assert names == [
        "identity/.api_secret", "identity/birth_certificate.json",
        "identity/identity_proof.json", "identity/node_info.json",
        "identity/previous/20260101T000000Z-abc/node_info.json",
        "identity/previous/20260101T000000Z-abc/rotation.json",
    ]
    assert nb.identity_files(None) == [] and nb.identity_files(tmp_path / "nope") == []


def test_backup_filename():
    from datetime import datetime, timezone
    when = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
    assert nb.backup_filename("ab" * 32, when) == "sautium-backup-abababababab-2026-09-13.sbk"


@pytest.mark.skipif(not __import__("desktop.node_identity", fromlist=["HAS_CRYPTO"]).HAS_CRYPTO,
                    reason="cryptography missing")
def test_write_identity_rederives_the_key_and_archives_a_foreign_one(tmp_path):
    from desktop import node_identity as ni
    private, pub, invite = ni.derive_account_identity("second", "hunter2xx")
    info = {"node_id": pub, "public_key_hex": pub, "algorithm": "Ed25519",
            "username": "second", "invite_code": invite, "email": "", "email_verified": False,
            "anonymous": False}
    files = {"node_info.json": json.dumps(info).encode(), ".api_secret": b"s3cret",
             "birth_certificate.json": b"{}", "previous/x/rotation.json": b"{}"}

    # a different identity lives here already → moved aside, never deleted
    (tmp_path / "node_info.json").write_text(json.dumps({"public_key_hex": "cd" * 32}))
    (tmp_path / "node_ed25519.key").write_text("old key")
    with pytest.raises(nb.Refused):
        nb.write_identity(tmp_path, files, username="second", password="wrong-password")
    assert (tmp_path / "node_ed25519.key").read_text() == "old key"   # nothing touched

    out = nb.write_identity(tmp_path, files, username="second", password="hunter2xx")
    assert out["pubkey"] == pub and out["moved_previous_identity"].startswith("replaced-")
    moved = tmp_path / out["moved_previous_identity"]
    assert (moved / "node_ed25519.key").read_text() == "old key"
    assert json.loads((tmp_path / "node_info.json").read_text()) == info
    assert (tmp_path / ".api_secret").read_bytes() == b"s3cret"
    assert (tmp_path / "previous" / "x" / "rotation.json").exists()
    from cryptography.hazmat.primitives import serialization
    key = serialization.load_pem_private_key((tmp_path / "node_ed25519.key").read_bytes(), None)
    assert key.public_key().public_bytes(serialization.Encoding.Raw,
                                         serialization.PublicFormat.Raw).hex() == pub
