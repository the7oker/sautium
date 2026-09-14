"""
The Sautium mark — five amber bars on a warm dark plate — drawn by code.

One renderer feeds every place the icon appears: the .icns inside the macOS
bundle, the .ico the Windows installer, its shortcuts and the launcher's
windows carry, and the tray icon. Rendering instead of storing means there is
no binary in the tree to fall out of step with the tokens the mark is built
from (backend/static/tokens.css).
"""

import sys
from pathlib import Path


def _blend(low: str, high: str, t: float) -> tuple:
    a = tuple(int(low[i:i + 2], 16) for i in (1, 3, 5))
    b = tuple(int(high[i:i + 2], 16) for i in (1, 3, 5))
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def render_icon(size: int = 1024):
    """The mark as an RGBA Pillow image, `size` pixels square. Drawn large and
    scaled down by the caller: ImageDraw does not anti-alias, LANCZOS does."""
    from PIL import Image, ImageDraw

    plate = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    gradient = Image.new("RGB", (1, size))
    for y in range(size):
        gradient.putpixel((0, y), _blend("#332B26", "#1B1714", y / (size - 1)))
    gradient = gradient.resize((size, size))

    margin = round(size * 0.098)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (margin, margin, size - margin - 1, size - margin - 1),
        radius=round(size * 0.185), fill=255,
    )
    plate.paste(gradient, (0, 0), mask)

    draw = ImageDraw.Draw(plate)
    bar_width = size * 0.062
    gap = size * 0.043
    heights = (0.20, 0.33, 0.47, 0.29, 0.21)
    # Depth comes from pre-blended colour, not alpha: ImageDraw writes RGBA
    # straight into the pixel, so a translucent bar would be translucent in the
    # finished icon and take its shade from whatever wallpaper sits behind it.
    depths = (0.45, 0.8, 1.0, 0.8, 0.45)
    total = len(heights) * bar_width + (len(heights) - 1) * gap
    x = (size - total) / 2
    centre = size / 2
    for height, depth in zip(heights, depths):
        half = size * height / 2
        draw.rounded_rectangle(
            (x, centre - half, x + bar_width, centre + half),
            radius=bar_width / 2,
            fill=_blend("#241F1B", "#E8B06F", depth) + (255,),
        )
        x += bar_width + gap
    return plate


# Windows reads the title bar from 16 px, the taskbar and Alt-Tab from 32 and
# 48, Explorer's large views from 256 — one file carries them all.
ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def write_ico(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    render_icon().save(path, format="ICO", sizes=ICO_SIZES)
    return path


def brand_windows(data_dir: Path) -> None:
    """Windows only: every launcher window carries the mark.

    The .ico is rendered into the data dir on each start (a tenth of a second,
    and never stale). Tk hands new toplevels the root window's default icon,
    but CustomTkinter overwrites it 200 ms after every window is created with
    its own — CTkToplevel unconditionally, CTk unless iconbitmap was called
    first — so its iconbitmap methods are pointed at ours here. Call before
    the first window exists; the root then applies it with `iconbitmap()`.
    """
    if sys.platform != "win32":
        return
    import tkinter
    import customtkinter as ctk

    ico = str(write_ico(data_dir / "Sautium.ico"))

    def ours(self, bitmap=None, default=None):
        self._iconbitmap_method_called = True
        tkinter.Wm.wm_iconbitmap(self, default=ico)

    for cls in (ctk.CTk, ctk.CTkToplevel):
        cls.iconbitmap = ours
        cls.wm_iconbitmap = ours
