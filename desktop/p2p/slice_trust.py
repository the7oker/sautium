"""Who a catalogue slice counts under, and what a falsehood MusicBrainz proves
costs the node that signed it and the node that passed it on.

Any node holding a dump may author slices, and any node may pass them on, so
no key is trusted for what it signs: the canon re-makes every irreversible
decision on MusicBrainz's own answers (backend/canon/confirm.py). What this
module keeps is the record and its consequences, the same for both families
(MB, LB) and both peer surfaces:

- **The author's certificate** rides each slice entry (`entry_certificate`):
  a birth certificate of any method and date, checked offline against the
  pinned authority, and for `method: pow` its proof, bound to it here and
  evaluated once per key when the node's load and memory allow (the identity
  registry). A relay forwards the author's certificate from its own record of
  the key, with the proof its registry keeps for it (`author_certificate`); a
  relayed proof is that relay's word — it verifies the author or its slice is
  dropped, never stored and never held against the author, and the false ones
  a relay brings are counted against that relay as well as the author, so no
  relay can keep the author's own presentation from verifying it. The sync's sealed
  payloads carry the same pair under each author's key (`author_entries`),
  with one rule more: an author this node only witnessed travels with its
  certificate alone, for the receiver's witness record, which needs no proof.
- **The subject index** (`slice_index`): one row per imported blob — its
  author, the node that passed it (by the key its TLS channel proved), the
  payload's hash, its dump version and the MBIDs it is about. Appended,
  never overwritten: a fact is attributed to a blob only through it.
- **Proven falsifications** need one occurrence: P1, a dump version later
  than the newest MetaBrainz has published; P2, rows outside the blob's own
  subtree (`subtree_ok`). **Suspected** ones (S1, S2: an MBID MusicBrainz
  answers unknown where an honest older dump cannot explain it) are counted
  as events and judged by rate (`judge`): an honest dump goes stale, so one
  is not enough. A clash between two slices' rows is no evidence at all: its
  web service shows no row ids, and MusicBrainz removes releases and
  recordings as a matter of course, so an unknown MBID on one side tells
  which row lands, never who lied (desktop/p2p/slice_checks.py).
- **The penalties** (`slice_penalties`) bind a key, never an address, and
  only the slice lanes: a convicted author's slices are refused, a
  quarantined relay is not asked and its signed slice requests are refused
  with the evidence (`refusal`), so its owner can read why. A mailbox whose
  second key is convicted is burned for authorship.
"""

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# The thresholds of a suspected-fabrication conviction, set above the rate at
# which an honest dump's slice disagrees with MusicBrainz as it ages. An event
# counts only from a blob whose dump version was at most FRESH_DAYS old when
# it was checked; the window keeps years of honest history from diluting a
# compromised key's new forgeries.
FRESH_DAYS = 60
WINDOW_DAYS = 90
AUTHOR_MIN_EVENTS = 3
AUTHOR_MIN_RATE = 0.01
RELAY_MIN_EVENTS = 3
RELAY_MIN_RATE = 0.01
RELAY_MIN_FACTS = 100
RELAY_MIN_AUTHORS = 2
PROVEN = ("P1", "P2")


# --------------------------------------------------------------------------
# Certificates
# --------------------------------------------------------------------------

def entry_certificate(entry: dict, author: str) -> Optional[Tuple[dict, Optional[dict]]]:
    """(certificate, proof) of the entry's author, or None: the certificate is
    the authority's, names `author`, and a `pow` one comes with a proof bound
    to it. The proof's memory-hard evaluation is not run here."""
    from desktop.p2p.birth_cert import verify_certificate
    from desktop.p2p.identity_proof import proof_binds
    cert, proof = entry.get("author_cert"), entry.get("author_proof")
    if not isinstance(cert, dict) or not verify_certificate(cert):
        return None
    if str(cert.get("pubkey", "")).lower() != author.lower():
        return None
    if cert.get("method") == "pow":
        if not proof_binds(proof, cert):
            return None
        return cert, proof
    return cert, None


