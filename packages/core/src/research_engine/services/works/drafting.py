"""Render a draft to markdown and read an edited one back — the drafting loop.

`export_draft` regenerates the §6.4 file from rows: front matter with the
citations in block order, then one `<!-- block:<key> -->` comment per block
with its markdown. `import_draft` parses edited markdown per the §5.2 block
boundaries, copies the revision forward, and applies inserts, updates,
deletions, and reorders, matching blocks by their comments. Markers resolve
against the copied occurrences; a marker with no occurrence refuses the
whole import with `AUTH_CITATION_MARKER_DANGLING`.

Round-trip contract (§5.4): export then import with no edits is a no-op new
revision — same keys, empty diff. The two deliberate deviations from the
guide's formats are recorded in Appendix F: non-heading titles ride in a
`<!-- title: … -->` comment (rows have titles, §5.2 markdown has nowhere to
put them), and missing `block_end` markers are appended at block end on
export (that placement renders at the end by definition).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any
from uuid import UUID  # noqa: TC003 - pydantic needs it at runtime

import structlog
import yaml
from pydantic import BaseModel, Field
from uuid_utils import uuid7

from research_engine import _rust as _rust_backend
from research_engine.domain.errors import NotFoundError
from research_engine.domain.works import WorkBlockDraft
from research_engine.services.works.assembly import assemble_revision
from research_engine.services.works.markers import find_markers, format_marker

if TYPE_CHECKING:
    from collections.abc import Callable

    from research_engine.domain.works import Work, WorkRevision
    from research_engine.services.works.assembly import AssembledRevision

logger = structlog.get_logger()

_BLOCK_COMMENT_RE = re.compile(r"^<!--\s*block:([0-9a-fA-F-]{36})\s*-->$")
_TITLE_COMMENT_RE = re.compile(r"^<!--\s*title:\s*(.*?)\s*-->$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_FOOTNOTE_DEF_RE = re.compile(r"^\[\^[^\]]+\]:.*$")
_LIST_ITEM_RE = re.compile(r"^\s*([-*+]|\d+[.)])\s+")
_FRONT_MATTER_RE = re.compile(r"\A---\n(.*?)\n---(?:\n|\Z)", re.DOTALL)


class ImportRefused(Exception):
    """The import was refused with nothing written; the tool reports the rule."""

    def __init__(
        self, rule_id: str, message: str, detail: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.rule_id = rule_id
        self.message = message
        self.detail = detail


class BlockChange(BaseModel):
    block_key: str
    change: str  # added | updated | moved | deleted


class ImportDiff(BaseModel):
    revision_id: UUID
    revision_number: int
    dry_run: bool = False
    changes: list[BlockChange] = Field(default_factory=list)


class _ParsedBlock(BaseModel):
    key: UUID | None = None
    block_type: str
    title: str | None = None
    body: str = ""
    level: int = 2
    #: Index of the parent in the parsed list, for heading nesting.
    parent_index: int | None = None


class WorkExportService:
    """Markdown out of rows, and edited markdown back into a new draft."""

    def __init__(
        self,
        *,
        works: Any,
        revisions: Any,
        blocks: Any,
        citations: Any,
        links: Any,
        spans: Any,
        transaction_factory: Callable[[], Any],
    ) -> None:
        self._works = works
        self._revisions = revisions
        self._blocks = blocks
        self._citations = citations
        self._links = links
        self._spans = spans
        self._transaction = transaction_factory

    async def export_draft(
        self, *, slug: str | None = None, work_id: UUID | None = None
    ) -> str:
        """Render the work's current revision to §6.4 markdown."""
        work, revision = await self._resolve_current(slug=slug, work_id=work_id)
        view = await self._assemble(work, revision)
        return render_markdown(view)

    async def export_draft_text(self, work_id: UUID) -> str:
        """Positional wrapper for the drift check's exporter callback."""
        return await self.export_draft(work_id=work_id)

    async def import_draft(
        self,
        *,
        slug: str | None = None,
        work_id: UUID | None = None,
        markdown: str,
        dry_run: bool = False,
    ) -> ImportDiff:
        """Apply edited markdown as a new draft revision; refuse on dangling markers."""
        work, revision = await self._resolve_current(slug=slug, work_id=work_id)
        front, parsed = parse_markdown(markdown)
        if front.get("work") != work.slug:
            raise ValueError(
                f"Import names work {front.get('work')!r}, not {work.slug!r}"
            )
        known_keys, depth_by_key = await self._copy_inventory(revision.id)
        async with self._transaction() as tx:
            created = await self._revisions.copy_forward(tx, revision.id)
            changes = await self._apply(
                tx, created.id, parsed,
                known_keys=known_keys, depth_by_key=depth_by_key,
            )
            if dry_run:
                await tx.conn.rollback()
            else:
                # The new draft is the work now: without this the import is
                # unreachable — get, validate, and freeze all default to
                # current, and the old draft would stay there.
                await self._works.set_current_revision(tx, work.id, created.id)
        logger.info(
            "draft_imported", slug=work.slug,
            revision_number=created.revision_number,
            changes=len(changes), dry_run=dry_run,
        )
        return ImportDiff(
            revision_id=created.id,
            revision_number=created.revision_number,
            dry_run=dry_run,
            changes=changes,
        )

    async def _resolve_current(
        self, *, slug: str | None, work_id: UUID | None
    ) -> tuple[Work, WorkRevision]:
        if slug is not None:
            work = await self._works.get_by_slug(slug)
        elif work_id is not None:
            work = await self._works.get(work_id)
        else:
            work = None
        if work is None:
            raise NotFoundError("work", slug or work_id)
        if work.current_revision_id is None:
            raise NotFoundError("work_revision", f"current of {work.slug}")
        revision = await self._revisions.get(work.current_revision_id)
        if revision is None:
            raise NotFoundError("work_revision", work.current_revision_id)
        return work, revision

    async def _assemble(self, work: Work, revision: WorkRevision) -> AssembledRevision:
        return await assemble_revision(
            work,
            revision,
            blocks=self._blocks,
            citations=self._citations,
            links=self._links,
            spans=self._spans,
        )

    async def _copy_inventory(
        self, revision_id: UUID
    ) -> tuple[dict[str, str], dict[str, int]]:
        """Citation keys with their block keys, and block depths, pre-copy.

        Copy-forward preserves keys while turning ids over, so the import
        checks markers against this inventory instead of querying rows the
        open transaction has not committed yet.
        """
        tree = await self._blocks.tree(revision_id)
        by_id = {item.id: str(item.block_key) for item in tree}
        depth: dict[str, int] = {}
        for item in tree:
            level = 0
            parent = item.parent_id
            seen = {item.id}
            while parent is not None and parent not in seen:
                level += 1
                seen.add(parent)
                parent = next(
                    (row.parent_id for row in tree if row.id == parent), None
                )
            depth[str(item.block_key)] = level
        attached = await self._citations.for_revision(revision_id)
        known = {
            str(entry.occurrence.citation_key): by_id[entry.occurrence.block_id]
            for entry in attached
            if entry.occurrence.block_id in by_id
        }
        return known, depth

    async def _apply(
        self, tx: Any, revision_id: UUID, parsed: list[_ParsedBlock], *,
        known_keys: dict[str, str], depth_by_key: dict[str, int],
    ) -> list[BlockChange]:
        key_of_index: dict[int, UUID] = {}
        changes: list[BlockChange] = []
        sibling_position: dict[str, int] = {}
        # Blocks without a comment are new rows, so only explicitly keyed
        # blocks survive: markers naming occurrences of deleted blocks are
        # dangling, decided here before any row moves.
        kept = {str(block.key) for block in parsed if block.key is not None}
        live_keys = {
            cited: holder
            for cited, holder in known_keys.items()
            if holder in kept
        }

        for index, block in enumerate(parsed):
            keys, invalid = _find_markers(block.body)
            if invalid:
                raise ImportRefused(
                    "AUTH_CITATION_MARKER_DANGLING",
                    f"Marker {invalid[0]} names no citation: keys are UUIDs",
                    detail={"marker": invalid[0]},
                )
            for key in keys:
                if key not in live_keys:
                    raise ImportRefused(
                        "AUTH_CITATION_MARKER_DANGLING",
                        f"Marker {{{{cite:{key}}}}} matches no occurrence",
                        detail={"citation_key": key},
                    )
            # uuid_utils ids never cross into pydantic (see WorkService.upsert_block).
            key = block.key or UUID(str(uuid7()))
            parent_key: UUID | None = None
            if block.parent_index is not None:
                if block.parent_index not in key_of_index:
                    raise ValueError(
                        "A block's parent must precede it in the file"
                    )
                parent_key = key_of_index[block.parent_index]
            key_of_index[index] = key
            group = str(parent_key)
            position = sibling_position.get(group, 0)
            sibling_position[group] = position + 1
            parent_id = None
            if parent_key is not None:
                parent_row = await self._blocks.by_key_in_tx(
                    tx, revision_id, parent_key
                )
                if parent_row is None:
                    raise ValueError(
                        f"Parent block {parent_key} is not in this revision"
                    )
                parent_id = parent_row.id
            old = await self._blocks.by_key_in_tx(tx, revision_id, key)
            if old is None:
                await self._blocks.upsert(
                    tx,
                    revision_id,
                    WorkBlockDraft(
                        revision_id=revision_id,
                        block_key=key,
                        parent_id=parent_id,
                        position=position,
                        block_type=block.block_type,
                        title=block.title,
                        body_markdown=block.body,
                        attributes={"level": block.level}
                        if block.block_type == "heading"
                        else {},
                    ),
                    expected_updated_at=None,
                )
                changes.append(BlockChange(block_key=str(key), change="added"))
            else:
                moved = old.parent_id != parent_id or old.position != position
                edited = (
                    (old.title or None) != (block.title or None)
                    or old.body_markdown != block.body
                    or old.block_type != block.block_type
                )
                attributes = dict(old.attributes)
                base = dict(old.attributes)
                if block.block_type == "heading":
                    # Export renders a missing level as 2; a row that never
                    # stored one is not a change on reimport.
                    attributes["level"] = block.level
                    base.setdefault("level", 2)
                if moved or edited or attributes != base:
                    await self._blocks.upsert(
                        tx,
                        revision_id,
                        WorkBlockDraft(
                            revision_id=revision_id,
                            block_key=key,
                            parent_id=parent_id,
                            position=position,
                            block_type=block.block_type,
                            title=block.title,
                            body_markdown=block.body,
                            attributes=attributes,
                        ),
                        expected_updated_at=old.updated_at,
                    )
                    changes.append(
                        BlockChange(
                            block_key=str(key),
                            change="moved" if moved else "updated",
                        )
                    )

        imported_keys = {str(key) for key in key_of_index.values()}
        removed = sorted(
            (key for key in depth_by_key if key not in imported_keys),
            key=lambda key: depth_by_key[key],
            reverse=True,
        )
        for key in removed:
            row = await self._blocks.by_key_in_tx(tx, revision_id, UUID(key))
            if row is not None:
                await self._blocks.delete(tx, row.id)
                changes.append(BlockChange(block_key=key, change="deleted"))
        return changes


