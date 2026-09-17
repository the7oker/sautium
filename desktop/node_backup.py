"""Node backup — format v1 (`.sbk`) and the database drivers around it.

Product A of docs/design/BACKUP.md: one encrypted container holding a
`pg_dump` of the node's own data (the MusicBrainz layer rides along as empty
tables) and the identity documents. Shared by the launcher (restore flow,
`desktop/restore.py`) and the backend (`backend/backup.py`: the job behind
Settings > Library > Backup, and the `python -m backup` CLI in the container)
the way `db_init` is: one implementation, imported by both.

The file
    magic "SAUTIUM-BACKUP\\0" · version u16 · header length u32 · header JSON
    then chunk frames: [ciphertext length u32][ciphertext] until EOF.

Keys. KEK = Argon2id(password, salt "sautium-backup:v1:<username>") with the
identity parameters (t=4, m=256 MiB, p=2). The salt domain differs from the
identity seed's ("<username>:sautium"), so neither key can be turned into the
other — same password, two unrelated keys. The KEK only wraps a random
per-file data key (envelope): the key is deterministic, the ciphertext never
is, and a second recipient (a printed recovery key, later) is one more
wrapped copy in the header list.

Chunks. XChaCha20-Poly1305 (libsodium's IETF AEAD through PyNaCl) with a
24-byte nonce = random 16-byte prefix || chunk counter (u64), unique by
construction under a per-file key, and the sha256 of the header bytes as
associated data on EVERY chunk — a header edited after the fact fails the
first chunk before anything inside is trusted. The design doc named
SecretBox; it has no associated-data slot, which is the whole reason for
this AEAD. The reader keeps its own counter, so a reordered, duplicated or
dropped chunk fails authentication; a file cut short fails as "truncated"
because the END record is authenticated plaintext the reader insists on.

Members. Each chunk's plaintext is one record: [type u8][payload] —
MEMBER_START {name}, DATA bytes (up to chunk_size), MEMBER_END {size,
sha256}, END. That replaces the tar the design sketched: a tar header
carries the member size up front, and pg_dump's size is unknown until it
exits, so a tar would have needed a plaintext spool — exactly what "never a
plaintext temp file" forbids. Member order: manifest.json first (so inspect
and every pre-check read one small chunk), db.dump, identity/<file>...

Never inside: the private Ed25519 seed (it derives from username +
password, which the restore asks for anyway — `node_ed25519.key` is
excluded at every level, the rotation archive's copies included), TLS
material (re-generated), the `mb_*` table DATA (re-loadable from the
MusicBrainz dump / slices; the schema is dumped so the restored database
is complete).
"""

import hashlib
import json
import logging
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Tuple

import psycopg2
import psycopg2.extensions
from psycopg2 import sql

logger = logging.getLogger(__name__)

MAGIC = b"SAUTIUM-BACKUP\0"
FORMAT_VERSION = 1
FILE_SUFFIX = ".sbk"
PART_SUFFIX = ".sbk.part"
CHUNK_SIZE = 16 * 1024 * 1024
HEADER_MAX = 64 * 1024
CONTROL_MAX = 64 * 1024
KEY_LEN = 32
TAG_LEN = 16
NONCE_PREFIX_LEN = 16
NONCE_LEN = 24
KDF_SALT_PREFIX = "sautium-backup:v1:"
WRAP_AAD = b"sautium-backup:v1:wrap:"

REC_END = 0
REC_MEMBER_START = 1
REC_DATA = 2
REC_MEMBER_END = 3

MANIFEST_MEMBER = "manifest.json"
DB_MEMBER = "db.dump"
IDENTITY_PREFIX = "identity/"

# The identity dir minus what a backup must never carry (module doc) and
# what is re-derived on restore (the public PEM comes with the key).
IDENTITY_EXCLUDE = frozenset({"node_ed25519.key", "node_ed25519.pub",
                              "tls_cert.pem", "tls_key.pem"})
EXCLUDED_TABLE_PATTERN = "mb_*"          # pg_dump pattern
EXCLUDED_TABLE_LIKE = "mb\\_%"           # the same set, in SQL
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
READ_BLOCK = 1024 * 1024
PG_PRIORITY_NICE = 10

ProgressFn = Callable[..., None]


class BackupError(Exception):
    """Anything that stops a backup or a restore; the message is for the user."""


class WrongPassword(BackupError):
    pass


class Tampered(BackupError):
    """Authentication failed, or the stream is truncated / reordered."""


class Refused(BackupError):
    """A pre-check said no: newer schema, an existing database, a foreign identity."""


class Cancelled(BackupError):
    pass


# ---------------------------------------------------------------------------
# KDF + envelope
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class KdfParams:
    time_cost: int = 4
    memory_cost: int = 262144      # KiB — 256 MiB, the identity's parameters
    parallelism: int = 2

    def header(self, username: str) -> dict:
        return {"alg": "argon2id", "t": self.time_cost, "m": self.memory_cost,
                "p": self.parallelism, "salt": KDF_SALT_PREFIX + username}


DEFAULT_KDF = KdfParams()


def derive_kek(password: str, username: str, kdf: KdfParams = DEFAULT_KDF) -> bytes:
    from argon2.low_level import Type, hash_secret_raw
    return hash_secret_raw(
        secret=password.encode("utf-8"),
        salt=(KDF_SALT_PREFIX + username).encode("utf-8"),
        time_cost=kdf.time_cost, memory_cost=kdf.memory_cost,
        parallelism=kdf.parallelism, hash_len=KEY_LEN, type=Type.ID)


