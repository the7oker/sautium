"""Write desktop/password_lists/, the lists the password estimate reads beside
zxcvbn's own (desktop/password_strength.py; SEC-S2-PROPOSAL §3, §14):

  * eff_large_wordlist.txt — EFF's long diceware list (CC BY), as EFF
    publishes it, checked against the SHA-256 it had when the list was taken.
    The phrase generator draws from it and the estimate prices its words.
  * phrases.txt — famous phrases from Wikidata (CC0): the titles of the
    works most linked across Wikipedias (films, songs, singles, albums,
    literary works, television series, video games) and every slogan,
    motto, proverb, battle cry, catchphrase and quotation that has an item
    of its own, in their original form — the work's title (P1476) or native
    label (P1705), the English label only where Wikidata names neither.
    Only phrases of two words or more: a single word is in zxcvbn's own
    lists or in no list at all. Ranked by sitelinks, most linked first.

Nothing language-specific is made here: the lists are the world's titles,
whatever their language (the owner, SEC-S2 §14 Q3).

Both files ship with the release, and password_strength loads them only
under the digests it pins; this script says how they were made. The
Wikidata half is a snapshot: run again, it makes the list of that day.

    python scripts/make_password_lists.py           # writes both files
    python scripts/make_password_lists.py --check   # EFF's file as pinned; the phrases as committed
"""

import argparse
import csv
import hashlib
import io
import re
import sys
import unicodedata
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "desktop" / "password_lists"

EFF_URL = "https://www.eff.org/files/2016/07/18/eff_large_wordlist.txt"
EFF_SHA256 = "addd35536511597a02fa0a9ff1e5284677b8883b83e986e43f15a3db996b903e"

# QLever's index of Wikidata: the same CC0 data as query.wikidata.org, whose
# sitelink ordering over the film class times out
SPARQL = "https://qlever.dev/api/wikidata"
USER_AGENT = "make-password-lists/1.0"

# class → how many of its most linked items are read
WORKS = {
    "Q11424": 6000,     # film
    "Q7366": 3000,      # song
    "Q134556": 2000,    # single
    "Q482994": 2000,    # album
    "Q7725634": 3000,   # literary work
    "Q5398426": 2000,   # television series
    "Q7889": 1000,      # video game
}
SAYINGS = ("Q30515", "Q1094729", "Q2101619", "Q1433428", "Q42470", "Q35102", "Q2708515",
           "Q206287")   # slogan, advertising and political slogan, battle cry, motto, proverb, catchphrase, quotation
# Phrases known as password advice, which no class above holds
KNOWN = ("correct horse battery staple",)   # xkcd 936, "Password Strength"
PHRASES_MAX = 20000
PHRASE_CHARS_MAX = 64

_PREFIXES = ("PREFIX wd: <http://www.wikidata.org/entity/> "
             "PREFIX wdt: <http://www.wikidata.org/prop/direct/> "
             "PREFIX wikibase: <http://wikiba.se/ontology#> "
             "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#> ")


def _query(select: str) -> list:
    url = SPARQL + "?" + urllib.parse.urlencode({"query": _PREFIXES + select})
    req = urllib.request.Request(url, headers={"Accept": "text/csv", "User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=300) as r:
        return list(csv.DictReader(io.StringIO(r.read().decode("utf-8"))))


def _forms_query(where: str, limit: int = 0) -> str:
    return ("SELECT ?item ?sl ?t ?nl ?en WHERE { " + where + " ?item wikibase:sitelinks ?sl . "
            "OPTIONAL { ?item wdt:P1476 ?t } OPTIONAL { ?item wdt:P1705 ?nl } "
            "OPTIONAL { ?item rdfs:label ?en FILTER(LANG(?en) = \"en\") } } ORDER BY DESC(?sl)"
            + (f" LIMIT {limit * 4}" if limit else ""))


def fold(text: str) -> str:
    """A phrase as the estimate matches it: NFKC, lower case, apostrophes
    dropped, every character that is not a letter, a digit or a mark a
    space, collapsed. Marks stay: a Hebrew point or a Devanagari vowel sign
    is part of its word."""
    text = re.sub(r"['’ʼ`]", "", unicodedata.normalize("NFKC", text).lower())
    return " ".join("".join(ch if unicodedata.category(ch)[0] in "LNM" else " " for ch in text).split())


def _phrases_of(rows: list, limit: int) -> list:
    """(sitelinks, phrase) of the first `limit` items, each item's original
    forms — P1476 and P1705, the English label only where both are absent."""
    by_item: dict = {}
    for row in rows:
        entry = by_item.setdefault(row["item"], {"sl": int(row["sl"] or 0), "orig": set(), "en": set()})
        for key in ("t", "nl"):
            if row.get(key):
                entry["orig"].add(row[key])
        if row.get("en"):
            entry["en"].add(row["en"])
    items = sorted(by_item.values(), key=lambda e: -e["sl"])
    if limit:
        items = items[:limit]
    return [(e["sl"], name) for e in items for name in (e["orig"] or e["en"])]


def make_phrases() -> list:
    found = []
    for cls, limit in WORKS.items():
        found += _phrases_of(_query(_forms_query(f"?item wdt:P31 wd:{cls} .", limit)), limit)
    values = " ".join(f"wd:{c}" for c in SAYINGS)
    found += _phrases_of(_query(_forms_query(f"VALUES ?c {{ {values} }} ?item wdt:P31 ?c .")), 0)
    seen, phrases = set(KNOWN), list(KNOWN)
    for _sl, name in sorted(found, key=lambda x: -x[0]):
        phrase = fold(name)
        if len(phrase.split()) < 2 or len(phrase) > PHRASE_CHARS_MAX or phrase in seen:
            continue
        seen.add(phrase)
        phrases.append(phrase)
    return phrases[:PHRASES_MAX]


def phrases_text(phrases: list) -> str:
    return ("# Famous phrases: titles of the most linked works and every saying with an item of its own,\n"
            f"# from Wikidata (CC0, https://www.wikidata.org/wiki/Wikidata:Licensing), taken {date.today()}\n"
            "# through QLever's index; made by scripts/make_password_lists.py. One phrase a line, most linked first.\n"
            + "".join(p + "\n" for p in phrases))


def fetch_eff() -> bytes:
    req = urllib.request.Request(EFF_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as r:
        body = r.read()
    got = hashlib.sha256(body).hexdigest()
    if got != EFF_SHA256:
        raise SystemExit(f"EFF's list is not the one pinned: sha256 {got}")
    return body


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    eff = fetch_eff()
    if args.check:
        same = (OUT / "eff_large_wordlist.txt").read_bytes() == eff
        print("EFF: same" if same else "EFF: differs")
        return 0 if same else 1
    phrases = make_phrases()
    OUT.mkdir(exist_ok=True)
    (OUT / "eff_large_wordlist.txt").write_bytes(eff)
    (OUT / "phrases.txt").write_text(phrases_text(phrases), encoding="utf-8", newline="\n")
    print(f"EFF: 7776 words; phrases: {len(phrases)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
