"""Check before passing on: a node re-serves a catalogue slice only after its
own check against MusicBrainz, so every node in a chain has checked what it
hands on — one that hands on a falsification has skipped its check, or is
compromised (desktop/p2p/slice_trust.py judges both).

A kept blob is passed on (`servable`) when it is this node's own build, or:

1. its author carries no penalty here and is verified, in the current
   certificate format, and the node it came from carries none either, nor
   is refused here;
2. it carried no proven falsification — its own subtree at import (P2), a
   dump version MetaBrainz has published (P1, `published_check`);
3. a check in the last CHECK_DAYS found no suspected fabrication in it: up to
   SUBJECTS_PER_CHECK of its subject artists drawn at random, with the groups
   it credits to them set against MusicBrainz's own list, and RANDOM_FACTS
   releases or recordings drawn at random (`request_check`, `evaluate`) —
   a difference an honest older dump explains (a rename, a credit, a merge)
   holds nothing back: every receiver re-makes its decisions on MusicBrainz
   anyway;
4. none of its artist, group, release or recording rows carries another
   MBID on an id held here, nor its artist or group rows an MBID held here
   under another id (a clash, settled on MusicBrainz's answers one at a time,
   at most CLASHES_ASKED of them — it decides which rows land and counts
   against no one: MusicBrainz's web service shows no row ids, and it removes
   releases and recordings as a matter of course, so the unknown side may be
   an honest older dump's);
5. its dump version is no later than the newest MetaBrainz has published, as
   this node last read its listing (`version_bound`).

The check is lazy: a blob is checked the first time a peer asks for it — that
answer is `missing`, and the requester takes the next source — and again once
CHECK_DAYS have passed, or once MusicBrainz answers one of its subject
artists or groups unknown, whatever asked (`uncheck_subject`). `/health`
advertises the blobs that could pass (`checkable_count`); without that, a
node holding only unchecked blobs would never be asked and so never check.

Only a node that asks MusicBrainz checks (mb_asks.node_asks: catalogue
slices, no full dump). A node holding the dump serves its own builds alone,
and one without a catalogue has nothing to check a slice against: neither
passes another author's blob on, nor queues a check.

What a check asks MusicBrainz goes through the answers' queue
(desktop/p2p/mb_asks.py) at the lowest priority; the backend's consumer
answers it and wakes `evaluate`.
"""

import gzip
import hashlib
import logging
import random
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from desktop.p2p import mb_asks, slice_trust

logger = logging.getLogger(__name__)

CHECK_DAYS = 30
SUBJECTS_PER_CHECK = 5
RANDOM_FACTS = 3
# The groups of one drawn subject a check looks up because MusicBrainz's own
# list does not carry them: a check's cost stays a few requests a blob
# however many groups a slice credits outside it; the rest go unchecked.
GROUP_LOOKUPS = 5
PRIORITY = 5
SUBJECT = "slice_check"


def subject_key(family: str, key: str) -> str:
    return f"{family}:{key}"


def parse_subject_key(value: str) -> Tuple[str, str]:
    family, key = value.split(":", 1)
    return family, key


# --------------------------------------------------------------------------
# What may be passed on
# --------------------------------------------------------------------------

_TABLE = {"mb": ("mb_slice_blobs", "name_key"), "lb": ("lb_slice_blobs", "artist_mbid")}


_VERSION = re.compile(r"^\d{8}-\d{6}$")