def _aead():
    from nacl import bindings
    return (bindings.crypto_aead_xchacha20poly1305_ietf_encrypt,
            bindings.crypto_aead_xchacha20poly1305_ietf_decrypt)


def _wrap_aad(node: dict) -> bytes:
    return WRAP_AAD + node["username"].encode("utf-8") + node["pubkey"].encode("ascii")


def _canonical(obj) -> bytes:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------

class BackupWriter:
    """Streams members into an open binary file. `kek` is the wrapping key
    (`derive_kek`) — the password itself never has to reach the thread that
    does the writing."""

    def __init__(self, fp, *, kek: bytes, username: str, pubkey: str,
                 kdf: KdfParams = DEFAULT_KDF, chunk_size: int = CHUNK_SIZE,
                 created_at: Optional[str] = None):
        self._fp = fp
        self._encrypt, _ = _aead()
        self._chunk_size = chunk_size
        self._key = os.urandom(KEY_LEN)
        self._prefix = os.urandom(NONCE_PREFIX_LEN)
        self._counter = 0
        self._file_hash = hashlib.sha256()
        self._members: List[dict] = []
        self._member: Optional[dict] = None
        self._buf = bytearray()
        self.plaintext_bytes = 0
        self.file_bytes = 0
        node = {"pubkey": pubkey.lower(), "username": username}
        wrap_nonce = os.urandom(NONCE_LEN)
        wrapped = self._encrypt(self._key, _wrap_aad(node), wrap_nonce, kek)
        self.header = {
            "kdf": kdf.header(username),
            "wrapped_keys": [{"kind": "password", "nonce": wrap_nonce.hex(),
                              "box": wrapped.hex()}],
            "chunk_size": chunk_size,
            "nonce_prefix": self._prefix.hex(),
            "node": node,
            "created_at": created_at or _now_iso(),
        }
        header_bytes = encode_header(self.header)
        self._aad = hashlib.sha256(header_bytes).digest()
        self._write(header_bytes)

    def _write(self, data: bytes) -> None:
        self._fp.write(data)
        self._file_hash.update(data)
        self.file_bytes += len(data)

    def _record(self, rec_type: int, payload: bytes) -> None:
        # Control records (member start/end, END) are small JSON; a data
        # record is at most one chunk. The reader bounds its allocation by
        # the same two numbers.
        if rec_type != REC_DATA and len(payload) > CONTROL_MAX:
            raise BackupError("control record too large")
        nonce = self._prefix + self._counter.to_bytes(8, "big")
        ct = self._encrypt(bytes([rec_type]) + payload, self._aad, nonce, self._key)
        self._counter += 1
        self._write(struct.pack(">I", len(ct)) + ct)

    def start_member(self, name: str) -> None:
        if self._member is not None:
            raise BackupError(f"member {self._member['name']} still open")
        self._member = {"name": name, "size": 0, "sha256": hashlib.sha256()}
        self._record(REC_MEMBER_START, _canonical({"name": name}))

    def write(self, data: bytes) -> None:
        if self._member is None:
            raise BackupError("write outside a member")
        self._member["size"] += len(data)
        self._member["sha256"].update(data)
        self.plaintext_bytes += len(data)
        self._buf += data
        while len(self._buf) >= self._chunk_size:
            self._record(REC_DATA, bytes(self._buf[:self._chunk_size]))
            del self._buf[:self._chunk_size]

    def end_member(self) -> dict:
        m = self._member
        if m is None:
            raise BackupError("no member open")
        if self._buf:
            self._record(REC_DATA, bytes(self._buf))
            self._buf.clear()
        done = {"name": m["name"], "size": m["size"], "sha256": m["sha256"].hexdigest()}
        self._record(REC_MEMBER_END, _canonical({"size": done["size"], "sha256": done["sha256"]}))
        self._members.append(done)
        self._member = None
        return done

    def add_member(self, name: str, data: bytes) -> dict:
        self.start_member(name)
        self.write(data)
        return self.end_member()

    def finish(self) -> dict:
        if self._member is not None:
            raise BackupError(f"member {self._member['name']} still open")
        self._record(REC_END, _canonical({"members": [m["name"] for m in self._members]}))
        self._fp.flush()
        return {"members": list(self._members), "sha256": self._file_hash.hexdigest(),
                "size": self.file_bytes}


def encode_header(header: dict) -> bytes:
    body = _canonical(header)
    return MAGIC + struct.pack(">HI", FORMAT_VERSION, len(body)) + body


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------

def read_header(fp) -> Tuple[dict, bytes]:
    """(header dict, raw header bytes). Validates the shape; no password."""
    lead = fp.read(len(MAGIC) + 6)
    if len(lead) < len(MAGIC) + 6 or lead[:len(MAGIC)] != MAGIC:
        raise BackupError("not a Sautium backup file")
    version, length = struct.unpack(">HI", lead[len(MAGIC):])
    if version != FORMAT_VERSION:
        raise Refused(f"backup format v{version} — this Sautium reads v{FORMAT_VERSION}; update first")
    if length > HEADER_MAX:
        raise Tampered("header length out of range")
    body = fp.read(length)
    if len(body) != length:
        raise Tampered("truncated header")
    try:
        header = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise Tampered("header is not JSON")
    try:
        node = header["node"]
        kdf = header["kdf"]
        ok = (isinstance(node["username"], str) and isinstance(node["pubkey"], str)
              and kdf["alg"] == "argon2id" and kdf["salt"] == KDF_SALT_PREFIX + node["username"]
              and isinstance(header["wrapped_keys"], list) and header["wrapped_keys"]
              and isinstance(header["chunk_size"], int) and 0 < header["chunk_size"] <= CHUNK_SIZE
              and len(bytes.fromhex(header["nonce_prefix"])) == NONCE_PREFIX_LEN)
    except (KeyError, TypeError, ValueError):
        ok = False
    if not ok:
        raise Tampered("malformed header")
    return header, lead + body