def render_markdown(view: AssembledRevision) -> str:
    """The §6.4 file: front matter with citations in block order, then blocks."""
    front: dict[str, Any] = {
        "work": view.work.slug,
        "title": view.work.title,
        "type": view.work.work_type,
        "revision": view.revision.revision_number,
        "state": view.revision.state.value,
    }
    entries: list[dict[str, Any]] = []
    for item in view.blocks:
        for entry in item.citations:
            for row in entry.items:
                span = (
                    view.spans.get(row.source_span_id)
                    if row.source_span_id is not None
                    else None
                )
                cited: dict[str, Any] = {
                    "key": str(entry.occurrence.citation_key),
                    "intent": entry.occurrence.intent.value,
                }
                if span is not None:
                    cited["document_id"] = str(span.document_id)
                    cited["char_start"] = span.char_start
                    cited["char_end"] = span.char_end
                if row.quoted_text is not None:
                    cited["quoted_text"] = row.quoted_text
                if row.verify_status is not None:
                    cited["verify_status"] = row.verify_status
                if row.edition_key is not None:
                    cited["edition_key"] = row.edition_key
                if row.edition_id is not None:
                    cited["edition_id"] = str(row.edition_id)
                if row.locator:
                    cited["locator"] = dict(row.locator)
                entries.append(cited)
    if entries:
        front["citations"] = entries
    parts = ["---", yaml.safe_dump(front, sort_keys=False, allow_unicode=True).rstrip(), "---", ""]
    for item in view.blocks:
        block = item.block
        parts.append(f"<!-- block:{block.block_key} -->")
        if block.block_type == "heading":
            level = block.attributes.get("level", 2)
            level = min(max(int(level), 1), 6)
            parts.append(f"{'#' * level} {block.title or ''}".rstrip())
            if block.body_markdown:
                parts.append(block.body_markdown)
        else:
            if block.title:
                parts.append(f"<!-- title: {block.title} -->")
            parts.append(block.body_markdown)
        for entry in item.citations:
            if entry.occurrence.placement.value != "block_end":
                continue
            marker = _format_marker(entry.occurrence.citation_key)
            if marker not in (block.body_markdown or ""):
                parts.append(marker)
        parts.append("")
    return "\n".join(parts)


