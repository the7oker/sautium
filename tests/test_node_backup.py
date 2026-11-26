"""Node backup format v2 (desktop/node_backup.py) — the pure parts: the key
from the identity's seed, the envelope, the chunked AEAD stream and its
member framing, the identity members a restore writes. Every tamper case
must fail closed. The pg_dump / pg_restore half runs against the live
database as `python -m backup selftest` (backend) — see CLAUDE.md "Testing
Expectations"."""

import hashlib
import io
import json
import os
import struct

import pytest

from desktop import node_backup as nb

# Throwaway identities: a seed is any 32 bytes
SEED = bytes(range(32))
OTHER = bytes(range(32, 64))
USER = "vale"
PUB = nb.seed_pubkey(SEED)
CHUNK = 64
ARCHIVE = "20260101T000000Z-0123456789ab"     # a retired identity's folder, as rotate names it


MEMBERS = [
    (nb.MANIFEST_MEMBER, json.dumps({"format_version": 2, "migrations": []}).encode()),
    (nb.DB_MEMBER, os.urandom(CHUNK * 7 + 13)),
    ("identity/empty.json", b""),
    ("identity/previous/x/rotation.json", b'{"v":1}'),
]


def _members():
    return list(MEMBERS)


def _write(members=None, chunk_size=CHUNK, seed=SEED):
    buf = io.BytesIO()
    w = nb.BackupWriter(buf, seed=seed, username=USER, chunk_size=chunk_size)
    for name, data in (members or _members()):
        w.add_member(name, data)
    summary = w.finish()
    return buf.getvalue(), summary


def _open(blob, seeds=(SEED,)):
    r = nb.BackupReader(io.BytesIO(blob))
    r.unlock_with_seeds(list(seeds))
    return r


def test_the_key_is_hkdf_over_the_seed_bound_to_its_public_key():
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    want = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"",
                info=b"sautium-backup:v2:kek:" + bytes.fromhex(PUB)).derive(SEED)
    assert nb.backup_kek(SEED) == want and len(want) == 32
    assert nb.backup_kek(OTHER) != want
    # the seed is not its own wrapping key, and the header names the seed's key alone
    assert want != SEED
    blob, _ = _write()
    header = nb.read_header(io.BytesIO(blob))[0]
    assert header["node"] == {"pubkey": PUB, "username": USER}
    assert [k["kind"] for k in header["wrapped_keys"]] == ["seed"] and "kdf" not in header
    assert blob[len(nb.MAGIC):len(nb.MAGIC) + 2] == b"\x00\x02"


def test_a_backup_opens_with_a_retired_seed_the_node_keeps():
    blob, _ = _write(seed=OTHER)
    r = _open(blob, seeds=(SEED, OTHER))               # the live key first, then the retired
    assert r.read_manifest()["format_version"] == 2


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
    assert r.read_manifest() == {"format_version": 2, "migrations": []}
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


def test_another_identitys_backup_is_said_as_such_before_any_unwrap():
    blob, _ = _write(seed=OTHER)
    r = nb.BackupReader(io.BytesIO(blob))
    with pytest.raises(nb.Refused, match="another identity's") as refused:
        r.unlock_with_seeds([SEED])
    words = str(refused.value)
    assert USER in words and nb.seed_pubkey(OTHER)[:12] in words
    assert "identity file" in words and "same nickname and password" in words
    with pytest.raises(nb.BackupError):
        list(r.records())                         # still locked
    with pytest.raises(nb.Refused, match="another identity's"):
        nb.BackupReader(io.BytesIO(blob)).unlock_with_seeds([])


def test_the_right_key_over_a_forged_wrap_is_a_damaged_header():
    blob, _ = _write()
    header, raw = nb.read_header(io.BytesIO(blob))
    header["node"]["username"] = "someone else"   # inside the wrap's AAD
    forged = nb.encode_header(header) + blob[len(raw):]
    with pytest.raises(nb.Tampered):
        nb.BackupReader(io.BytesIO(forged)).unlock_with_seeds([SEED])


