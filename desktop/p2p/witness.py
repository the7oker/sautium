"""Witness records: what this node saw of other identities.

Witnessed age is the trust design's anchor ("age = witnessed, first seen at
THIS node, never issued_at alone"), and it can count only what was recorded:
a node that starts writing down whom it met after the network's first weeks
can never witness them. So every node keeps, for each foreign identity it
meets with a valid certificate, one row per (identity, certificate,
channel) — the first sighting of each — in `p2p_witness`:

- the certificate exactly as the authority signed it, which anyone can check
  with Ed25519 alone (`certificate_holds`), with no builder of its format;
- how it came:
  - `introduced`: the subject signed a request to this node and introduced
    itself (X-Sautium-Peer-Cert);
  - `handshake`: an accepted token or grant handshake carried it;
  - `probe`: this node dialed it, and its /health, on a channel TLS-bound to
    its key, carried it;
  - `carried`: a peer's sealed sync payload carried it, in its `authors`
    map, beside records the subject authored whose seals verified here —
    pulled from that peer or pushed by it, the peer as `via`
    (desktop/sync_client.py);
- this node's clock (`seen_at`, the database's, to the second);
- the notary's seal, added later (backend/sign_audio.py): this node's
  signature over the statement, then a self-contained copy of the Worker
  stamp of the batch it rode. The stamp is the "no later than" a third
  party can check: testimony about the first weeks had to be committed in
  them.

The identity registry (p2p_identities) is not this: it is mutable, evicted,
rewritten by succession and filled only on the contacted side. The record
sits beside it, append-only — the database refuses any change to a recorded
sighting but its seal filling once, and every DELETE or TRUNCATE. Growth is
bounded where rows come in (CEILINGS, per channel and UTC day; a re-sign of
a known subject is exempt), never by deletion.

Local only: shared, it would be this node's contact graph with times. It is
life data — backed up, merged insert-only — and never part of the sync
protocol, a share export, the seed bundle or a support warrant. What leaves
it is a certificate alone: the newest one it holds of an author whose sealed
records this node serves, in the payload's `authors` map
(slice_trust.author_entries) — never a row's channel, time or deliverer.

Shared by both runtimes. The launcher records through its sync server's pool
(SyncServer.witness), the Docker backend through db_pool
(backend/p2p_identity.witness_recorder).
"""

import logging
import re
import threading
from collections import OrderedDict
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Callable, ContextManager, Optional

from desktop.p2p import record_sig
from desktop.p2p.birth_cert import TRUSTED_AUTHORITIES, canonical_payload, verify_certificate
from desktop.p2p.identity_registry import REFUSED_SQL

logger = logging.getLogger(__name__)

CHANNELS = ("introduced", "handshake", "probe", "carried")

# New rows a channel may add per UTC day — a flood bound, not a quota:
# certified keys are nearly free to an attacker, so a cheap channel must not
# grow the table without bound. A probe run dials ~120 candidates, 48 runs a
# day. `handshake` has no ceiling of its own: its rows are the certificates of
# friends, and a stranger becomes one only through a token accept, capped
# upstream (TOKEN_FRIENDS_PER_HOUR). Placeholders until T1's real numbers.
CEILINGS = {"introduced": 5000, "probe": 6000, "carried": 5000}

# A certificate's signature as the authority writes it. The one field its
# signature cannot cover: `bytes.fromhex` also takes upper case and spaces
# between the pairs, and every such spelling of one certificate would be a
# row of its own.
_SIG_RE = re.compile(r"[0-9a-f]{128}")

# Recorded (subject, cert_sig, channel) kept in memory, so a known peer's
# next probe or introduction costs no database touch (~1 MB).
MEMO_SIZE = 10_000


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


def _utc_seconds(at: datetime) -> str:
    return at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def statement_v1(witness: str, subject: str, cert_sig: str, channel: str,
                 via: Optional[str], seen_at: datetime) -> bytes:
    """The witness statement, version 1: "`witness` first saw `subject`'s
    certificate `cert_sig` through `channel` (delivered by `via`) at its own
    clock's `seen_at`". Frozen — it is the verifier of every v1 row, and a v2
    never re-signs one: a re-signed row would carry a later stamp, and the
    priority it exists for would be lost."""
    return (f"sautium-witness:v1:{witness}:{subject}:{cert_sig}:{channel}:"
            f"{via or '-'}:{_utc_seconds(seen_at)}").encode("utf-8")


STATEMENTS = {1: statement_v1}


def statement(row: dict, witness: str) -> bytes:
    """The statement a `p2p_witness` row makes when `witness` signs it, in
    the row's own version."""
    return STATEMENTS[row["witness_v"]](witness, row["subject"], row["cert_sig"],
                                        row["channel"], row["via"], row["seen_at"])


