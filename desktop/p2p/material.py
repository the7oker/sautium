"""Same material: when two Chromaprint fingerprints describe one audio.

The fingerprint is the content address of an analysis (backend/provenance.py),
and an equal digest is the fast path: equal prints are one material. But a
decoder change moves a print by a few bits (FFmpeg 4.4 against 7.1: 17 of 90
synthetic files, at a bit error rate under 0.0001; a fifth of a real
library, under 0.0043), and a lossy copy moves it a little more (MP3 128
against AAC 256 at most 0.0145), while two different recordings never come
closer than 0.347 (FP-SIM, 2026-11-09: 1 000 random pairs). So two prints of
one track are the same material when, aligned, they differ in at most one
bit in twenty — and their declared durations agree within two seconds,
because the print covers only fpcalc's first 120 s and an edit that differs
after its opening prints identically.

The rule binds both surfaces and every node: the passes' registrations, the
sync import, the identity merge, a stream's "already held", the relink of a
returned file and the fingerprinter's self-test all ask it, so it lives here
once, in pure Python (the launcher's process has no numpy), and every
constant of it is under RULE_VERSION. A change to any of them is a new
version.

Written from the format and the method, not from either project's sources:
Chromaprint's compressed fingerprint (a base64url string: one algorithm byte,
a 24-bit big-endian item count, then each 32-bit item XOR-ed with the one
before it, written as the ascending positions of its set bits in 3-bit
deltas — 0 ends an item, 7 continues in a 5-bit exception packed after the
3-bit stream) and, after AcoustID's method, the alignment (a histogram of
offsets between items whose top bits agree, within a bounded window; every
such pair votes here, where AcoustID's own matcher keeps one position per
value). THIRD_PARTY_NOTICES.md credits both.

A print arrives from other nodes too, so its length is bounded before it is
read (MAX_FINGERPRINT_CHARS, MAX_ITEMS — what fpcalc's window can produce,
with headroom) and the decoder is linear in it.
"""

import base64
import binascii
import math
from bisect import bisect_left, bisect_right
from collections import namedtuple
from typing import Iterable, Optional

RULE_VERSION = 1

# The audio fpcalc fingerprints (backend/provenance.py passes it as -length):
# the first 120 s, fpcalc's own default.
FPCALC_SECONDS = 120
ITEM_BITS = 32
# One item per 4096/3 samples at 11 025 Hz. A print of FPCALC_SECONDS holds
# about 20 items fewer than the window's (the first frame and the filters
# take them); 64 items of headroom above the window.
ITEMS_PER_SECOND = 11025 * 3 / 4096
MAX_ITEMS = math.ceil(FPCALC_SECONDS * ITEMS_PER_SECOND) + 64
# The longest string MAX_ITEMS can take: per item at most 33 3-bit values (32
# set bits and the end) and 32 5-bit exceptions, base64 of it with the header.
MAX_FINGERPRINT_CHARS = math.ceil(
    (4 + math.ceil(MAX_ITEMS * 33 * 3 / 8) + math.ceil(MAX_ITEMS * 32 * 5 / 8)) * 4 / 3)
# Items whose top bits agree vote for the offset between them.
MATCH_BITS = 14
# How far apart, in items (one item = 4096/3 samples at 11 025 Hz, 0.124 s),
# two prints are searched for their alignment.
MAX_OFFSET = 80
# The rule: the bit error rate over the overlap at the found offset, the
# offset itself (16 items, 2 s: the material starts where the other starts,
# so the 10 s analysis grid covers the same music), the overlap (at least
# half the shorter print) and the declared durations (whole seconds).
MAX_BER = 0.05
MAX_SHIFT = 16
MIN_OVERLAP = 0.5
MAX_DURATION_DELTA = 2
# The fingerprinter's self-test: a generated signal decoded locally against
# the reference print shipped in the tree — the same item count, compared
# at offset 0. Twice the worst decoder difference measured (0.0043), and
# far below a different signal.
PROBE_MAX_BER = 0.01

Alignment = namedtuple("Alignment", "offset ber overlap")


