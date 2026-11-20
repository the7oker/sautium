"""MusicBrainz's rows for the small set (tests/fixtures/canon_classical/mb.json,
made by scripts/make_canon_fixtures.py from the 20261111 dump; CC0): loaded
into a scratch database's `mb_*` tables, and each release as the generator
takes it."""
import json
from pathlib import Path

import psycopg2.extras

MB = Path(__file__).resolve().parent.parent / "fixtures" / "canon_classical" / "mb.json"
FORMS = MB.parent / "forms.json"

POLARITY = "ce732e13-4cca-443e-9b81-512891122f1a"
MEYERS = "f336c006-8ec0-4d65-8692-27e6e55eece2"
ZUKERMAN = "09f8a9c3-b863-4605-a494-a780b1c56b59"
GOLDBERG = "3c53507c-8a62-4c00-933f-d5bfda475b02"
APPASSIONATA = "7db1555d-0e99-447c-9c21-10426f14ed6c"        # a group with no primary type
BEETHOVEN_1_2 = "92c81fd7-b4d6-462b-bcba-5de00b0648a8"
BEETHOVEN_4_5 = "c2953d75-92e0-4541-90b3-8a149975c012"
BEETHOVEN_VOL_2 = "d735d66b-1a2d-4540-aab9-9d0fe14dc667"     # the duplicate group
SPES = "fe1c6411-8747-42ad-a9c1-04b8df229a98"                # one barcode,
JACO = "bbada3c5-5ed4-488d-acde-96cc3dbf35e1"                # two releases
# §12.7's two failed binds of the 50-bind gate: one disc of a box, and the one-disc release carrying its recordings
HITSTORY = "09ce2468-229a-4752-b7c1-0bdab7767d77"            # three media, the first Elv1s' 31
ELV1S = "a80c2124-5073-3628-b783-6aba8d945764"
WORKS_1965_1995 = "cebd2f0d-1bea-4d46-b92e-aa0830b01509"     # ten media, the tenth City Life / Proverb's 7
CITY_LIFE = "1834697e-d64d-45b0-a800-058de6fb9846"


def data() -> dict:
    return json.loads(MB.read_text(encoding="utf-8"))


def load(cur) -> None:
    """The fixture rows into the `mb_*` tables (emptied first)."""
    tables = data()["tables"]
    cur.execute("TRUNCATE " + ", ".join(tables))
    for table, t in tables.items():
        if t["rows"]:
            psycopg2.extras.execute_values(
                cur, f"INSERT INTO {table} ({', '.join(t['columns'])}) VALUES %s ON CONFLICT DO NOTHING",
                t["rows"], page_size=500)


def release(mbid: str, medium: int = None) -> dict:
    """{"mbid", "rg", "title", "artists", "tracks": [(title, length ms, recording mbid)], "barcode"} — what the
    generator takes, read from the fixture rows themselves; with `medium`, that medium's tracks alone."""
    t = {name: [dict(zip(x["columns"], r)) for r in x["rows"]] for name, x in data()["tables"].items()}
    rel = next(r for r in t["mb_release"] if r["gid"] == mbid)
    rg = next(g for g in t["mb_release_group"] if g["id"] == rel["release_group"])
    names = sorted((n for n in t["mb_artist_credit_name"] if n["artist_credit"] == rel["artist_credit"]),
                   key=lambda n: int(n["position"]))
    recs = {r["id"]: r for r in t["mb_recording"]}
    media = sorted((m for m in t["mb_medium"] if m["release"] == rel["id"]
                    and (medium is None or int(m["position"]) == medium)), key=lambda m: int(m["position"]))
    tracks = []
    for m in media:
        for tr in sorted((x for x in t["mb_track"] if x["medium"] == m["id"] and x["is_data_track"] != "True"),
                         key=lambda x: int(x["position"])):
            rec = recs[tr["recording"]]
            length = tr["length"] or rec["length"]
            tracks.append((tr["name"], int(length) if length else None, rec["gid"]))
    return {"mbid": mbid, "rg": rg["gid"], "title": rel["name"], "artists": [n["name"] for n in names],
            "tracks": tracks, "barcode": rel["barcode"]}
