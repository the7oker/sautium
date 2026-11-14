"""The release signature check (desktop/release_verify.py): SSHSIG over git
tag objects, `ssh-ed25519` and `sk-ssh-ed25519@openssh.com` only, an sk-
signature only with a touch AND the PIN, keys from a strictly read
allowed-signers list. Throwaway keys (tests/release_signing.py), plus one real
signature from the owner's security key — public data, the first check
against hardware output — and real ssh-keygen in both directions."""

import base64
import shutil
import subprocess
from pathlib import Path

import pytest

from desktop import release_verify as rv
from tests.release_signing import Key, signed_tag_object, signers, tag_payload

FIXTURES = Path(__file__).parent / "fixtures" / "release"
COMMIT = "8fa6a9677f1d81564e024480c331b4dc1612b7ee"


@pytest.fixture(params=[False, True], ids=["ed25519", "sk-ed25519"])
def key(request):
    return Key(sk=request.param)


def flip(data: bytes, at: int) -> bytes:
    return data[:at] + bytes([data[at] ^ 0x01]) + data[at + 1:]


# ── the real security key ─────────────────────────────────────────────────

def hardware():
    pub = (FIXTURES / "sautium_release_yk.pub").read_text().split()
    return (rv.parse_allowed_signers(f"release {pub[0]} {pub[1]}\n"),
            (FIXTURES / "yk_test.txt").read_bytes(), (FIXTURES / "yk_test.txt.sig").read_bytes())


def test_the_owners_security_key_verifies_under_its_own_namespace():
    listed, message, sig = hardware()
    assert [signer.key_type for signer in listed] == [rv.SK_ED25519]
    assert rv.verify(message, sig, listed, "sautium-test").principals == ("release",)


def test_the_owners_security_key_is_refused_under_git():
    listed, message, sig = hardware()
    with pytest.raises(rv.VerifyError, match="namespace"):
        rv.verify(message, sig, listed, "git")


def test_the_owners_security_key_is_refused_over_a_changed_byte():
    listed, message, sig = hardware()
    with pytest.raises(rv.VerifyError, match="does not match"):
        rv.verify(flip(message, 3), sig, listed, "sautium-test")


# ── good signatures ───────────────────────────────────────────────────────

def test_a_signed_tag_verifies(key):
    raw = signed_tag_object(key, COMMIT, "v0.2.0")
    tag = rv.verify_tag(raw, "v0.2.0", signers(key.line()))
    assert (tag.name, tag.version, tag.commit, tag.signer.principals) == \
        ("v0.2.0", (0, 2, 0), COMMIT, ("release",))


@pytest.mark.parametrize("hash_name", ["sha256", "sha512"])
def test_both_hashes_sshsig_allows_verify(key, hash_name):
    raw = signed_tag_object(key, COMMIT, "v0.2.0", hash_name=hash_name)
    assert rv.verify_tag(raw, "v0.2.0", signers(key.line())).commit == COMMIT


def test_any_listed_key_verifies_and_names_its_principal():
    daily, recovery = Key(sk=True), Key()
    listed = signers(daily.line("release"), recovery.line("recovery"))
    raw = signed_tag_object(recovery, COMMIT, "v0.3.0")
    assert rv.verify_tag(raw, "v0.3.0", listed).signer.principals == ("recovery",)


def test_a_line_without_namespaces_allows_every_namespace(key):
    listed = signers(key.line(namespaces=""))
    assert listed[0].namespaces is None
    assert rv.verify_tag(signed_tag_object(key, COMMIT, "v0.2.0"), "v0.2.0", listed)


# ── one changed byte anywhere ─────────────────────────────────────────────

def test_a_changed_byte_in_the_payload_is_refused(key):
    raw = signed_tag_object(key, COMMIT, "v0.2.0")
    payload_len = len(tag_payload(COMMIT, "v0.2.0"))
    for at in range(payload_len):
        changed = flip(raw, at)
        with pytest.raises(rv.VerifyError):
            rv.verify_tag(changed, "v0.2.0", signers(key.line()))