class _Member:
    def __init__(self, name: str, records: Iterator[Tuple[int, bytes]]):
        self.name = name
        self._records = records
        self._hash = hashlib.sha256()
        self.size = 0
        self.sha256: Optional[str] = None
        self.done = False

    def chunks(self) -> Iterator[bytes]:
        if self.done:
            return
        for rec_type, payload in self._records:
            if rec_type == REC_DATA:
                self.size += len(payload)
                self._hash.update(payload)
                yield payload
            elif rec_type == REC_MEMBER_END:
                end = json.loads(payload)
                if end.get("size") != self.size or end.get("sha256") != self._hash.hexdigest():
                    raise Tampered(f"member {self.name}: content does not match its digest")
                self.sha256 = end["sha256"]
                self.done = True
                return
            else:
                raise Tampered(f"member {self.name}: unexpected record {rec_type}")
        raise Tampered(f"member {self.name}: truncated")

    def read(self) -> bytes:
        return b"".join(self.chunks())

    def drain(self) -> None:
        for _ in self.chunks():
            pass


class BackupReader:
    """Sequential reader. `unlock` needs the password (or the KEK); then
    `members()` yields each member once, in file order — a consumer that
    skips one gets it drained (and verified) before the next arrives."""

    def __init__(self, fp):
        self._fp = fp
        _, self._decrypt = _aead()
        self.header, header_bytes = read_header(fp)
        self._aad = hashlib.sha256(header_bytes).digest()
        self._prefix = bytes.fromhex(self.header["nonce_prefix"])
        self._chunk_size = int(self.header["chunk_size"])
        self._key: Optional[bytes] = None
        self._counter = 0
        self._ended = False
        self.bytes_read = len(header_bytes)

    @property
    def node(self) -> dict:
        return self.header["node"]

    @property
    def created_at(self) -> str:
        return self.header.get("created_at", "")

    def unlock(self, password: str, kdf: Optional[KdfParams] = None) -> None:
        """Derive the KEK and unwrap the data key. The KDF parameters are the
        fixed v1 set: a header asking for more memory is refused before any
        derivation, so a crafted file cannot turn the check into a 16 GiB
        allocation."""
        expected = (kdf or DEFAULT_KDF).header(self.node["username"])
        if self.header["kdf"] != expected:
            raise Refused("unsupported key-derivation parameters in the header")
        self.unlock_with_kek(derive_kek(password, self.node["username"], kdf or DEFAULT_KDF))

    def unlock_with_kek(self, kek: bytes) -> None:
        from nacl.exceptions import CryptoError
        aad = _wrap_aad(self.node)
        for entry in self.header["wrapped_keys"]:
            if entry.get("kind") != "password":
                continue
            try:
                key = self._decrypt(bytes.fromhex(entry["box"]), aad,
                                    bytes.fromhex(entry["nonce"]), kek)
            except (CryptoError, ValueError, KeyError):
                continue
            if len(key) == KEY_LEN:
                self._key = key
                return
        raise WrongPassword("wrong password (or a damaged header)")

    def records(self) -> Iterator[Tuple[int, bytes]]:
        from nacl.exceptions import CryptoError
        if self._key is None:
            raise BackupError("unlock first")
        max_ct = 1 + max(self._chunk_size, CONTROL_MAX) + TAG_LEN
        while True:
            head = self._fp.read(4)
            if not head:
                raise Tampered("truncated: the stream ends before its END record")
            if len(head) != 4:
                raise Tampered("truncated chunk length")
            (length,) = struct.unpack(">I", head)
            if length > max_ct or length < 1 + TAG_LEN:
                raise Tampered("chunk length out of range")
            ct = self._fp.read(length)
            if len(ct) != length:
                raise Tampered("truncated chunk")
            self.bytes_read += 4 + length
            nonce = self._prefix + self._counter.to_bytes(8, "big")
            try:
                pt = self._decrypt(ct, self._aad, nonce, self._key)
            except CryptoError:
                raise Tampered(f"chunk {self._counter} failed authentication "
                               "(damaged, reordered, or the header was altered)")
            self._counter += 1
            rec_type, payload = pt[0], pt[1:]
            if rec_type == REC_END:
                if self._fp.read(1):
                    raise Tampered("data after the END record")
                self._ended = True
                return
            yield rec_type, payload

    def members(self) -> Iterator[_Member]:
        if self._ended:
            return
        records = self.records()
        for rec_type, payload in records:
            if rec_type != REC_MEMBER_START:
                raise Tampered(f"expected a member start, got record {rec_type}")
            member = _Member(json.loads(payload)["name"], records)
            yield member
            member.drain()

    def read_manifest(self) -> dict:
        """The first member, parsed. Leaves the reader on the second."""
        self._members_iter = self.members()
        first = next(self._members_iter, None)
        if first is None or first.name != MANIFEST_MEMBER:
            raise Tampered("the first member is not the manifest")
        return json.loads(first.read().decode("utf-8"))

    def remaining_members(self) -> Iterator[_Member]:
        it = getattr(self, "_members_iter", None)
        if it is None:
            raise BackupError("read the manifest first")
        return it


# ---------------------------------------------------------------------------
# Identity files
# ---------------------------------------------------------------------------

