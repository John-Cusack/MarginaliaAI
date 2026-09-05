"""work_trace tool — walk a work's grounding down, or a span's users up."""

from __future__ import annotations

from typing import Any

import structlog

from research_engine.domain.errors import NotFoundError

logger = structlog.get_logger()

TOOL_NAME = "work_trace"
TOOL_DESCRIPTION = (
    "Trace grounding as a tree. From a work, block, or citation key: which "
    "corpus addresses it rests on, down to document offsets. From a span or "
    "document: every block and anchor resting on it. Exactly one selector."
)
TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "slug": {"type": "string"},
        "block_key": {"type": "string", "format": "uuid"},
        "citation_key": {"type": "string", "format": "uuid"},
        "source_span_id": {"type": "string", "format": "uuid"},
        "document_id": {"type": "string", "format": "uuid"},
    },
}


async def handler(
    container: Any,
    *,
    slug: str | None = None,
    block_key: str | None = None,
    citation_key: str | None = None,
    source_span_id: str | None = None,
    document_id: str | None = None,
) -> dict[str, Any]:
    service = getattr(container, "work_trace", None)
    if service is None:  # pragma: no cover - composition always builds it
        return {
            "error": {
                "code": "works_not_configured",
                "message": "The trace service is not built.",
                "details": None,
            }
        }
    try:
        node = await service.trace(
            slug=slug,
            block_key=block_key,
            citation_key=citation_key,
            source_span_id=source_span_id,
            document_id=document_id,
        )
        return node.model_dump(mode="json")
    except NotFoundError as exc:
        return {"error": {"code": "not_found", "message": str(exc), "details": None}}
    except ValueError as exc:
        return {"error": {"code": "invalid_input", "message": str(exc), "details": None}}
    except Exception as exc:  # noqa: BLE001 - the dispatch envelope for the unexpected
        logger.error("work_trace_error", error=str(exc))
        return {"error": {"code": "work_trace_failed", "message": str(exc), "details": None}}