def version_bound(conn, family: str) -> str:
    """The latest dump version a slice of `family` may carry here: the newest
    MetaBrainz has published as this node last read its listing
    (desktop/p2p/published.py) — a day from now while it was never read."""
    with conn.cursor() as cur:
        cur.execute("SELECT value->>'version' FROM user_settings WHERE key = %s",
                    (f"{family}_slice.published",))
        row = cur.fetchone()
    if row and row[0] and _VERSION.match(row[0]):
        return row[0]
    return (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y%m%d-%H%M%S")


def version_ok(version, bound: str) -> bool:
    """A dump's own version, and none later than `bound` (version_bound)."""
    return isinstance(version, str) and bool(_VERSION.match(version)) and version <= bound


def _blob(conn, family: str, key: str) -> Optional[dict]:
    table, col = _TABLE[family]
    contested = "contested" if family == "mb" else "FALSE"
    payload = "payload_sha256" if family == "mb" else "NULL"
    with conn.cursor() as cur:
        cur.execute(f"""SELECT dump_version, btrim(author_pubkey), blob_gz, btrim(relay),
                               checked_at, suspect, {contested}, created_at,
                               checked_at > now() - make_interval(days => %s), {payload}
                          FROM {table} WHERE {col} = %s""", (CHECK_DAYS, key))
        row = cur.fetchone()
    if row is None:
        return None
    blob_gz = bytes(row[2])
    return {"dump_version": row[0], "author": row[1], "blob_gz": blob_gz, "relay": row[3],
            "checked_at": row[4], "suspect": row[5], "contested": row[6], "created_at": row[7],
            "fresh_check": bool(row[8]),
            "payload": (row[9] or "").strip() or hashlib.sha256(blob_gz).hexdigest()}


def _author_ok(conn, author: str, relay: Optional[str]) -> bool:
    """The author verified here in the current certificate format (the one
    its certificate can be passed on in, slice_trust.author_certificate), and
    neither it nor the node it came from penalized or refused here."""
    from desktop.p2p.birth_cert import CERT_VERSION
    with conn.cursor() as cur:
        cur.execute("""SELECT status::text, cert_v FROM p2p_identities WHERE pubkey = %s""",
                    (author.lower(),))
        row = cur.fetchone()
        if row is None or row[0] != "verified" or row[1] != CERT_VERSION:
            return False
        cur.execute("""SELECT 1 FROM slice_penalties WHERE pubkey = %(a)s OR pubkey = %(r)s
                       UNION ALL
                       SELECT 1 FROM p2p_node_bans WHERE lower(pubkey) IN (%(a)s, %(r)s)
                       UNION ALL
                       SELECT 1 FROM p2p_identities WHERE pubkey = %(r)s AND status = 'failed'
                        LIMIT 1""", {"a": author.lower(), "r": (relay or author).lower()})
        if cur.fetchone():
            return False
        cur.execute("""SELECT 1 FROM slice_burned_mailboxes b JOIN p2p_identities i
                           ON i.email_token = b.email_token WHERE i.pubkey = %s""", (author.lower(),))
        return cur.fetchone() is None


def source_refused(conn, relay: Optional[str]) -> bool:
    """A source this node takes no slices from: it carries a slice penalty,
    or it is refused here (a ban, or a proof that failed here) — read at each
    exchange, not once per list of sources, so a penalty found this run
    counts at once."""
    from desktop.p2p.identity_registry import REFUSED_SQL
    if not relay:
        return False
    with conn.cursor() as cur:
        cur.execute(f"""SELECT EXISTS (SELECT 1 FROM slice_penalties WHERE pubkey = %(k)s)
                               OR {REFUSED_SQL.format(key="%(k)s")}""", {"k": relay.lower()})
        return bool(cur.fetchone()[0])


def servable(conn, family: str, key: str, own_pubkey: Optional[str]) -> Optional[bool]:
    """True: pass it on. False: never (its author or source is penalized, it
    is suspect or contested). None: checkable, not checked lately — the caller
    asks for the check and answers `missing` this time."""
    b = _blob(conn, family, key)
    if b is None:
        return False
    if own_pubkey and b["author"].lower() == own_pubkey.lower() and b["relay"] is None:
        return True
    if not mb_asks.node_asks(conn):
        return False
    if not _author_ok(conn, b["author"], b["relay"]):
        return False
    if b["suspect"] or b["contested"]:
        return False
    if not version_ok(b["dump_version"], version_bound(conn, family)):
        # A version past MetaBrainz's newest: this node's own check sees it,
        # so it is never passed on (a node that does is quarantined for it).
        return False
    return True if b["fresh_check"] else None


def checkable_count(conn, family: str, own_pubkey: Optional[str]) -> int:
    """The blobs that can be passed on, checked or not yet: this node's own
    builds, and — on a node that checks — those of verified authors with no
    penalty, from sources with none, not suspect nor contested: what
    `/health` advertises."""
    from desktop.p2p.birth_cert import CERT_VERSION
    table, _ = _TABLE[family]
    contested = "AND NOT b.contested" if family == "mb" else ""
    checks = mb_asks.node_asks(conn)
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT count(*) FROM {table} b
             WHERE (b.relay IS NULL AND btrim(b.author_pubkey) = %(own)s)
                OR (%(checks)s AND NOT b.suspect {contested}
                    AND b.dump_version ~ '^[0-9]{{8}}-[0-9]{{6}}$' AND b.dump_version <= %(bound)s
                    AND EXISTS (SELECT 1 FROM p2p_identities i
                                 WHERE i.pubkey = btrim(b.author_pubkey) AND i.status = 'verified'
                                   AND i.cert_v = %(cert_v)s
                                   AND NOT EXISTS (SELECT 1 FROM slice_burned_mailboxes m
                                                    WHERE m.email_token = i.email_token))
                    AND NOT EXISTS (SELECT 1 FROM slice_penalties p
                                     WHERE p.pubkey IN (btrim(b.author_pubkey),
                                                        coalesce(btrim(b.relay), btrim(b.author_pubkey))))
                    AND NOT EXISTS (SELECT 1 FROM p2p_node_bans n
                                     WHERE lower(n.pubkey) IN (btrim(b.author_pubkey),
                                                               coalesce(btrim(b.relay), btrim(b.author_pubkey))))
                    AND NOT EXISTS (SELECT 1 FROM p2p_identities f
                                     WHERE f.pubkey = btrim(b.relay) AND f.status = 'failed'))
        """, {"own": (own_pubkey or "").lower(), "checks": checks,
              "bound": version_bound(conn, family), "cert_v": CERT_VERSION})
        return int(cur.fetchone()[0])


# --------------------------------------------------------------------------
# The check
# --------------------------------------------------------------------------

def _core(blob_gz: bytes) -> Tuple[dict, Optional[Dict[str, list]]]:
    """The blob's core and, for an MB blob, its rows as the import checked
    them (desktop/mb_slice_client._rows) — None when they would not pass."""
    from desktop.mb_slice_client import _rows
    from desktop.p2p import wire
    core = wire.loads(gzip.decompress(blob_gz))
    return core, (_rows(core) if "tables" in core else {})


def _lb_recordings(core: dict, mbid: str) -> List[str]:
    """The recording MBIDs of an LB blob the import counts for its artist
    (lb_slice_queries._row_of), in their canonical form."""
    from desktop.p2p.lb_slice_queries import _row_of, mbid_key
    return sorted({mbid_key(r[0]) for r in core.get("recordings") or [] if _row_of(r, mbid)})


def _mb_facts(core: dict, rows: Dict[str, list]) -> dict:
    """The blob's subject artists, the groups it credits to each, and each
    group's releases in the blob's own order — what the classes read."""
    from desktop.p2p.mb_slice_queries import SLICE_TABLES

    def col(t, c):
        return SLICE_TABLES[t].index(c)
    matched = set()
    for ids in (core.get("artists_matched") or {}).values():
        matched.update(ids or [])
    gid_of = {r[col("mb_artist", "id")]: r[col("mb_artist", "gid")] for r in rows.get("mb_artist") or []}
    credits: Dict[int, set] = {}
    for r in rows.get("mb_artist_credit_name") or []:
        if r[col("mb_artist_credit_name", "artist")] in matched:
            credits.setdefault(r[col("mb_artist_credit_name", "artist_credit")], set()).add(
                r[col("mb_artist_credit_name", "artist")])
    releases: Dict[int, List[str]] = {}
    for r in rows.get("mb_release") or []:
        releases.setdefault(r[col("mb_release", "release_group")], []).append(r[col("mb_release", "gid")])
    group_gid = {g[col("mb_release_group", "id")]: g[col("mb_release_group", "gid")]
                 for g in rows.get("mb_release_group") or []}
    groups: Dict[str, List[Tuple[str, List[str]]]] = {}
    for r in rows.get("mb_release_group") or []:
        for a in credits.get(r[col("mb_release_group", "artist_credit")], ()):
            if gid_of.get(a):
                groups.setdefault(gid_of[a], []).append(
                    (r[col("mb_release_group", "gid")], releases.get(r[col("mb_release_group", "id")], [])))
    subjects = sorted({gid_of[a] for a in matched if gid_of.get(a)})
    recordings = sorted({r[col("mb_recording", "gid")] for r in rows.get("mb_recording") or []
                         if r[col("mb_recording", "gid")]})
    all_releases = sorted({g for rels in releases.values() for g in rels if g})
    group_of_release = {r[col("mb_release", "gid")]: group_gid.get(r[col("mb_release", "release_group")])
                        for r in rows.get("mb_release") or []}
    return {"subjects": subjects, "groups": groups, "recordings": recordings,
            "releases": all_releases, "group_of_release": group_of_release}


def _plan(facts: dict, rng: random.Random) -> Tuple[List[str], List[Tuple[str, str]]]:
    """(the subject artists checked, the random facts) of one check."""
    subjects = facts["subjects"]
    picked = subjects if len(subjects) <= SUBJECTS_PER_CHECK else rng.sample(subjects, SUBJECTS_PER_CHECK)
    pool = [("release", g) for g in facts["releases"]] + [("recording", g) for g in facts["recordings"]]
    extra = pool if len(pool) <= RANDOM_FACTS else rng.sample(pool, RANDOM_FACTS)
    return sorted(picked), sorted(extra)


def request_check(conn, family: str, key: str, rng: Optional[random.Random] = None) -> None:
    """Draw the blob's check and queue what it asks MusicBrainz, the check
    waiting on it (an MB blob: its subject artists and their groups, and
    random releases or recordings; an LB blob: random recordings it counts).
    In the caller's transaction. A node that does not ask MusicBrainz checks
    nothing."""
    if not mb_asks.node_asks(conn):
        return
    b = _blob(conn, family, key)
    if b is None:
        return
    with conn.cursor() as cur:
        # A check under way keeps the facts it drew: asked again meanwhile, it
        # is not drawn anew (its answers would never all be in).
        cur.execute("""SELECT 1 FROM slice_check_plans WHERE family = %s AND key = %s
                          AND planned_at > now() - interval '1 day'""", (family, key))
        if cur.fetchone():
            return
    rng = rng or random.SystemRandom()
    core, rows = _core(b["blob_gz"])
    if rows is None:
        _malformed(conn, family, key, b["payload"])
        return
    if family == "lb":
        recs = _lb_recordings(core, key)
        subjects, extra = [], [("recording", r) for r in
                               (recs if len(recs) <= RANDOM_FACTS else rng.sample(recs, RANDOM_FACTS))]
        # The artist too, when a recording was drawn: a merged one answers
        # under its target, whom the drawn recordings may credit.
        items = list(extra) + ([("artist", key)] if extra else [])
    else:
        subjects, extra = _plan(_mb_facts(core, rows), rng)
        items = ([("artist", a) for a in subjects] + [("artist_release_groups", a) for a in subjects]
                 + list(extra))
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO slice_check_plans (family, key, subjects, releases, recordings)
            VALUES (%s, %s, %s::uuid[], %s::uuid[], %s::uuid[])
            ON CONFLICT (family, key) DO UPDATE
               SET subjects = EXCLUDED.subjects, releases = EXCLUDED.releases,
                   recordings = EXCLUDED.recordings, planned_at = now()""",
                    (family, key, subjects, [m for k, m in extra if k == "release"],
                     [m for k, m in extra if k == "recording"]))
    if items:
        mb_asks.enqueue(conn, items, PRIORITY, SUBJECT, subject_key(family, key), b["author"])
    else:
        evaluate(conn, family, key)