def test_a_header_naming_no_key_is_malformed():
    blob, _ = _write()
    header, raw = nb.read_header(io.BytesIO(blob))
    header["node"]["pubkey"] = "AB" * 32
    with pytest.raises(nb.Tampered, match="malformed"):
        nb.read_header(io.BytesIO(nb.encode_header(header) + blob[len(raw):]))


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
    future[len(nb.MAGIC) + 1] = 3                   # version u16 → 3
    with pytest.raises(nb.Refused, match="update first"):
        nb.read_header(io.BytesIO(bytes(future)))


def test_an_older_backup_is_refused_with_why_and_what_to_do():
    """A v1 file's key was Argon2id over the account's password: no key this
    version makes opens it."""
    v1_header = json.dumps({"kdf": {"alg": "argon2id", "t": 4, "m": 262144, "p": 2,
                                    "salt": "sautium-backup:v1:vale"},
                            "wrapped_keys": [{"kind": "password", "nonce": "00", "box": "00"}],
                            "chunk_size": 64, "nonce_prefix": "00" * 16,
                            "node": {"pubkey": PUB, "username": USER}}).encode()
    v1 = nb.MAGIC + struct.pack(">HI", 1, len(v1_header)) + v1_header
    with pytest.raises(nb.Refused) as refused:
        nb.read_header(io.BytesIO(v1))
    assert str(refused.value) == nb.OLDER_FORMAT
    assert "older Sautium" in nb.OLDER_FORMAT and "Make a new backup from the node itself" in nb.OLDER_FORMAT


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
                 "tls_key.pem", "birth_certificate.json", "identity_proof.json", ".api_secret",
                 "account.json", "env_ed25519.key"):
        (tmp_path / name).write_text(name)
    prev = tmp_path / "previous" / ARCHIVE
    prev.mkdir(parents=True)
    for name in ("node_info.json", "node_ed25519.key", "rotation.json"):
        (prev / name).write_text(name)
    (tmp_path / "replaced-20260102T000000Z").mkdir()
    (tmp_path / "replaced-20260102T000000Z" / "node_info.json").write_text("old")
    names = [n for n, _ in nb.identity_files(tmp_path)]
    assert names == [
        "identity/birth_certificate.json",
        "identity/identity_proof.json", "identity/node_info.json",
        f"identity/previous/{ARCHIVE}/node_info.json",
        f"identity/previous/{ARCHIVE}/rotation.json",
    ]
    assert nb.identity_files(None) == [] and nb.identity_files(tmp_path / "nope") == []


def test_a_backup_carries_no_file_the_identity_folder_does_not_hold(tmp_path):
    (tmp_path / "node_info.json").write_text("{}")
    (tmp_path / "notes.txt").write_text("x")
    (tmp_path / "previous" / "elsewhere").mkdir(parents=True)
    (tmp_path / "previous" / "elsewhere" / "rotation.json").write_text("{}")
    assert [n for n, _ in nb.identity_files(tmp_path)] == ["identity/node_info.json"]


def test_an_identity_member_is_a_file_of_the_identity_folder():
    for rel in ("node_info.json", "birth_certificate.json", "identity_proof.json",
                f"previous/{ARCHIVE}/node_info.json", f"previous/{ARCHIVE}/rotation.json"):
        assert nb.identity_member(rel), rel
    # every other name, whatever a platform's path rules make of it
    for rel in ("notes.txt", "node_ed25519.key", "tls_key.pem", f"previous/{ARCHIVE}/node_ed25519.key",
                ".api_secret", "account.json", "env_ed25519.key", f"previous/{ARCHIVE}/account.json",
                "previous/x/rotation.json", f"previous/{ARCHIVE}", "sub/node_info.json",
                f"previous/{ARCHIVE}/x/rotation.json", f"previous\\{ARCHIVE}\\rotation.json",
                "C:node_info.json", "", "node_info.json/"):
        assert not nb.identity_member(rel), rel


def test_the_account_file_is_never_a_member():
    """account.json holds the sign-in hash: inside a backup it would be a
    cheap test of the password beside a key a thousand times dearer."""
    from desktop.node_identity import ACCOUNT_FILENAME
    assert ACCOUNT_FILENAME == "account.json"
    assert ACCOUNT_FILENAME not in nb.IDENTITY_FILES | nb.ARCHIVE_FILES
    assert not nb.identity_member(ACCOUNT_FILENAME)
    assert not nb.identity_member(f"previous/{ARCHIVE}/{ACCOUNT_FILENAME}")


