"""A document's own identifiers become stable, conservative edition keys."""

from __future__ import annotations

import pytest

from research_engine.services.ingestion.identifiers import (
    normalize_doi,
    normalize_isbn,
    with_first_page_identity,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("DOI: 10.1177/026537880001700202.", "10.1177/026537880001700202"),
        (
            "https://doi.org/10.1080/08929882.2024.2393537",
            "10.1080/08929882.2024.2393537",
        ),
        ("(10.1000/ABC(123))", "10.1000/abc(123)"),
    ],
)
def test_doi_normalization(raw: str, expected: str) -> None:
    assert normalize_doi(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0-306-40615-2", "9780306406157"),
        ("978-0-306-40615-7", "9780306406157"),
        ("978-0-306-40615-8", None),
        ("not an isbn", None),
    ],
)
def test_isbn_normalization(raw: str, expected: str | None) -> None:
    assert normalize_isbn(raw) == expected


def test_doi_wins_over_provider_specific_and_book_identifiers() -> None:
    metadata = with_first_page_identity(
        {},
        "DOI 10.2307/26777866\n"
        "Stable URL: https://www.jstor.org/stable/26777866\n"
        "ISBN 978-0-306-40615-7",
    )

    assert metadata["edition_key"] == "doi:10.2307/26777866"
    assert metadata["doi"] == "10.2307/26777866"


def test_jstor_stable_url_is_identity_when_no_doi_exists() -> None:
    metadata = with_first_page_identity(
        {}, "Stable URL: https://www.jstor.org/stable/485371?seq=1"
    )

    assert metadata == {
        "edition_key": "jstor:485371",
        "jstor_stable_url": "https://www.jstor.org/stable/485371",
    }


def test_labeled_isbn_is_identity_when_no_article_identifier_exists() -> None:
    metadata = with_first_page_identity({}, "ISBN-10: 0-306-40615-2")

    assert metadata["edition_key"] == "isbn:9780306406157"
    assert metadata["isbn"] == "9780306406157"


def test_an_explicit_edition_key_is_never_reinterpreted() -> None:
    original = {"edition_key": "archive:declared", "isbn": "9780306406157"}

    assert with_first_page_identity(original, "DOI 10.1000/other") is original


def test_unlabeled_numbers_do_not_become_an_isbn() -> None:
    metadata = with_first_page_identity({}, "The serial number is 9780306406157.")

    assert "edition_key" not in metadata
