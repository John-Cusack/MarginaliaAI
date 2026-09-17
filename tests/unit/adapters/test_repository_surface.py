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
    PGCitationRepo,
    PGClaimRepo,
    PGDocumentNodeRepo,
    PGDocumentRepo,
    PGDocumentTextRepo,
    PGEditionRepo,
    PGPassageRepo,
    PGSourceSpanRepo,
    PGWaiverRepo,
    PGWorkBlockRepo,
    PGWorkLinkRepo,
    PGWorkRepo,
    PGWorkRevisionRepo,
)

EXPECTED = {
    PGClaimRepo: [
        "upsert_claim", "add_edge", "add_anchor", "existing_refs", "get_by_ref",
        "anchors_for", "anchor_by_id", "edges_for", "audit",
    ],
    PGDocumentTextRepo: [
        "put", "get", "get_text", "get_span", "count", "missing_document_ids",
        "find_documents_containing", "lengths", "find_raw", "find_normalized",
        "get_spans", "parser_versions",
    ],
    PGPassageRepo: [
        "get", "get_many", "get_by_document", "covering_span", "set_locators",
        "set_node_ids", "vector_search", "keyword_search", "insert_many",
    ],
    PGDocumentRepo: ["get", "get_many", "insert", "find_by_hash", "find_by_metadata"],
    PGDocumentNodeRepo: [
        "get", "get_tree", "get_outline", "get_subtree",
        "get_ancestors", "get_ancestors_many", "find_by_span", "insert_many",
    ],
    PGSourceSpanRepo: ["resolve", "get", "for_document", "stale"],
    PGEditionRepo: ["get", "get_by_key", "upsert_key", "list_keys"],
    PGWorkRepo: [
        "insert", "get", "get_by_slug", "list", "set_current_revision",
        "update", "archive",
    ],
    PGWorkRevisionRepo: [
        "insert", "get", "latest", "copy_forward", "set_message", "freeze",
        "publish", "supersede",
    ],
    PGWorkBlockRepo: ["upsert", "tree", "by_key", "delete"],
    PGCitationRepo: [
        "insert_occurrence", "insert_item", "for_block", "for_revision",
        "by_key", "citing_span", "citing_key",
    ],
    PGWorkLinkRepo: [
        "add_source_link", "add_entity_link", "for_block", "for_span", "for_entity",
    ],
    PGWaiverRepo: ["insert", "for_revision"],
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
