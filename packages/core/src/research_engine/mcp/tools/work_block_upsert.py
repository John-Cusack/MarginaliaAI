"""work_block_upsert tool — insert a block by fresh key or update one."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog

from research_engine.domain.errors import FrozenRevisionError, NotFoundError, StaleWriteError
from research_engine.mcp.errors import envelope, failed

logger = structlog.get_logger()

TOOL_NAME = "work_block_upsert"
TOOL_DESCRIPTION = (
    "Write one block of the work's current draft revision. Omit block_key "
    "to insert; pass it with the block's expected_updated_at (from "
    "work_get) to update. A stale timestamp answers conflict: re-read and "
    "retry, never last-write-wins."
)
TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "slug": {"type": "string"},
        "block_key": {"type": "string", "format": "uuid"},
        "parent_key": {"type": "string", "format": "uuid"},
        "position": {"type": "integer"},
        "block_type": {"type": "string"},
        "title": {"type": "string"},
        "body_markdown": {"type": "string"},
        "attributes": {"type": "object"},
        "expected_updated_at": {"type": "string"},
    },
    "required": ["slug", "position", "block_type", "body_markdown"],
}


async def handler(
    container: Any,
    *,
    slug: str,
    position: int,
    block_type: str,
    body_markdown: str,
    block_key: str | None = None,
    parent_key: str | None = None,
    title: str | None = None,
    attributes: dict[str, Any] | None = None,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    service = container.work_service
    try:
        block_uuid = UUID(block_key) if block_key is not None else None
        parent_uuid = UUID(parent_key) if parent_key is not None else None
    except (ValueError, TypeError):
        return envelope("invalid_input", "block_key and parent_key must be UUIDs", None)
    try:
        written = await service.upsert_block(
            slug=slug,
            position=position,
            block_type=block_type,
            body_markdown=body_markdown,
            block_key=block_uuid,
            parent_key=parent_uuid,
            title=title,
            attributes=attributes,
            expected_updated_at=expected_updated_at,
        )
        return written.model_dump(mode="json")
    except NotFoundError as exc:
        return envelope("not_found", str(exc), None)
    except (StaleWriteError, FrozenRevisionError) as exc:
        return envelope("conflict", str(exc), None)
    except ValueError as exc:
        return envelope("invalid_input", str(exc), None)
    except Exception as exc:  # noqa: BLE001 - the dispatch envelope for the unexpected
        logger.error("work_block_upsert_error", error=str(exc))
        return failed(TOOL_NAME, exc)
