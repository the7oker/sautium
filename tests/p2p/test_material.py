"""FP-SIM's rule (desktop/p2p/material.py): the decoder, the alignment and the
rule's edges, on real prints and on generated ones.

The real prints are the synthetic lab library's 90 files under the release's
FFmpeg 4.4 and Debian trixie's 7.1 (tests/fixtures/fingerprints/: 17 of them
differ between the two decoders) — generated signals, nothing of anyone's
music. The encoder (tests/p2p/fingerprints.py) is the format written the
other way: re-encoding every decoded print gives back fpcalc's own string, so
the decoder reads each bit where fpcalc wrote it."""

import base64
import itertools
import json
from pathlib import Path

import pytest

from desktop.p2p import material
from tests.p2p.fingerprints import encode, flip, noise

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "fingerprints" / "lab_ffmpeg44_71.json"
PROBE_REFERENCE = Path(__file__).resolve().parents[2] / "backend" / "fingerprint_probe" / "reference.txt"


@pytest.fixture(scope="module")
def lab():
    return json.loads(FIXTURE.read_text())["prints"]


# -- The decoder -------------------------------------------------------------

def test_every_lab_print_decodes_and_encodes_back_to_fpcalcs_own_string(lab):
    for p in lab:
        for fp in filter(None, (p["ffmpeg44"], p["ffmpeg71"])):
            items = material.decode(fp)
            assert encode(items) == fp
            # fpcalc's item rate: 11 025 Hz in hops of 4096/3 samples over its 120 s
            # window, less the frames its first window and its filters take
            assert -27 < len(items) - min(p["duration"], 120) * 11025 * 3 / 4096 < -16


def test_the_probe_reference_decodes():
    reference = PROBE_REFERENCE.read_text(encoding="ascii").strip()
    items = material.decode(reference)
    assert encode(items) == reference and len(items) == 75        # 12 s


def test_generated_items_round_trip_through_every_exception():
    items = [0, 1, 0x80000000, 0xFFFFFFFF, 0x00010001, 0x40000002] + noise(200, 7)
    assert material.decode(encode(items, algorithm=3)) == items
    assert material._unpack(encode(items, algorithm=3))[0] == 3


@pytest.mark.parametrize("bad", [
    "", "AQ", "!!!!", "AQAAé", "AQAA+/xx",            # no header, not base64url, not ascii
])
def test_what_is_no_fingerprint_raises(bad):
    with pytest.raises(ValueError):
        material.decode(bad)


