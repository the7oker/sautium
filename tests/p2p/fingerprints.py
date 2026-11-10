"""Chromaprint fingerprints made by hand, for the tests of the material rule
(desktop/p2p/material.py) and its users: the compressed format written the
other way — the decoder's tests encode every real print back to fpcalc's own
string with it — and generated items. Not a test module."""

import base64
import random

from desktop.p2p import material


def encode(items, algorithm=1) -> str:
    """Chromaprint's compressed form of `items`: each item XOR-ed with the one
    before it, its set bits as ascending 3-bit position deltas ended by 0,
    a delta of 7 or more continued in a 5-bit exception packed after them."""
    normal, exceptions, prev = [], [], 0
    for item in items:
        delta, last, prev = item ^ prev, 0, item
        for bit in range(material.ITEM_BITS):
            if delta >> bit & 1:
                step, last = bit + 1 - last, bit + 1
                if step >= 7:
                    normal.append(7)
                    exceptions.append(step - 7)
                else:
                    normal.append(step)
        normal.append(0)
    packed = sum(v << (3 * k) for k, v in enumerate(normal))
    packed_exc = sum(v << (5 * k) for k, v in enumerate(exceptions))
    raw = (bytes([algorithm]) + len(items).to_bytes(3, "big")
           + packed.to_bytes((3 * len(normal) + 7) // 8, "little")
           + packed_exc.to_bytes((5 * len(exceptions) + 7) // 8, "little"))
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def flip(items, rate, seed):
    """`items` with a share `rate` of their bits flipped, spread at random."""
    rng = random.Random(seed)
    out = list(items)
    total = len(items) * material.ITEM_BITS
    for k in rng.sample(range(total), round(rate * total)):
        out[k // material.ITEM_BITS] ^= 1 << (k % material.ITEM_BITS)
    return out


def noise(n, seed):
    rng = random.Random(seed)
    return [rng.getrandbits(32) for _ in range(n)]
