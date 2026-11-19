"""Write tests/fixtures/canon_classical/mb.json: the rows of the `mb_*` tables
the canon reads for a list of MusicBrainz releases (CC0), taken from a
database with a dump loaded (CANON-CLASSICAL-DESIGN §6.1).

For each release: its group with the group's types, its media and their
formats, its tracks and their recordings, its labels' catalogue numbers, and
every artist credit any of them names, with its artists and their aliases.
The rows are written with the dump's version; the public suite loads them into
its scratch database's `mb_*` tables (tests/canon_classical/fixtures.py).

    python scripts/make_canon_fixtures.py <dsn> <release mbid> [...]       # writes the file
    DUMP_VERSION=… python scripts/make_canon_fixtures.py …                 # a database with no dump marker
"""
import json
import os
import sys
from pathlib import Path

import psycopg2

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "canon_classical" / "mb.json"

# (table, the rows' query over the temp table _rel of release ids)
QUERIES = [
    ("mb_release", "SELECT r.* FROM mb_release r JOIN _rel x ON x.id = r.id"),
    ("mb_release_group", "SELECT DISTINCT g.* FROM mb_release_group g JOIN mb_release r ON r.release_group = g.id "
                         "JOIN _rel x ON x.id = r.id"),
    ("mb_release_group_primary_type", "SELECT * FROM mb_release_group_primary_type"),
    ("mb_release_group_secondary_type", "SELECT * FROM mb_release_group_secondary_type"),
    ("mb_release_group_secondary_type_join",
     "SELECT DISTINCT j.* FROM mb_release_group_secondary_type_join j JOIN mb_release r "
     "ON r.release_group = j.release_group JOIN _rel x ON x.id = r.id"),
    ("mb_medium", "SELECT m.* FROM mb_medium m JOIN _rel x ON x.id = m.release"),
    ("mb_medium_format", "SELECT * FROM mb_medium_format"),
    ("mb_track", "SELECT t.* FROM mb_track t JOIN mb_medium m ON m.id = t.medium JOIN _rel x ON x.id = m.release"),
    ("mb_recording", "SELECT DISTINCT rc.* FROM mb_recording rc JOIN mb_track t ON t.recording = rc.id "
                     "JOIN mb_medium m ON m.id = t.medium JOIN _rel x ON x.id = m.release"),
    ("mb_release_label", "SELECT rl.* FROM mb_release_label rl JOIN _rel x ON x.id = rl.release"),
]
CREDITS_SQL = """
SELECT DISTINCT c FROM (
    SELECT r.artist_credit FROM mb_release r JOIN _rel x ON x.id = r.id
    UNION SELECT g.artist_credit FROM mb_release_group g JOIN mb_release r ON r.release_group = g.id
                                                       JOIN _rel x ON x.id = r.id
    UNION SELECT t.artist_credit FROM mb_track t JOIN mb_medium m ON m.id = t.medium JOIN _rel x ON x.id = m.release
    UNION SELECT rc.artist_credit FROM mb_recording rc JOIN mb_track t ON t.recording = rc.id
          JOIN mb_medium m ON m.id = t.medium JOIN _rel x ON x.id = m.release) q(c)"""


def rows(cur, sql, params=None) -> dict:
    cur.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return {"columns": cols, "rows": [[None if v is None else str(v) for v in r] for r in cur.fetchall()]}


def main(dsn: str, releases: list) -> None:
    conn = psycopg2.connect(dsn)
    cur = conn.cursor()
    cur.execute("CREATE TEMP TABLE _rel AS SELECT id FROM mb_release WHERE gid = ANY(%s::uuid[])", (releases,))
    cur.execute("SELECT count(*) FROM _rel")
    assert cur.fetchone()[0] == len(set(releases)), "a release MBID is not in this dump"
    tables = {t: rows(cur, q) for t, q in QUERIES}
    cur.execute(CREDITS_SQL)
    credits = [r[0] for r in cur.fetchall()]
    tables["mb_artist_credit"] = rows(cur, "SELECT * FROM mb_artist_credit WHERE id = ANY(%s)", (credits,))
    tables["mb_artist_credit_name"] = rows(cur, "SELECT * FROM mb_artist_credit_name WHERE artist_credit = ANY(%s)",
                                           (credits,))
    cur.execute("SELECT DISTINCT artist FROM mb_artist_credit_name WHERE artist_credit = ANY(%s)", (credits,))
    artists = [r[0] for r in cur.fetchall()]
    tables["mb_artist"] = rows(cur, "SELECT * FROM mb_artist WHERE id = ANY(%s)", (artists,))
    tables["mb_artist_alias"] = rows(cur, "SELECT * FROM mb_artist_alias WHERE artist = ANY(%s)", (artists,))
    cur.execute("SELECT value FROM user_settings WHERE key = 'musicbrainz.db_version'")
    row = cur.fetchone()
    version = row[0] if row else os.environ["DUMP_VERSION"]   # a scratch load names it instead
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"source": "MusicBrainz (CC0)", "dump": version, "releases": sorted(releases),
                               "tables": tables}, ensure_ascii=False, indent=0) + "\n", encoding="utf-8")
    print(f"{len(releases)} releases, {sum(len(t['rows']) for t in tables.values())} rows, dump {version}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2:])
