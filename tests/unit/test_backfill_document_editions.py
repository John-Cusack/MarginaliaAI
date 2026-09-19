"""Only unambiguous article identity is eligible for automatic backfill."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from uuid import UUID

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/backfill_document_editions.py"
SPEC = importlib.util.spec_from_file_location("backfill_document_editions", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

Candidate = MODULE.Candidate
safe_candidates = MODULE.safe_candidates

pytestmark = pytest.mark.unit


def candidate(number: int, edition_key: str) -> Candidate:
    return Candidate(
        document_id=UUID(int=number),
        title=f"Document {number}",
        source=f"/documents/{number}.pdf",
        edition_key=edition_key,
        metadata_patch={"edition_key": edition_key},
    )


def test_only_unique_unlinked_doi_and_jstor_candidates_are_safe() -> None:
    doi = candidate(1, "doi:10.1000/article")
    jstor = candidate(2, "jstor:12345")
    isbn = candidate(3, "isbn:9780306406157")
    duplicate_a = candidate(4, "doi:10.1000/duplicate")
    duplicate_b = candidate(5, "doi:10.1000/duplicate")
    already_linked = candidate(6, "doi:10.1000/existing")

    safe, held = safe_candidates(
        [doi, jstor, isbn, duplicate_a, duplicate_b, already_linked],
        {already_linked.edition_key},
    )

    assert safe == [doi, jstor]
    assert held == [isbn, duplicate_a, duplicate_b, already_linked]
