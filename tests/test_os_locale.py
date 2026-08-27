"""The machine's language signal (desktop/os_locale.py) — normalisation of
what the three platform readers hand back, and the rule that decides which
answer wins. The readers themselves are OS calls and are verified on the
machines: Windows ['uk-UA', 'en-US'], macOS ['uk-UA'], a container []."""

import pytest

from desktop import os_locale


@pytest.mark.parametrize("raw, expected", [
    ("uk-UA", "uk-UA"),
    ("uk_UA.UTF-8", "uk-UA"),
    ("en_US.UTF-8@euro", "en-US"),
    ("pt_br", "pt-BR"),
    ("zh-hans-cn", "zh-Hans-CN"),
    ("en", "en"),
    # Carry no language: the POSIX defaults, the bare encoding macOS allows
    # as an LC_CTYPE, and anything that is not a tag at all.
    ("C", None), ("C.UTF-8", None), ("POSIX", None), ("UTF-8", None),
    ("", None), ("  ", None), ("x", None), ("!!", None), ("ru RU", None),
])
def test_normalize(raw, expected):
    assert os_locale._normalize(raw) == expected


def test_explicit_choice_always_wins_over_the_machine(monkeypatch):
    monkeypatch.setattr(os_locale, "_detect", lambda: ("win32", ("uk-UA", "en-US")))
    assert os_locale.resolve() == "uk-UA"
    assert os_locale.resolve("de_DE.UTF-8") == "de-DE"
    assert os_locale.describe("de-DE") == {
        "preferred": ["uk-UA", "en-US"], "source": "win32",
        "explicit": "de-DE", "effective": "de-DE"}


def test_silent_machine_falls_back_to_neutral_english(monkeypatch):
    monkeypatch.setattr(os_locale, "_detect", lambda: ("none", ()))
    assert os_locale.resolve() == os_locale.FALLBACK == "en"
    # A stored value that no longer parses must not become the answer.
    assert os_locale.resolve("C.UTF-8") == "en"
    assert os_locale.describe()["effective"] == "en"
