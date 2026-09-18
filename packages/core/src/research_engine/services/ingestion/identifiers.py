"""Stable edition identifiers recovered from a document's own first page."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote

_DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
_JSTOR_RE = re.compile(
    r"https?://(?:www\.)?jstor\.org/stable/(?P<identifier>[^\s?#]+)",
    re.IGNORECASE,
)
_ISBN_RE = re.compile(
    r"\bISBN(?:-1[03])?\s*:?[\s]*(?P<identifier>[0-9X][0-9X\-\s]{8,20}[0-9X])",
    re.IGNORECASE,
)
_TRAILING_PUNCTUATION = ".,;:!?"


def normalize_doi(value: str) -> str | None:
    """Return a lowercase bare DOI, stripping URL and sentence punctuation."""
    decoded = unquote(value).strip()
    match = _DOI_RE.search(decoded)
    if match is None:
        return None
    doi = match.group(0).rstrip(_TRAILING_PUNCTUATION)
    for closing, opening in ((")", "("), ("]", "["), ("}", "{")):
        while doi.endswith(closing) and doi.count(closing) > doi.count(opening):
            doi = doi[:-1]
    return doi.lower()


def _isbn13_check_digit(first_twelve: str) -> str:
    total = sum(
        int(digit) * (1 if index % 2 == 0 else 3)
        for index, digit in enumerate(first_twelve)
    )
    return str((10 - total % 10) % 10)


def _valid_isbn10(value: str) -> bool:
    total = sum(
        (10 - index) * (10 if digit == "X" else int(digit))
        for index, digit in enumerate(value)
    )
    return total % 11 == 0


def normalize_isbn(value: str) -> str | None:
    """Validate an ISBN and return its canonical ISBN-13 representation."""
    compact = re.sub(r"[^0-9X]", "", value.upper())
    if len(compact) == 10 and compact[:9].isdigit() and _valid_isbn10(compact):
        first_twelve = f"978{compact[:9]}"
        return f"{first_twelve}{_isbn13_check_digit(first_twelve)}"
    if (
        len(compact) == 13
        and compact.isdigit()
        and compact[-1] == _isbn13_check_digit(compact[:12])
    ):
        return compact
    return None


def _clean_jstor_identifier(value: str) -> str:
    identifier = unquote(value).rstrip(f"{_TRAILING_PUNCTUATION})]}}").lower()
    return identifier


def with_first_page_identity(
    metadata: dict[str, Any], first_page_text: str
) -> dict[str, Any]:
    """Complete parser metadata with the best stable identity on page one.

    The first page is deliberate. Searching a whole paper finds the works it
    cites and assigns one of *their* identifiers to this document.
    """
    if isinstance(metadata.get("edition_key"), str) and metadata["edition_key"].strip():
        return metadata

    enriched = dict(metadata)
    doi = normalize_doi(str(metadata.get("doi") or "")) or normalize_doi(first_page_text)
    if doi is not None:
        enriched["doi"] = doi
        enriched["edition_key"] = f"doi:{doi}"
        return enriched

    jstor_match = _JSTOR_RE.search(first_page_text)
    if jstor_match is not None:
        identifier = _clean_jstor_identifier(jstor_match.group("identifier"))
        if identifier:
            enriched["jstor_stable_url"] = (
                f"https://www.jstor.org/stable/{identifier}"
            )
            enriched["edition_key"] = f"jstor:{identifier}"
            return enriched

    raw_isbn = str(metadata.get("isbn") or "")
    isbn = normalize_isbn(raw_isbn)
    if isbn is None and (isbn_match := _ISBN_RE.search(first_page_text)) is not None:
        isbn = normalize_isbn(isbn_match.group("identifier"))
    if isbn is not None:
        enriched["isbn"] = isbn
        enriched["edition_key"] = f"isbn:{isbn}"
    return enriched
