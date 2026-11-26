"""The one password estimate (desktop/password_strength; SEC-S2): the counts
it gives the cases this project measured, the lists it loads only under its
digests, the year and the account's own words, a time that follows the key's
cost alone, the phrase and its own count, and no call crossing another's
words. Made-up passwords only."""

import importlib.metadata
import re
import threading

import pytest

from desktop import node_identity, password_strength as ps


def test_zxcvbn_is_the_version_whose_internals_the_estimate_reaches():
    assert importlib.metadata.version("zxcvbn") == "4.5.0"


# SEC-S2-PROPOSAL §3.6 and §14: the language-free fixes, nickname `alex`
COUNTS = {
    "sunshine": 47,
    "Sunshine1!": 2.84e4,
    "password1234567": 1.5e4,
    "alex2025": 1.5e4,              # zxcvbn alone: 2.3e5 (2025 read as a keypad run)
    "Kyiv2025": 1.01e6,             # alone: 4.3e7
    "correct horse battery staple": 2,   # alone: 2.1e20
    "the-lord-of-the-rings": 7,
    "t7Rk2pQx9LmZ": 1e12,
    "s1u2n3s4h5i6n7e8": 1e16,       # the blind spot that stays
}


@pytest.mark.parametrize("password,guesses", COUNTS.items())
def test_the_measured_cases_keep_their_counts(password, guesses):
    assert ps.estimate(password, ("alex",)).guesses == pytest.approx(guesses, rel=0.02)


def test_years_to_2050_read_as_years_and_2051_does_not():
    regex = ps.matching.REGEXEN["recent_year"]
    assert all(regex.fullmatch(str(y)) for y in range(1900, 2051))
    assert not regex.fullmatch("2051")
    assert ps.estimate("Kyiv2049").guesses < ps.estimate("Kyiv2051").guesses


def test_the_account_s_own_words_are_tried_first_the_email_s_pieces_too():
    alone = ps.estimate("ukrnet2024").guesses
    told = ps.estimate("ukrnet2024", ("someone", "alex.k@ukrnet.example")).guesses
    assert told < alone / 100
    assert ps.estimate("sautium1999").guesses < ps.estimate("qwmzvp1999").guesses


def test_an_eff_word_costs_its_list_s_length_whatever_its_place():
    first, last = ps.EFF_WORDS[0], ps.EFF_WORDS[-1]
    assert ps.matching.RANKED_DICTIONARIES["eff_long"][first] == 7776
    assert ps.matching.RANKED_DICTIONARIES["eff_long"][last] == 7776


def test_a_phrase_counts_with_or_without_its_separators():
    spaced = ps.estimate("make america great again").guesses
    for form in ("makeamericagreatagain", "make-america-great-again", "make_america_great_again"):
        assert ps.estimate(form).guesses <= spaced * 10


def test_the_lists_load_only_under_their_digests(tmp_path, monkeypatch):
    for name in ps._DIGESTS:
        (tmp_path / name).write_bytes((ps._LISTS / name).read_bytes() + b"x\n")
    monkeypatch.setattr(ps, "_LISTS", tmp_path)
    with pytest.raises(RuntimeError, match="not the list this release pins"):
        ps._read("phrases.txt")


def test_the_time_follows_the_key_s_cost_alone(monkeypatch):
    today = ps.estimate("Sunshine1!")
    assert today.zone == "refused" and ps.guesses_per_second() == 300
    monkeypatch.setattr(node_identity, "ARGON2_TIME_COST", 4000)      # S5's cost
    later = ps.estimate("Sunshine1!")
    assert later.guesses == today.guesses
    assert later.seconds == pytest.approx(today.seconds * 1000)
    assert later.zone == "word"
    monkeypatch.setattr(node_identity, "ARGON2_MEMORY_COST", 131072)  # 128 MiB, t doubled
    monkeypatch.setattr(node_identity, "ARGON2_TIME_COST", 8000)
    assert ps.guesses_per_second() == pytest.approx(0.3)


def test_the_zones_edges():
    assert ps.zone(3599) == "refused"
    assert ps.zone(3600) == "word"
    assert ps.zone(ps.YEAR - 1) == "word"
    assert ps.zone(ps.YEAR) == "clear"


def test_the_words_err_short():
    assert ps.words(0.2) == "under a second"
    assert ps.words(56 * 60) == "about 50 minutes"        # never "about 60" under the hour
    assert ps.words(3600) == "about 1 hour"
    assert ps.words(3 * 86400) == "about 3 days"
    assert ps.words(200 * ps.YEAR) == "about 2 centuries"
    assert ps.words(20000 * ps.YEAR) == "more than ten thousand years"


def test_a_phrase_is_four_words_of_the_list_with_the_draw_s_own_count():
    offer = ps.phrase_offer()
    words = offer["phrase"].split("-")
    assert len(words) == 4 and set(words) <= set(ps.EFF_WORDS)
    assert offer["guesses"] == 7776 ** 4
    assert offer["words"] == "more than ten thousand years"
    assert ps.estimate(offer["phrase"]).zone == "clear"      # the door takes it unedited
    assert ps.phrase() != ps.phrase()                         # drawn anew each time


