"""Test helpers for catalogue slices, which count under their author's birth
certificate (desktop/p2p/slice_trust.entry_certificate):

- `trust(monkeypatch)`: a birth authority of the tests' own becomes the one
  the node trusts, in both mirrors (desktop/p2p/birth_cert.py, and
  backend/birth_authority.py where it imports).
- `certificate(pubkey, method, email_token)`: a v4 certificate it signed —
  an `email` one carries a mailbox token of the key's own unless one is
  given, a `pow` one none; `proof(cert)` binds a proof to a `pow` one. Its
  memory-hard evaluation is the identity registry's: a test that reaches it
  replaces identity_proof.verify_proof (or identity_registry.verify_relayed).
- `certified(entry, cert, proof)`: a served entry carrying its author's
  certificate; `mb_entry` / `lb_entry` sign a slice with a key and certify
  it under that key's own email certificate unless told otherwise.
- `Subtree`: one artist's MB slice as a dump node's walk builds it.
- `Peer`: a peer answering slice exchanges from the entries it holds, its
  TLS channel having proved a key.
"""

import base64
import gzip
import hashlib
import importlib
import itertools
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from desktop.p2p import lb_slice_queries, mb_slice_queries

AUTHORITY = Ed25519PrivateKey.generate()
ISSUED_AT = "2026-10-01T12:00:00Z"


def pub(key: Ed25519PrivateKey) -> str:
    return key.public_key().public_bytes_raw().hex()


AUTHORITY_PUB = pub(AUTHORITY)


def trust(monkeypatch, authority: Ed25519PrivateKey = AUTHORITY) -> None:
    from desktop.p2p import birth_cert
    trusted = [pub(authority)]
    monkeypatch.setattr(birth_cert, "TRUSTED_AUTHORITIES", trusted)
    for name in ("birth_authority", "backend.birth_authority"):
        try:
            mirror = importlib.import_module(name)
        except ImportError:
            continue
        monkeypatch.setattr(mirror, "TRUSTED_AUTHORITIES", trusted)


def certificate(pubkey: str, method: str = "email", email_token: Optional[str] = None, *,
                authority: Ed25519PrivateKey = AUTHORITY, issued_at: str = ISSUED_AT) -> dict:
    from desktop.p2p import birth_cert
    pubkey = pubkey.lower()
    email = method == "email"
    cert = {"v": birth_cert.CERT_VERSION, "pubkey": pubkey, "issued_at": issued_at,
            "method": method, "difficulty": 32, "params_version": 1,
            "email_token": (email_token or hashlib.sha256(b"mailbox of " + pubkey.encode()).hexdigest())
                           if email else None,
            "email_class": "major" if email else None,
            "email_domain_token": None, "predecessor": None,
            "issuer": pub(authority)}
    cert["sig"] = authority.sign(birth_cert.canonical_payload(cert)).hex()
    return cert


def proof(cert: dict) -> dict:
    """A proof bound to `cert` in shape — its work is not done."""
    from desktop.p2p import identity_pow, identity_proof
    return {"v": identity_proof.PROOF_VERSION, "pubkey": cert["pubkey"], "cert_sig": cert["sig"],
            "nonce": "00" * identity_pow.NONCE_LEN, "difficulty": cert["difficulty"],
            "params_version": cert["params_version"]}


def certified(entry: dict, cert: Optional[dict], proof_: Optional[dict] = None) -> dict:
    return {**entry, "author_cert": cert, "author_proof": proof_}


def uncertified(entry: dict) -> dict:
    return {k: v for k, v in entry.items() if k not in ("author_cert", "author_proof")}


def _gz64(blob: bytes) -> str:
    return base64.b64encode(gzip.compress(blob)).decode("ascii")


def mb_entry(key: Ed25519PrivateKey, name: str, one: dict, cert: Optional[dict] = None,
             proof_: Optional[dict] = None) -> dict:
    blob = mb_slice_queries.name_blob(name, one)
    entry = {"dump_version": one["dump_version"], "author_pubkey": pub(key),
             "sig": key.sign(mb_slice_queries.receipt_message_for(blob)).hex(),
             "blob_gz": _gz64(blob)}
    return certified(entry, cert or certificate(pub(key)), proof_)