def certificate_holds(row: dict, trusted: Optional[list] = None) -> bool:
    """The row's evidence: the authority's signature over the stored payload,
    by a trusted authority, the payload naming the row's subject. Ed25519
    alone — the fields are read from the signed string, so no historical
    builder of the certificate format is needed."""
    parts = row["cert_payload"].split(":")
    return (len(parts) > 2 and parts[0] == "sautium-birth"
            and parts[2].lower() == row["subject"]
            and row["issuer"] in (trusted or TRUSTED_AUTHORITIES)
            and record_sig.verify(row["cert_payload"].encode("utf-8"),
                                  row["cert_sig"], row["issuer"]))


def own_keys(current: str, identity_dir: Optional[Path]) -> frozenset:
    """This node's key and every key it retired (node_info.json's
    `previous`) — never witnessed: a DHT lookup lists the node itself, and
    the walk dials whatever it lists."""
    keys = {current}
    if identity_dir is not None:
        from desktop.node_identity import previous_identities
        keys.update(str(p.get("public_key_hex") or "") for p in previous_identities(identity_dir))
    return frozenset(k.lower() for k in keys if k)


# A re-sign does not count against a ceiling: a format bump re-presents every
# known subject, once on each channel it is met on, and that wave is bounded
# by the subjects known before the bump times the channels. A re-sign is a
# sighting of a subject recorded under an older format, on a channel with
# nothing of its own under this format or a newer one yet — so a second
# certificate under the new format (an upgrade, another email) counts again on
# a channel that already holds one.
#
# A sighting is refused when its subject is refused here, or when a key that is
# delivered it: the certificate may be genuine, but a carried sighting's date
# is the deliverer's word, and a banned node must not start an accomplice's age
# clock.
_PROBE_SQL = f"""
    SELECT {REFUSED_SQL.format(key="%(subject)s")}
           OR (%(via)s IS NOT NULL AND {REFUSED_SQL.format(key="%(via)s")}),
           EXISTS (SELECT 1 FROM p2p_witness
                    WHERE subject = %(subject)s AND cert_sig = %(cert_sig)s
                      AND channel = %(channel)s),
           EXISTS (SELECT 1 FROM p2p_witness WHERE subject = %(subject)s AND cert_v < %(cert_v)s)
           AND NOT EXISTS (SELECT 1 FROM p2p_witness
                            WHERE subject = %(subject)s AND channel = %(channel)s
                              AND cert_v >= %(cert_v)s)"""

# A day's rows of a channel that count: all but those that were re-signs when
# they went in.
_TODAY_SQL = """
    SELECT count(*) FROM p2p_witness w
     WHERE w.channel = %(channel)s AND w.seen_at >= %(since)s
       AND NOT (EXISTS (SELECT 1 FROM p2p_witness o
                         WHERE o.subject = w.subject AND o.id < w.id AND o.cert_v < w.cert_v)
                AND NOT EXISTS (SELECT 1 FROM p2p_witness o
                                 WHERE o.subject = w.subject AND o.channel = w.channel
                                   AND o.id < w.id AND o.cert_v >= w.cert_v))"""

_INSERT_SQL = """
    INSERT INTO p2p_witness (subject, cert_payload, cert_sig, issuer, cert_v, issued_at,
                             method, difficulty, params_version, email_token, email_class,
                             email_domain_token, predecessor, channel, via)
    VALUES (%(subject)s, %(cert_payload)s, %(cert_sig)s, %(issuer)s, %(cert_v)s, %(issued_at)s,
            %(method)s, %(difficulty)s, %(params_version)s, %(email_token)s, %(email_class)s,
            %(email_domain_token)s, %(predecessor)s, %(channel)s, %(via)s)
    ON CONFLICT (subject, cert_sig, channel) DO NOTHING"""