_PAYLOAD = {"mb": "coalesce(payload_sha256, encode(sha256(blob_gz), 'hex'))",
            "lb": "encode(sha256(blob_gz), 'hex')"}

# Which of a kept blob's indexed subjects (slice_index) an answer's kind names.
_SUBJECT_COLUMN = {"artist": "subject_artists", "release_group": "subject_groups"}

_MB_PAYLOAD_OF_B = "coalesce(b.payload_sha256, encode(sha256(b.blob_gz), 'hex'))"


def uncheck_subject(conn, kind: str, mbid: str) -> int:
    """MusicBrainz answered `mbid` unknown: every kept blob about it — an MB
    blob indexed with it as a subject, by the version of the blob its index
    row names; the LB blob of that artist, whose one subject is its key — is
    checked no more, so it is passed on again only after a check made since
    (rule 0's condition 4 in its narrowest form: the canon's own
    confirmations reach the check through the answers they store). Run by the
    backend's slice checker, after the answer's commit and between two
    verdicts, so a verdict's stamp never lands over it; under a verdict's
    lock, as the verdicts take it. Returns the blobs un-checked."""
    from desktop.p2p import slice_rebuild
    column = _SUBJECT_COLUMN.get(kind)
    if column is None:
        return 0
    with slice_rebuild.verdict(conn), conn.cursor() as cur:
        cur.execute(f"""UPDATE mb_slice_blobs b SET checked_at = NULL FROM slice_index i
                         WHERE i.family = 'mb' AND i.{column} @> ARRAY[%(m)s::uuid]
                           AND b.name_key = i.key AND b.checked_at IS NOT NULL
                           AND i.payload_sha256 = {_MB_PAYLOAD_OF_B}""", {"m": mbid})
        unchecked = cur.rowcount
        if column == "subject_artists":
            cur.execute("""UPDATE lb_slice_blobs SET checked_at = NULL
                            WHERE artist_mbid = %s::uuid AND checked_at IS NOT NULL""", (mbid,))
            unchecked += cur.rowcount
    if unchecked:
        logger.debug("%s %s unknown to MusicBrainz: %d kept slice(s) to check again",
                     kind, mbid, unchecked)
    return unchecked


