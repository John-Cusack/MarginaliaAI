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


def normalize_with_map(text: str) -> tuple[str, list[int]]:
    """`normalize`, plus a map from each output character to its raw offset.

    ``index_map[i]`` is the offset in *text* of ``out[i]``. This is what lets a
    quotation that only matches after typographic folding still report the exact
    characters of the source it was found at — without it, the `normalized` tier
    could say "yes, it's in there" but not where, which is half an answer for
    someone who needs a page number.

    Divergence from `normalize` is deliberate and small: NFKC is applied per
    character rather than to the whole string, because whole-string NFKC
    recombines a base character and a following combining mark into one, and a
    2->1 contraction spanning input characters has no single raw offset to point
    at. Hebrew pointing and Greek accents make that common in this corpus rather
    than exotic.

    That divergence is safe **only because both sides go through this same
    function**. A decomposed source and a composed quotation both stay
    decomposed here, so they still match each other. Compare against
    `normalize`'s output and they would not — which is why the matching path
    uses `normalize_for_matching` on the query rather than `normalize`.
    """
    deleted = _linebreak_hyphen_deletions(text)
    out: list[str] = []
    index_map: list[int] = []
    in_whitespace = False

    for i, ch in enumerate(text):
        if i in deleted or ch == _SOFT_HYPHEN:
            continue
        if ch.isspace():
            if not in_whitespace and out:
                out.append(" ")
                index_map.append(i)
                in_whitespace = True
            continue
        in_whitespace = False
        folded = unicodedata.normalize(
            "NFKC", ch.translate(_QUOTES).translate(_DASHES)
        )
        for folded_ch in folded:
            out.append(folded_ch)
            index_map.append(i)

    # Leading whitespace is never emitted (the `and out` guard); trailing can be.
    while out and out[-1] == " ":
        out.pop()
        index_map.pop()
    return "".join(out), index_map


def normalize_for_matching(text: str) -> str:
    """The query-side counterpart of `normalize_with_map`.

    Same transforms, no map. Use this rather than `normalize` whenever the
    result is compared against `normalize_with_map` output.
    """
    return normalize_with_map(text)[0]


def _linebreak_hyphen_deletions(text: str) -> set[int]:
    """Raw offsets removed by de-hyphenating across a line break.

    `_LINEBREAK_HYPHEN` replaces the whole match with its two captured
    characters, so everything strictly between them — the hyphen and the
    surrounding whitespace — disappears.
    """
    deleted: set[int] = set()
    for match in _LINEBREAK_HYPHEN.finditer(text):
        deleted.update(range(match.start() + 1, match.end() - 1))
    return deleted