def test_write_identity_writes_inside_the_identity_folder_or_nothing(tmp_path):
    folder = tmp_path / "identity"
    folder.mkdir()
    info = {"public_key_hex": PUB, "username": USER}
    for rel in ("notes.txt", "../outside.json", "account.json", ".api_secret"):
        with pytest.raises(nb.Tampered):
            nb.write_identity(folder, {"node_info.json": json.dumps(info).encode(), rel: b"{}"},
                              backup_key=PUB, live_key=PUB)
    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")) == ["identity"]


def test_backup_filename():
    from datetime import datetime, timezone
    when = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
    assert nb.backup_filename("ab" * 32, when) == "sautium-backup-abababababab-2026-09-13.sbk"


def _cert(pubkey: str, **extra) -> bytes:
    return json.dumps({"pubkey": pubkey, **extra}).encode()


def test_a_restore_fills_in_what_the_identity_lacks_and_keeps_what_it_holds(tmp_path):
    """The identity is brought first: a restore of its live key's backup
    writes this key's documents the folder lacks and the retired identities
    it does not list — never over the node's info, its documents of this
    key, or its .api_secret."""
    live_info = {"public_key_hex": PUB, "username": USER, "previous": []}
    (tmp_path / "node_info.json").write_text(json.dumps(live_info))
    (tmp_path / ".api_secret").write_bytes(b"the node's own")
    (tmp_path / "identity_proof.json").write_bytes(_cert(PUB, mined="here"))
    theirs = {"public_key_hex": PUB, "username": "renamed", "email": "elsewhere@example.org",
              "previous": [{"public_key_hex": "00" * 32, "dir": ARCHIVE,
                            "retired_at": "2026-01-01T00:00:00Z"}]}
    files = {"node_info.json": json.dumps(theirs).encode(),
             "birth_certificate.json": _cert(PUB, issued="there"),
             "identity_proof.json": _cert(PUB, mined="there"),
             f"previous/{ARCHIVE}/rotation.json": b'{"v":1}'}
    out = nb.write_identity(tmp_path, files, backup_key=PUB, live_key=PUB.upper())
    assert out == {"files": ["birth_certificate.json"], "retired_added": 1}
    assert json.loads((tmp_path / "birth_certificate.json").read_text())["issued"] == "there"
    assert json.loads((tmp_path / "identity_proof.json").read_text())["mined"] == "here"
    assert (tmp_path / ".api_secret").read_bytes() == b"the node's own"
    info = json.loads((tmp_path / "node_info.json").read_text())
    assert (info["username"], info.get("email")) == (USER, None)
    assert [e["dir"] for e in info["previous"]] == [ARCHIVE]
    assert (tmp_path / "previous" / ARCHIVE / "rotation.json").read_bytes() == b'{"v":1}'
    assert nb.write_identity(tmp_path, files, backup_key=PUB, live_key=PUB) == {
        "files": [], "retired_added": 0}


def test_a_retired_keys_backup_restores_the_database_alone(tmp_path):
    live_info = {"public_key_hex": PUB, "username": USER, "previous": []}
    (tmp_path / "node_info.json").write_text(json.dumps(live_info))
    old = nb.seed_pubkey(OTHER)
    files = {"node_info.json": json.dumps({"public_key_hex": old, "previous": [
                 {"public_key_hex": "00" * 32, "dir": ARCHIVE}]}).encode(),
             "birth_certificate.json": _cert(old),
             f"previous/{ARCHIVE}/rotation.json": b"{}"}
    assert nb.write_identity(tmp_path, files, backup_key=old, live_key=PUB) == {
        "files": [], "retired_added": 0}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["node_info.json"]
    assert json.loads((tmp_path / "node_info.json").read_text()) == live_info


def test_a_document_of_another_key_in_the_folder_is_moved_aside(tmp_path):
    (tmp_path / "birth_certificate.json").write_bytes(_cert("cd" * 32))
    out = nb.write_identity(tmp_path, {"birth_certificate.json": _cert(PUB)},
                            backup_key=PUB, live_key=PUB)
    assert out["files"] == ["birth_certificate.json"]
    assert json.loads((tmp_path / "birth_certificate.json").read_text())["pubkey"] == PUB
    aside = [p for p in tmp_path.iterdir() if p.name.startswith("replaced-")]
    assert len(aside) == 1 and json.loads(
        (aside[0] / "birth_certificate.json").read_text())["pubkey"] == "cd" * 32