def identity_files(identity_dir: Optional[Path]) -> List[Tuple[str, Path]]:
    """(member name, path) for every file the backup carries out of the
    identity dir: the info, the certificate, the proof, the rotation archive
    and `.api_secret`; never a private key or TLS material, at any depth."""
    if identity_dir is None or not identity_dir.is_dir():
        return []
    out = []
    for path in sorted(identity_dir.rglob("*")):
        if not path.is_file() or path.name in IDENTITY_EXCLUDE:
            continue
        rel = path.relative_to(identity_dir).as_posix()
        if rel.startswith("replaced-"):
            continue
        out.append((IDENTITY_PREFIX + rel, path))
    return out


# ---------------------------------------------------------------------------
# PostgreSQL side
# ---------------------------------------------------------------------------

@dataclass
class PgTarget:
    """One database on one server, seen through two roles: `user` runs the
    app and owns the data; `admin` (a superuser — `postgres` on the launcher's
    cluster, the same `musicai` on Docker) creates, renames and drops
    databases from `maintenance_db`."""
    host: str
    port: int
    dbname: str
    user: str
    password: str
    admin_user: Optional[str] = None
    admin_password: Optional[str] = None
    maintenance_db: str = "postgres"
    pg_bin: Optional[Path] = None

    def connect(self, dbname: Optional[str] = None, *, admin: bool = False, **kw):
        return psycopg2.connect(
            host=self.host, port=self.port, dbname=dbname or self.dbname,
            user=(self.admin_user or self.user) if admin else self.user,
            password=(self.admin_password if self.admin_user else self.password) if admin else self.password,
            **kw)

    def tool(self, name: str) -> str:
        exe = name + (".exe" if sys.platform == "win32" else "")
        if self.pg_bin:
            candidate = Path(self.pg_bin) / exe
            if candidate.exists():
                return str(candidate)
        found = shutil.which(name)
        if not found:
            raise BackupError(f"{name} not found — set PG_BIN to the PostgreSQL bin directory")
        return found

    def tool_env(self, *, admin: bool = False) -> dict:
        env = os.environ.copy()
        env["PGPASSWORD"] = (self.admin_password or "") if admin and self.admin_user else self.password
        env["PGCLIENTENCODING"] = "UTF8"
        return env


def _spawn_kwargs(low_priority: bool) -> dict:
    kw: dict = {}
    if sys.platform == "win32":
        flags = subprocess.CREATE_NO_WINDOW
        if low_priority:
            flags |= subprocess.BELOW_NORMAL_PRIORITY_CLASS
        kw["creationflags"] = flags
    elif low_priority:
        kw["preexec_fn"] = lambda: os.nice(PG_PRIORITY_NICE)
    return kw


def _stderr_tail(tmp, limit: int = 2000) -> str:
    tmp.seek(0)
    data = tmp.read()
    return data[-limit:].decode("utf-8", "replace").strip()


def own_tables(conn) -> List[str]:
    with conn.cursor() as cur:
        cur.execute("""SELECT tablename FROM pg_tables
                        WHERE schemaname = 'public' AND tablename NOT LIKE %s
                        ORDER BY tablename""", (EXCLUDED_TABLE_LIKE,))
        return [r[0] for r in cur.fetchall()]


def table_counts(conn, tables: List[str]) -> Dict[str, int]:
    counts = {}
    with conn.cursor() as cur:
        for t in tables:
            cur.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(t)))
            counts[t] = int(cur.fetchone()[0])
    return counts


def _database_facts(conn, dbname: str) -> dict:
    with conn.cursor() as cur:
        cur.execute("SHOW server_version")
        server_version = cur.fetchone()[0]
        cur.execute("""SELECT datlocprovider, datcollate, datctype, datlocale,
                              pg_encoding_to_char(encoding)
                         FROM pg_database WHERE datname = %s""", (dbname,))
        prov, collate, ctype, loc, enc = cur.fetchone()
        cur.execute("SELECT extname FROM pg_extension WHERE extname <> 'plpgsql' ORDER BY 1")
        extensions = [r[0] for r in cur.fetchall()]
        cur.execute("""SELECT tablename FROM pg_tables
                        WHERE schemaname = 'public' AND tablename LIKE %s ORDER BY 1""",
                    (EXCLUDED_TABLE_LIKE,))
        excluded = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT filename FROM _schema_migrations ORDER BY id")
        migrations = [r[0] for r in cur.fetchall()]
    return {
        "server_version": server_version,
        "locale": {"provider": prov, "collate": collate, "ctype": ctype,
                   "locale": loc, "encoding": enc},
        "extensions": extensions,
        "excluded": excluded,
        "migrations": migrations,
    }


def code_migrations() -> List[str]:
    return sorted(p.name for p in MIGRATIONS_DIR.glob("*.sql"))


def code_identity_rule() -> Optional[int]:
    """`uuid_utils.IDENTITY_RULE` of the backend beside this package: the
    backend process has it on its path; the launcher reads the file."""
    try:
        import uuid_utils
        return int(uuid_utils.IDENTITY_RULE)
    except ImportError:
        pass
    import re
    path = Path(__file__).resolve().parent.parent / "backend" / "uuid_utils.py"
    try:
        m = re.search(r"^IDENTITY_RULE\s*=\s*(\d+)", path.read_text(encoding="utf-8"), re.M)
    except OSError:
        return None
    return int(m.group(1)) if m else None