def _certificate(pubkey: str, v, method, issued_at, difficulty, params_version, token, klass,
                 domain, predecessor, issuer, sig) -> dict:
    """A stored certificate rebuilt as it travels. An empty field travels as
    null: the signed payload reads null and "" alike, and a receiver's
    registry stores null — "" is no email class. The signature travels in the
    authority's spelling, whatever spelling the registry last observed
    (`bytes.fromhex` takes upper case and spaced pairs, and the signature is
    the one field the authority's cannot cover): a witness records only that
    spelling (desktop/p2p/witness.py)."""
    return {"v": v, "pubkey": pubkey.lower(),
            "issued_at": issued_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "method": method, "difficulty": int(difficulty), "params_version": int(params_version),
            "email_token": token or None, "email_class": klass or None,
            "email_domain_token": domain or None, "predecessor": predecessor or None,
            "issuer": issuer, "sig": bytes.fromhex(sig).hex()}


def _verified_certificates(conn, pubkeys: List[str]) -> Dict[str, Tuple[dict, Optional[dict]]]:
    """The certificate and proof of each of `pubkeys` that this node verified
    (p2p_identities `verified`), as a relay forwards them — while the
    registry's row is in the current certificate format: after a format bump
    a row not observed since is no certificate a receiver takes (its check
    refuses an old format), and on the sync lanes the witness record may hold
    the current one."""
    from desktop.p2p.birth_cert import CERT_VERSION
    from desktop.p2p.identity_proof import PROOF_VERSION
    with conn.cursor() as cur:
        cur.execute("""
            SELECT pubkey, cert_v, method::text, issued_at, difficulty, params_version, email_token,
                   email_class::text, email_domain_token, predecessor, issuer, cert_sig,
                   proof_nonce
              FROM p2p_identities
             WHERE pubkey = ANY(%s) AND status = 'verified' AND cert_v = %s""",
                    ([k.lower() for k in pubkeys], CERT_VERSION))
        rows = cur.fetchall()
    pairs = {}
    for row in rows:
        cert = _certificate(*row[:12])
        proof = None
        if cert["method"] == "pow":
            # The proof names the certificate's signature as the certificate spells it.
            proof = {"v": PROOF_VERSION, "pubkey": cert["pubkey"], "cert_sig": cert["sig"],
                     "nonce": row[12], "difficulty": cert["difficulty"],
                     "params_version": cert["params_version"]}
        pairs[cert["pubkey"]] = (cert, proof)
    return pairs


def author_certificate(conn, pubkey: str) -> Optional[Tuple[dict, Optional[dict]]]:
    """The author's certificate and proof as this node's registry holds them
    for a key it verified (p2p_identities) — what a relay forwards. None for
    a key this node has not verified, or whose row is in an older certificate
    format: its slices are not passed on."""
    return _verified_certificates(conn, [pubkey]).get(pubkey.lower())