def test_a_changed_byte_in_the_signature_is_refused(key):
    message = b"payload\n"
    armored = key.sign(message)
    lines = armored.splitlines()
    blob = base64.b64decode(b"".join(lines[1:-1]))
    for at in range(len(blob)):
        text = base64.b64encode(flip(blob, at))
        changed = lines[0] + b"\n" + text + b"\n" + lines[-1] + b"\n"
        with pytest.raises(rv.VerifyError):
            rv.verify(message, changed, signers(key.line()), "git")


def test_a_changed_byte_in_the_listed_key_is_refused(key):
    raw = signed_tag_object(key, COMMIT, "v0.2.0")
    for at in range(len(key.blob)):
        line = f"release {key.key_type} {base64.b64encode(flip(key.blob, at)).decode()}"
        with pytest.raises(rv.VerifyError):
            rv.verify_tag(raw, "v0.2.0", signers(line))


# ── what is refused ───────────────────────────────────────────────────────

def test_a_namespace_other_than_git_is_refused(key):
    payload = tag_payload(COMMIT, "v0.2.0")
    raw = payload + key.sign(payload, namespace="file")
    with pytest.raises(rv.VerifyError, match="namespace 'file'"):
        rv.verify_tag(raw, "v0.2.0", signers(key.line(namespaces="")))


def test_a_signed_namespace_differing_from_the_declared_one_is_refused(key):
    payload = tag_payload(COMMIT, "v0.2.0")
    raw = payload + key.sign(payload, namespace="git", signed_namespace="file")
    with pytest.raises(rv.VerifyError, match="does not match"):
        rv.verify_tag(raw, "v0.2.0", signers(key.line()))


def test_a_listed_key_is_refused_outside_its_namespaces(key):
    raw = signed_tag_object(key, COMMIT, "v0.2.0")
    with pytest.raises(rv.VerifyError, match="not listed"):
        rv.verify_tag(raw, "v0.2.0", signers(key.line(namespaces='namespaces="sautium-file"')))


def test_a_comma_list_of_namespaces_allows_each_and_a_tag_verifies_under_git_only(key):
    listed = signers(key.line(namespaces='namespaces="git,sautium-file"'))
    assert listed[0].namespaces == ("git", "sautium-file")
    assert rv.verify_tag(signed_tag_object(key, COMMIT, "v0.2.0"), "v0.2.0", listed).commit == COMMIT
    message = b"abc  Sautium-0.2.0-x64.exe\n"
    assert rv.verify(message, key.sign(message, namespace="sautium-file"), listed, "sautium-file")
    payload = tag_payload(COMMIT, "v0.2.0")
    with pytest.raises(rv.VerifyError, match="namespace 'sautium-file'"):
        rv.verify_tag(payload + key.sign(payload, namespace="sautium-file"), "v0.2.0", listed)


def test_a_key_listed_for_files_only_signs_no_release(key):
    listed = signers(key.line(namespaces='namespaces="sautium-file"'))
    message = b"sums\n"
    assert rv.verify(message, key.sign(message, namespace="sautium-file"), listed, "sautium-file")
    with pytest.raises(rv.VerifyError, match="not listed for 'git'"):
        rv.verify_tag(signed_tag_object(key, COMMIT, "v0.2.0"), "v0.2.0", listed)


@pytest.mark.parametrize("flags, missing", [(0x00, "touch"), (0x04, "touch"), (0x01, "PIN")])
def test_a_security_key_signature_needs_a_touch_and_the_pin(flags, missing):
    key = Key(sk=True)
    raw = signed_tag_object(key, COMMIT, "v0.2.0", flags=flags)
    with pytest.raises(rv.VerifyError, match=missing):
        rv.verify_tag(raw, "v0.2.0", signers(key.line()))


def test_a_security_key_signature_with_more_flags_than_both_verifies():
    key = Key(sk=True)
    raw = signed_tag_object(key, COMMIT, "v0.2.0", flags=0x05 | 0x40)
    assert rv.verify_tag(raw, "v0.2.0", signers(key.line()))