def check_compatible(manifest: dict, *, migrations: Optional[List[str]] = None,
                     identity_rule: Optional[int] = None) -> None:
    """A dump written by newer code must not land under older code: the
    migration runner would skip files it does not know about, and the
    identity-rule pass would re-normalize a newer rule's data down to an
    older one and mark it done. Refuse with 'update first'."""
    known = set(code_migrations() if migrations is None else migrations)
    applied = list(manifest.get("migrations") or [])
    unknown = [m for m in applied if m.endswith(".sql") and m not in known]
    if unknown:
        raise Refused(f"the backup was written by a newer Sautium ({unknown[0]} is "
                      "unknown here) — update, then restore")
    rule = code_identity_rule() if identity_rule is None else identity_rule
    rules = [int(m[len("identity_rule_v"):]) for m in applied
             if m.startswith("identity_rule_v") and m[len("identity_rule_v"):].isdigit()]
    if rule is not None and rules and max(rules) > rule:
        raise Refused(f"the backup carries identity rule v{max(rules)}; this Sautium "
                      f"is at v{rule} — update, then restore")


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

def backup_filename(pubkey: str, when: Optional[datetime] = None) -> str:
    when = when or datetime.now(timezone.utc)
    return f"sautium-backup-{pubkey[:12]}-{when.strftime('%Y-%m-%d')}{FILE_SUFFIX}"


def _unique_path(directory: Path, name: str) -> Path:
    path = directory / name
    if not path.exists():
        return path
    stem = name[:-len(FILE_SUFFIX)]
    return directory / f"{stem}-{datetime.now(timezone.utc).strftime('%H%M%S')}{FILE_SUFFIX}"


def create_backup(out_dir: Path, *, target: PgTarget, kek: bytes, username: str,
                  pubkey: str, identity_dir: Optional[Path] = None,
                  app_commit: Optional[str] = None, app_build: Optional[str] = None,
                  progress: Optional[ProgressFn] = None,
                  cancel: Optional[threading.Event] = None,
                  wait_ok: Optional[Callable[[], None]] = None,
                  kdf: KdfParams = DEFAULT_KDF, chunk_size: int = CHUNK_SIZE) -> dict:
    """Write `<out_dir>/sautium-backup-<node>-<date>.sbk`. The row counts in
    the manifest and the dump come from ONE snapshot (`pg_export_snapshot` →
    `pg_dump --snapshot`), so a restore can be checked against them exactly
    even while the node keeps writing. `wait_ok()` is called before every
    read and blocks while the caller wants the job paused (the load meter's
    playback hold); `cancel` ends the job and removes the partial file.

    Returns {"path", "size", "sha256", "manifest"}."""
    progress = progress or (lambda *_a, **_k: None)
    cancel = cancel or threading.Event()
    wait_ok = wait_ok or (lambda: None)
    out_dir.mkdir(parents=True, exist_ok=True)
    final_path = _unique_path(out_dir, backup_filename(pubkey))
    part_path = final_path.with_name(final_path.name[:-len(FILE_SUFFIX)] + PART_SUFFIX)

    def check_cancel() -> None:
        if cancel.is_set():
            raise Cancelled("backup cancelled")

    progress("counting")
    conn = target.connect()
    proc = None
    stderr = tempfile.TemporaryFile()
    try:
        conn.set_session(isolation_level=psycopg2.extensions.ISOLATION_LEVEL_REPEATABLE_READ,
                         readonly=True, autocommit=False)
        with conn.cursor() as cur:
            cur.execute("SELECT pg_export_snapshot()")
            snapshot = cur.fetchone()[0]
        facts = _database_facts(conn, target.dbname)
        migrations = facts.pop("migrations")
        tables = own_tables(conn)
        counts = table_counts(conn, tables)
        check_cancel()
        manifest = {
            "format_version": FORMAT_VERSION,
            "created_at": _now_iso(),
            "app": {"commit": app_commit, "build": app_build},
            "node": {"pubkey": pubkey.lower(), "username": username},
            "database": {"name": target.dbname, "snapshot": snapshot, "tables": counts,
                         **facts},
            "migrations": migrations,
            "identity_files": [name[len(IDENTITY_PREFIX):] for name, _ in identity_files(identity_dir)],
        }

        with open(part_path, "wb") as fp:
            writer = BackupWriter(fp, kek=kek, username=username, pubkey=pubkey,
                                  kdf=kdf, chunk_size=chunk_size,
                                  created_at=manifest["created_at"])
            writer.add_member(MANIFEST_MEMBER, _canonical(manifest))

            progress("dumping", bytes=0)
            cmd = [target.tool("pg_dump"), "-h", target.host, "-p", str(target.port),
                   "-U", target.user, "-d", target.dbname, "-Fc",
                   f"--exclude-table-data={EXCLUDED_TABLE_PATTERN}",
                   f"--snapshot={snapshot}"]
            wait_ok()
            check_cancel()
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=stderr,
                                    env=target.tool_env(), **_spawn_kwargs(True))
            writer.start_member(DB_MEMBER)
            last_report = time.monotonic()
            while True:
                wait_ok()
                check_cancel()
                block = proc.stdout.read(READ_BLOCK)
                if not block:
                    break
                writer.write(block)
                now = time.monotonic()
                if now - last_report >= 0.5:
                    last_report = now
                    progress("dumping", bytes=writer.plaintext_bytes)
            rc = proc.wait()
            if rc != 0:
                raise BackupError(f"pg_dump failed (exit {rc}): {_stderr_tail(stderr)}")
            dump = writer.end_member()
            progress("dumping", bytes=writer.plaintext_bytes)

            progress("identity")
            for name, path in identity_files(identity_dir):
                check_cancel()
                writer.add_member(name, path.read_bytes())
            summary = writer.finish()
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(part_path, final_path)
    except BaseException:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait()
        part_path.unlink(missing_ok=True)
        raise
    finally:
        stderr.close()
        try:
            conn.rollback()
        finally:
            conn.close()
    logger.info("backup written: %s (%d bytes, dump %d bytes)", final_path,
                summary["size"], dump["size"])
    return {"path": final_path, "size": summary["size"], "sha256": summary["sha256"],
            "dump_size": dump["size"], "manifest": manifest}


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------

