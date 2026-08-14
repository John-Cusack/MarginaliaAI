"""Discover conceptual overlaps between the article and the Kindle book.

Uses the hybrid search engine to find passages in each document that
are semantically similar to passages from the other, then creates
edges recording the thematic connections.

Documents:
  - Article: "A Biblical Case Against Government Economic Redistribution"
  - Book: "The Ethics of Money Production" by Jörg Guido Hülsmann
"""

from __future__ import annotations

import asyncio
import sys
from uuid import UUID

import structlog

from research_engine.adapters.storage.postgres.engine import transaction
from research_engine.composition import build_container
from research_engine.config import load_settings
from research_engine.domain.common import NodeKind
from research_engine.domain.edges import EdgeDraft
from research_engine.domain.passages import SearchFilters, SearchQuery

logger = structlog.get_logger()

# Document IDs from the database
ARTICLE_DOC_ID = UUID("019dbaea-e844-7201-83fe-69200d255ca5")
BOOK_DOC_ID = UUID("019dbac5-b33c-74e2-9a25-2117d837dcda")

# The 8 conceptual themes identified in the analysis, with search queries
# that capture each theme from both perspectives.
THEMES = [
    {
        "id": "property_rights_as_moral_foundation",
        "label": "Property Rights as a Moral Foundation",
        "queries": [
            "property rights moral foundation sacred inviolable justice",
            "taking property without authorization is theft",
            "private property sacred commutative justice",
        ],
    },
    {
        "id": "government_coercion",
        "label": "Government Force / Coercion as the Mechanism of the State",
        "queries": [
            "government monopoly on legitimate use of force compel compliance",
            "compulsion coercion by the ruler government force",
            "forcing citizens to use money of the government's choice",
        ],
    },
    {
        "id": "inflation_as_theft",
        "label": "Inflation / Money Creation as Theft and Redistribution",
        "queries": [
            "inflation redistributes real income illegitimate gains",
            "money production redistributes wealth from poor to rich",
            "redistribution unauthorized taking of property",
        ],
    },
    {
        "id": "voluntariness_principle",
        "label": "The Voluntariness Principle",
        "queries": [
            "voluntary cooperation without violating property rights",
            "giving must not be reluctantly or under compulsion",
            "free responsible initiatives private individuals",
        ],
    },
    {
        "id": "christian_moral_tradition",
        "label": "The Scholastic / Christian Moral Tradition",
        "queries": [
            "scholastic tradition Aquinas Oresme natural law moral reasoning",
            "Scripture Eighth Commandment Romans 13 biblical ethics",
            "Christian morals economics compatible Austrian",
        ],
    },
    {
        "id": "government_as_beneficiary",
        "label": "Government as Beneficiary of the Unjust System",
        "queries": [
            "government main beneficiary inflation unjust system",
            "state third party no ownership claim inserts itself",
            "legal monopolies instruments of social injustice",
        ],
    },
    {
        "id": "institutional_spheres_of_authority",
        "label": "Institutional Design — Proper Spheres of Authority",
        "queries": [
            "poverty relief assigned to individuals church not state",
            "government should not run banks or produce paper money",
            "institutional design coercive mechanism incompatible voluntary",
        ],
    },
    {
        "id": "debasement_as_theft",
        "label": "Debasement as the Historical Paradigm of Government Theft",
        "queries": [
            "debasement standard form of inflation altering coins",
            "debasement inherently unjust never permissible",
            "Mosaic theocracy authorized compulsory redistribution",
        ],
    },
]


async def find_matching_passages(
    container,
    query_text: str,
    target_doc_id: UUID,
    k: int = 5,
    min_score: float = 0.0,
):
    """Search for passages in target_doc matching query_text."""
    # Get all passage IDs for the target document
    all_passages = await container.passages.get_by_document(target_doc_id)
    candidate_ids = [p.id for p in all_passages]

    if not candidate_ids:
        return []

    # Do vector search against those candidates
    query_vec = await container.embedding.embed(query_text)
    hits = await container.passages.vector_search(
        query_vec,
        container.embedding.model_name,
        container.embedding.model_version,
        candidate_ids,
        k,
    )

    return [(pid, score) for pid, score in hits if score >= min_score]