def lb_entry(key: Ed25519PrivateKey, mbid: str, one: dict, cert: Optional[dict] = None,
             proof_: Optional[dict] = None) -> dict:
    blob = lb_slice_queries.slice_blob(mbid, one)
    entry = {"dump_version": one["dump_version"], "author_pubkey": pub(key),
             "sig": key.sign(lb_slice_queries.receipt_message_for(blob)).hex(),
             "blob_gz": _gz64(blob)}
    return certified(entry, cert or certificate(pub(key)), proof_)


def dump_version(days_ago: float) -> str:
    """A dump version of `days_ago` days back, in the dumps' own format."""
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y%m%d-%H%M%S")


_ids = itertools.count(7_000_000)


class Subtree:
    """One artist's MB slice as a dump node's walk builds it
    (mb_slice_queries.get_slice_one): the artist, a credit naming it alone,
    its groups — each with its releases, a medium apiece — and recordings
    credited to it. MB's internal ids are made up, unique in the process."""

    def __init__(self, name: str, *, gid: Optional[str] = None, artist_id: Optional[int] = None,
                 version: Optional[str] = None):
        self.name = name
        self.version = version or dump_version(3)
        self.gid = gid or str(uuid.uuid4())
        self.artist_id = artist_id or next(_ids)
        self.credit = next(_ids)
        self.tables = {t: [] for t in mb_slice_queries.SLICE_TABLES}
        self._add("mb_artist", id=self.artist_id, gid=self.gid, name=name, sort_name=name)
        self._add("mb_artist_credit", id=self.credit, name=name, artist_count=1)
        self._add("mb_artist_credit_name", artist_credit=self.credit, position=0,
                  artist=self.artist_id, name=name, join_phrase="")

    def _add(self, table: str, **values) -> None:
        cols = mb_slice_queries.SLICE_TABLES[table]
        assert set(values) <= set(cols), set(values) - set(cols)
        self.tables[table].append([values.get(c) for c in cols])

    def group(self, releases: int = 1, gid: Optional[str] = None):
        """A group credited to the artist: (its MBID, its releases' MBIDs)."""
        gid = gid or str(uuid.uuid4())
        group_id = next(_ids)
        self._add("mb_release_group", id=group_id, gid=gid, name=f"Album {group_id}",
                  artist_credit=self.credit)
        mbids = []
        for _ in range(releases):
            release_id, release_gid = next(_ids), str(uuid.uuid4())
            self._add("mb_release", id=release_id, gid=release_gid, name=f"Album {group_id}",
                      artist_credit=self.credit, release_group=group_id)
            self._add("mb_medium", id=next(_ids), release=release_id, position=1, track_count=0)
            mbids.append(release_gid)
        return gid, mbids

    def recording(self, gid: Optional[str] = None) -> str:
        gid = gid or str(uuid.uuid4())
        rid = next(_ids)
        self._add("mb_recording", id=rid, gid=gid, name=f"Song {rid}", artist_credit=self.credit)
        return gid

    def outside(self) -> str:
        """A row the walk does not reach: an artist neither matched nor credited."""
        gid = str(uuid.uuid4())
        self._add("mb_artist", id=next(_ids), gid=gid, name="Someone Else", sort_name="Someone Else")
        return gid

    def one(self) -> dict:
        return {"dump_version": self.version, "artists_matched": {self.name: [self.artist_id]},
                "truncated": [], "tables": {t: rows for t, rows in self.tables.items() if rows}}


class Peer:
    """A peer answering slice exchanges from `entries` (name or MBID →
    entry; what it lacks is `missing`), its TLS channel having proved
    `pubkey`."""

    def __init__(self, entries: dict, pubkey: Optional[str] = None,
                 base_url: str = "https://198.51.100.20:21000"):
        self.entries, self.peer_pubkey, self.base_url = dict(entries), pubkey, base_url
        self.session_deadline = None
        self.asked: list = []

    def _answer(self, keys: list, version: int) -> dict:
        self.asked.append(list(keys))
        return {"v": version, "slices": {k: self.entries[k] for k in keys if k in self.entries},
                "missing": [k for k in keys if k not in self.entries]}

    def mb_slice(self, names: list) -> dict:
        return self._answer(names, mb_slice_queries.PROTOCOL_VERSION)

    def lb_slice(self, artist_mbids: list, min_version: Optional[str] = None) -> dict:
        return self._answer(artist_mbids, lb_slice_queries.PROTOCOL_VERSION)
