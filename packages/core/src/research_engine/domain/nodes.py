"""Document structure — the tree a reader navigates, addressed like a passage.

A document node is a span, not a copy: ``char_start`` / ``char_end`` index into
the same canonical text that passages address, so a node's prose is read back
with a substring and the tree costs nothing but its own skeleton. That is the
whole point. Chunking answers "what are the retrievable fragments"; this answers
"what are the parts the author wrote", and the two are different questions over
one substrate.

Paths are ``ltree`` values so that "everything under chapter 4" is one index
scan rather than a recursive walk. Labels are positional (``r.n0.n2``) because
ltree labels admit only alphanumerics and underscores, which rules out UUIDs.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

#: Label of the synthetic root every document tree carries. The root spans the
#: whole canonical text, which gives ancestor queries a uniform terminus and
#: gives documents with no recovered structure a valid (if trivial) tree.
ROOT_PATH = "r"


class DocumentNodeDraft(BaseModel):
    """A node before it has been given an identity by the repository.

    ``parent_path`` rather than ``parent_id``: the builder works in paths, and
    the repository resolves them to ids as it inserts parents before children.
    """

    path: str
    parent_path: str | None
    depth: int
    position: int
    node_type: str = "section"
    title: str | None = None
    char_start: int
    char_end: int
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _span_is_well_formed(self) -> DocumentNodeDraft:
        if self.char_start < 0:
            raise ValueError(f"char_start must be non-negative, got {self.char_start}")
        if self.char_end < self.char_start:
            raise ValueError(
                f"char_end ({self.char_end}) precedes char_start ({self.char_start})"
            )
        return self


class DocumentNode(BaseModel):
    """A stored node in a document's structural tree."""

    id: UUID
    document_id: UUID
    parent_id: UUID | None
    path: str
    depth: int
    position: int
    node_type: str
    title: str | None = None
    char_start: int
    char_end: int
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


def build_node_tree(
    sections: list[dict[str, Any]],
    *,
    text_length: int,
    title: str | None = None,
) -> list[DocumentNodeDraft]:
    """Turn a parser's flat section list into a containment tree.

    *sections* are the parser's own decomposition in document order, each with
    ``char_start`` / ``char_end`` and an optional ``level`` and ``heading``.
    Nesting is inferred from ``level`` with a stack: a section parents to the
    nearest preceding section of a shallower level, which handles both flat
    lists and levels that skip a rank.

    The returned drafts are in insertion order — every parent precedes its
    children — and satisfy the containment invariant: a node's span encloses the
    spans of all its descendants. Parsers report a heading's own span, not the
    span of everything beneath it, so parent spans are widened to cover their
    children; subtree queries and passage-to-node lookups both depend on it.
    """
    root = DocumentNodeDraft(
        path=ROOT_PATH,
        parent_path=None,
        depth=0,
        position=0,
        node_type="document",
        title=title,
        char_start=0,
        char_end=text_length,
    )
    drafts: list[DocumentNodeDraft] = [root]

    # Stack of (level, draft) for candidate parents, root first. The root sits
    # at level 0 so that any section, whatever its level, finds a parent.
    stack: list[tuple[int, DocumentNodeDraft]] = [(0, root)]
    child_counts: dict[str, int] = {ROOT_PATH: 0}

    for section in sections:
        start, end = section.get("char_start"), section.get("char_end")
        if start is None or end is None:
            # A node without a span cannot be addressed, cited, or re-anchored.
            # Skipping is right: the passage layer still covers the prose.
            continue

        level = section.get("level") or 1
        while len(stack) > 1 and stack[-1][0] >= level:
            stack.pop()
        parent = stack[-1][1]

        position = child_counts[parent.path]
        child_counts[parent.path] = position + 1
        path = f"{parent.path}.n{position}"
        child_counts[path] = 0

        draft = DocumentNodeDraft(
            path=path,
            parent_path=parent.path,
            depth=parent.depth + 1,
            position=position,
            node_type=section.get("node_type", "section"),
            title=section.get("heading"),
            char_start=start,
            char_end=end,
            metadata={
                key: value
                for key, value in section.items()
                if key not in {"char_start", "char_end", "heading", "level", "node_type"}
            },
        )
        drafts.append(draft)
        stack.append((level, draft))

    _widen_parents_to_cover_children(drafts)
    return drafts


def _widen_parents_to_cover_children(drafts: list[DocumentNodeDraft]) -> None:
    """Extend each node's span to enclose its descendants', deepest first.

    Walking in reverse means a child has already absorbed its own descendants by
    the time its parent reads it, so one pass suffices.
    """
    by_path = {draft.path: draft for draft in drafts}
    for draft in reversed(drafts):
        parent = by_path.get(draft.parent_path or "")
        if parent is None:
            continue
        parent.char_start = min(parent.char_start, draft.char_start)
        parent.char_end = max(parent.char_end, draft.char_end)