def _witnessed_certificates(conn, pubkeys: List[str]) -> Dict[str, dict]:
    """The newest certificate in the current format this node witnessed for
    each of `pubkeys` (p2p_witness, any channel): the certificate alone — the
    record keeps no proof."""
    from desktop.p2p.birth_cert import CERT_VERSION
    if not pubkeys:
        return {}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT ON (subject) subject, cert_v, method::text, issued_at, difficulty,
                   params_version, email_token, email_class::text, email_domain_token,
                   predecessor, issuer, cert_sig
              FROM p2p_witness
             WHERE subject = ANY(%s) AND cert_v = %s
             ORDER BY subject, seen_at DESC, id DESC""", (pubkeys, CERT_VERSION))
        rows = cur.fetchall()
    return {r[0]: _certificate(*r) for r in rows}


def author_entries(conn, pubkeys: Iterable[str],
                   own: Optional[Tuple[dict, Optional[dict]]] = None) -> Dict[str, dict]:
    """The `authors` map of a sealed sync payload (BIRTH-SYNC slice 2): for
    each author its certificate in a slice entry's form, under its key —
    this node's own (`own`: its certificate and proof, as an entry of its own
    build carries them); an author verified here, as a slice relay forwards
    one, while its registry row is in the current certificate format; any
    other author whose certificate this node witnessed, the newest in the
    current format, alone. An author it knows no certificate of, or one
    banned here — a ban row, or a proof that failed here — gets no entry.
    The receiver records the certificate as a `carried` sighting by the
    witness record's own check (desktop/p2p/witness.py), which reads no
    proof; nothing in the sync evaluates one."""
    keys = {str(p).lower() for p in pubkeys if p}
    if not keys:
        return {}
    entries: Dict[str, dict] = {}
    own_key = own[0]["pubkey"].lower() if own else None
    if own_key in keys:
        entries[own_key] = {"author_cert": own[0], "author_proof": own[1]}
    others = sorted(keys - {own_key})
    if not others:
        return entries
    with conn.cursor() as cur:
        cur.execute("""SELECT pubkey FROM p2p_node_bans WHERE pubkey = ANY(%(k)s)
                       UNION SELECT pubkey FROM p2p_identities
                              WHERE pubkey = ANY(%(k)s) AND status = 'failed'""", {"k": others})
        refused = {r[0] for r in cur.fetchall()}
    others = [k for k in others if k not in refused]
    for key, (cert, proof) in _verified_certificates(conn, others).items():
        entries[key] = {"author_cert": cert, "author_proof": proof}
    for key, cert in _witnessed_certificates(conn, [k for k in others if k not in entries]).items():
        entries[key] = {"author_cert": cert, "author_proof": None}
    return entries


def observe_author(conn, cert: dict, proof: Optional[dict], relay: str) -> Optional[str]:
    """The author's identity recorded (p2p_identities) from its certificate,
    and for `pow` the proof that came with the slice from `relay` (the key
    of the node that passed it on) evaluated once when the node can afford
    the 2 GiB (identity_registry.verify_relayed) — the proof is the relay's
    word, so it is never stored unless it verifies, and one that fails is
    never held against the author. Returns 'verified',
    'unverified' (not evaluated yet), 'failed' (the author's own proof failed
    here, or this one did: the slice is dropped), or None when the key is
    banned or its record does not match."""
    from desktop.p2p import identity_registry
    if identity_registry.is_banned(conn, cert["pubkey"]):
        return None
    row = identity_registry.observe(conn, cert)
    if row is None or row.get("anomaly"):
        return None
    if row["status"] == "unverified" and proof is not None:
        return identity_registry.verify_relayed(conn, cert, proof, relay)
    return row["status"]


# --------------------------------------------------------------------------
# Penalties
# --------------------------------------------------------------------------

def penalty(conn, pubkey: str) -> Optional[dict]:
    """The penalty a key carries on the slice lanes, with its evidence."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT p.party::text, p.created_at, e.kind::text, e.entity::text, e.mbid::text,
                   e.key, e.author, e.dump_version, e.seen_at
              FROM slice_penalties p LEFT JOIN slice_evidence e ON e.id = p.evidence
             WHERE p.pubkey = %s""", (pubkey.lower(),))
        row = cur.fetchone()
    if row is None:
        return None
    return {"party": row[0], "since": row[1].isoformat(),
            "kind": row[2], "entity": row[3], "mbid": row[4], "name": row[5],
            "author": (row[6] or "").strip(), "dump_version": row[7],
            "seen": row[8].isoformat() if row[8] else None}


def refused_author(conn, cert: dict) -> bool:
    """A convicted key, or one whose verified mailbox was burned."""
    if penalty(conn, cert["pubkey"]) is not None:
        return True
    token = cert.get("email_token")
    if not token:
        return False
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM slice_burned_mailboxes WHERE email_token = %s", (token,))
        return cur.fetchone() is not None


def penalized_keys(conn) -> Set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT btrim(pubkey) FROM slice_penalties")
        return {r[0] for r in cur.fetchall()}


_PARTIES = frozenset({"author", "relay"})
_KINDS = frozenset({"P1", "P2", "S1", "S2"})
_ENTITIES = frozenset({"dump_version", "subtree", "artist", "release_group", "release", "recording"})
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_VERSION = re.compile(r"^\d{8}-\d{6}$")
_NAME_MAX = 512


def refusal_of(body: dict) -> Optional[dict]:
    """A peer's refusal of this node's slice requests as `refusal` builds it,
    each field of its kind — the party from its enum; the kind, the entity,
    the author's key in hex, the MBID canonical, the name plain text, the dump
    version and the time in their forms, or empty where the penalty carries no
    evidence — or None: anything else is no refusal this node records (the
    source is dropped all the same)."""
    from desktop.mb_slice_client import canonical_mbid
    if not isinstance(body, dict):
        return None
    party, kind, entity = body.get("party"), body.get("kind"), body.get("entity")
    author, mbid, name = body.get("author") or "", body.get("mbid"), body.get("name")
    version, seen = body.get("dump_version") or None, body.get("seen")
    if party not in _PARTIES:
        return None
    if kind is not None and kind not in _KINDS or entity is not None and entity not in _ENTITIES:
        return None
    if not (isinstance(author, str) and (author == "" or _HEX64.match(author))):
        return None
    if mbid is not None and not canonical_mbid(mbid):
        return None
    if name is not None and not (isinstance(name, str) and len(name) <= _NAME_MAX
                                 and name.isprintable()):
        return None
    if version is not None and not (isinstance(version, str) and _VERSION.match(version)):
        return None
    if seen is not None:
        if not isinstance(seen, str):
            return None
        try:
            seen = datetime.fromisoformat(seen).isoformat()
        except ValueError:
            return None
    return {"party": party, "kind": kind, "entity": entity, "mbid": mbid, "name": name,
            "author": author, "dump_version": version, "seen": seen}


def refusal_report(refused: dict) -> dict:
    """What a node keeps for support of a refusal it met (validated by
    `refusal_of`): the kind of the falsification and when it was seen, the
    key's prefix — no artist, no name, no MBID (those stay in its own log)."""
    return {"party": refused["party"], "kind": refused["kind"], "entity": refused["entity"],
            "seen": refused["seen"], "author": refused["author"][:16]}