def parse_markdown(markdown: str) -> tuple[dict[str, Any], list[_ParsedBlock]]:
    """Split edited markdown into front matter and §5.2 blocks."""
    match = _FRONT_MATTER_RE.match(markdown)
    if match is None:
        raise ValueError("Import needs YAML front matter between --- lines")
    front = yaml.safe_load(match.group(1)) or {}
    if not isinstance(front, dict):
        raise ValueError("Front matter must be a mapping")
    body = markdown[match.end():]
    parsed: list[_ParsedBlock] = []
    heading_stack: list[tuple[int, int]] = []  # (level, parsed index)

    def parent_for() -> int | None:
        return heading_stack[-1][1] if heading_stack else None

    pending_key: UUID | None = None
    pending_title: str | None = None
    lines = body.splitlines()
    index = 0
    current: list[str] = []

    def flush_paragraph() -> None:
        nonlocal current
        text = "\n".join(current).strip("\n")
        current = []
        if text.strip():
            parsed.append(
                _ParsedBlock(
                    key=_take_key(), block_type="paragraph",
                    title=_take_title(), body=text,
                    parent_index=parent_for(),
                )
            )

    def _take_key() -> UUID | None:
        nonlocal pending_key
        key, pending_key = pending_key, None
        return key

    def _take_title() -> str | None:
        nonlocal pending_title
        title, pending_title = pending_title, None
        return title

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        block_match = _BLOCK_COMMENT_RE.match(stripped)
        title_match = _TITLE_COMMENT_RE.match(stripped)
        heading_match = _HEADING_RE.match(line)
        if block_match is not None:
            flush_paragraph()
            try:
                pending_key = UUID(block_match.group(1))
            except ValueError:
                raise ValueError(
                    f"Bad block comment: {stripped!r}"
                ) from None
        elif title_match is not None:
            pending_title = title_match.group(1) or None
        elif heading_match is not None:
            flush_paragraph()
            level = len(heading_match.group(1))
            title = heading_match.group(2).strip() or None
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            parsed.append(
                _ParsedBlock(
                    key=_take_key(), block_type="heading", title=title,
                    body="", level=level,
                    parent_index=parent_for(),
                )
            )
            heading_stack.append((level, len(parsed) - 1))
        elif stripped.startswith("```"):
            flush_paragraph()
            fence = [line]
            index += 1
            while index < len(lines) and not lines[index].strip().startswith("```"):
                fence.append(lines[index])
                index += 1
            if index < len(lines):
                fence.append(lines[index])
            parsed.append(
                _ParsedBlock(
                    key=_take_key(), block_type="code",
                    title=_take_title(), body="\n".join(fence),
                    parent_index=parent_for(),
                )
            )
        elif stripped.startswith(">"):
            flush_paragraph()
            quote = [line]
            index += 1
            while index < len(lines) and lines[index].strip().startswith(">"):
                quote.append(lines[index])
                index += 1
            index -= 1
            parsed.append(
                _ParsedBlock(
                    key=_take_key(), block_type="quotation",
                    title=_take_title(), body="\n".join(quote),
                    parent_index=parent_for(),
                )
            )
        elif _LIST_ITEM_RE.match(line) and not current:
            quote_lines = [line]
            index += 1
            while index < len(lines) and (
                _LIST_ITEM_RE.match(lines[index])
                or (lines[index].strip() and lines[index][0].isspace())
            ):
                quote_lines.append(lines[index])
                index += 1
            index -= 1
            parsed.append(
                _ParsedBlock(
                    key=_take_key(), block_type="list",
                    title=_take_title(), body="\n".join(quote_lines),
                    parent_index=parent_for(),
                )
            )
        elif _FOOTNOTE_DEF_RE.match(stripped):
            pass  # footnotes render from rows; definitions do not round-trip
        elif not stripped:
            flush_paragraph()
        else:
            current.append(line)
        index += 1
    flush_paragraph()
    return front, parsed


def _find_markers(text):
    """`find_markers`, via the Rust backend when selected (see `research_engine._rust`)."""
    rs = _rust_backend.rust_works()
    if rs is not None:
        keys, invalid = rs.find_markers(text or "")
        return set(keys), list(invalid)
    return find_markers(text)


def _format_marker(citation_key):
    """`format_marker`, via the Rust backend when selected."""
    rs = _rust_backend.rust_works()
    if rs is not None:
        return rs.format_marker(str(citation_key))
    return format_marker(citation_key)