def test_a_security_keys_flags_are_signed():
    key = Key(sk=True)
    payload = tag_payload(COMMIT, "v0.2.0")
    armored = key.sign(payload, flags=0x01)
    lines = armored.splitlines()
    blob = bytearray(base64.b64decode(b"".join(lines[1:-1])))
    blob[-5] = 0x05                         # the flags byte, raised after signing
    forged = lines[0] + b"\n" + base64.b64encode(bytes(blob)) + b"\n" + lines[-1] + b"\n"
    with pytest.raises(rv.VerifyError, match="does not match"):
        rv.verify_tag(payload + forged, "v0.2.0", signers(key.line()))


def test_another_application_than_the_keys_is_refused():
    key = Key(sk=True)
    raw = signed_tag_object(key, COMMIT, "v0.2.0")
    other = Key(sk=True, application="ssh:", private=key.private)
    with pytest.raises(rv.VerifyError, match="not listed"):
        rv.verify_tag(raw, "v0.2.0", signers(other.line()))


@pytest.mark.parametrize("key_type", ["ssh-rsa", "ecdsa-sha2-nistp256",
                                      "sk-ecdsa-sha2-nistp256@openssh.com",
                                      "ssh-ed25519-cert-v01@openssh.com"])
def test_an_unknown_key_type_is_refused(key_type):
    key = Key()
    blob = len(key_type).to_bytes(4, "big") + key_type.encode() + key.blob[15:]
    assert signers(f"release {key_type} {base64.b64encode(blob).decode()}") == []


def test_a_key_not_listed_is_refused(key):
    raw = signed_tag_object(key, COMMIT, "v0.2.0")
    with pytest.raises(rv.VerifyError, match="not listed"):
        rv.verify_tag(raw, "v0.2.0", signers(Key(sk=key.sk).line()))


def test_an_empty_list_verifies_nothing(key):
    raw = signed_tag_object(key, COMMIT, "v0.2.0")
    assert rv.parse_allowed_signers("") == []
    assert rv.parse_allowed_signers("# only a comment\n\n") == []
    with pytest.raises(rv.VerifyError, match="not listed"):
        rv.verify_tag(raw, "v0.2.0", [])


@pytest.mark.parametrize("broken", [
    'release cert-authority ssh-ed25519 {b64}',
    'release namespaces="git",valid-after="20260101" ssh-ed25519 {b64}',
    'release valid-before="20300101" ssh-ed25519 {b64}',
    '"release" ssh-ed25519 {b64}',
    'rel* ssh-ed25519 {b64}',
    'release namespaces="" ssh-ed25519 {b64}',
    'release ssh-ed25519 {b64}!',
    'release ssh-ed25519',
    'release sk-ssh-ed25519@openssh.com {b64}',
])
def test_a_broken_line_is_refused_alone_and_said(broken, caplog):
    bad, good = Key(), Key(sk=True)
    text = broken.format(b64=base64.b64encode(bad.blob).decode()) + "\n" + good.line("release")
    listed = rv.parse_allowed_signers(text, "test list")
    assert [s.key_blob for s in listed] == [good.blob]
    assert "test list line 1 refused" in caplog.text


def test_a_name_mismatch_is_refused(key):
    raw = signed_tag_object(key, COMMIT, "v0.2.0")
    with pytest.raises(rv.VerifyError, match="named 'v0.2.0', fetched as 'v9.9.9'"):
        rv.verify_tag(raw, "v9.9.9", signers(key.line()))


@pytest.mark.parametrize("name", ["v1.2", "1.2.3", "v01.2.3", "v1.2.3-rc1", "v1.2.3.4", "release",
                                  "v1.0.1234567", "v1.0." + "9" * 300])
def test_a_name_that_is_not_a_release_is_refused(key, name):
    raw = signed_tag_object(key, COMMIT, name)
    with pytest.raises(rv.VerifyError, match="not a release name"):
        rv.verify_tag(raw, name, signers(key.line()))


def test_a_version_part_has_six_digits_at_most():
    assert rv.parse_version("v999999.0.123456") == (999999, 0, 123456)
    assert rv.parse_version("v1000000.0.0") is None


def test_a_tag_of_anything_but_a_commit_is_refused(key):
    payload = tag_payload(COMMIT, "v0.2.0").replace(b"type commit", b"type tree")
    with pytest.raises(rv.VerifyError, match="'tree'"):
        rv.verify_tag(payload + key.sign(payload), "v0.2.0", signers(key.line()))