def test_a_truncated_print_raises_rather_than_reading_zeros():
    fp = encode(noise(300, 1))
    for cut in (len(fp) // 2, len(fp) - 40, 8):
        with pytest.raises(ValueError):
            material.decode(fp[:cut])
    with pytest.raises(ValueError):                        # a count no stream can hold
        material.decode(base64.urlsafe_b64encode(b"\x01\xff\xff\xff\x00").decode().rstrip("="))


def test_a_position_past_the_item_raises():
    raw = bytes([1, 0, 0, 1, 7, 31])           # one item: a delta of 7 + 31, then 0
    with pytest.raises(ValueError, match="past its item"):
        material.decode(base64.urlsafe_b64encode(raw).decode().rstrip("="))


# -- The alignment -----------------------------------------------------------

@pytest.mark.parametrize("shift", [0, 1, 5, 16, 17, 40, 80, -1, -16, -17, -80])
def test_align_finds_the_shift_between_two_cuts_of_one_print(shift):
    base = noise(900, 3)
    a, b = (base[shift:], base) if shift >= 0 else (base, base[-shift:])
    # a's item i is b's item i + shift: b lines up with a at offset -shift
    found = material.align(a, b)
    assert found.offset == -shift and found.ber == 0.0
    assert found.overlap == len(base) - abs(shift)


def test_align_beyond_its_window_finds_no_vote_and_compares_at_zero():
    base = noise(900, 4)
    found = material.align(base[81:], base)
    assert found.offset != -81 and found.ber > 0.4


def test_a_silent_print_aligns_at_zero():
    found = material.align([0x12345678] * 300, [0x12345678] * 280)
    assert found == material.Alignment(0, 0.0, 280)


def test_two_lab_decoders_align_at_zero_within_a_ten_thousandth(lab):
    moved = [p for p in lab if p["ffmpeg71"]]
    assert len(moved) == 17
    for p in moved:
        found = material.align(material.decode(p["ffmpeg44"]), material.decode(p["ffmpeg71"]))
        assert found.offset == 0 and 0 < found.ber <= 0.0002


# -- The rule ---------------------------------------------------------------

def test_every_lab_file_is_one_material_under_both_decoders(lab):
    for p in lab:
        assert material.same_material(p["ffmpeg44"], p["duration"],
                                      p["ffmpeg71"] or p["ffmpeg44"], p["duration"])


def test_no_two_lab_files_are_one_material(lab):
    """Generated signals, some of them alike: a channel alone against the
    other alone prints identically for 39 s against 27 s, and only the
    duration tells them apart; the closest two others are at BER 0.0625."""
    for x, y in itertools.combinations(lab, 2):
        assert not material.same_material(x["ffmpeg44"], x["duration"],
                                          y["ffmpeg71"] or y["ffmpeg44"], y["duration"])


@pytest.mark.parametrize("rate,same", [(0.0, True), (0.01, True), (0.049, True),
                                       (0.051, False), (0.1, False), (0.5, False)])
def test_the_bit_error_bound(rate, same):
    base = noise(950, 11)
    assert material.same_material(encode(base), 240, encode(flip(base, rate, 12)), 240) is same


@pytest.mark.parametrize("delta,same", [(0, True), (1, True), (2, True), (-2, True),
                                        (3, False), (-3, False), (30, False)])
def test_the_declared_durations_bound(delta, same):
    """An edit differs after its opening, which the 120 s print never sees."""
    fp = encode(noise(950, 13))
    assert material.same_material(fp, 240, fp, 240 + delta) is same
    assert material.same_material(fp, 240, encode(flip(material.decode(fp), 0.01, 1)),
                                  240 + delta) is same


def test_an_undeclared_duration_agrees_only_with_another():
    fp = encode(noise(950, 14))
    assert material.same_material(fp, None, fp, None)
    assert not material.same_material(fp, None, fp, 240)
    assert not material.same_material(fp, 240, fp, None)


@pytest.mark.parametrize("shift,same", [(16, True), (-16, True), (17, False), (-17, False)])
def test_the_offset_bound(shift, same):
    """The material starts where the other starts: a lead-in longer than two
    seconds is another cut."""
    base = noise(950, 15)
    a, b = (base[shift:], base[:len(base) - shift]) if shift >= 0 else (base[:shift], base[-shift:])
    assert material.same_material(encode(a), 240, encode(b), 240) is same


def test_the_overlap_bound():
    """At least half the shorter print overlaps; the longer's rest does not
    count against it."""
    base = noise(950, 16)
    assert material.same_material(encode(base), 240, encode(base[:100]), 240)
    assert material.same_material(encode(base[:20]), 3, encode(base[4:24]), 3)       # 16 of 20
    assert not material.same_material(encode(base[:20]), 3, encode(base[16:40]), 3)  # 4 of 20


def test_a_print_that_does_not_decode_is_no_material():
    good = encode(noise(300, 17))
    assert not material.same_material(good, 60, good[:20], 60)
    assert not material.same_material("AQAA_broken", 60, good, 60)
    assert material.same_material("AQAA_broken", 60, "AQAA_broken", 60)   # an equal digest


def test_another_algorithm_is_another_material():
    items = noise(300, 18)
    assert not material.same_material(encode(items, 1), 60, encode(items, 2), 60)


# -- The probe ----------------------------------------------------------------

def test_the_probe_passes_a_print_a_decoder_moved_and_refuses_another():
    reference = PROBE_REFERENCE.read_text(encoding="ascii").strip()
    items = material.decode(reference)
    assert material.matches_probe(reference, reference)
    assert material.matches_probe(encode(flip(items, 0.009, 19)), reference)
    assert not material.matches_probe(encode(flip(items, 0.012, 19)), reference)
    assert not material.matches_probe(encode(items[1:]), reference)          # another item count
    assert not material.matches_probe(encode(items[1:] + items[:1]), reference)  # offset 1
    assert not material.matches_probe(encode(items, 2), reference)
    assert not material.matches_probe("AQAAZFMUJq0U4VaOkhRC_another", reference)


# -- The group a new row joins -------------------------------------------------

def test_a_new_row_joins_an_equal_print_without_comparing_and_a_similar_one_by_the_rule():
    base = noise(950, 20)
    fp, near, far = encode(base), encode(flip(base, 0.01, 21)), encode(noise(950, 22))
    rows = [(5, None, far, 240), (7, 5, near, 240), (9, None, "AQAA_kept_unread", 240)]
    assert material.root_among(fp, 240, rows) == 5            # the member's root, never the member
    assert material.root_among("AQAA_kept_unread", None, rows) == 9   # equal: no comparison, no duration
    assert material.root_among(fp, 250, rows) is None         # the duration is the second key
    assert material.root_among(fp, 240, [(3, None, near, 240), (4, None, fp, 300)]) == 4