def refusal(conn, pubkey: Optional[str]) -> Optional[dict]:
    """What a signed slice request from `pubkey` is told when the key carries a
    penalty here: the kind, the fact and when it was seen — so the node's owner
    can read why in its own diagnostics. None otherwise (an unsigned request
    carries no key to refuse)."""
    if not pubkey:
        return None
    p = penalty(conn, pubkey)
    if p is None:
        return None
    return {"error": "slice_refused", "party": p["party"], "kind": p["kind"],
            "entity": p["entity"], "mbid": p["mbid"], "name": p["name"],
            "author": p["author"], "dump_version": p["dump_version"], "seen": p["seen"]}


def _punish(conn, pubkey: str, party: str, evidence: Optional[int]) -> bool:
    """Penalize `pubkey`; True when this is new. An author convicted while a key
    with its mailbox was convicted before burns the mailbox."""
    with conn.cursor() as cur:
        cur.execute("SELECT email_token FROM p2p_identities WHERE pubkey = %s", (pubkey.lower(),))
        row = cur.fetchone()
        token = row[0] if row and row[0] else None
        cur.execute("""INSERT INTO slice_penalties (pubkey, party, email_token, evidence)
                       VALUES (%s, %s, %s, %s) ON CONFLICT (pubkey) DO NOTHING RETURNING 1""",
                    (pubkey.lower(), party, token, evidence))
        new = cur.fetchone() is not None
        if new and party == "author" and token:
            cur.execute("""SELECT 1 FROM slice_penalties
                            WHERE email_token = %s AND pubkey <> %s AND party = 'author'""",
                        (token, pubkey.lower()))
            if cur.fetchone():
                cur.execute("INSERT INTO slice_burned_mailboxes (email_token) VALUES (%s) "
                            "ON CONFLICT DO NOTHING", (token,))
    if new:
        logger.warning("slice %s %s… penalized (evidence %s)",
                       "author" if party == "author" else "relay", pubkey[:16], evidence)
        # Content-free for support: the kind and the key's prefix, never a name
        # or an MBID (desktop/p2p/diag_events.py).
        from desktop.p2p import diag_events
        with conn.cursor() as cur:
            cur.execute("SELECT kind::text FROM slice_evidence WHERE id = %s", (evidence,))
            row = cur.fetchone()
        # In the caller's transaction: a penalty that rolls back leaves no event.
        diag_events.record(conn, "slice.penalized", {"party": party, "kind": row[0] if row else None,
                                                     "key": pubkey[:16]}, commit=False)
    return new


# --------------------------------------------------------------------------
# The subject index and the evidence
# --------------------------------------------------------------------------

