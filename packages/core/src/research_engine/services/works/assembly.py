"""Assemble one revision's tree with its citations, links, and spans.

`work_get`, validation, freezing, export, and trace all walk the same shape:
blocks depth-first, each with its occurrences (and their items) and its
links, plus every span the items and source links name. One function builds
it so the five callers cannot disagree about what a revision contains.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID  # noqa: TC003 - pydantic needs it at runtime

from pydantic import BaseModel, Field

from research_engine import _rust as _rust_backend

# noqa TC001 below: pydantic resolves the annotations at model definition,
# so these are runtime imports despite looking annotation-only.
from research_engine.domain.citations import BlockCitations  # noqa: TC001
from research_engine.domain.spans import SourceSpan  # noqa: TC001
from research_engine.domain.works import (  # noqa: TC001
    BlockLinks,
    Work,
    WorkBlock,
    WorkRevision,
)
from research_engine.services.works.hashing import compute_content_hash


class AssembledBlock(BaseModel):
    """One block with everything hung on it, and its parent's key."""

    block: WorkBlock
    parent_key: UUID | None
    citations: list[BlockCitations] = Field(default_factory=list)
    links: BlockLinks | None = None


class AssembledRevision(BaseModel):
    """A revision ready to validate, hash, export, or trace."""

    work: Work
    revision: WorkRevision
    blocks: list[AssembledBlock] = Field(default_factory=list)
    #: Every span named by an item or a source link, by id.
    spans: dict[UUID, SourceSpan] = Field(default_factory=dict)

    def block_index(self) -> dict[str, AssembledBlock]:
        """Blocks by row-id string, for occurrence and link joins."""
        return {str(item.block.id): item for item in self.blocks}

    def key_index(self) -> dict[str, AssembledBlock]:
        """Blocks by block-key string, for marker and parent joins."""
        return {str(item.block.block_key): item for item in self.blocks}


def hash_assembled(view: AssembledRevision) -> bytes:
    """The frozen-revision content hash: authored content, nothing volatile.

    Blocks in tree order; citations and links sorted by identity. Row ids turn
    over on copy-forward and timestamps move on reverify, so neither enters —
    `AUTH_REVISION_MUTATED` means the words changed, not the bookkeeping.
    """
    blocks = [
        {
            "block_key": str(item.block.block_key),
            "parent_key": str(item.parent_key) if item.parent_key else None,
            "position": item.block.position,
            "block_type": item.block.block_type,
            "title": item.block.title,
            "body_markdown": item.block.body_markdown,
        }
        for item in view.blocks
    ]
    citations = [
        {
            "block_key": str(item.block.block_key),
            "citation_key": str(entry.occurrence.citation_key),
            "position": row.position,
            "intent": entry.occurrence.intent.value,
            "placement": entry.occurrence.placement.value,
            "edition_id": str(row.edition_id) if row.edition_id else None,
            "edition_key": row.edition_key,
            "source_span_id": str(row.source_span_id)
            if row.source_span_id
            else None,
            "quoted_text": row.quoted_text,
            "verify_status": row.verify_status,
            "locator": dict(row.locator),
            "prefix": row.prefix,
            "suffix": row.suffix,
            "suppress_author": row.suppress_author,
        }
        for item in view.blocks
        for entry in item.citations
        for row in entry.items
    ]
    source_links = [
        {
            "block_key": str(item.block.block_key),
            "source_span_id": str(link.source_span_id),
            "relation": link.relation,
            "confidence": link.confidence,
            "note": link.note,
        }
        for item in view.blocks
        for link in (item.links.sources if item.links else [])
    ]
    entity_links = [
        {
            "block_key": str(item.block.block_key),
            "entity_id": str(link.entity_id),
            "relation": link.relation,
            "surface_form": link.surface_form,
        }
        for item in view.blocks
        for link in (item.links.entities if item.links else [])
    ]
    rs = _rust_backend.rust_works()
    if rs is not None:
        return _hash_assembled_rs(rs, blocks, citations, source_links, entity_links)
    return compute_content_hash(blocks, citations, source_links, entity_links)


def _hash_assembled_rs(rs, blocks, citations, source_links, entity_links):
    """`hash_assembled` rows via the Rust backend (see `research_engine._rust`).

    Rows cross as JSON (ids pre-stringified, exactly as the Python path
    consumes them); the digest crosses as bytes.
    """
    return bytes(
        rs.compute_content_hash(
            json.dumps(blocks),
            json.dumps(citations),
            json.dumps(source_links),
            json.dumps(entity_links),
        )
    )


async def assemble_revision(
    work: Work,
    revision: WorkRevision,
    *,
    blocks: Any,
    citations: Any,
    links: Any,
    spans: Any,
) -> AssembledRevision:
    """Load the tree, attach citations and links, and cache the spans."""
    tree = await blocks.tree(revision.id)
    by_id = {item.id: item for item in tree}
    attached = await citations.for_revision(revision.id)
    per_block: dict[str, list[BlockCitations]] = {}
    for entry in attached:
        per_block.setdefault(str(entry.occurrence.block_id), []).append(entry)

    assembled: list[AssembledBlock] = []
    span_ids: set[UUID] = set()
    for block in tree:
        parent_key = (
            by_id[block.parent_id].block_key if block.parent_id in by_id else None
        )
        block_citations = per_block.get(str(block.id), [])
        for entry in block_citations:
            for item in entry.items:
                if item.source_span_id is not None:
                    span_ids.add(item.source_span_id)
        block_links = await links.for_block(block.id)
        for link in block_links.sources:
            span_ids.add(link.source_span_id)
        assembled.append(
            AssembledBlock(
                block=block,
                parent_key=parent_key,
                citations=block_citations,
                links=block_links,
            )
        )

    span_index: dict[UUID, SourceSpan] = {}
    for span_id in span_ids:
        span = await spans.get(span_id)
        if span is not None:
            span_index[span_id] = span
    return AssembledRevision(
        work=work, revision=revision, blocks=assembled, spans=span_index
    )
