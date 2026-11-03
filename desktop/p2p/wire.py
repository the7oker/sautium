"""Bounded decoding of what a peer sends — request bodies and replies alike
— and the encoding of what a node answers.

A peer chooses the bytes; what they cost to decode is ours. Everything here
refuses with `WireError` (a ValueError, so a caller's existing "malformed"
branch takes it) instead of growing without bound: gzip is inflated in
steps against a ceiling, and the JSON parser's recursion limit — reached by
a body only a few KB long — is a refusal, not an uncaught RecursionError.

Shared by both peer surfaces and every peer client (desktop/api_client.py,
the slice verifiers, the Docker peer app); depends on the stdlib only.
"""

import gzip
import json
import zlib
from typing import Optional

KiB = 1024
MiB = 1024 * KiB

_STEP = 256 * KiB


class WireError(ValueError):
    """What a peer sent is over its bound or not well-formed."""


# An answer is mostly base64 and hex, which gzip shrinks by the encoding's
# overhead at any level: on a segments answer level 1 came within 2 % of
# level 9's size in under three quarters of its time.
ANSWER_GZIP_LEVEL = 1
ANSWER_GZIP_MIN = 1024


def loads(data):
    """json.loads that refuses a document nested past the parser's limit."""
    try:
        return json.loads(data)
    except RecursionError:
        raise WireError("JSON nested too deeply") from None


def render(answer: dict, gzip_ok: bool) -> tuple[bytes, Optional[str]]:
    """An answer's bytes and their Content-Encoding (None: identity). Both
    peer surfaces make a bulk answer with the work that produced it, off the
    loop: tens of MB of JSON and its gzip, on the loop, held every other
    request meanwhile."""
    body = json.dumps(answer, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if gzip_ok and len(body) > ANSWER_GZIP_MIN:
        return gzip.compress(body, compresslevel=ANSWER_GZIP_LEVEL, mtime=0), "gzip"
    return body, None


async def read_json(resp, limit: int) -> dict:
    """An aiohttp response's JSON object, its body read to `limit` bytes.
    aiohttp inflates a compressed body in bounded steps as it is read, so
    the cap holds for what it yields; its own .json() reads, and inflates,
    whatever arrives inside the session's timeout."""
    raw = bytearray()
    while chunk := await resp.content.read(min(_STEP, limit + 1 - len(raw))):
        raw += chunk
        if len(raw) > limit:
            raise WireError("answer over its bound")
    data = loads(bytes(raw))
    if not isinstance(data, dict):
        raise WireError("answer is not an object")
    return data


def gunzip(data: bytes, limit: int) -> bytes:
    """The gzip stream `data`, inflated to at most `limit` bytes — refused
    past that, truncated or corrupt, or followed by anything else."""
    inflater = Inflater(limit)
    out = inflater.feed(data)
    inflater.finish()
    return out


class Inflater:
    """Incremental gzip inflation against a ceiling on the OUTPUT: a reply
    read in chunks is inflated as it arrives, and a stream that would
    inflate past `limit` is refused at that step, never held."""

    def __init__(self, limit: int):
        self._d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        self._left = limit

    def feed(self, chunk: bytes) -> bytes:
        out = []
        data = chunk
        try:
            while data:
                if self._d.eof:
                    raise WireError("data after the end of the gzip stream")
                piece = self._d.decompress(data, min(_STEP, self._left + 1))
                self._take(piece)
                out.append(piece)
                data = self._d.unconsumed_tail
        except zlib.error as e:
            raise WireError(f"gzip stream corrupt: {e}") from None
        return b"".join(out)

    def finish(self) -> None:
        if not self._d.eof or self._d.unused_data:
            raise WireError("gzip stream truncated or followed by other data")

    def _take(self, piece: bytes) -> None:
        self._left -= len(piece)
        if self._left < 0:
            raise WireError("inflates past its bound")