OWN_DATA_TABLES = ("media_files", "listening_history", "demo_plays", "friends", "p2p_messages",
                   "chat_messages", "user_gear")


def _db_exists(admin_conn, dbname: str) -> bool:
    with admin_conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,))
        return cur.fetchone() is not None


def has_own_data(conn) -> bool:
    """Whether a database holds anything a restore would destroy: owned
    files, listens, friends, chat, gear. A fresh node (schema, seed, settings)
    has none of these and may be replaced without asking twice."""
    with conn.cursor() as cur:
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        present = {r[0] for r in cur.fetchall()}
        for t in OWN_DATA_TABLES:
            if t not in present:
                continue
            cur.execute(sql.SQL("SELECT EXISTS (SELECT 1 FROM {})").format(sql.Identifier(t)))
            if cur.fetchone()[0]:
                return True
    return False


def _terminate_sessions(admin_conn, dbname: str) -> None:
    with admin_conn.cursor() as cur:
        cur.execute("""SELECT pg_terminate_backend(pid) FROM pg_stat_activity
                        WHERE datname = %s AND pid <> pg_backend_pid()""", (dbname,))


def _drop_database(admin_conn, dbname: str) -> None:
    _terminate_sessions(admin_conn, dbname)
    with admin_conn.cursor() as cur:
        cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(dbname)))


def _rename_database(admin_conn, old: str, new: str) -> None:
    """Sessions are terminated first; a client that reconnected in between
    (the backend is stopped by contract, but a psql or an MCP server may
    not be) gets one more termination before the error stands."""
    stmt = sql.SQL("ALTER DATABASE {} RENAME TO {}").format(sql.Identifier(old), sql.Identifier(new))
    for attempt in (1, 2):
        _terminate_sessions(admin_conn, old)
        try:
            with admin_conn.cursor() as cur:
                cur.execute(stmt)
            return
        except psycopg2.errors.ObjectInUse:
            if attempt == 2:
                raise


def _locale_clause(locale: Optional[dict]):
    if not locale:
        return None
    prov = locale.get("provider")
    if prov == "i":
        return sql.SQL(" LOCALE_PROVIDER icu ICU_LOCALE {} LC_COLLATE {} LC_CTYPE {}").format(
            sql.Literal(locale.get("locale") or "und"),
            sql.Literal(locale.get("collate") or "C"),
            sql.Literal(locale.get("ctype") or "C"))
    if prov == "b":
        return sql.SQL(" LOCALE_PROVIDER builtin BUILTIN_LOCALE {}").format(
            sql.Literal(locale.get("locale") or "C.UTF-8"))
    if prov == "c" and locale.get("collate"):
        return sql.SQL(" LC_COLLATE {} LC_CTYPE {}").format(
            sql.Literal(locale["collate"]), sql.Literal(locale.get("ctype") or locale["collate"]))
    return None


def _database_locale(admin_conn, dbname: str) -> Optional[dict]:
    with admin_conn.cursor() as cur:
        cur.execute("""SELECT datlocprovider, datcollate, datctype, datlocale
                         FROM pg_database WHERE datname = %s""", (dbname,))
        row = cur.fetchone()
    if row is None:
        return None
    return {"provider": row[0], "collate": row[1], "ctype": row[2], "locale": row[3]}


def _create_database(admin_conn, dbname: str, owner: str, locale: Optional[dict],
                     fallback: Optional[dict] = None) -> None:
    """The dump carries no CREATE DATABASE (it is restored under whatever
    name the caller picked), so the collation comes from the manifest — a
    clone keeps its sort order. A locale the target OS does not have (a
    Docker `en_US.utf8` dump restored on Windows) falls back to the
    database being replaced (the launcher's ICU `und`, which is what makes
    lower() fold Cyrillic there), then to the cluster default; indexes are
    rebuilt by the restore either way."""
    base = sql.SQL("CREATE DATABASE {} OWNER {} TEMPLATE template0 ENCODING 'UTF8'").format(
        sql.Identifier(dbname), sql.Identifier(owner))
    attempts = [(base + clause, label) for clause, label in
                ((_locale_clause(locale), "the dump's locale"),
                 (_locale_clause(fallback), "the replaced database's locale"))
                if clause is not None]
    attempts.append((base, "the cluster default"))
    with admin_conn.cursor() as cur:
        for i, (stmt, label) in enumerate(attempts):
            try:
                cur.execute(stmt)
                if i:
                    logger.info("database %s created with %s", dbname, label)
                return
            except psycopg2.Error as e:
                if i == len(attempts) - 1:
                    raise
                logger.warning("CREATE DATABASE with %s failed (%s) — trying %s", label,
                               str(e).strip().splitlines()[0], attempts[i + 1][1])


