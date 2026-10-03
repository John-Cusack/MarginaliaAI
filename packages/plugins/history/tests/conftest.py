"""Make this directory's shared fixtures importable.

The suite runs under ``--import-mode=importlib``, which puts no test directory
on ``sys.path``; `letter_fixtures` holds real datelines several test modules
share.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = str(Path(__file__).parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