@pytest.mark.parametrize("extra", [b"gpgsig-sha256 x\n", b"tag v0.3.0\n", b"object " + COMMIT.encode() + b"\n"])
def test_a_tag_header_beyond_gits_four_is_refused(key, extra):
    payload = tag_payload(COMMIT, "v0.2.0")
    payload = payload.replace(b"tagger", extra + b"tagger", 1)
    with pytest.raises(rv.VerifyError, match="header|twice"):
        rv.verify_tag(payload + key.sign(payload), "v0.2.0", signers(key.line()))


def test_an_unsigned_tag_is_refused(key):
    with pytest.raises(rv.VerifyError, match="no SSH signature"):
        rv.verify_tag(tag_payload(COMMIT, "v0.2.0"), "v0.2.0", signers(key.line()))


def test_text_after_the_signature_is_refused(key):
    raw = signed_tag_object(key, COMMIT, "v0.2.0") + b"appended\n"
    with pytest.raises(rv.VerifyError):
        rv.verify_tag(raw, "v0.2.0", signers(key.line()))


def test_a_second_signature_block_is_refused(key):
    payload = tag_payload(COMMIT, "v0.2.0")
    first = payload + key.sign(payload)
    with pytest.raises(rv.VerifyError, match="more than one"):
        rv.verify_tag(first + key.sign(first), "v0.2.0", signers(key.line()))


# ── real ssh-keygen and git, both directions ──────────────────────────────

needs_ssh_keygen = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="no ssh-keygen")


@needs_ssh_keygen
def test_a_tag_git_signed_with_ssh_keygen_verifies(tmp_path, monkeypatch):
    for k, v in {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}.items():
        monkeypatch.setenv(k, v)
    keyfile = tmp_path / "k"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(keyfile)], check=True)
    repo = tmp_path / "r"
    run = lambda *a: subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
                                     *a], check=True, capture_output=True).stdout
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    run("commit", "-q", "--allow-empty", "-m", "one")
    run("-c", "gpg.format=ssh", "-c", f"user.signingkey={keyfile}", "tag", "-s", "v0.2.0", "-m", "Sautium 0.2.0")
    pub = (tmp_path / "k.pub").read_text().split()
    listed = signers(f'release namespaces="git" {pub[0]} {pub[1]}')
    tag = rv.verify_tag(run("cat-file", "tag", "v0.2.0"), "v0.2.0", listed)
    assert tag.commit == run("rev-parse", "HEAD").decode().strip()


@needs_ssh_keygen
def test_ssh_keygen_verifies_what_the_test_signer_makes(tmp_path):
    key = Key()
    (tmp_path / "signers").write_text(key.line() + "\n")
    message = b"a message\n"
    (tmp_path / "sig").write_bytes(key.sign(message))
    result = subprocess.run(["ssh-keygen", "-Y", "verify", "-f", str(tmp_path / "signers"), "-I", "release",
                             "-n", "git", "-s", str(tmp_path / "sig")], input=message, capture_output=True)
    assert result.returncode == 0, result.stderr


CMD = Path("/mnt/c/Windows/System32/cmd.exe")
WINDOWS_SSH_KEYGEN = Path("/mnt/c/Windows/System32/OpenSSH/ssh-keygen.exe")


def _persons_verify_line(where: Path, ssh_keygen: str, sep: str) -> str:
    """The one line docs/RELEASING.md and the release notes give a person."""
    return (f"{ssh_keygen} -Y verify -f {where}{sep}release_signers -I release -n sautium-file "
            f"-s {where}{sep}SHA256SUMS.txt.sig < {where}{sep}SHA256SUMS.txt")


