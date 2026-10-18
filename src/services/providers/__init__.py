"""Pluggable video-generation providers.

Two backends live side-by-side:

* **veo** (default): existing ``VeoClient`` — talks to Google's internal HTTP API.
* **browser**: new browser-automation path via the vendored ``browser2api``
  fork (``vendor/browser2api``) — drives the real Flow UI via Chrome CDP.

The browser path does NOT change the default behaviour. It is only activated
when ``config.provider_mode == "browser"`` (or, later, as an auto-fallback).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# Make the vendored browser2api importable without an install step.
_VENDOR_SRC = Path(__file__).resolve().parents[3] / "vendor" / "browser2api" / "src"
if _VENDOR_SRC.is_dir():
    p = str(_VENDOR_SRC)
    if p not in sys.path:
        sys.path.insert(0, p)


# Wire ``browser2api.*`` (and Playwright) loggers into the veoapi logger so
# Flow-client / browser-pool / flow-auth output shows up in app.log alongside
# everything else. Without this, those loggers propagate to root which has
# no handlers in the main service → all browser2api activity is invisible.
def _attach_browser2api_logging() -> None:
    from ...core.logger import logger as veoapi_logger  # local import — avoid cycles

    for name in ("browser2api", "playwright"):
        lg = logging.getLogger(name)
        lg.setLevel(logging.DEBUG)
        lg.propagate = False
        # Avoid duplicate handlers on hot-reload
        veoapi_handler_ids = {id(h) for h in lg.handlers}
        for h in veoapi_logger.handlers:
            if id(h) not in veoapi_handler_ids:
                lg.addHandler(h)


_attach_browser2api_logging()
