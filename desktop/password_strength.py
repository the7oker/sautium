"""How long one GPU would take to find a password — the estimate every door
that makes or changes an account reads, and the meter beside its field
(SEC-S2-PROPOSAL; SECURITY-PRE-RELEASE §9.3).

The key is derived from the nickname and the password, and an invite code,
`/health`, a birth certificate or a share file fixes it: whoever holds one
tests guesses at home, unseen and without a limit. So the estimate is a guess
count — zxcvbn's, the Python port pinned at 4.5.0, which finds leaked
passwords, dictionary words, names, dates, keyboard rows and repeats — turned
into time by one GPU's rate at the key's ACTUAL cost (node_identity's
constants, read on every call: a new cost moves the time and nothing else).

One estimator for every surface: the backend's doors and its meter routes,
and the launcher's wizard, import this module; the browser asks the node.
Nothing here logs, keeps or returns the password; the result is a count.

What it adds to zxcvbn, language-free (the owner, SEC-S2 §14):
  * years to 2050 read as years — 4.5.0's regex stops at 2019;
  * the account's own words (`user_inputs`: the nickname, the email and its
    pieces, `sautium`);
  * EFF's long word list at its uniform rank, the list the phrase generator
    draws from — an attacker knows it, so its words cost 7,776 each;
  * famous phrases (Wikidata's titles and sayings, CC0), as typed, joined,
    and with `-` or `_`;
  * a separators pass: zxcvbn prices a word between separators as brute
    force, so the password is estimated again as its words alone — read as
    the phrase list reads a title (apostrophes dropped, anything not a
    letter, a digit or a mark a separator) and joined — and the lower count
    stands.
The lists are scripts/make_password_lists.py's, loaded only under the
digests pinned here.
"""

import collections
import hashlib
import math
import re
import secrets
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from zxcvbn import matching, scoring

from desktop import node_identity

# The estimate reads this many characters: a longer password is longer than
# any list, and gets at least the count of its first 64 (a floor — the time
# errs short). zxcvbn's cost grows with the length (about 2 s at 256) and
# with the l33t substitutions it tries, one dictionary pass each — 736 for a
# crafted 64 characters, 1.3 s; an ordinary password has a handful. So it
# tries the first L33T_SUBS_MAX: a crafted string costs ~0.1 s, and loses
# only l33t readings, which make a count higher, never lower.
ESTIMATED_CHARS = 64
L33T_SUBS_MAX = 32

YEAR = 365.25 * 86400
REFUSED_BELOW = 3600            # an hour of one GPU: refused
WORD_BELOW = YEAR               # to a year: the person's own word

PHRASE_WORDS = 4

# The words beside every new-password field (the Web UI has them in auth.js):
# the hardware the rate was measured on, and what an attacker holds
MEASURED_ON = ("measured on an RTX 4090 Laptop GPU and counted for a faster card; "
               "more or faster cards take less")
PASSWORD_NOTE = ("Anyone who has your invite code can test guesses at home, unseen and without a "
                 "limit. The time above is how long one GPU would take to find this password; a "
                 "hundred rented ones take a hundredth of it. Do not use this password anywhere "
                 "else. Personal facts — a pet's name, a street, a date — are not in what the "
                 "estimate knows.")
# The separators pass: a separator it removed is chosen from about this many
# (_separator_price says how many it prices)
SEPARATOR_CHOICES = 10

_LISTS = Path(__file__).resolve().parent / "password_lists"
_DIGESTS = {
    "eff_large_wordlist.txt": "addd35536511597a02fa0a9ff1e5284677b8883b83e986e43f15a3db996b903e",
    "phrases.txt": "53ba9c120af5c88377ef8f64def753060a708ac08d836d81290f9628cb5010a1",
}


def _read(name: str) -> str:
    # The digest of the lines, whatever a checkout made of their ends
    body = (_LISTS / name).read_bytes().replace(b"\r\n", b"\n")
    if hashlib.sha256(body).hexdigest() != _DIGESTS[name]:
        raise RuntimeError(f"password_lists/{name} is not the list this release pins")
    return body.decode("utf-8")


