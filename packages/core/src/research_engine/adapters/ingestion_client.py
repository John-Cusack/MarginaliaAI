"""SDK ingestion client backed by the core orchestration boundary."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from research_engine.domain.nodes import DocumentNodeDraft, build_node_tree
from research_engine.services.ingestion.pipeline import run_chunking
from research_engine_sdk import NodeDraft, PassageDraft

if TYPE_CHECKING:
    from research_engine.plugins.registry import PluginRegistry
    from research_engine.ports.repositories import DocumentRepo
    from research_engine.services.ingestion.orchestrator import IngestionOrchestrator


class IngestionServiceAdapter:
    """Apply SDK DTOs and full-text ingest requests to core transactions."""

    def __init__(
        self,
        orchestrator: IngestionOrchestrator,
        registry: PluginRegistry,
        documents: DocumentRepo | None = None,
    ) -> None:
        self._orchestrator = orchestrator
        self._registry = registry
        self._documents = documents

    async def ingest_paths(
        self, paths: list[Path], hint: str | None = None
    ) -> dict[str, Any]:
        return await self._orchestrator.ingest_paths(
            [Path(path) for path in paths], plugin_hint=hint
        )

    async def ingest_document(
        self,
        *,
        title: str,
        document_type: str,
        text: str,
        source: str = "",
        metadata: dict[str, Any] | None = None,
        language: str | None = None,
        sections: list[dict[str, Any]] | None = None,
        created_date_start: datetime | str | None = None,
        created_date_end: datetime | str | None = None,
        created_precision: str | None = None,
        edition_id: UUID | str | None = None,
    ) -> dict[str, Any]:
        self._registry.validate_document_type(document_type)
        definition = self._registry.list_document_types().get(document_type, {})
        chunker_id = definition.get("default_chunker", "prose_window")

        chunk_metadata = dict(metadata or {})
        if sections and chunker_id == "structural":
            chunk_metadata["sections"] = sections
        drafts = await run_chunking(
            text,
            chunker_id,
            chunk_metadata,
            parser_id="plugin_document",
        )
        node_drafts = (
            build_node_tree(sections, text_length=len(text), title=title)
            if sections
            else None
        )
        return await self._orchestrator.ingest_drafts(
            title,
            document_type,
            drafts,
            source=source,
            metadata=metadata,
            language=language,
            full_text=text,
            node_drafts=node_drafts,
            created_date_start=_as_datetime(created_date_start),
            created_date_end=_as_datetime(created_date_end),
            created_precision=created_precision,
            edition_id=UUID(str(edition_id)) if edition_id else None,
        )

    async def ingest_drafts(
        self,
        title: str,
        document_type: str,
        passage_drafts: list[PassageDraft],
        *,
        source: str = "",
        metadata: dict[str, Any] | None = None,
        language: str | None = None,
        full_text: str | None = None,
        node_drafts: list[NodeDraft] | None = None,
    ) -> dict[str, Any]:
        self._registry.validate_document_type(document_type)
        if full_text is not None:
            for draft in passage_drafts:
                if draft.text != full_text[draft.char_start : draft.char_end]:
                    raise ValueError(
                        f"passage {draft.position} text does not match its canonical span"
                    )
        core_nodes = (
            [
                DocumentNodeDraft.model_validate(
                    draft.model_dump() if isinstance(draft, NodeDraft) else draft
                )
                for draft in node_drafts
            ]
            if node_drafts
            else None
        )
        return await self._orchestrator.ingest_drafts(
            title,
            document_type,
            passage_drafts,
            source=source,
            metadata=metadata,
            language=language,
            full_text=full_text,
            node_drafts=core_nodes,
        )

    async def find_existing(
        self, *, source: str | None = None, source_pattern: str | None = None
    ) -> list[dict[str, Any]]:
        return await self._orchestrator.find_existing(
            source=source, source_pattern=source_pattern
        )

    async def update_document(
        self,
        document_id: UUID | str,
        *,
        title: str | None = None,
        document_type: str | None = None,
        created_date_start: datetime | str | None = None,
        created_date_end: datetime | str | None = None,
        created_precision: str | None = None,
        clear_created_date: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Change a document's description without touching its content.

        Title, type and date can change — a letter's date is decided after it
        has been split out of its volume, and revised on review — and
        *metadata* is merged in. Text, source and identity cannot: a document
        whose text changes is a different document, and is ingested as one.
        ``None`` arguments leave a field alone; *clear_created_date* removes the
        date. Returns ``None`` when no such document exists.
        """
        if self._documents is None:
            raise RuntimeError("this ingestion client was built without a document repository")
        values: dict[str, Any] = {}
        if title is not None:
            values["title"] = title
        if document_type is not None:
            self._registry.validate_document_type(document_type)
            values["document_type"] = document_type
        if clear_created_date:
            values.update(
                created_date_start=None, created_date_end=None, created_precision=None
            )
        else:
            if created_date_start is not None:
                values["created_date_start"] = _as_datetime(created_date_start)
            if created_date_end is not None:
                values["created_date_end"] = _as_datetime(created_date_end)
            if created_precision is not None:
                values["created_precision"] = created_precision
        stored = await self._documents.update_fields(
            UUID(str(document_id)), values, metadata_patch=metadata
        )
        if stored is None:
            return None
        return {
            "document_id": str(stored.id),
            "title": stored.title,
            "document_type": stored.document_type,
            "created_date_start": _iso(stored.created_date_start),
            "created_date_end": _iso(stored.created_date_end),
            "created_precision": stored.created_precision,
            "metadata": stored.metadata,
        }

    async def delete_document(self, document_id: UUID | str) -> bool:
        """Delete a document with its text, nodes, passages and their indexes.

        For documents a pack derived and is replacing — a letter whose slice of
        its volume moved. Anything that pins the document (a citation, a claim
        anchor) is a foreign key that refuses the delete, and the error says so.
        """
        if self._documents is None:
            raise RuntimeError("this ingestion client was built without a document repository")
        document_id = UUID(str(document_id))
        if await self._documents.get(document_id) is None:
            return False
        await self._documents.delete(document_id)
        return True


def _as_datetime(value: datetime | str | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None
