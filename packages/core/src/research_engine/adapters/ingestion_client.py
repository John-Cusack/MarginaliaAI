"""SDK ingestion client backed by the core orchestration boundary."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from research_engine.domain.nodes import DocumentNodeDraft, build_node_tree
from research_engine.services.ingestion.pipeline import run_chunking
from research_engine_sdk import NodeDraft, PassageDraft

if TYPE_CHECKING:
    from research_engine.plugins.registry import PluginRegistry
    from research_engine.services.ingestion.orchestrator import IngestionOrchestrator


class IngestionServiceAdapter:
    """Apply SDK DTOs and full-text ingest requests to core transactions."""

    def __init__(
        self,
        orchestrator: IngestionOrchestrator,
        registry: PluginRegistry,
    ) -> None:
        self._orchestrator = orchestrator
        self._registry = registry

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
