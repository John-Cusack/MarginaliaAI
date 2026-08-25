"""Repository classes still expose the methods their callers rely on.

Guarding a failure mode that is invisible to every other kind of test: a
module-level `def` placed inside a class body ends the class, and everything
below it silently becomes a nested function rather than a method. It is legal
Python, so there is no syntax error and no import error — the methods simply
stop existing, and only a caller finds out.

That happened here. A helper added mid-class removed `missing_document_ids` and
`count` from `PGDocumentTextRepo`; the whole unit suite stayed green and one
integration test caught it.
"""

from __future__ import annotations

import inspect

import pytest

from research_engine.adapters.storage.postgres.repositories import (
    PGDocumentRepo,
    PGDocumentTextRepo,
    PGPassageRepo,
)

EXPECTED = {
    PGDocumentTextRepo: [
        "put", "get", "get_text", "get_span", "count", "missing_document_ids",
        "find_documents_containing", "lengths", "find_raw", "find_normalized",
    ],
    PGPassageRepo: [
        "get", "get_by_document", "covering_span",
        "vector_search", "keyword_search", "insert_many",
    ],
    PGDocumentRepo: ["get", "insert", "find_by_hash"],
}


@pytest.mark.parametrize(
    ("repo", "method"),
    [(repo, m) for repo, methods in EXPECTED.items() for m in methods],
    ids=lambda v: v if isinstance(v, str) else v.__name__,
)
def test_repository_exposes_method(repo: type, method: str) -> None:
    attribute = getattr(repo, method, None)
    assert attribute is not None, (
        f"{repo.__name__}.{method} is missing. A module-level `def` inside the "
        f"class body ends it, turning everything below into nested functions."
    )
    assert inspect.isfunction(attribute), (
        f"{repo.__name__}.{method} is not a function on the class."
    )
