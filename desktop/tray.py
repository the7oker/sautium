"""
System tray integration for Sautium.

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

    from desktop.icon import render_icon
    icon_image = render_icon().resize((64, 64), Image.LANCZOS)

    menu = pystray.Menu(
        pystray.MenuItem("Show", on_show, default=True),
        pystray.MenuItem("Open Web UI", on_open_ui),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Check for Updates", on_check_updates),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", on_quit),
    )

    icon = pystray.Icon(
        name="Sautium",
        icon=icon_image,
        title="Sautium",
        menu=menu,
    )

    # Run in a separate thread
    tray_thread = threading.Thread(target=icon.run, daemon=True)
    tray_thread.start()

    return icon