class Recorder:
    """One per process: where every channel's sightings go in.

    `connect()` is a context manager yielding an autocommit connection (the
    runtime's pool); `own_keys()` returns this node's own and retired keys."""

    def __init__(self, connect: Callable[[], ContextManager], own_keys: Callable[[], frozenset]):
        self._connect = connect
        self._own_keys = own_keys
        self._lock = threading.Lock()
        self._memo: "OrderedDict[tuple, None]" = OrderedDict()
        # channel -> [UTC day, its rows that count, sightings turned away]:
        # counted once a day from the table, then by this process's inserts —
        # another process's (the launcher's backend probes too) are seen the
        # next day, so the bound is soft by what they add.
        self._days: dict = {}

    def record(self, cert: dict, channel: str, subject: str, via: Optional[str] = None) -> bool:
        """Put one sighting on record. `subject` is the key the channel
        authenticated (a signed request's, a TLS-bound /health's, a guest's
        signature's — for `carried`, the author whose records' seals
        verified); the certificate must be a valid one FOR it. True when the
        sighting is on record, now or from before. False when it is refused:
        no valid certificate for `subject`, this node's own or a retired key,
        a key banned here (a ban row, or a proof that failed here) as the
        subject or as `via`, or the channel's ceiling met today — a refusal is never
        remembered, so a sighting past a ceiling counts on a later day it
        recurs (a `carried` one recurs only with more of its author's records)
        and a lifted ban is seen at once. Until it recurs, a node that met
        the subject only that way holds no certificate of it to pass on with
        its records (slice_trust.author_entries reads this record)."""
        subject = subject.lower()
        sig = cert.get("sig")
        if not isinstance(sig, str) or not _SIG_RE.fullmatch(sig):
            logger.debug("p2p_witness: %s certificate of %s… is not as the authority wrote it — "
                         "not recorded", channel, subject[:12])
            return False
        key = (subject, sig, channel)
        today = _utc_today()
        with self._lock:
            if key in self._memo:
                self._memo.move_to_end(key)
                return True
            self._close_days(today)
        if subject in self._own_keys():
            return False
        if str(cert.get("pubkey") or "").lower() != subject or not verify_certificate(cert):
            logger.debug("p2p_witness: %s certificate of %s… does not hold for that key — "
                         "not recorded", channel, subject[:12])
            return False
        row = {
            "subject": subject, "cert_payload": canonical_payload(cert).decode("utf-8"),
            "cert_sig": sig, "issuer": cert["issuer"], "cert_v": cert["v"],
            "issued_at": cert["issued_at"], "method": cert["method"],
            "difficulty": cert["difficulty"], "params_version": cert["params_version"],
            "email_token": cert.get("email_token") or None,
            "email_class": cert.get("email_class") or None,
            "email_domain_token": cert.get("email_domain_token") or None,
            "predecessor": cert.get("predecessor") or None,
            "channel": channel, "via": via.lower() if via else None,
        }
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(_PROBE_SQL, row)
            banned, known, resign = cur.fetchone()
            if banned:
                return False
            if not known:
                counts = not resign
                if counts and self._over_ceiling(cur, channel, today):
                    return False
                # The check and the insert are two statements: sightings that
                # land at once can pass the ceiling by as many as land — a
                # flood bound, not an exact quota.
                cur.execute(_INSERT_SQL, row)
                if cur.rowcount:
                    logger.debug("p2p_witness: %s sighting of %s… recorded", channel, subject[:12])
                    if counts:
                        self._count(channel, today)
        with self._lock:
            self._memo[key] = None
            if len(self._memo) > MEMO_SIZE:
                self._memo.popitem(last=False)
        return True

    def note(self, cert: dict, channel: str, subject: str, via: Optional[str] = None) -> None:
        """record() for the paths a sighting rides on — a request, a probe, a
        handshake, an import that carried its authors' certificates: a
        failure to write it is said and never reaches them. A `carried`
        sighting lost so does not come again on its own: the import it rode
        has filled its gap."""
        try:
            self.record(cert, channel, subject, via)
        except Exception as e:
            logger.warning("p2p_witness: a %s sighting of %s… could not be recorded: %s",
                           channel, subject[:12], e)

    def _over_ceiling(self, cur, channel: str, today) -> bool:
        ceiling = CEILINGS.get(channel)
        if ceiling is None:
            return False
        with self._lock:
            day = self._days.get(channel)
        if day is None:
            cur.execute(_TODAY_SQL, {"channel": channel,
                                     "since": datetime.combine(today, time(0), tzinfo=timezone.utc)})
            counted = cur.fetchone()[0]
            with self._lock:
                day = self._days.setdefault(channel, [today, counted, 0])
        with self._lock:
            if day[1] < ceiling:
                return False
            day[2] += 1
            first = day[2] == 1
        if first:
            logger.warning("p2p_witness: the %s ceiling (%d new rows a day) is met — further %s "
                           "sightings today are not recorded, nor passed on with the records "
                           "this node serves; a later day records those that come again",
                           channel, ceiling, channel)
        return True

    def _count(self, channel: str, today) -> None:
        with self._lock:
            day = self._days.get(channel)
            if day is not None and day[0] == today:
                day[1] += 1

    def _close_days(self, today) -> None:
        """Called under the lock: a day that is over is counted afresh, and
        one that met its ceiling says how many sightings it turned away."""
        for channel, (day, _counted, skipped) in list(self._days.items()):
            if day != today:
                del self._days[channel]
                if skipped:
                    logger.warning("p2p_witness: %d %s sighting(s) past the ceiling on %s were "
                                   "not recorded", skipped, channel, day.isoformat())