def index(conn, family: str, key: str, author: str, relay: str, relay_addr: Optional[str],
          payload_sha256: str, dump_version: str, artists: Iterable[str],
          groups: Iterable[str]) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO slice_index (family, key, author, relay, relay_addr, payload_sha256,
                                     dump_version, subject_artists, subject_groups)
            VALUES (%s, %s, %s, %s, %s::uuid, %s, %s, %s::uuid[], %s::uuid[])""",
                    (family, key, author.lower(), relay.lower(), relay_addr, payload_sha256,
                     dump_version or "", sorted(set(artists)), sorted(set(groups))))


def record(conn, family: str, key: str, author: str, relay: str, kind: str, entity: str,
           mbid: Optional[str], dump_version: str, payload_sha256: str) -> Optional[int]:
    """One event of evidence; None when the same blob's same fact was recorded
    already (an event counts once)."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO slice_evidence (family, key, author, relay, kind, entity, mbid,
                                        dump_version, payload_sha256)
            VALUES (%s, %s, %s, %s, %s, %s, %s::uuid, %s, %s)
            ON CONFLICT (payload_sha256, kind, entity, mbid) DO NOTHING RETURNING id""",
                    (family, key, author.lower(), relay.lower(), kind, entity, mbid,
                     dump_version or "", payload_sha256))
        row = cur.fetchone()
    return row[0] if row else None


def tally(conn, family: str, author: str, relay: str, facts: int, events: int) -> None:
    """The day's checked facts and events of a blob, for its author and for the
    node that passed it (the denominators of the rates)."""
    parties = [("author", author)] + ([("relay", relay)] if relay.lower() != author.lower() else [])
    with conn.cursor() as cur:
        for party, key in parties:
            cur.execute("""
                INSERT INTO slice_tally (day, party, pubkey, family, facts, events)
                VALUES (current_date, %s, %s, %s, %s, %s)
                ON CONFLICT (day, party, pubkey, family)
                DO UPDATE SET facts = slice_tally.facts + EXCLUDED.facts,
                              events = slice_tally.events + EXCLUDED.events""",
                        (party, key.lower(), family, facts, events))


def proven(conn, family: str, key: str, author: str, relay: str, kind: str, entity: str,
           mbid: Optional[str], dump_version: str, payload_sha256: str) -> List[str]:
    """A proven falsification: one is enough. The author is convicted, and a
    relay that is not the author is quarantined — its own check before passing
    the blob on would have seen it. Returns the keys newly penalized."""
    ev = record(conn, family, key, author, relay, kind, entity, mbid, dump_version, payload_sha256)
    out = []
    if _punish(conn, author, "author", ev):
        out.append(author.lower())
    if relay.lower() != author.lower() and _punish(conn, relay, "relay", ev):
        out.append(relay.lower())
    return out


def judge(conn, family: str, author: str, relay: str) -> List[str]:
    """The suspected-fabrication thresholds over the window: the author at
    AUTHOR_MIN_EVENTS events and AUTHOR_MIN_RATE of its checked facts; a relay
    at RELAY_MIN_EVENTS events of at least RELAY_MIN_AUTHORS authors and
    RELAY_MIN_RATE of at least RELAY_MIN_FACTS facts it passed on — or one
    event of an author this node convicted on one of the events that convicted
    it — the same fact, recorded in the window before the conviction — when
    the relay's own check necessarily met it (`covered`: found through a
    subject artist of a blob with no more subjects than a check draws; a drawn
    fact another draw may have missed counts only by rate). Returns the keys
    newly penalized."""
    since = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)
    out: List[str] = []
    with conn.cursor() as cur:
        cur.execute("""SELECT count(*), min(id) FROM slice_evidence
                        WHERE author = %s AND kind IN ('S1', 'S2') AND seen_at > %s""",
                    (author.lower(), since))
        events, first = cur.fetchone()
        cur.execute("""SELECT coalesce(sum(facts), 0) FROM slice_tally
                        WHERE party = 'author' AND pubkey = %s AND day > %s::date""",
                    (author.lower(), since))
        facts = cur.fetchone()[0]
    if events >= AUTHOR_MIN_EVENTS and facts and events >= AUTHOR_MIN_RATE * facts:
        if _punish(conn, author, "author", first):
            out.append(author.lower())
            # Rule 2 (b) at the conviction, for every node that passed one of
            # the convicting events on where its own check met it — not only
            # the one whose blob was checked now.
            with conn.cursor() as cur:
                cur.execute("""SELECT relay, min(id) FROM slice_evidence
                                WHERE author = %s AND relay <> author AND covered
                                  AND kind IN ('S1', 'S2') AND seen_at > %s
                                GROUP BY relay""", (author.lower(), since))
                carriers = cur.fetchall()
            for carrier, ev in carriers:
                if _punish(conn, carrier.strip(), "relay", ev):
                    out.append(carrier.strip().lower())
    if relay.lower() == author.lower():
        return out
    with conn.cursor() as cur:
        cur.execute("""SELECT count(*), count(DISTINCT author), min(id) FROM slice_evidence
                        WHERE relay = %s AND author <> relay AND kind IN ('S1', 'S2')
                          AND seen_at > %s""", (relay.lower(), since))
        events, authors, first = cur.fetchone()
        cur.execute("""SELECT coalesce(sum(facts), 0) FROM slice_tally
                        WHERE party = 'relay' AND pubkey = %s AND day > %s::date""",
                    (relay.lower(), since))
        facts = cur.fetchone()[0]
        cur.execute("""SELECT min(e.id) FROM slice_evidence e
                         JOIN slice_penalties p ON p.pubkey = e.author AND p.party = 'author'
                        WHERE e.relay = %(r)s AND e.author <> e.relay AND e.covered
                          AND e.kind IN ('S1', 'S2')
                          AND EXISTS (SELECT 1 FROM slice_evidence c
                                       WHERE c.author = e.author AND c.kind = e.kind
                                         AND c.entity = e.entity
                                         AND c.mbid IS NOT DISTINCT FROM e.mbid
                                         AND c.seen_at <= p.created_at
                                         AND c.seen_at > p.created_at - make_interval(days => %(w)s))""",
                    {"r": relay.lower(), "w": WINDOW_DAYS})
        carried = cur.fetchone()[0]
    if carried is not None:
        if _punish(conn, relay, "relay", carried):
            out.append(relay.lower())
    elif (events >= RELAY_MIN_EVENTS and authors >= RELAY_MIN_AUTHORS
          and facts >= RELAY_MIN_FACTS and events >= RELAY_MIN_RATE * facts):
        if _punish(conn, relay, "relay", first):
            out.append(relay.lower())
    return out


def fresh(dump_version: str, now: Optional[datetime] = None) -> bool:
    """A blob whose events count: its dump at most FRESH_DAYS old."""
    try:
        stamp = datetime.strptime(dump_version[:15], "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return False
    return stamp >= (now or datetime.now(timezone.utc)) - timedelta(days=FRESH_DAYS)


# --------------------------------------------------------------------------
# P2 — a slice is its own subtree
# --------------------------------------------------------------------------

def subtree_ok(core: dict, rows: Dict[str, list]) -> bool:
    """The protocol's own walk (mb_slice_queries.get_slice_one) re-run over the
    blob's rows: from the artists it matched, through their credits and every
    member of those credits, their groups, releases, media, tracks and
    recordings, the Bandcamp links, tags and areas, and the whole
    dictionaries. A row the walk does not reach was put there by the author,
    and so was a matched artist without its own row; such a blob is refused
    whole (P2)."""
    from desktop.p2p.mb_slice_queries import SLICE_TABLES, _DICT_TABLES

    def col(table, name):
        return SLICE_TABLES[table].index(name)

    def rows_of(table):
        return rows.get(table) or []

    matched = core.get("artists_matched")
    if not isinstance(matched, dict):
        return False
    a_ids: Set[int] = set()
    for ids in matched.values():
        if not isinstance(ids, list):
            return False
        a_ids.update(ids)
    # A matched artist the blob carries no row for: the name "matched" nothing
    # a reader can see, yet it would close the name for good (not a zero-match).
    # A build never cuts the artist table (only those with a row limit).
    if not a_ids <= {r[col("mb_artist", "id")] for r in rows_of("mb_artist")}:
        return False
    acn = rows_of("mb_artist_credit_name")
    ac_ids = {r[col("mb_artist_credit_name", "artist_credit")] for r in acn
              if r[col("mb_artist_credit_name", "artist")] in a_ids}
    reach = {
        "mb_artist_credit_name": lambda r: r[col("mb_artist_credit_name", "artist_credit")] in ac_ids,
        "mb_artist_credit": lambda r: r[col("mb_artist_credit", "id")] in ac_ids,
        "mb_artist_alias": lambda r: r[col("mb_artist_alias", "artist")] in a_ids,
    }
    members = {r[col("mb_artist_credit_name", "artist")] for r in acn if reach["mb_artist_credit_name"](r)}
    reach["mb_artist"] = lambda r: r[col("mb_artist", "id")] in a_ids | members
    rg_ids = {r[col("mb_release_group", "id")] for r in rows_of("mb_release_group")
              if r[col("mb_release_group", "artist_credit")] in ac_ids}
    reach["mb_release_group"] = lambda r: r[col("mb_release_group", "id")] in rg_ids
    reach["mb_release_group_secondary_type_join"] = \
        lambda r: r[col("mb_release_group_secondary_type_join", "release_group")] in rg_ids
    r_ids = {r[col("mb_release", "id")] for r in rows_of("mb_release")
             if r[col("mb_release", "release_group")] in rg_ids}
    reach["mb_release"] = lambda r: r[col("mb_release", "id")] in r_ids
    reach["mb_release_country"] = lambda r: r[col("mb_release_country", "release")] in r_ids
    reach["mb_release_unknown_country"] = lambda r: r[col("mb_release_unknown_country", "release")] in r_ids
    m_ids = {r[col("mb_medium", "id")] for r in rows_of("mb_medium")
             if r[col("mb_medium", "release")] in r_ids}
    reach["mb_medium"] = lambda r: r[col("mb_medium", "id")] in m_ids
    tracks = [r for r in rows_of("mb_track") if r[col("mb_track", "medium")] in m_ids]
    reach["mb_track"] = lambda r: r[col("mb_track", "medium")] in m_ids
    rec_ids = {r[col("mb_track", "recording")] for r in tracks}
    reach["mb_recording"] = lambda r: (r[col("mb_recording", "id")] in rec_ids
                                       or r[col("mb_recording", "artist_credit")] in ac_ids)
    links = ([r for r in rows_of("mb_l_release_url") if r[col("mb_l_release_url", "entity0")] in r_ids]
             + [r for r in rows_of("mb_l_artist_url") if r[col("mb_l_artist_url", "entity0")] in a_ids])
    reach["mb_l_release_url"] = lambda r: r[col("mb_l_release_url", "entity0")] in r_ids
    reach["mb_l_artist_url"] = lambda r: r[col("mb_l_artist_url", "entity0")] in a_ids
    url_ids = {r[col("mb_l_release_url", "entity1")] for r in links}
    reach["mb_url"] = lambda r: r[col("mb_url", "id")] in url_ids
    reach["mb_artist_tag"] = lambda r: (r[col("mb_artist_tag", "artist")] in a_ids
                                        and (r[col("mb_artist_tag", "count")] or 0) > 0)
    reach["mb_release_group_tag"] = lambda r: r[col("mb_release_group_tag", "release_group")] in rg_ids
    tag_ids = ({r[col("mb_artist_tag", "tag")] for r in rows_of("mb_artist_tag") if reach["mb_artist_tag"](r)}
               | {r[col("mb_release_group_tag", "tag")] for r in rows_of("mb_release_group_tag")
                  if reach["mb_release_group_tag"](r)})
    reach["mb_tag"] = lambda r: r[col("mb_tag", "id")] in tag_ids
    areas = {r[col("mb_artist", "area")] for r in rows_of("mb_artist") if reach["mb_artist"](r)} - {None}
    reach["mb_area"] = lambda r: r[col("mb_area", "id")] in areas
    for table in SLICE_TABLES:
        if table in _DICT_TABLES:
            continue
        test = reach.get(table)
        if test is None:
            return False
        if not all(test(r) for r in rows_of(table)):
            return False
    return True


def subjects(core: dict, rows: Dict[str, list]) -> Tuple[List[str], List[str]]:
    """(artist MBIDs, release-group MBIDs) a blob is about: the artists it
    matched and the groups it credits to them."""
    from desktop.p2p.mb_slice_queries import SLICE_TABLES
    a_col, g_col = SLICE_TABLES["mb_artist"].index("id"), SLICE_TABLES["mb_artist"].index("gid")
    matched: Set[int] = set()
    for ids in (core.get("artists_matched") or {}).values():
        matched.update(ids or [])
    artists = sorted({r[g_col] for r in rows.get("mb_artist") or [] if r[a_col] in matched and r[g_col]})
    acn = SLICE_TABLES["mb_artist_credit_name"]
    credits = {r[acn.index("artist_credit")] for r in rows.get("mb_artist_credit_name") or []
               if r[acn.index("artist")] in matched}
    rg = SLICE_TABLES["mb_release_group"]
    groups = sorted({r[rg.index("gid")] for r in rows.get("mb_release_group") or []
                     if r[rg.index("artist_credit")] in credits and r[rg.index("gid")]})
    return artists, groups