def uncheck_answered(conn) -> int:
    """uncheck_subject for every answer stored unknown after the check of a
    kept blob about it — those a restart took from the slice checker before
    it ran them. Returns the blobs un-checked."""
    from desktop.p2p import slice_rebuild
    with slice_rebuild.verdict(conn), conn.cursor() as cur:
        cur.execute(f"""UPDATE mb_slice_blobs b SET checked_at = NULL
                         WHERE b.checked_at IS NOT NULL AND EXISTS (
                               SELECT 1 FROM slice_index i JOIN mbws_answers a
                                   ON a.status = 'unknown' AND a.answered_at > b.checked_at
                                  AND (a.kind = 'artist' AND a.mbid = ANY(i.subject_artists)
                                       OR a.kind = 'release_group' AND a.mbid = ANY(i.subject_groups))
                                WHERE i.family = 'mb' AND i.key = b.name_key
                                  AND i.payload_sha256 = {_MB_PAYLOAD_OF_B})""")
        unchecked = cur.rowcount
        cur.execute("""UPDATE lb_slice_blobs b SET checked_at = NULL
                        WHERE b.checked_at IS NOT NULL AND EXISTS (
                              SELECT 1 FROM mbws_answers a
                               WHERE a.kind = 'artist' AND a.mbid = b.artist_mbid
                                 AND a.status = 'unknown' AND a.answered_at > b.checked_at)""")
        unchecked += cur.rowcount
    if unchecked:
        logger.info("%d kept slice(s) to check again: MusicBrainz answered one of their "
                    "subjects unknown since their check", unchecked)
    return unchecked


