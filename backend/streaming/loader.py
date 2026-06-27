"""Discovery of bring-your-own StreamProvider plugins.

Sautium core ships ONE provider (YouTube — non-DRM). Additional providers, e.g.
a lossless Deezer bridge, are NOT bundled (§1201 — see project_spotify_preview):
they live in the user's own closed repos, cloned into the local providers
directory (gitignored, empty in the public tree). This loader imports each plugin
package there and registers the StreamProvider its top-level ``provider()``
factory returns.

Core has zero knowledge of which providers exist or what they do — the directory
is the only seam. A plugin that is missing, broken or misconfigured (no
credentials, an uninstalled backend tool) is logged and skipped, never fatal.

A plugin is a Python *package*: a sub-directory ``<name>/`` with an
``__init__.py`` exposing ``def provider() -> StreamProvider``. It imports the
host contract from ``streaming.base`` (same class objects → ``isinstance`` holds)
and reads its own config (e.g. a Deezer ARL) from the environment itself. Each is
imported under a private ``sautium_ext_<name>`` module name so a plugin directory
can share a name with a pip package (e.g. ``deezer``) without shadowing it.
"""
from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

from .base import ProviderRegistry, StreamProvider

logger = logging.getLogger(__name__)


def load_external_providers(registry: ProviderRegistry, plugins_dir: Path) -> int:
    """Import every plugin package in ``plugins_dir`` and register the provider
    its ``provider()`` factory returns. Returns the number registered."""
    if not plugins_dir.is_dir():
        return 0

    count = 0
    for entry in sorted(plugins_dir.iterdir()):
        if not entry.is_dir() or entry.name.startswith((".", "_")):
            continue
        init = entry / "__init__.py"
        if not init.is_file():
            continue

        pkg = f"sautium_ext_{entry.name}"
        try:
            spec = importlib.util.spec_from_file_location(
                pkg, init, submodule_search_locations=[str(entry)])
            mod = importlib.util.module_from_spec(spec)
            sys.modules[pkg] = mod          # so the package's own imports resolve
            spec.loader.exec_module(mod)

            factory = getattr(mod, "provider", None)
            if factory is None:
                logger.warning("provider plugin %r exposes no provider() factory — skipped",
                               entry.name)
                sys.modules.pop(pkg, None)
                continue
            prov = factory()
            if not isinstance(prov, StreamProvider):
                logger.warning("provider plugin %r: provider() returned %s, not a StreamProvider — skipped",
                               entry.name, type(prov).__name__)
                sys.modules.pop(pkg, None)
                continue
            registry.register(prov)
            count += 1
        except Exception:
            sys.modules.pop(pkg, None)
            logger.exception("provider plugin %r failed to load — skipped", entry.name)
    return count