EFF_WORDS = tuple(line.split("\t")[1] for line in _read("eff_large_wordlist.txt").splitlines())
PHRASE_GUESSES = len(EFF_WORDS) ** PHRASE_WORDS


def _unaccented(text: str) -> str:
    return unicodedata.normalize("NFC", "".join(
        ch for ch in unicodedata.normalize("NFD", text) if unicodedata.category(ch) != "Mn"))


def _phrase_forms() -> dict:
    ranked: dict = {}
    phrases = [line for line in _read("phrases.txt").splitlines() if line and not line.startswith("#")]
    for rank, phrase in enumerate(phrases, 1):
        for base in {phrase, _unaccented(phrase)}:
            for joiner in (" ", "", "-", "_"):
                ranked.setdefault(joiner.join(base.split()), rank)
    return ranked


# zxcvbn's own tables, widened once at import. They are module globals: its
# repeat matcher reads them directly whatever dictionaries a call passes, so
# the lists go in there; only `user_inputs` is per call (estimate), and
# zxcvbn() itself — which writes the global one — is never called.
matching.REGEXEN["recent_year"] = re.compile(r"19\d\d|20[0-4]\d|2050")
matching.RANKED_DICTIONARIES["eff_long"] = {word: len(EFF_WORDS) for word in EFF_WORDS}
matching.RANKED_DICTIONARIES["phrases"] = _phrase_forms()

_PIECES = re.compile(r"[._+\-]+")
_APOSTROPHES = re.compile(r"['’ʼ`]")
_l33t_subs = matching.enumerate_l33t_subs
matching.enumerate_l33t_subs = lambda table: _l33t_subs(table)[:L33T_SUBS_MAX]


def _separator_price(text: str) -> int:
    """The power of SEPARATOR_CHOICES the separators words_of removed cost
    an attacker: one for the separator a phrase repeats between its words
    (the commonest one-character run, when it recurs — `the lord of the
    rings`, `make-america-great-again`), and one for every other separator
    character, so a run of symbols between random characters
    keeps its price. An apostrophe between two letters is no separator
    (`ocean's`, as the list folds a title); any other is one."""
    runs, run = [], ""
    for i, ch in enumerate(text):
        between_letters = (_APOSTROPHES.fullmatch(ch) and 0 < i < len(text) - 1
                           and unicodedata.category(text[i - 1])[0] == "L"
                           and unicodedata.category(text[i + 1])[0] == "L")
        if unicodedata.category(ch)[0] in "LNM" or between_letters:
            if run:
                runs.append(run)
            run = ""
        else:
            run += ch
    if run:
        runs.append(run)
    singles = collections.Counter(r for r in runs if len(r) == 1)
    repeated = singles.most_common(1)[0][1] if singles else 0
    return 1 + sum(map(len, runs)) - (repeated if repeated > 1 else 0)


def words_of(text: str) -> list:
    """`text`'s words as the phrase list folds a title
    (scripts/make_password_lists.fold, held to it by a test): apostrophes
    dropped, every character that is not a letter, a digit or a mark a
    separator. The case stays: zxcvbn prices capitals."""
    text = _APOSTROPHES.sub("", text)
    return "".join(ch if unicodedata.category(ch)[0] in "LNM" else " " for ch in text).split()


@dataclass(frozen=True)
class Estimate:
    guesses: float
    seconds: float
    zone: str               # "refused" | "word" | "clear"


def guesses_per_second() -> float:
    """One GPU's rate at the key's cost as the derivation reads it now."""
    t0, m0 = node_identity.ONE_GPU_COUNTED_AT
    return (node_identity.ONE_GPU_GUESSES_PER_SECOND * t0 * m0
            / (node_identity.ARGON2_TIME_COST * node_identity.ARGON2_MEMORY_COST))


def zone(seconds: float) -> str:
    return "refused" if seconds < REFUSED_BELOW else "word" if seconds < WORD_BELOW else "clear"


def _inputs(user_inputs) -> list:
    words = {"sautium"}
    for given in user_inputs:
        given = unicodedata.normalize("NFKC", given or "").strip().lower()
        if not given:
            continue
        words.add(given)
        if "@" in given:
            local, _, domain = given.partition("@")
            words.add(local)
            words.update(_PIECES.split(local))
            words.add(domain.split(".")[0])
    return sorted(w for w in words if len(w) > 1)


