"""Test bootstrap.

``packages/ocr/tools`` holds the synthetic corpus library and the Phase 0
measurement scripts. It is not an importable product package — WP1 introduces
``marginalia_ocr`` — so the tests put it on the path here rather than turning a
tool into a distribution.
"""

import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / "tools"
TESTS = Path(__file__).resolve().parent
for path in (TOOLS, TESTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
