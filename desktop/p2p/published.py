"""The newest dumps MetaBrainz has published, read from its own listings.

What a node measures a slice's dump version against. A version later than the
newest MetaBrainz has published is a falsification no honest node produces
(P1, desktop/p2p/slice_checks.published_check), and the newest ListenBrainz
version a reachable source serves decides which ListenBrainz slices are asked
again. Both come from MetaBrainz's listings, never from a peer's word: a
stranger's claimed version could otherwise freeze every node's asking.

Each listing is read at most once a day per node (`newest`, cached in
user_settings with the time it was read), and a listing that could not be
read is not asked again for an hour (RETRY), however often the cycles run.
The request carries no name, no MBID and no taste. The ListenBrainz listing's parsing is the dump loader's too
(backend/lb_dump_load.py imports it).
"""

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

USER_AGENT = "Sautium/1.0 ( https://sautium.net )"
MB_LATEST = "https://data.metabrainz.org/pub/musicbrainz/data/fullexport/LATEST"
LB_FULLEXPORT = "https://data.metabrainz.org/pub/musicbrainz/listenbrainz/fullexport"
REFRESH = timedelta(days=1)
RETRY = timedelta(hours=1)
_VERSION = re.compile(r"^\d{8}-\d{6}$")

# The ListenBrainz mirror is an nginx autoindex (no LATEST file): the newest
# export is the highest dump id; a directory still being uploaded lacks the
# archive or its checksum and is skipped for the previous one.
DIR_RE = re.compile(r'href="(listenbrainz-dump-(\d+)-\d{8}-\d{6}-full)/?"')
ARCHIVE_RE = re.compile(r'href="(listenbrainz-statistics-dump-(\d{8}-\d{6})\.tar\.zst)"')
SHA_RE = re.compile(r'href="listenbrainz-statistics-dump-(\d{8}-\d{6})\.tar\.zst\.sha256"')


def parse_listing(html: str) -> List[Tuple[int, str]]:
    """(dump id, directory name) for every full export in the listing, newest first."""
    return sorted(((int(i), d) for d, i in DIR_RE.findall(html)), reverse=True)


def parse_directory(html: str) -> Optional[Tuple[str, str]]:
    """(version, archive file name) when the directory holds the statistics
    archive AND its .sha256 — anything less is an upload in progress."""
    m = ARCHIVE_RE.search(html)
    if not m:
        return None
    version = m.group(2)
    if version not in SHA_RE.findall(html):
        return None
    return version, m.group(1)


def _text(url: str) -> str:
    """One listing page (standard library only: the launcher's P2P layer
    carries no HTTP client of its own beyond aiohttp's server). An HTTP error
    raises, as a redirect is followed."""
    from urllib.request import Request, urlopen
    with urlopen(Request(url, headers={"User-Agent": USER_AGENT}), timeout=30.0) as r:
        return r.read(1 << 20).decode("utf-8", "replace")


def fetch(family: str) -> Optional[str]:
    """The newest published version of `family` ('mb', 'lb'), or None when the
    listing cannot be read."""
    try:
        if family == "mb":
            version = _text(MB_LATEST).strip()
            return version if _VERSION.match(version) else None
        for _, d in parse_listing(_text(f"{LB_FULLEXPORT}/"))[:3]:
            found = parse_directory(_text(f"{LB_FULLEXPORT}/{d}/"))
            if found:
                return found[0]
    except Exception as e:
        logger.warning("the %s dump listing could not be read: %s", family.upper(), e)
    return None


def _when(held: Optional[dict], field: str) -> Optional[datetime]:
    return datetime.fromisoformat(held[field]) if held and held.get(field) else None


def newest(conn, family: str) -> Optional[Tuple[str, datetime]]:
    """(the newest published version, when the listing was read) — from the
    node's record when it is less than a day old, else read now. A listing
    that cannot be read leaves the record's version as it was and is not
    asked again before RETRY has passed. The version never moves back: a dump
    MetaBrainz withdrew after publishing it was published, and the nodes that
    loaded it are no forgers (P1 is judged against this)."""
    key = f"{family}_slice.published"
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM user_settings WHERE key = %s", (key,))
        row = cur.fetchone()
    held = row[0] if row and isinstance(row[0], dict) else None
    read_at, failed_at = _when(held, "read_at"), _when(held, "failed_at")
    kept = (held["version"], read_at) if held and held.get("version") else None
    now = datetime.now(timezone.utc)
    if kept and read_at > now - REFRESH:
        return kept
    if failed_at and failed_at > now - RETRY:
        return kept
    version = fetch(family)
    if version is not None and kept and _VERSION.match(kept[0] or "") and kept[0] > version:
        version = kept[0]
    record = ({"version": version, "read_at": now.isoformat()} if version is not None else
              {**({"version": held["version"], "read_at": held["read_at"]} if kept else {}),
               "failed_at": now.isoformat()})
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO user_settings (key, value) VALUES (%s, %s::jsonb)
                       ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value,
                                                       updated_at = CURRENT_TIMESTAMP""",
                    (key, json.dumps(record)))
    return (version, now) if version is not None else kept
