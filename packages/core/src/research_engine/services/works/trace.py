"""Trace a work's grounding — blocks to spans to documents, and back.

Down from a work, block, or citation: which corpus addresses it rests on.
Up from a span or a document: every block and anchor resting on it. Output
is a tree of labelled nodes, not prose, so callers can render or judge it.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from research_engine.domain.errors import NotFoundError
from research_engine.services.works.assembly import assemble_revision


class TraceNode(BaseModel):
    kind: str
    id: str | None = None
    label: str
    children: list[TraceNode] = Field(default_factory=list)


class WorkTraceService:
    """One selector in, a grounding tree out."""

    def __init__(
        self,
        *,
        works: Any,
        revisions: Any,
        blocks: Any,
        citations: Any,
        links: Any,
        spans: Any,
        documents: Any,
    ) -> None:
        self._works = works
        self._revisions = revisions
        self._blocks = blocks
        self._citations = citations
        self._links = links
        self._spans = spans
        self._documents = documents

    async def trace(
        self,
        *,
        slug: str | None = None,
        block_key: str | None = None,
        citation_key: str | None = None,
        source_span_id: str | None = None,
        document_id: str | None = None,
    ) -> TraceNode:
        """Walk down from authored rows or up from corpus addresses."""
        given = {
            name: value
            for name, value in (
                ("slug", slug),
                ("block_key", block_key),
                ("citation_key", citation_key),
                ("source_span_id", source_span_id),
                ("document_id", document_id),
            )
            if value is not None
        }
        if len(given) != 1:
            raise ValueError(
                "trace takes exactly one selector: slug, block_key, "
                "citation_key, source_span_id, or document_id"
            )
        if slug is not None:
            return await self._trace_work(slug)
        if block_key is not None:
            return await self._trace_block(block_key)
        if citation_key is not None:
            return await self._trace_citation(citation_key)
        if source_span_id is not None:
            return await self._trace_span(source_span_id)
        assert document_id is not None
        return await self._trace_document(document_id)

    async def _trace_work(self, slug: str) -> TraceNode:
        work = await self._works.get_by_slug(slug)
        if work is None:
            raise NotFoundError("work", slug)
        if work.current_revision_id is None:
            raise NotFoundError("work_revision", f"current of {slug}")
        revision = await self._revisions.get(work.current_revision_id)
        if revision is None:
            raise NotFoundError("work_revision", work.current_revision_id)
        view = await assemble_revision(
            work,
            revision,
            blocks=self._blocks,
            citations=self._citations,
            links=self._links,
            spans=self._spans,
        )
        by_id = {item.block.id: item for item in view.blocks}
        roots = [item for item in view.blocks if item.block.parent_id is None]
        return TraceNode(
            kind="work",
            id=str(work.id),
            label=f"{work.slug} rev {revision.revision_number} ({revision.state.value})",
            children=[self._block_down(item, view, by_id) for item in roots],
        )

    async def _trace_block(self, raw_key: str) -> TraceNode:
        key = _uuid(raw_key, "block_key")
        found = await self._find_block(key)
        if found is None:
            raise NotFoundError("work_block", raw_key)
        _work, _revision, view, item = found
        by_id = {entry.block.id: entry for entry in view.blocks}
        return self._block_down(item, view, by_id)

    async def _trace_citation(self, raw_key: str) -> TraceNode:
        key = _uuid(raw_key, "citation_key")
        # Occurrences are addressed per revision: search the works' current
        # revisions for the key. Works are few by nature, so this fan-out
        # stays small.
        works = await self._works.list()
        for work in works:
            if work.current_revision_id is None:
                continue
            revision = await self._revisions.get(work.current_revision_id)
            if revision is None:
                continue
            entry = await self._citations.by_key(revision.id, key)
            if entry is not None:
                view = await assemble_revision(
                    work,
                    revision,
                    blocks=self._blocks,
                    citations=self._citations,
                    links=self._links,
                    spans=self._spans,
                )
                return self._occurrence_down(view, entry)
        raise NotFoundError("citation_occurrence", raw_key)

    async def _trace_span(self, raw_id: str) -> TraceNode:
        span_id = _uuid(raw_id, "source_span_id")
        span = await self._spans.get(span_id)
        if span is None:
            raise NotFoundError("source_span", raw_id)
        node = await self._span_down(span)
        node.children.extend(await self._span_up(span_id))
        return node

    async def _trace_document(self, raw_id: str) -> TraceNode:
        document_id = _uuid(raw_id, "document_id")
        document = await self._documents.get(document_id)
        if document is None:
            raise NotFoundError("document", raw_id)
        children: list[TraceNode] = []
        for span in await self._spans.for_document(document_id):
            up = await self._span_up(span.id)
            children.append(
                TraceNode(
                    kind="source_span",
                    id=str(span.id),
                    label=f"[{span.char_start}, {span.char_end})",
                    children=up,
                )
            )
        return TraceNode(
            kind="document",
            id=str(document_id),
            label=document.title or str(document_id),
            children=children,
        )

    def _block_down(self, item: Any, view: Any, by_id: Any) -> TraceNode:
        block = item.block
        children = [
            self._occurrence_down(view, entry) for entry in item.citations
        ]
        for link in (item.links.sources if item.links else []):
            span = view.spans.get(link.source_span_id)
            children.append(
                TraceNode(
                    kind="source_link",
                    id=str(link.source_span_id),
                    label=f"{link.relation}"
                    + (
                        f" [{span.char_start}, {span.char_end})"
                        if span is not None
                        else ""
                    ),
                )
            )
        for link in (item.links.entities if item.links else []):
            children.append(
                TraceNode(
                    kind="entity_link",
                    id=str(link.entity_id),
                    label=f"{link.relation}"
                    + (f" «{link.surface_form}»" if link.surface_form else ""),
                )
            )
        for child in view.blocks:
            if child.block.parent_id == block.id:
                children.append(self._block_down(child, view, by_id))
        title = f" «{block.title}»" if block.title else ""
        return TraceNode(
            kind="block",
            id=str(block.block_key),
            label=f"{block.block_type}{title}",
            children=children,
        )

    def _occurrence_down(self, view: Any, entry: Any) -> TraceNode:
        occurrence = entry.occurrence
        children = []
        for row in entry.items:
            if row.source_span_id is not None:
                span = view.spans.get(row.source_span_id)
                if span is not None:
                    children.append(
                        TraceNode(
                            kind="source_span",
                            id=str(span.id),
                            label=f"[{span.char_start}, {span.char_end}) "
                            f"{row.verify_status or 'unverified'}",
                        )
                    )
            elif row.zotero_key is not None:
                children.append(
                    TraceNode(
                        kind="bibliography",
                        label=f"{row.zotero_key} (no span)",
                    )
                )
        return TraceNode(
            kind="citation",
            id=str(occurrence.citation_key),
            label=f"{occurrence.intent.value} {occurrence.placement.value}",
            children=children,
        )

    async def _span_down(self, span: Any) -> TraceNode:
        document = await self._documents.get(span.document_id)
        doc_label = (
            document.title or str(span.document_id)
            if document is not None
            else str(span.document_id)
        )
        return TraceNode(
            kind="source_span",
            id=str(span.id),
            label=f"[{span.char_start}, {span.char_end})",
            children=[
                TraceNode(
                    kind="document",
                    id=str(span.document_id),
                    label=f"{doc_label} offsets {span.char_start}–{span.char_end}",
                )
            ],
        )

    async def _span_up(self, span_id: UUID) -> list[TraceNode]:
        """Every block and anchor resting on this span."""
        nodes = []
        for entry in await self._citations.citing_span(span_id):
            block = await self._blocks.get(entry.occurrence.block_id)
            label = str(entry.occurrence.citation_key)
            if block is not None:
                label += f" in block {block.block_key} ({block.block_type})"
                work_slug = await self._slug_of_revision(block.revision_id)
                if work_slug is not None:
                    label += f" of {work_slug}"
            nodes.append(
                TraceNode(
                    kind="citation",
                    id=str(entry.occurrence.citation_key),
                    label=label,
                )
            )
        for link in await self._links.for_span(span_id):
            block = await self._blocks.get(link.block_id)
            label = link.relation
            if block is not None:
                label += f" from block {block.block_key} ({block.block_type})"
            nodes.append(
                TraceNode(kind="source_link", id=str(span_id), label=label)
            )
        return nodes

    async def _find_block(self, key: UUID) -> tuple | None:
        for work in await self._works.list():
            if work.current_revision_id is None:
                continue
            revision = await self._revisions.get(work.current_revision_id)
            if revision is None:
                continue
            block = await self._blocks.by_key(revision.id, key)
            if block is not None:
                view = await assemble_revision(
                    work,
                    revision,
                    blocks=self._blocks,
                    citations=self._citations,
                    links=self._links,
                    spans=self._spans,
                )
                for item in view.blocks:
                    if item.block.id == block.id:
                        return work, revision, view, item
        return None

    async def _slug_of_revision(self, revision_id: UUID) -> str | None:
        revision = await self._revisions.get(revision_id)
        if revision is None:
            return None
        work = await self._works.get(revision.work_id)
        return work.slug if work is not None else None


def _uuid(raw: str, name: str) -> UUID:
    try:
        return UUID(raw)
    except (ValueError, TypeError):
        raise ValueError(f"{name} is not a UUID: {raw}") from None