@pytest.mark.parametrize("system", ["posix", "windows"])
def test_the_persons_verify_line_takes_the_sums_signature_and_nothing_else(tmp_path, key, system):
    """The installers' SHA256SUMS.txt signed by the daily key (a line that
    allows `git,sautium-file`, as the shipped one): ssh-keygen's own
    `-Y verify` takes it — Linux's, and Windows' ssh-keygen.exe through
    cmd.exe — and refuses a changed byte and a recovery key listed for `git`
    alone."""
    if system == "posix" and shutil.which("ssh-keygen") is None:
        pytest.skip("no ssh-keygen")
    if system == "windows" and not (CMD.exists() and WINDOWS_SSH_KEYGEN.exists()):
        pytest.skip("no Windows ssh-keygen.exe here")
    recovery = Key()
    sums = b"ab12  Sautium-0.2.0-x64.exe\ncd34  Sautium-0.2.0-arm64.dmg\n"

    def verify(listed: str, signed_by: Key, text: bytes) -> subprocess.CompletedProcess:
        (tmp_path / "release_signers").write_text(listed)
        (tmp_path / "SHA256SUMS.txt").write_bytes(text)
        (tmp_path / "SHA256SUMS.txt.sig").write_bytes(signed_by.sign(sums, namespace="sautium-file"))
        if system == "posix":
            return subprocess.run(_persons_verify_line(tmp_path, "ssh-keygen", "/"), shell=True,
                                  capture_output=True, text=True)
        where = subprocess.run(["wslpath", "-w", str(tmp_path)], capture_output=True, text=True).stdout.strip()
        return subprocess.run([str(CMD), "/c", _persons_verify_line(where, "ssh-keygen", "\\")], cwd="/mnt/c",
                              capture_output=True, text=True)

    shipped_form = key.line("release", 'namespaces="git,sautium-file"') + "\n" + recovery.line("recovery") + "\n"
    good = verify(shipped_form, key, sums)
    assert good.returncode == 0 and 'Good "sautium-file" signature for release' in good.stdout, good.stderr
    assert verify(shipped_form, key, flip(sums, 3)).returncode != 0
    assert verify(key.line("release") + "\n", key, sums).returncode != 0           # listed for `git` only
    assert verify(shipped_form, recovery, sums).returncode != 0


def test_the_shipped_list_holds_the_owners_two_keys_for_git(caplog):
    """desktop/release_signers as it ships: every line parses, both keys are
    allowed for `git`, and they are the keys the owner named (2026-11-11) —
    the security key's and the recovery key's fingerprints. The daily key also
    signs the installers' SHA256SUMS (`sautium-file`, the owner 2026-11-14);
    the recovery key signs nothing but a rotation."""
    import hashlib
    text = (Path(__file__).parent.parent / "desktop" / "release_signers").read_text()
    listed = rv.parse_allowed_signers(text, "shipped")
    assert "refused" not in caplog.text
    fingerprints = {s.principals: base64.b64encode(hashlib.sha256(s.key_blob).digest()).decode().rstrip("=")
                    for s in listed}
    assert fingerprints == {("release",): "VfUGpBDGFCxAw2J4n8WNeFJj/LcrTac5Zgp73egmpqU",
                            ("recovery",): "a6UW2+JUmY9Ak8b0u62XJchKL3yrPj7D/mHb/PAUCrY"}
    assert {s.principals: s.namespaces for s in listed} == {("release",): ("git", "sautium-file"),
                                                           ("recovery",): ("git",)}
    assert [s.key_type for s in listed] == [rv.SK_ED25519, rv.ED25519]


def test_the_shipped_security_key_is_the_one_that_made_the_fixture():
    shipped = next(s for s in rv.parse_allowed_signers(
        (Path(__file__).parent.parent / "desktop" / "release_signers").read_text()) if s.key_type == rv.SK_ED25519)
    listed, message, sig = hardware()
    assert shipped.key_blob == listed[0].key_blob


def test_the_published_copies_of_the_daily_keys_line_are_the_shipped_one():
    """The README and docs/RELEASING.md publish the line a person verifies the
    installers' checksums with: the very line the launcher ships."""
    root = Path(__file__).parent.parent
    shipped = (root / "desktop" / "release_signers").read_text().splitlines()
    release = next(line for line in shipped if line.startswith("release "))
    assert " ".join(release.split()[:4]) in (root / "README.md").read_text()
    releasing = (root / "docs" / "RELEASING.md").read_text()
    assert release in releasing and next(line for line in shipped if line.startswith("recovery ")) in releasing