async def main():
    settings = load_settings()
    container = await build_container(settings)

    try:
        # Load all passages for both documents
        article_passages = await container.passages.get_by_document(ARTICLE_DOC_ID)
        book_passages = await container.passages.get_by_document(BOOK_DOC_ID)

        print(f"Article passages: {len(article_passages)}")
        print(f"Book passages: {len(book_passages)}")
        print()

        edge_count = 0

        for theme in THEMES:
            print(f"=== {theme['label']} ===")

            # For each theme, find the best matching passages in each document
            article_hits = []
            book_hits = []

            for query in theme["queries"]:
                a_hits = await find_matching_passages(
                    container, query, ARTICLE_DOC_ID, k=3, min_score=0.3
                )
                b_hits = await find_matching_passages(
                    container, query, BOOK_DOC_ID, k=3, min_score=0.3
                )
                article_hits.extend(a_hits)
                book_hits.extend(b_hits)

            # Deduplicate by passage ID, keeping highest score
            article_best: dict[UUID, float] = {}
            for pid, score in article_hits:
                if pid not in article_best or score > article_best[pid]:
                    article_best[pid] = score

            book_best: dict[UUID, float] = {}
            for pid, score in book_hits:
                if pid not in book_best or score > book_best[pid]:
                    book_best[pid] = score

            # Take top passages from each
            top_article = sorted(article_best.items(), key=lambda x: x[1], reverse=True)[:3]
            top_book = sorted(book_best.items(), key=lambda x: x[1], reverse=True)[:3]

            if not top_article or not top_book:
                print(f"  (no matches found)")
                print()
                continue

            print(f"  Article matches: {len(top_article)}")
            for pid, score in top_article:
                passage = await container.passages.get(pid)
                preview = passage.text[:100].replace("\n", " ") if passage else "?"
                print(f"    [{score:.3f}] {preview}...")

            print(f"  Book matches: {len(top_book)}")
            for pid, score in top_book:
                passage = await container.passages.get(pid)
                preview = passage.text[:100].replace("\n", " ") if passage else "?"
                print(f"    [{score:.3f}] {preview}...")

            # Create edges between the best article passages and best book passages
            async with transaction(container.engine) as tx:
                for a_pid, a_score in top_article:
                    for b_pid, b_score in top_book:
                        combined_confidence = min(a_score, b_score)
                        # Only create edges where both sides have reasonable relevance
                        if combined_confidence < 0.35:
                            continue

                        draft = EdgeDraft(
                            source_kind=NodeKind.passage,
                            source_id=a_pid,
                            target_kind=NodeKind.passage,
                            target_id=b_pid,
                            relation_type="conceptual_overlap",
                            attributes={
                                "theme": theme["id"],
                                "theme_label": theme["label"],
                                "source_document": "article",
                                "target_document": "book",
                                "source_score": round(a_score, 4),
                                "target_score": round(b_score, 4),
                            },
                            confidence=round(combined_confidence, 4),
                        )
                        edge = await container.edges.insert(tx, draft)
                        edge_count += 1

            print()

        # Also create a document-level edge
        async with transaction(container.engine) as tx:
            doc_edge = EdgeDraft(
                source_kind=NodeKind.document,
                source_id=ARTICLE_DOC_ID,
                target_kind=NodeKind.document,
                target_id=BOOK_DOC_ID,
                relation_type="conceptual_overlap",
                attributes={
                    "themes": [t["id"] for t in THEMES],
                    "description": (
                        "Both works argue that redistribution through government "
                        "coercion is a violation of property rights regardless of "
                        "good intentions, and that the proper alternative is "
                        "voluntary action by individuals and private institutions."
                    ),
                    "article_framework": "biblical_protestant",
                    "book_framework": "scholastic_catholic",
                    "article_domain": "fiscal_policy",
                    "book_domain": "monetary_policy",
                },
                confidence=0.95,
            )
            await container.edges.insert(tx, doc_edge)
            edge_count += 1

        print(f"Created {edge_count} edges total.")

    finally:
        await container.close()


if __name__ == "__main__":
    asyncio.run(main())