def _malformed(conn, family: str, key: str, payload: str) -> None:
    """A kept blob whose rows the import's own check would not pass (kept
    before it): never passed on, never checked — that blob, not one that
    replaced it meanwhile."""
    from desktop.p2p import slice_rebuild
    table, col = _TABLE[family]
    logger.warning("kept %s slice %s fails the import's row check — held back", family, key[:64])
    with slice_rebuild.verdict(conn), conn.cursor() as cur:
        cur.execute(f"UPDATE {table} SET suspect = TRUE WHERE {col}::text = %s AND {_PAYLOAD[family]} = %s",
                    (key, payload))


def _plan_of(conn, family: str, key: str) -> Optional[dict]:
    with conn.cursor() as cur:
        cur.execute("""SELECT subjects::text[], releases::text[], recordings::text[]
                         FROM slice_check_plans WHERE family = %s AND key = %s""", (family, key))
        row = cur.fetchone()
    if row is None:
        return None
    return {"subjects": list(row[0]),
            "extra": [("release", m) for m in row[1]] + [("recording", m) for m in row[2]]}


def _status(conn, kind: str, mbid: str, need: list) -> Optional[Tuple[str, Optional[str]]]:
    a = mb_asks.answer(conn, kind, mbid)
    if a is None:
        need.append((kind, mbid))
    return a


def evaluate(conn, family: str, key: str) -> Optional[bool]:
    """The check's verdict on MusicBrainz's answers: True (passed), False
    (suspect: held back), None (answers still to come — queued). Records the
    events and the day's facts of a blob whose dump is fresh, and judges its
    author and the node it came from. None, and nothing queued, on a node
    that does not ask MusicBrainz."""
    if not mb_asks.node_asks(conn):
        return None
    b = _blob(conn, family, key)
    if b is None:
        return None
    core, rows = _core(b["blob_gz"])
    if rows is None:
        _malformed(conn, family, key, b["payload"])
        return False
    need: list = []
    if family == "lb":
        plan = _plan_of(conn, family, key)
        if plan is None:
            request_check(conn, family, key)
            return None
        artist = core.get("artist_mbid")
        # A merged artist is the artist it was merged into: an honest older
        # dump credits its recordings either way.
        a = _status(conn, "artist", artist, need) if artist and plan["extra"] else None
        artists = {artist} | ({a[1]} if a and a[0] != "unknown" and a[1] else set())
        suspect = False
        for _kind, m in plan["extra"]:
            st = _status(conn, "recording", m, need)
            if st is not None and (st[0] == "unknown"
                                   or not any(_credits(conn, st[1], x) for x in artists)):
                suspect = True
        if need:
            mb_asks.enqueue(conn, need, PRIORITY, SUBJECT, subject_key(family, key), b["author"])
            return None
        _finish(conn, family, key, b, facts=0, events=[], suspect=suspect)
        return not suspect
    settled = _settle_clashes(conn, key, b, need)
    if settled is None:
        mb_asks.enqueue(conn, need, PRIORITY, SUBJECT, subject_key(family, key), b["author"])
        return None
    now = _blob(conn, family, key)
    if now is None or now["payload"] != b["payload"] or now["suspect"]:
        # It lost a clash (or gave way to another blob): no check to make.
        return False
    plan = _plan_of(conn, family, key)
    if plan is None:
        request_check(conn, family, key)
        return None
    facts = _mb_facts(core, rows)
    # (kind, entity, MBID, covered): covered when found through a subject
    # artist of a blob with no more subjects than a check draws — every
    # node's check of it found the same (slice_trust.judge, rule 2 (b)).
    events: List[Tuple[str, str, str, bool]] = []
    covered = len(facts["subjects"]) <= SUBJECTS_PER_CHECK
    checked_facts = 0
    unknown_subjects = [a for a in facts["subjects"]
                        if (st := mb_asks.answer(conn, "artist", a)) and st[0] == "unknown"]
    # The facts counted are those this node checked: a subject, its groups
    # set against MusicBrainz's own list, the ones looked up outside it.
    for subject in sorted(set(plan["subjects"]) | set(unknown_subjects)):
        checked_facts += 1
        a = _status(conn, "artist", subject, need)
        if a is None:
            continue
        credited = [(g, rels) for g, rels in facts["groups"].get(subject, []) if rels]
        if a[0] == "unknown":
            if credited:
                checked_facts += 1
                first = credited[0][1][0]
                r = _status(conn, "release", first, need)
                if r is not None and r[0] == "unknown":
                    events.append(("S1", "artist", subject, covered))
            continue
        listed = _status(conn, "artist_release_groups", subject, need)
        if listed is None:
            continue
        theirs = _groups_of(conn, listed[1])
        outside = [(g, rels) for g, rels in credited if g not in theirs]
        checked_facts += (sum(1 for g, _ in facts["groups"].get(subject, []) if g in theirs)
                          + min(len(outside), GROUP_LOOKUPS))
        # Inside the first GROUP_LOOKUPS of the subject's credited groups a
        # group is among any node's lookups, whatever MusicBrainz's list held
        # when that node checked; past them, another node may not have met it.
        position = {g: i for i, (g, _) in enumerate(credited)}
        for g, rels in outside[:GROUP_LOOKUPS]:
            rg = _status(conn, "release_group", g, need)
            if rg is None or rg[0] != "unknown":
                continue
            r = _status(conn, "release", rels[0], need)
            if r is not None and r[0] == "unknown":
                events.append(("S2", "release_group", g, covered and position[g] < GROUP_LOOKUPS))
                break
    # One event per subject artist, however many of its MBIDs are involved.
    subject_of = {g: s for s, groups in facts["groups"].items() for g, _ in groups}
    hit = {subject_of.get(m, m) for _kind, _entity, m, _covered in events}
    for kind, mbid in plan["extra"]:
        st = _status(conn, kind, mbid, need)
        if st is None or st[0] != "unknown" or kind != "release":
            continue
        g = facts["group_of_release"].get(mbid)
        if g is None or subject_of.get(g, g) in hit:
            continue
        rg = _status(conn, "release_group", g, need)
        if rg is not None and rg[0] == "unknown":
            # A drawn fact: another node's draw need not have met it.
            events.append(("S2", "release_group", g, False))
            hit.add(subject_of.get(g, g))
    if need:
        mb_asks.enqueue(conn, need, PRIORITY, SUBJECT, subject_key(family, key), b["author"])
        return None
    _finish(conn, family, key, b, facts=checked_facts, events=events)
    return not events