def restore_database(reader: BackupReader, target: PgTarget, *, manifest: Optional[dict] = None,
                     replace: bool = False, progress: Optional[ProgressFn] = None,
                     file_size: Optional[int] = None,
                     identity_sink: Optional[Callable[[str, bytes], None]] = None) -> dict:
    """Rebuild `target.dbname` from an unlocked reader.

    The dump lands in `<dbname>__restore` first; only a complete, migrated
    restore is swapped into place, so a failure half-way leaves the node's
    database untouched. A database that holds own data is replaced only
    with `replace=True`, and then kept as `<dbname>__previous` (one copy —
    the next restore drops the older one); a fresh database is dropped.

    `identity_sink(relative path, bytes)` receives the identity members as
    they stream by (they follow the dump); `write_identity` applies them."""
    progress = progress or (lambda *_a, **_k: None)
    manifest = manifest or reader.read_manifest()
    check_compatible(manifest)
    if not target.admin_user:
        target.admin_user, target.admin_password = target.user, target.password

    admin = target.connect(target.maintenance_db, admin=True)
    admin.autocommit = True
    staging = f"{target.dbname}__restore"
    previous = f"{target.dbname}__previous"
    kept_previous: Optional[str] = None
    original_moved = False
    try:
        exists = _db_exists(admin, target.dbname)
        had_data = False
        if exists:
            probe = target.connect()
            try:
                had_data = has_own_data(probe)
            finally:
                probe.close()
            if had_data and not replace:
                raise Refused(f"database {target.dbname} already holds this node's data "
                              "— restoring replaces it (pass --replace / confirm)")

        progress("preparing")
        _drop_database(admin, staging)
        _create_database(admin, staging, target.user, (manifest.get("database") or {}).get("locale"),
                         fallback=_database_locale(admin, target.dbname) if exists else None)
        stage_admin = target.connect(staging, admin=True)
        stage_admin.autocommit = True
        try:
            with stage_admin.cursor() as cur:
                for ext in (manifest.get("database") or {}).get("extensions") or []:
                    cur.execute(sql.SQL("CREATE EXTENSION IF NOT EXISTS {}").format(sql.Identifier(ext)))
        finally:
            stage_admin.close()

        progress("restoring", bytes=reader.bytes_read, total=file_size)
        stderr = tempfile.TemporaryFile()
        cmd = [target.tool("pg_restore"), "-h", target.host, "-p", str(target.port),
               "-U", target.user, "-d", staging, "--no-owner", "--no-privileges",
               "--no-comments", "--exit-on-error"]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=stderr,
                                env=target.tool_env(), **_spawn_kwargs(False))
        dump_seen = False
        identity_seen: List[str] = []
        try:
            for member in reader.remaining_members():
                if member.name == DB_MEMBER:
                    dump_seen = True
                    last_report = time.monotonic()
                    try:
                        for block in member.chunks():
                            proc.stdin.write(block)
                            now = time.monotonic()
                            if now - last_report >= 0.5:
                                last_report = now
                                progress("restoring", bytes=reader.bytes_read, total=file_size)
                    except BrokenPipeError:
                        pass
                    proc.stdin.close()
                    rc = proc.wait()
                    if rc != 0:
                        raise BackupError(f"pg_restore failed (exit {rc}): {_stderr_tail(stderr)}")
                elif member.name.startswith(IDENTITY_PREFIX):
                    rel = member.name[len(IDENTITY_PREFIX):]
                    if ".." in rel.split("/") or rel.startswith("/"):
                        raise Tampered(f"identity member with an unsafe path: {rel}")
                    data = member.read()
                    identity_seen.append(rel)
                    if identity_sink is not None:
                        identity_sink(rel, data)
                else:
                    raise Tampered(f"unexpected member {member.name}")
            if not dump_seen:
                raise Tampered("the backup has no database dump")
        except BaseException:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            raise
        finally:
            stderr.close()
        progress("restoring", bytes=reader.bytes_read, total=file_size)

        progress("migrating")
        from desktop import db_init
        app_conn = target.connect(staging)
        app_conn.autocommit = False
        try:
            applied = db_init.apply_migrations(app_conn)
        finally:
            app_conn.close()

        progress("swapping")
        if exists:
            if had_data:
                _drop_database(admin, previous)
                _rename_database(admin, target.dbname, previous)
                original_moved = True
                kept_previous = previous
            else:
                _drop_database(admin, target.dbname)
        _rename_database(admin, staging, target.dbname)
    except BaseException:
        # Leave the node exactly as it was: the original back under its name
        # if the swap had moved it, the staging copy gone either way.
        if original_moved:
            try:
                _rename_database(admin, previous, target.dbname)
            except psycopg2.Error as e:
                logger.error("could not move %s back to %s: %s", previous, target.dbname, e)
        try:
            _drop_database(admin, staging)
        except psycopg2.Error as e:
            logger.warning("could not drop %s after a failed restore: %s", staging, e)
        raise
    finally:
        admin.close()
    logger.info("restored %s from backup of %s (%s); %d migration(s) applied; previous=%s",
                target.dbname, manifest["node"]["username"], manifest["created_at"],
                applied, kept_previous)
    return {"database": target.dbname, "previous": kept_previous,
            "migrations_applied": applied, "identity_files": identity_seen,
            "manifest": manifest}


