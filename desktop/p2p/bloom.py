"""Bloom filter over Sautium UUIDs — the compressed inventory a peer
publishes so a node with millions of gaps can ask about them without
sending them (P2P_NETWORK.md § Holdings filter).

A bit array of m bits and k positions per element. Adding sets the k
bits; testing reads them: one zero bit means "certainly absent", all ones
mean "probably present". A miss is therefore a final answer, a hit is a
reason to ask the ordinary inventory, which answers exactly. Sized for a
target false-positive rate — at 1% that is 9.6 bits per element and seven
positions per lookup.

No hash functions: a Sautium UUID is v5, i.e. SHA-1 bits, already uniform.
Its two 64-bit halves are h1 and h2, and the i-th position is
h1 + i*h2 mod m (Kirsch–Mitzenmacher double hashing), so a lookup is
integer arithmetic only. Adds only — analysis is never deleted — which is
also what makes a future delta update a plain append.
"""

from __future__ import annotations

import base64
import math
from typing import Iterable

_LN2 = math.log(2)
_LN2_SQ = _LN2 * _LN2


def _halves(uuid_str: str) -> tuple[int, int]:
    b = bytes.fromhex(uuid_str.replace("-", ""))
    # h2 forced odd: with an even step the sequence could fold onto few
    # positions when m is even.
    return int.from_bytes(b[:8], "big"), int.from_bytes(b[8:], "big") | 1


class BloomFilter:
    __slots__ = ("m", "k", "n", "bits")

    def __init__(self, m: int, k: int, bits: bytearray | None = None, n: int = 0):
        if m <= 0 or k <= 0:
            raise ValueError("m and k must be positive")
        self.m = m
        self.k = k
        self.n = n
        self.bits = bits if bits is not None else bytearray((m + 7) // 8)

    @classmethod
    def sized(cls, capacity: int, fpr: float = 0.01) -> "BloomFilter":
        """A filter that holds `capacity` elements at about `fpr` false
        positives — fewer elements only make it more exact."""
        capacity = max(int(capacity), 1)
        m = math.ceil(-capacity * math.log(fpr) / _LN2_SQ)
        m = ((m + 7) // 8) * 8
        k = max(1, round(m / capacity * _LN2))
        return cls(m, k)

    def add(self, uuid_str: str) -> None:
        h1, h2 = _halves(uuid_str)
        bits, m = self.bits, self.m
        for i in range(self.k):
            pos = (h1 + i * h2) % m
            bits[pos >> 3] |= 1 << (pos & 7)
        self.n += 1

    def update(self, uuids: Iterable[str]) -> None:
        for u in uuids:
            self.add(u)

    def __contains__(self, uuid_str: str) -> bool:
        h1, h2 = _halves(uuid_str)
        bits, m = self.bits, self.m
        for i in range(self.k):
            pos = (h1 + i * h2) % m
            if not bits[pos >> 3] & (1 << (pos & 7)):
                return False
        return True

    def hits(self, uuids: Iterable[str]) -> list[str]:
        """The members of `uuids` that are probably in the set, in order."""
        return [u for u in uuids if u in self]

    def expected_fpr(self) -> float:
        """False-positive rate at the current fill: (1 - e^(-kn/m))^k."""
        return (1.0 - math.exp(-self.k * self.n / self.m)) ** self.k

    def to_dict(self) -> dict:
        return {"m": self.m, "k": self.k, "n": self.n,
                "bits": base64.b64encode(bytes(self.bits)).decode("ascii")}

    @classmethod
    def from_dict(cls, d: dict) -> "BloomFilter":
        m, k, n = int(d["m"]), int(d["k"]), int(d.get("n", 0))
        bits = bytearray(base64.b64decode(d["bits"]))
        if len(bits) != (m + 7) // 8:
            raise ValueError("bit array length does not match m")
        return cls(m, k, bits, n)