_KIND_OF = {"mb_artist": ("artist", "artist"), "mb_release_group": ("release_group", "release_group"),
            "mb_release": ("release", "release"), "mb_recording": ("recording", "recording")}


# Wins a winning author's clashes may score a day: each takes kept blobs out
# and asks for a rebuild, so a forger who pairs removed MBIDs with real ones
# costs a node a few of them a day, never one per row.
CLASH_WINS_A_DAY = 3


def _clash_rows(conn, key: str, payload: str) -> list:
    with conn.cursor() as cur:
        cur.execute("""SELECT entity::text, incoming::text, held::text, incoming_name, held_name
                         FROM slice_clashes WHERE family = 'mb' AND key = %s AND payload_sha256 = %s
                        ORDER BY entity, incoming, held""", (key, payload))
        return cur.fetchall()


def _named_by(conn, kind: str, mbid: str, name: Optional[str]) -> bool:
    """MusicBrainz's answer for `mbid` carries `name`: the artist's name or
    an alias, the group's title."""
    if not name:
        return False
    with conn.cursor() as cur:
        if kind == "artist":
            cur.execute("""SELECT EXISTS (SELECT 1 FROM mbws_artists
                                           WHERE mbid = %(m)s::uuid AND lower(name) = lower(%(n)s))
                               OR EXISTS (SELECT 1 FROM mbws_artist_aliases
                                           WHERE artist = %(m)s::uuid AND lower(name) = lower(%(n)s))""",
                        {"m": mbid, "n": name})
        else:
            cur.execute("""SELECT EXISTS (SELECT 1 FROM mbws_release_groups
                                           WHERE mbid = %(m)s::uuid AND lower(title) = lower(%(n)s))""",
                        {"m": mbid, "n": name})
        return bool(cur.fetchone()[0])


def _clash_verdict(conn, row: tuple, need: list) -> Optional[str]:
    """'won' or 'lost' for one clash of a blob, None while its answers are
    missing (noted in `need`). An id under two MBIDs: this blob wins only
    where MusicBrainz knows its MBID and not the held one. One MBID under two
    ids: only where MusicBrainz's answer carries this blob's row name and not
    the held row's. Anything else keeps the held row."""
    table, incoming, held, incoming_name, held_name = row
    kind = _KIND_OF[table][0]
    if incoming == held:
        a = _status(conn, kind, incoming, need)
        if a is None:
            return None
        if a[0] == "unknown":
            return "lost"
        mine = _named_by(conn, kind, a[1], incoming_name)
        theirs = _named_by(conn, kind, a[1], held_name)
        return "won" if mine and not theirs else "lost"
    a, h = _status(conn, kind, incoming, need), _status(conn, kind, held, need)
    if a is None or h is None:
        return None
    return "won" if h[0] == "unknown" and a[0] != "unknown" else "lost"


