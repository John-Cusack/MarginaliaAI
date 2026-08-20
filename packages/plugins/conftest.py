"""Put each in-tree pack on `sys.path` so its own tests can import it.

A pack is imported by absolute name — `history.tools._holdings`, not a path
relative to the engine — because at runtime the loader inserts the installed
pack directory on `sys.path` and imports it that way (`plugins/loader.py`).
Nothing does that during a test run, so without this the packs' tests collect
as ImportError and CI reports success having run none of them.

Inserting the pack root here rather than in a per-pack conftest means a new
pack's tests run from the moment it has a `pack.yaml`, with nothing to
remember to wire up.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PLUGINS_DIR = Path(__file__).parent

for _pack in sorted(_PLUGINS_DIR.iterdir()):
    if (_pack / "pack.yaml").is_file() and str(_pack) not in sys.path:
        sys.path.insert(0, str(_pack))