def test_the_estimate_reads_its_first_64_characters():
    tail = "Zq8#" * 30
    assert ps.estimate("a" * 64 + tail).guesses == ps.estimate("a" * 64).guesses


def test_two_estimates_at_once_never_cross_their_words():
    """zxcvbn() writes the call's words into a global; the estimate never
    calls it, so a thread's nickname never prices another thread's password."""
    results, errors = [], []

    def run(name, password):
        try:
            for _ in range(30):
                results.append((name, ps.estimate(password, (name,)).guesses))
        except Exception as e:            # noqa: BLE001 — reported below
            errors.append(e)
    threads = [threading.Thread(target=run, args=("zorbulon", "zorbulon2024")),
               threading.Thread(target=run, args=("quixotes", "zorbulon2024"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    mine = {g for n, g in results if n == "zorbulon"}
    other = {g for n, g in results if n == "quixotes"}
    assert len(mine) == 1 and len(other) == 1 and mine.pop() < other.pop()
    assert "user_inputs" not in ps.matching.RANKED_DICTIONARIES or \
        not ps.matching.RANKED_DICTIONARIES["user_inputs"]


def test_the_module_logs_nothing():
    source = (ps._LISTS.parent / "password_strength.py").read_text(encoding="utf-8")
    assert not re.search(r"\blogging\b|\blogger\b|\bprint\(", source)


@pytest.mark.parametrize("typed", ["harry potter and the philosopher's stone", "ocean's eleven",
                                   "Schindler's List", "make-america-great-again!"])
def test_a_title_typed_with_its_apostrophe_or_its_punctuation_is_the_listed_title(typed):
    assert ps.estimate(typed).zone == "refused"


def test_the_estimate_reads_words_as_the_list_was_folded():
    """scripts/make_password_lists.fold made the list; words_of reads a
    password the same way, so a title matches as typed."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "make_password_lists", ps._LISTS.parent.parent / "scripts" / "make_password_lists.py")
    make = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(make)
    for text in ("Ocean's Eleven", "L'Étranger — Camus!", "Tiến quân ca", "ROCK & roll: part 2",
                 "don’t stop me now", "मह भारतम"):
        assert make.fold(text) == " ".join(ps.words_of(ps.unicodedata.normalize("NFKC", text))).lower()
    phrases = [line for line in ps._read("phrases.txt").splitlines() if line and not line.startswith("#")]
    assert all(make.fold(p) == p for p in phrases[:3000])


def test_a_list_checked_out_with_other_line_ends_is_the_same_list(tmp_path, monkeypatch):
    for name in ps._DIGESTS:
        (tmp_path / name).write_bytes((ps._LISTS / name).read_bytes().replace(b"\n", b"\r\n"))
    monkeypatch.setattr(ps, "_LISTS", tmp_path)
    assert ps._read("eff_large_wordlist.txt").splitlines()[0].endswith("abacus")
    root = __import__("pathlib").Path(ps.__file__).resolve().parent.parent
    assert "desktop/password_lists/* -text" in (root / ".gitattributes").read_text(encoding="utf-8")


def test_an_empty_password_is_refused_not_an_error():
    assert ps.estimate("").zone == "refused"
    assert ps.verdict("")["zone"] == "refused"


def test_a_crafted_l33t_string_costs_a_bounded_time():
    import time
    crafted = ("a@4b8c({[<e3g69i1!|l1|7o0s$5t+7x%z2" * 2)[:64]
    started = time.perf_counter()
    ps.estimate(crafted)
    assert time.perf_counter() - started < 0.6          # 1.3 s unbounded on the machine it was measured on
    assert len(ps.matching.enumerate_l33t_subs(
        ps.matching.relevant_l33t_subtable(crafted, ps.matching.L33T_TABLE))) == ps.L33T_SUBS_MAX


def test_every_surface_says_the_node_s_sentences():
    v = ps.verdict("qmzvhwkt")
    assert v["meter"] == ("One GPU would find it in about 3 days — measured on an RTX 4090 Laptop GPU and "
                          "counted for a faster card; more or faster cards take less. It is yours to keep.")
    assert v["door"].startswith("One GPU would find this password in about 3 days — and with it")
    offer = ps.phrase_offer()
    assert offer["count"] == "4 words from 7,776, more than ten thousand years for one GPU."
    assert offer["note"] == ps.PASSWORD_NOTE


def test_the_backend_derives_the_key_it_derived_before_the_constants_were_shared():
    """§2.4: p2p_identity reads node_identity's Argon2 constants now — the
    same key for the same pair (a value derived at 05fcd1a1)."""
    import sys
    sys.path.insert(0, str(ps._LISTS.parent.parent / "backend"))
    import p2p_identity
    assert p2p_identity.ARGON2_TIME_COST is node_identity.ARGON2_TIME_COST
    assert (p2p_identity.derive_identity("kat-owner", "tangerine volcano quiet")["public_key_hex"]
            == "510a8132c8511bee2fe2709fd701d4978ab8fb8aa94e8c8db19068ffca9ff5d9")
