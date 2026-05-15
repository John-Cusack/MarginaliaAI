"""Adapter that conforms core search/document/passage services to the SDK
``CorpusClient`` Protocol.

Plugins receive an instance of this adapter as their ``corpus`` client. The
simple Protocol surface — ``find_passages(query, filters=, k=)`` plus
``get_document`` / ``get_passage_context`` — keeps plugin code free of the
core domain layer for the common case. Plugins that need fusion mode, alpha,
or rerank control reach for ``find_passages_advanced(SearchQuery)``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from research_engine.domain.passages import SearchFilters, SearchQuery, SearchResult

if TYPE_CHECKING:
    from research_engine.ports.repositories import DocumentRepo, PassageRepo
    from research_engine.services.search.hybrid import HybridSearchService


class CorpusServiceAdapter:
    """Concrete implementation of the SDK ``CorpusClient`` Protocol."""

    def __init__(
        self,
        search: HybridSearchService,
        documents: DocumentRepo,
        passages: PassageRepo,
    ) -> None:
        self._search = search
        self._documents = documents
        self._passages = passages

    async def find_passages(
        self,
        query: str,
        *,
        filters: dict[str, Any] | None = None,
        k: int = 20,
    ) -> SearchResult:
        """Hybrid search with the simple plugin-facing surface.

        ``filters`` accepts the same keys as ``SearchFilters`` (document_types,
        date_range_start/end, metadata, extensions, etc.). Pass ``None`` to
        search the full corpus with default fusion settings.
        """
        search_filters = SearchFilters(**filters) if filters else None
        return await self._search.find_passages(
            SearchQuery(text=query, filters=search_filters, k=k)
        )

    async def find_passages_advanced(self, query: SearchQuery) -> SearchResult:
        """Escape hatch for plugins that need full SearchQuery control —
        fusion mode, alpha, rerank, k_vec/k_kw splits."""
        return await self._search.find_passages(query)

    async def get_document(self, document_id: UUID) -> dict[str, Any] | None:
        doc = await self._documents.get(document_id)
        if doc is None:
            return None
        passages = await self._passages.get_by_document(document_id)
        passages_sorted = sorted(passages, key=lambda p: p.position)
        return {
            "id": str(doc.id),
            "title": doc.title,
            "document_type": doc.document_type,
            "source": doc.source,
            "metadata": doc.metadata,
            "passages": [
                {"id": str(p.id), "position": p.position, "text": p.text}
                for p in passages_sorted
            ],
        }

    async def get_passage_context(
        self, passage_id: UUID, *, before: int = 0, after: int = 0
    ) -> dict[str, Any]:
        before_p, target, after_p = await self._passages.get_context(
            passage_id, before=before, after=after
        )
        return {
            "target": {"passage_id": str(target.id), "text": target.text},
            "before": [{"passage_id": str(p.id), "text": p.text} for p in before_p],
            "after": [{"passage_id": str(p.id), "text": p.text} for p in after_p],
            "document_id": str(target.document_id),
        }