def write_identity(identity_dir: Path, files: Dict[str, bytes], *, username: str,
                   password: Optional[str]) -> dict:
    """Make this machine the node the backup came from. Identity files that
    belong to a DIFFERENT key are moved aside (`replaced-<time>/`), never
    deleted; the same key's files are overwritten in place. With
    `node_info.json` in the set and a password, the private key is
    re-derived and written like `create_account` does — nothing is written
    unless the derivation reproduces the recorded public key."""
    from desktop import node_identity

    identity_dir.mkdir(parents=True, exist_ok=True)
    info = None
    if "node_info.json" in files:
        info = json.loads(files["node_info.json"].decode("utf-8"))
    incoming_pub = (info or {}).get("public_key_hex", "").lower()

    private_key = None
    if info is not None and info.get("username"):
        if not password:
            raise Refused("the account password is needed to re-derive the node key")
        private_key, derived_pub, _ = node_identity.derive_account_identity(
            info["username"], password)
        if derived_pub.lower() != incoming_pub:
            raise Refused("this password does not derive the identity in the backup")

    live_info = identity_dir / "node_info.json"
    moved = None
    if live_info.exists():
        try:
            live_pub = json.loads(live_info.read_text(encoding="utf-8")).get("public_key_hex", "").lower()
        except (OSError, ValueError):
            live_pub = ""
        if live_pub and live_pub != incoming_pub:
            moved = identity_dir / ("replaced-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
            moved.mkdir()
            for entry in list(identity_dir.iterdir()):
                if entry == moved or entry.name.startswith("replaced-"):
                    continue
                entry.rename(moved / entry.name)
            logger.info("existing identity %s… moved to %s", live_pub[:16], moved.name)

    for rel, data in files.items():
        path = identity_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if rel == ".api_secret" or rel.endswith(".key"):
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
    if private_key is not None:
        node_identity._save_keypair(private_key, info, identity_dir)
    return {"pubkey": incoming_pub or None, "username": (info or {}).get("username") or username,
            "moved_previous_identity": moved.name if moved else None,
            "files": sorted(files)}


# ---------------------------------------------------------------------------
# Inspect + selftest
# ---------------------------------------------------------------------------

def inspect_file(path: Path, password: Optional[str] = None,
                 kdf: Optional[KdfParams] = None) -> dict:
    """Header facts, plus the manifest when a password is given."""
    out = {"path": str(path), "size": path.stat().st_size}
    with open(path, "rb") as fp:
        reader = BackupReader(fp)
        out["node"] = reader.node
        out["created_at"] = reader.created_at
        if password is not None:
            reader.unlock(password, kdf)
            out["manifest"] = reader.read_manifest()
    return out


def selftest(target: PgTarget, *, test_db: str, identity_dir: Optional[Path],
             pubkey: str, username: str, out_dir: Optional[Path] = None,
             keep: bool = False, log: Callable[[str], None] = print) -> bool:
    """Dump this node → restore into `test_db` → compare every table's rows
    with the manifest → drop the test database. Returns True when every
    count matches. The schema-migration ledger is compared separately: a
    restore under newer code legitimately grows it."""
    import secrets
    password = secrets.token_urlsafe(24)
    out_dir = out_dir or Path(tempfile.mkdtemp(prefix="sautium-selftest-"))
    t0 = time.monotonic()
    log(f"[selftest] backing up {target.dbname} → {out_dir}")

    def show(phase, **f):
        if phase in ("dumping", "restoring") and f.get("bytes") is not None:
            mb = f["bytes"] / 1e6
            total = f.get("total")
            log(f"[selftest] {phase} {mb:,.0f} MB" + (f" / {total / 1e6:,.0f} MB" if total else ""))
        else:
            log(f"[selftest] {phase}")

    result = create_backup(out_dir, target=target, kek=derive_kek(password, username),
                           username=username, pubkey=pubkey, identity_dir=identity_dir,
                           progress=show)
    path = result["path"]
    log(f"[selftest] backup {path.name}: {result['size'] / 1e6:,.0f} MB in {time.monotonic() - t0:,.0f} s")

    test_target = PgTarget(host=target.host, port=target.port, dbname=test_db,
                           user=target.user, password=target.password,
                           admin_user=target.admin_user, admin_password=target.admin_password,
                           maintenance_db=target.maintenance_db, pg_bin=target.pg_bin)
    # A test database left by an earlier --keep is dropped outright — the
    # "kept as __previous" courtesy is for a node's own database, not a copy.
    admin = test_target.connect(test_target.maintenance_db, admin=True)
    admin.autocommit = True
    try:
        _drop_database(admin, test_db)
        _drop_database(admin, test_db + "__previous")
    finally:
        admin.close()
    identity: Dict[str, bytes] = {}
    t1 = time.monotonic()
    with open(path, "rb") as fp:
        reader = BackupReader(fp)
        reader.unlock(password)
        manifest = reader.read_manifest()
        restored = restore_database(reader, test_target, manifest=manifest, replace=True,
                                    progress=show, file_size=path.stat().st_size,
                                    identity_sink=identity.__setitem__)
    log(f"[selftest] restored into {test_db} in {time.monotonic() - t1:,.0f} s "
        f"({restored['migrations_applied']} newer migration(s) applied)")

    ok = True
    conn = test_target.connect()
    try:
        got = table_counts(conn, own_tables(conn))
    finally:
        conn.close()
    expected = manifest["database"]["tables"]
    for table in sorted(set(expected) | set(got)):
        e, g = expected.get(table), got.get(table)
        if table == "_schema_migrations":
            if g != e:
                log(f"[selftest] {table}: {e} → {g} (newer migrations recorded)")
            continue
        if e != g:
            ok = False
            log(f"[selftest] MISMATCH {table}: manifest {e} restored {g}")
    if manifest["identity_files"] != sorted(identity):
        ok = False
        log(f"[selftest] MISMATCH identity files: {manifest['identity_files']} vs {sorted(identity)}")
    log(f"[selftest] {len(expected)} tables compared — {'OK' if ok else 'FAILED'}")

    if not keep:
        admin = test_target.connect(test_target.maintenance_db, admin=True)
        admin.autocommit = True
        try:
            _drop_database(admin, test_db)
        finally:
            admin.close()
        path.unlink(missing_ok=True)
        log(f"[selftest] dropped {test_db}, removed {path.name}")
    return ok
