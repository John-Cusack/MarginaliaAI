"""locate_passage tool -- turn a search hit into a place in a document."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog

logger = structlog.get_logger()

TOOL_NAME = "locate_passage"
TOOL_DESCRIPTION = (
    "Given a passage from find_passages, say where in its document it sits: the "
    "chapter and section containing it, and the chain of titles above it. Use "
    "it to cite a hit as something a reader can check rather than as an opaque "
    "id, and to find the node to read when the surrounding argument matters. "
    "Pass several hits at once to see how many distinct discussions they "
    "actually represent — repeated terminology often means far fewer places "
    "than hits."
)
TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "passage_ids": {
            "type": "array",
            "items": {"type": "string", "format": "uuid"},
            "description": "UUIDs of passages, typically straight from find_passages.",
        }
    },
    "required": ["passage_ids"],
}


async def handler(
    container: Any,
    *,
    passage_ids: list[str],
) -> dict[str, Any]:
    """Locate each passage in its document's structure, and group by node."""
    try:
        passage_repo = container.passage_repo
        nodes_repo = container.document_nodes

        located: list[dict[str, Any]] = []
        unlocated: list[str] = []
        # Ancestor chains are shared by every hit in the same section, and a
        # search returns many; resolve each node once.
        chains: dict[UUID, list[str]] = {}

        for raw_id in passage_ids:
            passage = await passage_repo.get(UUID(raw_id))
            if passage is None:
                unlocated.append(raw_id)
                continue

            node_id = passage.node_id
            if node_id is None:
                # The passage exists but its document has no structure. Not an
                # error — most of a corpus can be like this — but the caller
                # must not read the omission as "nowhere in particular".
                unlocated.append(raw_id)
                continue

            if node_id not in chains:
                ancestors = await nodes_repo.get_ancestors(node_id)
                chains[node_id] = [a.title for a in ancestors if a.title]

            located.append(
                {
                    "passage_id": raw_id,
                    "document_id": str(passage.document_id),
                    "node_id": str(node_id),
                    "breadcrumb": chains[node_id],
                    "citation": " > ".join(chains[node_id]) or None,
                }
            )

        by_node: dict[str, int] = {}
        for entry in located:
            by_node[entry["node_id"]] = by_node.get(entry["node_id"], 0) + 1

        return {
            "located": located,
            # The point of asking about several at once: how many places these
            # hits really come from.
            "distinct_nodes": len(by_node),
            "hits_per_node": by_node,
            "unlocated": unlocated,
            "note": (
                "Passages listed under 'unlocated' have no recorded structure — "
                "their document was ingested before structure was captured, or "
                "its parser reports none."
            )
            if unlocated
            else None,
        }
    except ValueError as e:
        return {"error": {"code": "invalid_input", "message": str(e), "details": None}}
    except Exception as e:
        logger.error("locate_passage_error", error=str(e))
        return {
            "error": {
                "code": "locate_passage_failed",
                "message": str(e),
                "details": None,
            }
        }
