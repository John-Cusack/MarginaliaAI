"""Normalization for quote matching.

A researcher pasting a quotation from a printed edition will not reproduce the
OCR's typography. Every transform here corresponds to a real artefact of scanned
text:

* soft hyphens and line-break hyphenation, from justified print
* curly quotes and dashes, from typesetting
* ligatures and full-width forms, from NFKC-normalisable source encodings
* collapsed whitespace, from column and page breaks

Normalization is *lossy on purpose*, so it never replaces the raw text — raw is
what offsets address. ``NORMALIZATION_VERSION`` is stored alongside so this can
evolve without invalidating stored spans.
"""

from __future__ import annotations

import re
import unicodedata

#: Bump whenever the output of `normalize` changes for some input.
NORMALIZATION_VERSION = "1.0"

_SOFT_HYPHEN = "­"

#: Hyphen at a line break: "fis-\ncal" -> "fiscal". Only when the next line
#: starts lowercase, so a real compound like "Anglo-\nSaxon" keeps its hyphen.
#: Python's `re` has no \p{Ll}, hence the explicit Latin-1 lowercase ranges.
_LINEBREAK_HYPHEN = re.compile(r"(\w)[-‐‑]\s*\n\s*([a-zß-öø-ÿ])")

_QUOTES = str.maketrans(
    {
        "‘": "'", "’": "'", "‚": "'", "‛": "'",
        "“": '"', "”": '"', "„": '"', "‟": '"',
        "‹": "'", "›": "'",
        "«": '"', "»": '"',
        "ʼ": "'", "′": "'", "″": '"',
    }
)

_DASHES = str.maketrans(
    {
        "‐": "-", "‑": "-", "‒": "-", "–": "-",
        "—": "-", "―": "-", "−": "-",
    }
)

_WHITESPACE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Fold away the differences that separate a quotation from its source.

    Not idempotent-safe to apply to a *span* and compare against a normalized
    *document* offset-for-offset: lengths change. Matching maps back to raw
    offsets separately.
    """
    text = unicodedata.normalize("NFKC", text)
    text = text.replace(_SOFT_HYPHEN, "")
    text = _LINEBREAK_HYPHEN.sub(r"\1\2", text)
    text = text.translate(_QUOTES)
    text = text.translate(_DASHES)
    text = _WHITESPACE.sub(" ", text)
    return text.strip()


def normalize_whitespace(text: str) -> str:
    """Collapse whitespace runs only.

    The narrower transform used for re-anchoring: passages written by
    ``prose_window`` 1.0 were rebuilt with ``" ".join(sentences)``, which
    collapsed whitespace runs and changed nothing else. Applying the same
    collapse to both sides makes an old passage an exact substring of the
    canonical text, which is what lets P1-5 find it again.
    """
    return _WHITESPACE.sub(" ", text).strip()
