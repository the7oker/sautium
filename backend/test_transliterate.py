"""Unit tests for latinize() — cross-script search transliteration.

Pure logic, no DB. Guards the symmetric-normalization contract and the
per-script dispatch against library drift.
"""
import pytest

from transliterate import latinize, latin_alt_forms


@pytest.mark.parametrize("raw, expected", [
    ("Madonna", "madonna"),
    ("Sade", "sade"),
    ("Björk", "bjork"),               # accented Latin folds
    ("2Pac", "2pac"),                 # digits preserved
    ("мадонна", "madonna"),           # Cyrillic phonetic of a Western name
    ("Высоцкий", "vysotskiy"),        # BGN romanization
    ("Океан Ельзи", "okean elzi"),    # apostrophe (ь) dropped in-word, not split
    ("美空ひばり", "misora hibari"),    # kana present → Japanese (cutlet)
    ("ケラケラ", "kerakera"),
    ("邓丽君", "deng li jun"),          # kana-less Han → Chinese (pypinyin)
    ("이아립", "iarip"),               # Hangul → Korean (koroman)
    # Documented 0a limit: kana-less Japanese name reads as Chinese; the
    # phase-0b multi-form alias recovers the Japanese reading.
    ("中森明菜", "zhong sen ming cai"),
])
def test_latinize_known(raw, expected):
    assert latinize(raw) == expected


@pytest.mark.parametrize("blank", [None, "", "   ", "!!!"])
def test_latinize_blank_is_none(blank):
    assert latinize(blank) is None


@pytest.mark.parametrize("latin", ["madonna", "vysotskiy", "deng li jun", "sigur ros"])
def test_latinize_idempotent_on_latin(latin):
    # Symmetric normalization: query-side latinize must equal the stored form,
    # so an already-Latin string must pass through unchanged.
    assert latinize(latin) == latin


@pytest.mark.parametrize("raw, alts", [
    ("中森明菜", ["nakamori akina"]),   # kana-less Japanese → Japanese reading as alias
    ("田部井辰雄", ["tabei tatsuo"]),
    ("邓丽君", []),                      # Chinese → cutlet junk ("??kun") filtered out
    ("美空ひばり", []),                  # kana present → already Japanese in name_latin
    ("Высоцкий", []),                    # Cyrillic → fuzzy already collapses variants
    ("Madonna", []),
    (None, []),
])
def test_latin_alt_forms(raw, alts):
    # Phase 0b: only genuinely cross-language-ambiguous (kana-less Han) names get
    # an alternate reading; requires cutlet + unidic-lite (present in the image).
    assert latin_alt_forms(raw) == alts
