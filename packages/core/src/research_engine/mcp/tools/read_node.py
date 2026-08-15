"""read_node tool -- read a section of a document as the author wrote it."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog

logger = structlog.get_logger()

TOOL_NAME = "read_node"
TOOL_DESCRIPTION = (
    "Read one part of a document — a chapter, a section — as continuous prose "
    "rather than as retrieval fragments. Use it after get_document_outline, or "
    "on the node a search hit belongs to when the surrounding argument matters "
    "more than the matching sentence. Check char_length from the outline first: "
    "a whole chapter can be very long, and include_descendants makes it longer."
)
#: Refuse rather than silently truncate past this. A caller that asked for a
#: 400,000-character part and got 40,000 without being told would reason about
#: an argument it had only seen the opening of.
MAX_CHARS = 120_000

TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "node_id": {
            "type": "string",
            "format": "uuid",
            "description": "UUID of the node, from get_document_outline.",
        },
        "include_descendants": {
            "type": "boolean",
            "default": True,
            "description": (
                "Include everything nested beneath this node. True reads a "
                "chapter whole; False reads only the prose before its first "
                "subsection."
            ),
        },
        "max_chars": {
            "type": "integer",
            "description": (
                f"Refuse rather than return more than this many characters "
                f"(default and ceiling {MAX_CHARS}). Narrow your node instead."
            ),
        },
    },
    "required": ["node_id"],
}


async def handler(
    container: Any,
    *,
    node_id: str,
    include_descendants: bool = True,
    max_chars: int | None = None,
) -> dict[str, Any]:
    """Return a node's prose, sliced from the document's canonical text."""
    try:
        nid = UUID(node_id)
        nodes_repo = container.document_nodes

        ancestors = await nodes_repo.get_ancestors(nid)
        if not ancestors:
            return {
                "error": {
                    "code": "node_not_found",
                    "message": f"No node {node_id}.",
                    "details": None,
                }
            }
        node = ancestors[-1]

        # The node's own span already covers its descendants — build_node_tree
        # widens parents to contain them — so narrowing, not widening, is the
        # work when the caller wants only this node's own prose.
        start, end = node.char_start, node.char_end
        if not include_descendants:
            children = [
                child
                for child in await nodes_repo.get_subtree(nid)
                if child.parent_id == node.id
            ]
            if children:
                end = min(child.char_start for child in children)

        limit = min(max_chars or MAX_CHARS, MAX_CHARS)
        if end - start > limit:
            return {
                "error": {
                    "code": "node_too_large",
                    "message": (
                        f"This part is {end - start} characters, over the "
                        f"{limit} limit. Read a node further down the tree, or "
                        f"set include_descendants=false."
                    ),
                    "details": {"char_length": end - start, "limit": limit},
                }
            }

        text = await container.document_texts.get_text(node.document_id)
        if text is None:
            return {
                "error": {
                    "code": "no_canonical_text",
                    "message": (
                        "The document's canonical text is not stored, so its "
                        "structure cannot be read back."
                    ),
                    "details": None,
                }
            }

        return {
            "node_id": node_id,
            "document_id": str(node.document_id),
            "title": node.title,
            "node_type": node.node_type,
            # The ancestor titles are the citation: "Vol. II, ch. 14, §3".
            "breadcrumb": [a.title for a in ancestors if a.title],
            "char_start": start,
            "char_end": end,
            "include_descendants": include_descendants,
            "text": text[start:end],
        }
    except ValueError as e:
        return {"error": {"code": "invalid_input", "message": str(e), "details": None}}
    except Exception as e:
        logger.error("read_node_error", error=str(e))
        return {
            "error": {"code": "read_node_failed", "message": str(e), "details": None}
        }
