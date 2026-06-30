"""Latin transliteration for cross-script fuzzy search.

Symmetric normalization: the same ``latinize()`` runs at write time (filling
``artists.name_latin`` / ``albums.title_latin`` / ``tracks.title_latin``) and at
query time, so a query in any script matches the stored Latin form through
``pg_trgm`` + ``levenshtein``. See ``docs/design/DISCOVERY-SEARCH-ENGINE.md``.

Universal engine = ``anyascii`` (ISC): BGN-phonetic Cyrillic and accent folding.
CJK gets script-dispatched specialists for correct phonetic readings — anyascii
reads CJK ideographs as Chinese, which is wrong for Japanese (美空ひばり →
"meikonghibari" vs cutlet "misora hibari"). The heavy Japanese stack
(fugashi + unidic-lite) is imported lazily, only when kana/kanji actually appear.

Kana-less Han is ambiguous (Chinese vs Japanese can't be told apart without
language context) — we default to pinyin, which always yields a valid reading
(cutlet emits garbage on Chinese: 邓丽君 → "??kun"). Pure-kanji Japanese names
therefore romanize as Chinese here; the CJK multi-form alias layer (phase 0b)
recovers them.
"""
import re

from anyascii import anyascii

# Unicode script ranges for dispatch.
_HANGUL = (0xAC00, 0xD7A3)
_KANA = (0x3040, 0x30FF)
_HAN = ((0x4E00, 0x9FFF), (0x3400, 0x4DBF))

_NONWORD = re.compile(r"[^a-z0-9]+")

# The Japanese tagger is ~50MB and slow to build — construct once, on first use.
_cutlet = None


def _has(s: str, lo: int, hi: int) -> bool:
    return any(lo <= ord(c) <= hi for c in s)


def _japanese(s: str) -> str:
    global _cutlet
    if _cutlet is None:
        import cutlet
        _cutlet = cutlet.Cutlet()
    return _cutlet.romaji(s)


def _romanize(s: str) -> str:
    if _has(s, *_HANGUL):
        import koroman
        return koroman.romanize(s)
    if _has(s, *_KANA):
        return _japanese(s)
    if _has(s, *_HAN[0]) or _has(s, *_HAN[1]):
        from pypinyin import lazy_pinyin
        return " ".join(lazy_pinyin(s))
    return anyascii(s)


def latinize(s: str | None) -> str | None:
    """Transliterate a name/title to a lowercase ASCII search form.

    Apostrophes (Cyrillic soft/hard sign romanizations) are dropped in-word so
    "Океан Ельзи" → "okean elzi", not "okean el zi"; every other non-alphanumeric
    run collapses to a single space. Returns ``None`` for blank input so a NULL
    column stays NULL. The identical function must transform the query string at
    search time.
    """
    if not s or not s.strip():
        return None
    out = _romanize(s).lower().replace("'", "").replace("’", "")
    out = _NONWORD.sub(" ", out).strip()
    return out or None
