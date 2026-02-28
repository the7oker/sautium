"""
System tray integration for Music AI DJ.

Uses pystray to show an icon in the system tray with a context menu.
"""

import logging
import threading
from typing import Callable, Optional

logger = logging.getLogger(__name__)


def create_tray(
    on_show: Callable,
    on_open_ui: Callable,
    on_check_updates: Callable,
    on_quit: Callable,
):
    """
    Create and start a system tray icon.

    Returns the pystray.Icon instance.
    """
    import pystray
    from PIL import Image

    # Create a simple icon (blue circle)
    icon_image = _create_default_icon()

    # Try loading custom icon
    from pathlib import Path
    icon_path = Path(__file__).parent / "assets" / "icon.ico"
    if icon_path.exists():
        try:
            icon_image = Image.open(icon_path)
        except Exception:
            pass

    menu = pystray.Menu(
        pystray.MenuItem("Show", on_show, default=True),
        pystray.MenuItem("Open Web UI", on_open_ui),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Check for Updates", on_check_updates),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", on_quit),
    )

    icon = pystray.Icon(
        name="MusicAIDJ",
        icon=icon_image,
        title="Music AI DJ",
        menu=menu,
    )

    # Run in a separate thread
    tray_thread = threading.Thread(target=icon.run, daemon=True)
    tray_thread.start()

    return icon


def _create_default_icon():
    """Create a simple default icon (blue circle on transparent background)."""
    from PIL import Image, ImageDraw

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Blue circle
    margin = 4
    draw.ellipse(
        [margin, margin, size - margin, size - margin],
        fill=(59, 130, 246, 255),
    )

    # Music note symbol (simple)
    cx, cy = size // 2, size // 2
    draw.ellipse(
        [cx - 8, cy + 2, cx, cy + 10],
        fill=(255, 255, 255, 255),
    )
    draw.line(
        [(cx, cy + 6), (cx, cy - 12)],
        fill=(255, 255, 255, 255),
        width=2,
    )

    return img