def _guesses(password: str, dictionaries: dict) -> float:
    matches = matching.omnimatch(password, dictionaries)
    return float(scoring.most_guessable_match_sequence(password, matches)["guesses"])


def estimate(password: str, user_inputs=()) -> Estimate:
    """The guesses one would need for `password`, and the time and zone they
    make at the key's cost. `user_inputs`: the account's own words — its
    nickname, its email — which an attacker tries first. The password is
    read as the derivation will read it (NFKC), its first ESTIMATED_CHARS."""
    password = unicodedata.normalize("NFKC", password)[:ESTIMATED_CHARS]
    if not password:
        return Estimate(guesses=1.0, seconds=1 / guesses_per_second(), zone="refused")
    dictionaries = {**matching.RANKED_DICTIONARIES,
                    "user_inputs": matching.build_ranked_dict(_inputs(user_inputs))}
    guesses = _guesses(password, dictionaries)
    joined = "".join(words_of(password))
    if joined and joined != password:
        choices = SEPARATOR_CHOICES ** _separator_price(password)
        guesses = min(guesses, _guesses(joined, dictionaries) * choices)
    seconds = guesses / guesses_per_second()
    return Estimate(guesses=guesses, seconds=seconds, zone=zone(seconds))


def phrase() -> str:
    """Four words of EFF's list, drawn by `secrets`: PHRASE_GUESSES with the
    list known to the attacker — the strength is the draw's."""
    return "-".join(secrets.choice(EFF_WORDS) for _ in range(PHRASE_WORDS))


def phrase_seconds() -> float:
    return PHRASE_GUESSES / guesses_per_second()


_UNITS = ((100 * YEAR, "century", "centuries"), (YEAR, "year", "years"), (YEAR / 12, "month", "months"),
          (86400, "day", "days"), (3600, "hour", "hours"), (60, "minute", "minutes"), (1, "second", "seconds"))


def words(seconds: float) -> str:
    """The time in words, its first significant figure kept and the rest
    dropped — "about 50 minutes" for 56, never "about 60" for a time under
    the hour: the words err short, as the rate does."""
    if seconds < 1:
        return "under a second"
    if seconds >= 10000 * YEAR:
        return "more than ten thousand years"
    for size, one, many in _UNITS:
        if seconds >= size:
            n = seconds / size
            step = 10 ** int(math.floor(math.log10(n)))
            n = int(n // step * step)
            return f"about {n} {one if n == 1 else many}"
    raise AssertionError(seconds)


def verdict(password: str, user_inputs=()) -> dict:
    """What a door and the meter say of a password: its zone, its time in
    seconds and in words, and the sentences every surface shows — the
    meter's line under the field, and a door's when it refuses or asks."""
    e = estimate(password, user_inputs)
    said = words(e.seconds)
    return {"zone": e.zone, "seconds": e.seconds, "words": said,
            "meter": f"One GPU would find it in {said} — {MEASURED_ON}." + _ZONE_TAIL[e.zone],
            "door": (f"One GPU would find this password in {said}. Sautium takes none it would find "
                     "within an hour." if e.zone == "refused" else
                     f"One GPU would find this password in {said} — and with it your identity and your "
                     "friends' trust in it. It is yours to keep.")}


_ZONE_TAIL = {"refused": " Sautium takes none it would find within an hour.",
              "word": " It is yours to keep.", "clear": ""}


def phrase_offer() -> dict:
    """A phrase drawn now, with the draw's own count — shown while the field
    holds it unchanged (zxcvbn's would be the estimate's, ~60× long) — and
    the words around it."""
    time_words = words(phrase_seconds())
    return {"phrase": phrase(), "guesses": PHRASE_GUESSES, "seconds": phrase_seconds(), "words": time_words,
            "count": f"4 words from 7,776, {time_words.removeprefix('about ')} for one GPU.",
            "meter": f"4 words from 7,776: one GPU would need {time_words}.",
            "note": PASSWORD_NOTE}
