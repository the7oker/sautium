"""The identity file (desktop/identity_file, SEC-S5 §16–§17): the key with
the certificate, the proof and the retired keys, plain or under a
passphrase, and its import — exclusive, never over another identity."""

import io
import json
import struct
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from desktop import identity_file as idf
from desktop import node_identity as ni

PHRASE = "tundra-maple-orbit-kettle"            # made up, of the EFF list's shape
PASSWORD = "lantern-quiver-mosaic-ember"


def _account(d: Path, username: str = "alex", password: str = PASSWORD, email: str = "a@example.org",
             key=None) -> str:
    d.mkdir(parents=True, exist_ok=True)
    account = ni.new_account(username, password, source="form", email=email)
    ni.write_account(d, account)
    key = key or ni.key_from_seed(ni.derive_seed(username, password))
    ni.place_account_key(d, key, account)
    pubkey = ni.public_hex(key)
    (d / ni.CERT_FILENAME).write_text(json.dumps({"pubkey": pubkey, "v": 4, "method": "pow"}))
    (d / idf.PROOF_FILENAME).write_text(json.dumps({"pubkey": pubkey, "nonce": "00"}))
    return pubkey


def _rotated(d: Path) -> tuple:
    old = _account(d)
    new_key = Ed25519PrivateKey.generate()
    result = ni.rotate_identity(d, "alex2", new_key, False, ni.signin_hash(PASSWORD))
    return old, result["info"]["public_key_hex"]


