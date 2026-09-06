"""What each search hit is cited from — the citation draft.

A hit's text is quotable only when its document has canonical text and its
row carries offsets. This reads both for a whole result page at once: one
document query and one text query per page, never one per hit, the same way
the window reader batches its span and ancestor reads.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from research_engine.domain.passages import HitSource

if TYPE_CHECKING:
    from uuid import UUID

    from research_engine.domain.passages import Passage


class HitSourceReader:
    """Batch-read the citation draft for a page of passages."""

    def __init__(self, documents: Any, document_texts: Any) -> None:
        self._documents = documents
        self._texts = document_texts

    async def read(self, passages: list[Passage]) -> dict[UUID, HitSource]:
        """One `HitSource` per passage, keyed by passage id."""
        doc_ids = sorted({passage.document_id for passage in passages})
        documents, versions = await asyncio.gather(
            self._documents.get_many(doc_ids),
            self._texts.parser_versions(doc_ids),
        )
        by_document = {document.id: document for document in documents}
        return {
            passage.id: HitSource(
                document_title=(
                    by_document[passage.document_id].title
                    if passage.document_id in by_document
                    else None
                ),
                edition_key=_metadata(by_document, passage, "edition_key"),
                edition=_metadata(by_document, passage, "edition"),
                parser_version=versions.get(passage.document_id),
                has_canonical_text=passage.document_id in versions,
                has_offsets=passage.char_start is not None
                and passage.char_end is not None,
            )
            for passage in passages
        }


def _metadata(
    by_document: dict[UUID, Any], passage: Passage, key: str
) -> str | None:
    document = by_document.get(passage.document_id)
    if document is None:
        return None
    value = (document.metadata or {}).get(key)
    return str(value) if value is not None else None