def clash_questions(conn, key: str, payload: str, need: list) -> None:
    """What the next unsettled clash of the blob asks MusicBrainz, into
    `need` — one clash at a time, the next only once the one before is won."""
    for row in _clash_rows(conn, key, payload):
        if _clash_verdict(conn, row, need) != "won":
            return


def lose(conn, key: str, payload: str) -> None:
    """The blob of `key` with `payload` lost a clash: out of every rebuild and
    not passed on, its name asked again of another author first (reopen).
    Nothing is held against anyone. In the caller's transaction."""
    with conn.cursor() as cur:
        cur.execute(f"""UPDATE mb_slice_blobs SET suspect = TRUE, contested = TRUE
                         WHERE name_key = %s AND {_PAYLOAD["mb"]} = %s""", (key, payload))
        if cur.rowcount:
            reopen(conn, [key])


def _settle_clashes(conn, key: str, b: dict, need: list) -> Optional[bool]:
    """This blob's clashes (its own payload's: an earlier blob of the name
    was judged on its own rows) settled on MusicBrainz's answers — which rows
    land, nothing more — one at a time, the first loss ending it. When every
    one is won, the kept blobs that carry the held rows lose (_lose_held), the
    name closes and a rebuild lands this blob (slice_rebuild.request) — at
    most CLASH_WINS_A_DAY such wins a day for one author; past that the blob
    loses. A blob that loses stays out of every rebuild and is not passed on,
    and its name is asked again, of another author first (`lose`). No one is
    blamed: the unknown side can be an honest dump of a release MusicBrainz
    removed since, the known one any real MBID a forger chose. True when
    nothing is left to settle, None while answers are missing."""
    rows = _clash_rows(conn, key, b["payload"])
    if not rows:
        return True
    won = []
    for row in rows:
        verdict = _clash_verdict(conn, row, need)
        if verdict is None:
            return None
        if verdict == "lost":
            break
        won.append(row)
    from desktop.p2p import slice_rebuild
    with slice_rebuild.verdict(conn):
        with conn.cursor() as cur:
            cur.execute("DELETE FROM slice_clashes WHERE family = 'mb' AND key = %s AND payload_sha256 = %s",
                        (key, b["payload"]))
        if len(won) < len(rows) or not _win_allowed(conn, b["author"]):
            lose(conn, key, b["payload"])
            return True
        for row in won:
            _lose_held(conn, row, key)
        with conn.cursor() as cur:
            cur.execute("UPDATE mb_slice_fetches SET void = FALSE WHERE name_key = %s", (key,))
        slice_rebuild.request(conn, "clash")
    return True


