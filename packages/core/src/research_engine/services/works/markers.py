"""`{{cite:<key>}}` markers — the one bijection validation judges.

The caller places the marker `CitationService.attach` returns into the block
text; `work_validate` checks that markers and occurrences match one per one
in one block. Every reader of markers uses this regex so a marker no reader
sees cannot exist.
"""

from __future__ import annotations

import re
from uuid import UUID

#: A marker names its occurrence's `citation_key`, a UUID.
MARKER_RE = re.compile(r"\{\{cite:([^}]+)\}\}")


def format_marker(citation_key: UUID) -> str:
    """The marker text for an occurrence, placed by the caller."""
    return "{{cite:" + str(citation_key) + "}}"


def find_markers(text: str) -> tuple[set[str], list[str]]:
    """Split a block's markers into valid citation-key strings and dangling raw text.

    A `{{cite:…}}` whose key is not a UUID can match no occurrence, so it is
    reported dangling without a lookup. Returns `(keys, invalid)`.
    """
    keys: set[str] = set()
    invalid: list[str] = []
    for match in MARKER_RE.finditer(text or ""):
        raw = match.group(1)
        try:
            keys.add(str(UUID(raw)))
        except ValueError:
            invalid.append(match.group(0))
    return keys, invalid
