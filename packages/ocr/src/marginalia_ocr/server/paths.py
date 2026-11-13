"""Where the service keeps its state, in one place.

The service's defaults and the sweep unit's ExecStart must agree. If they do not,
the timer sweeps an empty store while the images it was written to delete live
somewhere else, and the 14-day cap - a hard cap, and the only thing standing
between a licensed page image and an indefinite stay on disk - quietly stops
holding. Nothing at runtime would notice, so the unit is checked against these
defaults by test.

A systemd *user* unit cannot write /var/lib, so the root is under the user's own
data directory.
"""

from __future__ import annotations

import os
from pathlib import Path

#: The port the service listens on. 9882 is the embed server and 9883 is the v1
#: Kindle OCR service; both are other people's processes, so the OCR service takes
#: 9884 and a test pins the unit to it.
DEFAULT_PORT = 9884

#: The one root. `MARGINALIA_OCR_ROOT` moves it; nothing else may.
DEFAULT_ROOT = Path(
    os.environ.get("MARGINALIA_OCR_ROOT", str(Path.home() / ".local/share/marginalia-ocr"))
).expanduser()