def _unpack(fingerprint: str) -> tuple:
    """(algorithm, items) of a compressed fingerprint; ValueError for
    anything that is not one, or longer than fpcalc's window can make."""
    if len(fingerprint) > MAX_FINGERPRINT_CHARS:
        raise ValueError("fingerprint longer than fpcalc's window can make")
    try:
        raw = base64.b64decode(fingerprint.encode("ascii") + b"=" * (-len(fingerprint) % 4),
                               altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as e:
        raise ValueError(f"not a compressed fingerprint: {e}") from e
    if len(raw) < 4:
        raise ValueError("not a compressed fingerprint: no header")
    algorithm = raw[0]
    count = int.from_bytes(raw[1:4], "big")
    if count > MAX_ITEMS:
        raise ValueError("fingerprint holds more items than fpcalc's window can make")
    body = raw[4:] + b"\0"                      # a byte past the end: every read takes two
    nbits = (len(raw) - 4) * 8

    def read(pos: int, width: int) -> int:
        byte = pos >> 3
        return ((body[byte] | body[byte + 1] << 8) >> (pos & 7)) & ((1 << width) - 1)

    deltas, pos, ended = [], 0, 0
    while ended < count:
        if pos + 3 > nbits:
            raise ValueError("fingerprint truncated in its bit positions")
        v = read(pos, 3)
        pos += 3
        deltas.append(v)
        ended += v == 0
    pos = -(-pos // 8) * 8                       # the exceptions start on a byte

    items, prev, cur, last = [], 0, 0, 0
    for v in deltas:
        if v == 7:
            if pos + 5 > nbits:
                raise ValueError("fingerprint truncated in its exceptions")
            v += read(pos, 5)
            pos += 5
        if v == 0:
            prev ^= cur
            items.append(prev)
            cur = last = 0
            continue
        last += v
        if last > ITEM_BITS:
            raise ValueError("fingerprint names a bit past its item")
        cur |= 1 << (last - 1)
    return algorithm, items


def decode(fingerprint: str) -> list:
    """The 32-bit items of fpcalc's compressed fingerprint; ValueError for
    anything that is not one."""
    return _unpack(fingerprint)[1]


def _ber_at(a: list, b: list, offset: int) -> tuple:
    """(bit error rate, overlap) of a against b shifted by `offset`: a's item
    i against b's item i - offset."""
    lo, hi = max(0, offset), min(len(a), len(b) + offset)
    if hi <= lo:
        return 1.0, 0
    flipped = sum((a[i] ^ b[i - offset]).bit_count() for i in range(lo, hi))
    return flipped / (ITEM_BITS * (hi - lo)), hi - lo


def align(a: list, b: list) -> Alignment:
    """The offset at which b lines up with a (a's item i against b's item
    i - offset), after AcoustID's method: every pair of items within
    MAX_OFFSET of each other whose top MATCH_BITS agree votes for its
    offset, and the most voted wins (the smaller shift on a tie, so a
    silent or repetitive print aligns at 0); then the bit error rate and the
    overlap there. No vote at all aligns at 0."""
    shift = ITEM_BITS - MATCH_BITS
    where: dict = {}
    for j, item in enumerate(b):
        where.setdefault(item >> shift, []).append(j)
    votes: dict = {}
    for i, item in enumerate(a):
        js = where.get(item >> shift)
        if js is None:
            continue
        for j in js[bisect_left(js, i - MAX_OFFSET):bisect_right(js, i + MAX_OFFSET)]:
            votes[i - j] = votes.get(i - j, 0) + 1
    offset = min(votes.items(), key=lambda v: (-v[1], abs(v[0]), v[0]))[0] if votes else 0
    ber, overlap = _ber_at(a, b, offset)
    return Alignment(offset, ber, overlap)


def _durations_agree(a: Optional[int], b: Optional[int]) -> bool:
    """The declared whole seconds within MAX_DURATION_DELTA; a material that
    declares none agrees only with another that declares none."""
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= MAX_DURATION_DELTA


def _same(a: tuple, duration_a: Optional[int], fp_b: str, duration_b: Optional[int]) -> bool:
    if not _durations_agree(duration_a, duration_b):
        return False
    try:
        b = _unpack(fp_b)
    except ValueError:
        return False
    if a[0] != b[0] or not a[1] or not b[1]:
        return False
    found = align(a[1], b[1])
    return (abs(found.offset) <= MAX_SHIFT
            and found.overlap >= MIN_OVERLAP * min(len(a[1]), len(b[1]))
            and found.ber <= MAX_BER)


def same_material(fp_a: str, duration_a: Optional[int],
                  fp_b: str, duration_b: Optional[int]) -> bool:
    """Whether two fingerprints with their declared durations are one
    material under the rule. A print that does not decode is no material
    to compare: False."""
    if fp_a == fp_b:
        return _durations_agree(duration_a, duration_b)
    try:
        a = _unpack(fp_a)
    except ValueError:
        return False
    return _same(a, duration_a, fp_b, duration_b)


def matches_probe(fingerprint: str, reference: str) -> bool:
    """The fingerprinter's self-test: the probe's print against the
    reference — the same algorithm and item count, at most PROBE_MAX_BER
    at offset 0. A print that does not decode fails."""
    try:
        got, want = _unpack(fingerprint), _unpack(reference)
    except ValueError:
        return False
    if got[0] != want[0] or not got[1] or len(got[1]) != len(want[1]):
        return False
    return _ber_at(got[1], want[1], 0)[0] <= PROBE_MAX_BER


def root_among(fingerprint: str, duration: Optional[int], rows: Iterable) -> Optional[int]:
    """The group a new analysis_sources row of `fingerprint` and `duration`
    joins among its track's other rows — each (id, material_of, chromaprint,
    duration_seconds), in id order: the root (material_of NULL marks one) of
    a row with the equal print, with no comparison, else of the first row
    the rule matches — either only where the declared duration agrees with
    that row's and with its root's, so the bound never chains through a
    member; None when none is, and the new row is a root of its own."""
    rows = list(rows)
    durations = {r[0]: r[3] for r in rows}

    def agrees(r) -> bool:
        root = r[1] or r[0]
        return (_durations_agree(duration, r[3]) and root in durations
                and _durations_agree(duration, durations[root]))

    rows = [r for r in rows if agrees(r)]
    if not rows:
        return None
    for r in rows:
        if r[2] == fingerprint:
            return r[1] or r[0]
    try:
        mine = _unpack(fingerprint)
    except ValueError:
        return None
    for r in rows:
        if _same(mine, duration, r[2], r[3]):
            return r[1] or r[0]
    return None
