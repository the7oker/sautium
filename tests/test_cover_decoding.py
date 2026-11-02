"""What a cover is decoded as (covers.open_cover, and the DLNA output's JPEG
of one): the raster formats covers come in and nothing else a Pillow plugin
reads, a size judged from the header before a pixel is decoded, Pillow's own
bound for it, and no Ghostscript for the process."""

import io
import struct
import sys
import zlib
from pathlib import Path

from PIL import EpsImagePlugin, Image, ImageFile

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import covers  # noqa: E402
from playback.dlna_backend import DlnaBackend  # noqa: E402

PILLOW_BOUND = 1024 * 1024 * 1024 // 4 // 3          # Image.MAX_IMAGE_PIXELS as Pillow ships it
EPS = (b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 8 8\n%%EndComments\n"
       b"newpath 0 0 moveto 8 8 lineto stroke\nshowpage\n%%EOF\n")


def _png(width: int, height: int, rows: bytes = b"") -> bytes:
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def _small_png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (200, 30, 30)).save(buf, "PNG")
    return buf.getvalue()


def test_a_postscript_picture_reaches_no_plugin_of_its_own(monkeypatch):
    opened = []
    real = EpsImagePlugin.EpsImageFile._open
    monkeypatch.setattr(EpsImagePlugin.EpsImageFile, "_open",
                        lambda self: opened.append(1) or real(self))
    assert covers._encode_and_hash(EPS) is None
    assert DlnaBackend._to_jpeg(EPS, "eps") is None
    assert opened == []
    assert EpsImagePlugin.gs_binary is False


def test_a_picture_past_the_bound_is_refused_from_its_header(monkeypatch):
    decoded = []
    real = ImageFile.ImageFile.load
    monkeypatch.setattr(ImageFile.ImageFile, "load", lambda self: decoded.append(self.size) or real(self))
    assert Image.MAX_IMAGE_PIXELS == PILLOW_BOUND
    assert covers._encode_and_hash(_png(10000, 10000)) is None
    assert decoded == []
    # a cover in a raster format within the bound is decoded as before
    webp, w, h, orig_w, orig_h, fmt, _phash = covers._encode_and_hash(_small_png())
    assert (w, h, orig_w, orig_h, fmt) == (4, 4, 4, 4, "png") and webp[:4] == b"RIFF"
    assert DlnaBackend._to_jpeg(_small_png(), "png")[:2] == b"\xff\xd8"