def _win_allowed(conn, author: str) -> bool:
    """One more clash win for `author` today, when it has fewer than
    CLASH_WINS_A_DAY — counted, in the caller's transaction."""
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO slice_clash_wins (day, author, wins) VALUES (current_date, %s, 1)
                       ON CONFLICT (day, author) DO UPDATE SET wins = slice_clash_wins.wins + 1
                        WHERE slice_clash_wins.wins < %s
                       RETURNING wins""", (author.lower(), CLASH_WINS_A_DAY))
        return cur.fetchone() is not None


def reopen(conn, keys: List[str]) -> None:
    """The names whose answer counts for nothing now (its blob lost or is in a
    clash, or a rebuild left it out): asked again at once, of another author
    first (mb_slice_cycle.reopened_authors), the slice cycle woken for them."""
    if not keys:
        return
    with conn.cursor() as cur:
        cur.execute("UPDATE mb_slice_fetches SET void = TRUE WHERE name_key = ANY(%s)", (sorted(keys),))
        if cur.rowcount:
            cur.execute("NOTIFY sautium_mb_pending")


def _lose_held(conn, row: tuple, key: str) -> None:
    """The kept blobs that carry the held row a won clash went against —
    found by the row itself, whoever authored or passed them: an id's held
    MBID by the blobs that carry that MBID (mb_slice_blobs.mbids); an MBID
    held under another id by the blobs whose rows hold that id under it.
    They lose (`lose`); nothing is held against their authors."""
    table, incoming, held, _incoming_name, _held_name = row
    with conn.cursor() as cur:
        cur.execute(f"""SELECT name_key, {_PAYLOAD["mb"]}, blob_gz FROM mb_slice_blobs
                         WHERE mbids @> ARRAY[%s::uuid] AND name_key <> %s""", (held, key))
        candidates = cur.fetchall()
        held_id = None
        if incoming == held:
            cur.execute(f"SELECT id FROM {table} WHERE gid = %s::uuid", (held,))
            found = cur.fetchone()
            held_id = found[0] if found else None
    from desktop.p2p.mb_slice_queries import SLICE_TABLES
    cols = SLICE_TABLES[table]
    i, g = cols.index("id"), cols.index("gid")
    for name_key, payload, blob_gz in candidates:
        if incoming == held:
            _core_, rows = _core(bytes(blob_gz))
            if rows is None or not any(r[i] == held_id and r[g] == held for r in rows.get(table) or []):
                continue
        lose(conn, name_key, payload)


def _groups_of(conn, artist: Optional[str]) -> set:
    if artist is None:
        return set()
    with conn.cursor() as cur:
        cur.execute("SELECT release_group::text FROM mbws_artist_release_groups WHERE artist = %s::uuid",
                    (artist,))
        return {r[0] for r in cur.fetchall()}


def _credits(conn, recording: Optional[str], artist: Optional[str]) -> bool:
    if recording is None or artist is None:
        return False
    with conn.cursor() as cur:
        cur.execute("""SELECT 1 FROM mbws_credits WHERE owner_kind = 'recording'
                          AND owner = %s::uuid AND artist = %s::uuid""", (recording, artist))
        return cur.fetchone() is not None


def _finish(conn, family: str, key: str, b: dict, facts: int, events: list,
            suspect: Optional[bool] = None) -> None:
    """Stamp the check of the blob it read (by its payload: one that replaced
    it meanwhile is checked on its own), and — for a blob whose dump is fresh
    — record its events against its author and the node it came from, then
    judge both, all in one transaction: a check that fails half-way stamps
    nothing and loses no event. A penalty drops what the penalized keys
    brought (desktop/p2p/slice_rebuild.py)."""
    from desktop.p2p import slice_rebuild
    table, col = _TABLE[family]
    held = bool(events) if suspect is None else suspect
    with slice_rebuild.verdict(conn):
        with conn.cursor() as cur:
            # Suspect stays suspect for this version of the blob; a new version
            # is stored unsuspected and checked anew (store_slice_blob).
            cur.execute(f"UPDATE {table} SET checked_at = now(), suspect = suspect OR %s "
                        f"WHERE {col}::text = %s AND {_PAYLOAD[family]} = %s", (held, key, b["payload"]))
            if not cur.rowcount:
                return
            cur.execute("DELETE FROM slice_check_plans WHERE family = %s AND key = %s", (family, key))
        if family != "mb" or b["relay"] is None or not slice_trust.fresh(b["dump_version"]):
            return
        seen = set()
        counted = 0
        for kind, entity, mbid, covered in events:
            if mbid in seen:
                continue
            seen.add(mbid)
            ev = slice_trust.record(conn, family, key, b["author"], b["relay"], kind, entity, mbid,
                                    b["dump_version"], b["payload"])
            if ev is None:
                continue
            counted += 1
            if covered:
                with conn.cursor() as cur:
                    cur.execute("UPDATE slice_evidence SET covered = TRUE WHERE id = %s", (ev,))
        slice_trust.tally(conn, family, b["author"], b["relay"], facts, counted)
        penalized = slice_trust.judge(conn, family, b["author"], b["relay"])
        if penalized:
            slice_rebuild.drop(conn, penalized)


# --------------------------------------------------------------------------
# P1 — a dump version from the future
# --------------------------------------------------------------------------

def published_check(conn, family: str, newest: str, fetched_at) -> List[str]:
    """Every kept blob whose dump version is later than the newest version
    MetaBrainz has published (`newest`, its listing read at `fetched_at`),
    and that arrived at least six hours before that reading — a dump published
    minutes ago is not yet on every mirror. Each is a proven falsification
    (P1). Returns the keys penalized. An empty or malformed version is never
    P1 (slice_trust.fresh reads it as unknown). The node that passed the
    blob on is quarantined with its author only when the version was past
    what its own check bounds — a day after the blob reached this node, the
    most a node that never read the listing takes (version_bound): one that
    read it would have held the blob back anyway."""
    from desktop.p2p import slice_rebuild
    table, col = _TABLE[family]
    with conn.cursor() as cur:
        cur.execute(f"""SELECT {col}::text, dump_version, btrim(author_pubkey),
                               CASE WHEN relay IS NOT NULL AND dump_version > to_char(
                                        (created_at AT TIME ZONE 'UTC') + interval '1 day',
                                        'YYYYMMDD-HH24MISS')
                                    THEN btrim(relay) ELSE btrim(author_pubkey) END,
                               blob_gz
                          FROM {table}
                         WHERE dump_version ~ '^[0-9]{{8}}-[0-9]{{6}}$' AND dump_version > %s
                           AND created_at <= %s - interval '6 hours'""", (newest, fetched_at))
        rows = cur.fetchall()
    out: List[str] = []
    with slice_rebuild.verdict(conn):
        for key, version, author, relay, blob_gz in rows:
            out += slice_trust.proven(conn, family, key, author, relay, "P1", "dump_version", None,
                                      version, hashlib.sha256(bytes(blob_gz)).hexdigest())
        if out:
            slice_rebuild.drop(conn, sorted(set(out)))
    return out
