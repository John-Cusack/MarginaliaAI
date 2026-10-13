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

from research_engine.domain.documents import DocumentFilter
from research_engine.domain.passages import SearchFilters, SearchQuery, SearchResult

if TYPE_CHECKING:
    from datetime import datetime

    from research_engine.ports.repositories import (
        DocumentRepo,
        DocumentTextRepo,
        PassageRepo,
    )
    from research_engine.services.search.hybrid import HybridSearchService

#: The most documents `find_documents` returns in one call.
MAX_FOUND_DOCUMENTS = 5000


class CorpusServiceAdapter:
    """Concrete implementation of the SDK ``CorpusClient`` Protocol."""

    def __init__(
        self,
        search: HybridSearchService,
        documents: DocumentRepo,
        passages: PassageRepo,
        document_nodes: Any = None,
        document_texts: DocumentTextRepo | None = None,
    ) -> None:
        self._search = search
        self._documents = documents
        self._passages = passages
        self._nodes = document_nodes
        self._texts = document_texts

    async def get_document_outline(
        self, document_id: UUID, dated_only: bool = False
    ) -> list[dict[str, Any]]:
        """The document's structural tree — its chapters, entries, or letters.

        A pack could search a corpus and read a passage but had no way to ask
        what a document is *made of*, which is what any question about holdings
        needs: "which letters do I have, and when were they written" is a
        question about sections, not passages.

        ``dated_only`` keeps the sections that state a date of their own, which
        for correspondence is one entry per letter.
        """
        if self._nodes is None:
            return []
        nodes = await self._nodes.get_tree(document_id)
        return [
            {
                "id": str(node.id),
                "title": node.title,
                "depth": node.depth,
                "path": node.path,
                "char_start": node.char_start,
                "char_end": node.char_end,
                "metadata": node.metadata,
            }
            for node in nodes
            if not dated_only or (node.metadata or {}).get("date_start")
        ]

    async def find_passages(
        self,
        query: str,
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
        doc = await self._documents.get(UUID(str(document_id)))
        if doc is None:
            return None
        passages = await self._passages.get_by_document(doc.id)
        passages_sorted = sorted(passages, key=lambda p: p.position)
        return {
            **_document_json(doc),
            "passages": [
                {
                    "id": str(p.id),
                    "position": p.position,
                    "text": p.text,
                    # Offsets into the document's canonical text: what a pack
                    # needs to map an extraction's evidence back onto the page.
                    "char_start": p.char_start,
                    "char_end": p.char_end,
                    "node_id": str(p.node_id) if p.node_id else None,
                }
                for p in passages_sorted
            ],
        }

    async def get_document_text(self, document_id: UUID) -> str | None:
        """The canonical text a document's passage offsets index into.

        A pack that splits a volume into its letters has to cut the volume's
        own text: passages overlap and are bounded by chunking, not by where a
        letter starts.
        """
        if self._texts is None:
            return None
        return await self._texts.get_text(UUID(str(document_id)))

    async def find_documents(
        self,
        *,
        document_types: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        source_pattern: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Documents by type, metadata containment and source substring.

        No passages, no text — the listing a holdings check or a review queue
        needs. *metadata* matches by containment (``{"review_status":
        "needs_review"}``). At most *limit* rows, capped at 5,000.
        """
        if not (document_types or metadata or source_pattern):
            raise ValueError(
                "find_documents needs document_types, metadata or source_pattern; "
                "it does not list the whole corpus"
            )
        filt = DocumentFilter(
            document_types=document_types,
            metadata=metadata,
            source_pattern=source_pattern,
        )
        found: list[dict[str, Any]] = []
        async for doc in self._documents.iter_by_filter(filt):
            found.append(_document_json(doc))
            if len(found) >= min(limit, MAX_FOUND_DOCUMENTS):
                break
        return found

    async def get_passage_context(
        self, passage_id: UUID, before: int = 0, after: int = 0
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


def _document_json(doc: Any) -> dict[str, Any]:
    return {
        "id": str(doc.id),
        "title": doc.title,
        "document_type": doc.document_type,
        "source": doc.source,
        "language": doc.language,
        "created_date_start": _iso(doc.created_date_start),
        "created_date_end": _iso(doc.created_date_end),
        "created_precision": doc.created_precision,
        "edition_id": str(doc.edition_id) if doc.edition_id else None,
        "metadata": doc.metadata,
    }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None