def test_a_plain_file_round_trips_the_identity(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    pubkey = _account(src)
    data = idf.export(src, None)
    assert data.startswith(idf.MAGIC)
    assert idf.summary(data) == {"public_key_hex": pubkey, "username": "alex",
                                 "invite_code": ni.make_invite_code("alex", bytes.fromhex(pubkey)),
                                 "locked": False, "created_at": idf.summary(data)["created_at"]}
    placed = idf.import_(dst, data, None)
    assert placed["placed"] and placed["public_key_hex"] == pubkey
    assert ni.pubkey_on_disk(dst) == pubkey
    assert json.loads((dst / "node_info.json").read_text()) == json.loads((src / "node_info.json").read_text())
    for name in (ni.CERT_FILENAME, idf.PROOF_FILENAME):
        assert (dst / name).read_bytes() == (src / name).read_bytes()
    account = ni.read_account(dst)
    assert account["pubkey"] == pubkey and account["username"] == "alex"
    assert ni.signin_matches(account["signin"], PASSWORD)
    assert account["source"] == "file" and account["email"] == "a@example.org"
    assert not (dst / "node_ed25519.key").stat().st_mode & 0o077


def test_the_account_id_is_the_new_machine_s_own(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _account(src)
    data = idf.export(src, None)
    assert ni.read_account(src)["account_id"].encode() not in data
    idf.import_(dst, data, None)
    assert ni.read_account(dst)["account_id"] != ni.read_account(src)["account_id"]


def test_a_locked_file_opens_with_its_passphrase_alone(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    pubkey = _account(src)
    data = idf.export(src, PHRASE)
    assert idf.summary(data)["locked"]
    assert b"PRIVATE KEY" not in data and b"argon2id$v=19" not in data
    header = idf._split(data)[0]["passphrase"]
    assert (header["t"], header["m"], header["p"]) == (
        ni.ARGON2_TIME_COST, ni.ARGON2_MEMORY_COST, ni.ARGON2_PARALLELISM)
    with pytest.raises(idf.WrongPassphrase):
        idf.open_file(data, "tundra-maple-orbit-kettles")
    with pytest.raises(idf.WrongPassphrase):
        idf.open_file(data, None)
    assert not (dst / "node_ed25519.key").exists()
    assert idf.import_(dst, data, PHRASE)["public_key_hex"] == pubkey
    assert ni.pubkey_on_disk(dst) == pubkey


def test_a_file_opens_at_the_cost_its_header_names(tmp_path, monkeypatch):
    src = tmp_path / "src"
    _account(src)
    data = idf.export(src, PHRASE)
    monkeypatch.setattr(ni, "ARGON2_TIME_COST", 2)       # a later release's constants
    assert idf.open_file(data, PHRASE).username == "alex"


def test_the_passphrase_is_held_to_the_account_password_s_edge(tmp_path):
    src = tmp_path / "src"
    _account(src)
    with pytest.raises(idf.WeakPassphrase) as refused:
        idf.export(src, "password1")
    assert refused.value.verdict["zone"] == "refused"
    with pytest.raises(idf.WeakPassphrase):
        idf.export(src, "")
    with pytest.raises(idf.WeakPassphrase) as asked:
        idf.export(src, "kettle77")                 # weeks of one GPU: the person's word
    assert asked.value.verdict["zone"] == "word"
    assert idf.export(src, "kettle77", keep=True).startswith(idf.MAGIC)


def test_a_changed_file_is_refused(tmp_path):
    src = tmp_path / "src"
    _account(src)
    for passphrase in (None, PHRASE):
        data = bytearray(idf.export(src, passphrase))
        data[-5] ^= 1
        with pytest.raises(idf.Damaged):
            idf.open_file(bytes(data), passphrase)
    locked = idf.export(src, PHRASE)
    header, before, body = idf._split(locked)
    header["created_at"] = "2020-01-01T00:00:00Z"            # the AAD binds the header
    raw = json.dumps(header, separators=(",", ":"), sort_keys=True).encode()
    with pytest.raises(idf.Damaged):
        idf.open_file(idf.MAGIC + struct.pack(">HI", 1, len(raw)) + raw + body, PHRASE)
    with pytest.raises(idf.NotAnIdentityFile):
        idf.summary(b"SAUTIUM-BACKUP\0" + bytes(40))
    with pytest.raises(idf.NewerFormat):
        idf.summary(idf.MAGIC + struct.pack(">HI", 2, 2) + b"{}")
    with pytest.raises(idf.Damaged):
        idf.summary(idf.MAGIC + struct.pack(">HI", 1, 1 << 20) + b"{}")
    with pytest.raises(idf.Damaged):
        idf.summary(bytes(locked) + bytes(idf.FILE_MAX))


def _rewritten(data: bytes, change) -> bytes:
    header, _, body = idf._split(data)
    parsed = json.loads(body)
    change(header, parsed)
    raw = idf._canonical(header)
    return idf.MAGIC + struct.pack(">HI", 1, len(raw)) + raw + idf._canonical(parsed)


def test_a_file_whose_key_is_not_its_public_key_is_refused(tmp_path):
    src = tmp_path / "src"
    _account(src)
    data = idf.export(src, None)
    other = Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode()
    with pytest.raises(idf.Damaged, match="not the public key"):
        idf.open_file(_rewritten(data, lambda h, b: b.update(key=other)), None)
    with pytest.raises(idf.Damaged):
        idf.open_file(_rewritten(data, lambda h, b: b.update(key="not a key")), None)


def test_names_and_sizes_are_checked_before_anything_is_written(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _, new = _rotated(src)
    data = idf.export(src, None)
    folder = next(iter(json.loads(idf._split(data)[2])["previous"]))

    def escape(h, b):
        b["previous"]["../../outside"] = b["previous"].pop(folder)

    def info_escape(h, b):
        b["info"]["previous"][0]["dir"] = "../../outside"

    def big(h, b):
        b["documents"][ni.CERT_FILENAME] = json.dumps({"pubkey": new, "pad": "x" * idf.MEMBER_MAX})

    def stranger(h, b):
        b["documents"]["tls_key.pem"] = "x"

    def other_cert(h, b):
        b["documents"][ni.CERT_FILENAME] = json.dumps({"pubkey": "ab" * 32})

    def other_archive_key(h, b):
        b["previous"][folder]["node_ed25519.key"] = b["key"]

    for change in (escape, info_escape, big, stranger, other_cert, other_archive_key):
        with pytest.raises(idf.Damaged):
            idf.import_(dst, _rewritten(data, change), None)
    assert not dst.exists() or not any(dst.iterdir())
    assert not (tmp_path / "outside").exists()


def test_the_rotation_archive_travels(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    old, new = _rotated(src)
    idf.import_(dst, idf.export(src, PHRASE), PHRASE)
    assert ni.pubkey_on_disk(dst) == new
    assert ni.load_previous_seeds(dst) == ni.load_previous_seeds(src)
    assert [e["public_key_hex"] for e in ni.previous_identities(dst)] == [old]
    assert ni.rotation_record(old, dst) == ni.rotation_record(old, src)


def test_an_import_over_another_identity_is_refused_and_changes_nothing(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _account(src)
    theirs = _account(dst, username="sam", key=Ed25519PrivateKey.generate())
    before = {p.name: p.read_bytes() for p in dst.iterdir() if p.is_file()}
    data = idf.export(src, None)
    with pytest.raises(idf.IdentityHere, match="sam#"):
        idf.here(dst, idf.summary(data)["public_key_hex"])
    with pytest.raises(idf.IdentityHere):
        idf.import_(dst, data, None)
    assert ni.pubkey_on_disk(dst) == theirs
    assert {p.name: p.read_bytes() for p in dst.iterdir() if p.is_file()} == before


def test_an_account_whose_key_is_still_to_be_made_is_not_imported_over(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _account(src)
    dst.mkdir()
    ni.write_account(dst, ni.new_account("sam", PASSWORD, source="form"))
    with pytest.raises(idf.IdentityHere, match="still to be made"):
        idf.import_(dst, idf.export(src, None), None)
    assert ni.read_account(dst)["username"] == "sam"


def test_the_console_makes_a_random_identity_that_travels(tmp_path, capsys):
    """The master's road (SEC-S5 §18): a random key under the reserved
    nickname, made by the console, carried by its file like any other."""
    src, dst = tmp_path / "src", tmp_path / "dst"
    assert idf.main(["new", "--nickname", "Sautium", "--dir", str(src)]) == 0
    printed = capsys.readouterr().out
    pubkey = ni.pubkey_on_disk(src)
    assert pubkey in printed and ni.make_invite_code("Sautium", bytes.fromhex(pubkey)) in printed
    account = ni.read_account(src)
    assert account["anonymous"] and account["signin"] is None and account["source"] == "console"
    assert idf.main(["new", "--nickname", "Other", "--dir", str(src)]) == 1
    assert ni.pubkey_on_disk(src) == pubkey
    placed = idf.import_(dst, idf.export(src, PHRASE), PHRASE)
    assert placed["invite_code"].startswith("Sautium#") and ni.pubkey_on_disk(dst) == pubkey


def test_an_anonymous_identity_travels_with_no_sign_in_hash(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    info = ni.anonymous_account(src, "anon-quiet", source="wizard")
    idf.import_(dst, idf.export(src, None), None)
    account = ni.read_account(dst)
    assert account["anonymous"] is True and account["signin"] is None
    assert ni.pubkey_on_disk(dst) == info["public_key_hex"]


def test_a_placement_cut_short_completes_when_run_again(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    pubkey = _account(src)
    data = idf.export(src, None)
    idf.import_(dst, data, None)
    (dst / ni.ACCOUNT_FILENAME).unlink()
    (dst / ni.CERT_FILENAME).unlink()
    again = idf.import_(dst, data, None)
    assert again == {**again, "placed": False, "public_key_hex": pubkey}
    assert ni.read_account(dst)["pubkey"] == pubkey and (dst / ni.CERT_FILENAME).exists()


def test_a_folder_with_no_identity_exports_nothing(tmp_path):
    with pytest.raises(idf.NothingToExport):
        idf.export(tmp_path, None)
    ni.write_account(tmp_path, ni.new_account("sam", PASSWORD, source="form"))
    with pytest.raises(idf.NothingToExport, match="still to be made"):
        idf.export(tmp_path, None)


def test_write_file_is_new_and_the_owner_s_alone(tmp_path):
    path = tmp_path / ("x" + idf.FILE_SUFFIX)
    idf.write_file(path, b"bytes")
    assert path.read_bytes() == b"bytes" and not path.stat().st_mode & 0o077
    with pytest.raises(FileExistsError):
        idf.write_file(path, b"other")
    assert path.read_bytes() == b"bytes"


# --- the command a Docker node's console speaks to -----------------------------------

class _Pipe(io.BytesIO):
    def isatty(self):
        return False


@pytest.fixture
def node(tmp_path, monkeypatch):
    d = tmp_path / "node"
    d.mkdir()
    named = {"another": False}
    monkeypatch.setattr(idf, "_node", lambda: (d, "dsn"))
    monkeypatch.setattr(ni, "another_identity_at", lambda dsn, pubkey: named["another"])
    return d, named


def _run(monkeypatch, argv, stdin: bytes):
    out = _Pipe()
    monkeypatch.setattr(sys, "stdin", type("In", (), {"buffer": _Pipe(stdin), "isatty": lambda s: False})())
    monkeypatch.setattr(sys, "stdout", type("Out", (), {
        "buffer": out, "write": lambda s, t: out.write(t.encode()), "flush": lambda s: None,
        "isatty": lambda s: False})())
    return idf.main(argv), out.getvalue()


def test_the_command_exports_and_imports_through_its_pipes(tmp_path, monkeypatch, node):
    d, named = node
    src = tmp_path / "src"
    pubkey = _account(src)
    monkeypatch.setattr(idf, "_node", lambda: (src, "dsn"))
    code, data = _run(monkeypatch, ["export"], PHRASE.encode() + b"\n")
    assert code == 0 and idf.summary(data)["locked"]
    monkeypatch.setattr(idf, "_node", lambda: (d, "dsn"))

    code, said = _run(monkeypatch, ["peek"], data)
    facts = json.loads(said)
    assert code == 0 and facts["refusal"] is None and facts["another_identity"] is False
    assert facts["locked"] and facts["public_key_hex"] == pubkey

    framed = PHRASE.encode() + b"\n" + str(len(data)).encode() + b"\n" + data
    code, said = _run(monkeypatch, ["import"], framed)        # no "place": nothing placed
    assert code == 0 and json.loads(said.splitlines()[0])["opened"]["public_key_hex"] == pubkey
    assert not (d / "node_ed25519.key").exists()

    named["another"] = True
    code, said = _run(monkeypatch, ["import"], framed + b"place\n")
    assert code == 1 and not (d / "node_ed25519.key").exists()
    code, said = _run(monkeypatch, ["import", "--another-identity"], b"wrong-phrase\n"
                      + str(len(data)).encode() + b"\n" + data + b"place\n")
    assert code == 2 and not (d / "node_ed25519.key").exists()
    code, said = _run(monkeypatch, ["import", "--another-identity"], framed + b"place\n")
    assert code == 0 and json.loads(said.splitlines()[1])["placed"]["public_key_hex"] == pubkey
    assert ni.pubkey_on_disk(d) == pubkey


def test_the_command_refuses_a_weak_passphrase_with_its_own_code(tmp_path, monkeypatch, node):
    src = tmp_path / "src"
    _account(src)
    monkeypatch.setattr(idf, "_node", lambda: (src, "dsn"))
    code, data = _run(monkeypatch, ["export"], b"password1\n")
    assert code == 3 and data == b""
    code, said = _run(monkeypatch, ["check"], b"password1\n")
    assert code == 0 and json.loads(said)["zone"] == "refused"