D0, D1 = "20260101T000000Z-000000000000", "20260201T000000Z-111111111111"   # as rotate names them


def test_rotation_archive_union(tmp_path):
    live = {"public_key_hex": "ab" * 32, "username": "vale", "previous": [
        {"public_key_hex": "11" * 32, "dir": D1, "retired_at": "2026-02-01T00:00:00Z"}]}
    (tmp_path / "node_info.json").write_text(json.dumps(live))
    theirs = {"public_key_hex": "ab" * 32, "previous": [
        {"public_key_hex": "11" * 32, "dir": D1, "retired_at": "2026-02-01T00:00:00Z"},
        {"public_key_hex": "00" * 32, "dir": D0, "retired_at": "2026-01-01T00:00:00Z"}]}
    files = {"node_info.json": json.dumps(theirs).encode(),
             f"previous/{D0}/rotation.json": b'{"v":1}', f"previous/{D0}/birth_certificate.json": b"{}",
             f"previous/{D1}/rotation.json": b"stale"}
    assert nb.merge_archive(tmp_path, files) == 1
    info = json.loads((tmp_path / "node_info.json").read_text())
    assert [e["dir"] for e in info["previous"]] == [D0, D1]          # oldest first
    assert (tmp_path / "previous" / D0 / "rotation.json").read_bytes() == b'{"v":1}'
    assert not (tmp_path / "previous" / D1).exists()                 # known: nothing written
    assert nb.merge_archive(tmp_path, files) == 0
    assert nb.merge_archive(None, files) == 0
    assert nb.merge_archive(tmp_path / "nowhere", files) == 0


def test_a_retired_identity_is_taken_only_under_an_archive_name(tmp_path):
    folder = tmp_path / "identity"
    folder.mkdir()
    live = {"public_key_hex": "ab" * 32, "username": "vale", "previous": []}
    (folder / "node_info.json").write_text(json.dumps(live))
    theirs = {"public_key_hex": "ab" * 32, "previous": [
        {"public_key_hex": "22" * 32, "dir": "../elsewhere", "retired_at": "2026-03-01T00:00:00Z"}]}
    files = {"node_info.json": json.dumps(theirs).encode(),
             "previous/../elsewhere/rotation.json": b"{}"}
    assert nb.merge_archive(folder, files) == 0
    assert json.loads((folder / "node_info.json").read_text()) == live
    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")) == [
        "identity", "identity/node_info.json"]


def _key_in(folder):
    from cryptography.hazmat.primitives import serialization
    from desktop import node_identity as ni
    folder.mkdir(parents=True, exist_ok=True)
    key = ni.key_from_seed(SEED)
    (folder / "node_ed25519.key").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    return key


def test_the_seeds_held_are_the_live_keys_then_the_retired(tmp_path):
    from cryptography.hazmat.primitives import serialization
    from desktop import node_identity as ni
    key = _key_in(tmp_path)
    archive = tmp_path / "previous" / ARCHIVE
    archive.mkdir(parents=True)
    (archive / "node_ed25519.key").write_bytes(ni.key_from_seed(OTHER).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    (tmp_path / "node_info.json").write_text(json.dumps({"public_key_hex": PUB, "previous": [
        {"public_key_hex": nb.seed_pubkey(OTHER), "dir": ARCHIVE}]}))
    assert nb.seeds_held(key, tmp_path, to="restore") == [SEED, OTHER]
    assert nb.seeds_held(key, None, to="restore") == [SEED]


def test_no_key_here_is_said_before_anything_opens(tmp_path):
    from desktop import node_identity as ni
    with pytest.raises(nb.Refused, match="no identity yet") as refused:
        nb.seeds_held(None, tmp_path, to="restore")
    assert "identity file" in str(refused.value) and str(refused.value).endswith("then restore.")
    with pytest.raises(nb.Refused, match="no identity yet"):
        nb.live_seed(None, None, to="back up")
    ni.write_account(tmp_path, ni.new_account(USER, "a made-up passphrase amber", source="form"))
    with pytest.raises(nb.Refused, match="key is still being made — back up once it is ready"):
        nb.live_seed(None, tmp_path, to="back up")
