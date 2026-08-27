"""The machine's language preference — the only signal a node has about the
language its owner speaks, until the human states one.

Python's `locale` module gives nothing usable here: `getdefaultlocale()` is
deprecated (removal in 3.15) and reads environment variables, which Windows
never sets and which macOS leaves empty for anything not started from a
terminal — the same blindness that made PG18 abort at startup (see
db_init._get_pg_env). Both platforms answer properly through their native
API instead, and both answer with an ORDERED list of BCP-47 tags, best
first. Measured: Windows ['uk-UA', 'en-US'] (0.04 ms), macOS ['uk-UA']
(2.4 ms for the framework load), a Linux container [].

Nothing here is ever written to the database. `user_settings["ui.language"]`
holds an explicit human choice and nothing else, so a node without that key
follows the OS for free — including after its owner switches the system
language — while an explicit choice is never overwritten. Same shape as
hardware_profile.resolve(): detected once per process, an explicit row
always wins.
"""

import functools
import logging
import os
import re
import sys
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# BCP-47 'en' with no region IS the neutral, international form; 'en-001'
# ("English (world)") exists in CLDR but no consumer we have takes it —
# Whisper wants ISO-639-1, TTS wants a voice.
FALLBACK = "en"

_TAG_RE = re.compile(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")
# Locale names that carry no language: the POSIX defaults, and the encoding
# macOS accepts as a bare LC_CTYPE ("UTF-8").
_NOT_A_LANGUAGE = {"c", "posix", "utf"}

_CF_UTF8 = 0x08000100          # kCFStringEncodingUTF8
_MUI_LANGUAGE_NAME = 0x8       # GetUserPreferredUILanguages: names, not LCIDs


def _normalize(raw: str) -> Optional[str]:
    """OS text → a BCP-47 tag worth storing, or None. Strips the encoding
    and modifier a POSIX name carries (`uk_UA.UTF-8@euro` → `uk-UA`) and
    casts case to convention: language lower, region upper, script title."""
    tag = raw.strip().split(".")[0].split("@")[0].replace("_", "-")
    if not tag or not _TAG_RE.match(tag):
        return None
    parts = tag.split("-")
    if parts[0].lower() in _NOT_A_LANGUAGE:
        return None
    out = [parts[0].lower()]
    for part in parts[1:]:
        if len(part) == 2:
            out.append(part.upper())
        elif len(part) == 4 and part.isalpha():
            out.append(part.title())
        else:
            out.append(part)
    return "-".join(out)


def _from_windows() -> List[str]:
    import ctypes
    from ctypes import wintypes

    fn = ctypes.WinDLL("kernel32", use_last_error=True).GetUserPreferredUILanguages
    fn.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.ULONG),
                   wintypes.LPWSTR, ctypes.POINTER(wintypes.ULONG)]
    fn.restype = wintypes.BOOL
    count, size = wintypes.ULONG(), wintypes.ULONG()
    if not fn(_MUI_LANGUAGE_NAME, ctypes.byref(count), None, ctypes.byref(size)):
        return []
    buf = ctypes.create_unicode_buffer(size.value)
    if not fn(_MUI_LANGUAGE_NAME, ctypes.byref(count), buf, ctypes.byref(size)):
        return []
    return buf[:size.value].split("\0")


def _from_macos() -> List[str]:
    import ctypes
    from ctypes import c_char_p, c_int, c_long, c_void_p

    cf = ctypes.cdll.LoadLibrary(
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    cf.CFLocaleCopyPreferredLanguages.restype = c_void_p
    cf.CFArrayGetCount.restype, cf.CFArrayGetCount.argtypes = c_long, [c_void_p]
    cf.CFArrayGetValueAtIndex.restype = c_void_p
    cf.CFArrayGetValueAtIndex.argtypes = [c_void_p, c_long]
    cf.CFStringGetCString.restype = c_int
    cf.CFStringGetCString.argtypes = [c_void_p, c_char_p, c_long, c_int]
    cf.CFRelease.argtypes = [c_void_p]

    array = cf.CFLocaleCopyPreferredLanguages()
    if not array:
        return []
    try:
        out = []
        for i in range(cf.CFArrayGetCount(array)):
            buf = ctypes.create_string_buffer(64)
            if cf.CFStringGetCString(cf.CFArrayGetValueAtIndex(array, i),
                                     buf, len(buf), _CF_UTF8):
                out.append(buf.value.decode())
        return out
    finally:
        cf.CFRelease(array)


def _from_env() -> List[str]:
    """POSIX fallback. LANGUAGE first — it is the user's explicit UI-language
    list (colon-separated) rather than a formatting locale."""
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        raw = os.environ.get(var)
        if raw:
            return raw.split(":")
    return []


@functools.lru_cache(maxsize=1)
def _detect() -> Tuple[str, Tuple[str, ...]]:
    reader, source = ((_from_windows, "win32") if sys.platform == "win32" else
                      (_from_macos, "corefoundation") if sys.platform == "darwin" else
                      (_from_env, "env"))
    try:
        raw = reader()
    except Exception as e:
        logger.warning("OS language: %s reader failed — %s", source, e)
        return "none", ()
    tags: List[str] = []
    for item in raw:
        tag = _normalize(item)
        if tag and tag not in tags:
            tags.append(tag)
    logger.info("OS language: %s (%s)", ", ".join(tags) or "unknown", source)
    return (source if tags else "none"), tuple(tags)


def preferred_languages() -> Tuple[str, ...]:
    """The owner's UI languages, best first; empty when the machine cannot
    say — a container, or a Linux session with no locale. Resolved once per
    process: switching the system language needs a re-login anyway."""
    return _detect()[1]


def resolve(explicit: Optional[str] = None) -> str:
    """The language this node should speak: the human's explicit choice
    (user_settings["ui.language"]), else what the machine prefers, else
    neutral English."""
    if explicit:
        return _normalize(explicit) or FALLBACK
    tags = preferred_languages()
    return tags[0] if tags else FALLBACK


def describe(explicit: Optional[str] = None) -> dict:
    """Diagnostics view: what the machine offers, what the human chose, and
    which of them wins."""
    source, tags = _detect()
    return {"preferred": list(tags), "source": source,
            "explicit": explicit, "effective": resolve(explicit)}
