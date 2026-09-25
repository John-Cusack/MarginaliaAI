"""work_publish tool — validate at publish, seal the frozen revision."""

from __future__ import annotations

from typing import Any

import structlog

from research_engine.domain.errors import NotFoundError
from research_engine.mcp.errors import envelope, failed
from research_engine.services.works.publication import FreezeBlocked

logger = structlog.get_logger()

TOOL_NAME = "work_publish"
TOOL_DESCRIPTION = (
    "Publish the work's frozen revision: validate at the publish gate, where "
    "citation edition identity graduates from warning to error, and seal the "
    "revision as published. A human act: only publish what was reviewed. "
    "A never-frozen draft is refused."
)
TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "slug": {"type": "string"},
    },
    "required": ["slug"],
}


async def handler(container: Any, *, slug: str) -> dict[str, Any]:
    service = container.work_publication
    try:
        sealed = await service.publish(slug=slug)
        return sealed.model_dump(mode="json")
    except FreezeBlocked as exc:
        return envelope("validation_error", str(exc), {"blockers": exc.blockers})
    except NotFoundError as exc:
        return envelope("not_found", str(exc), None)
    except ValueError as exc:
        return envelope("invalid_input", str(exc), None)
    except Exception as exc:  # noqa: BLE001 - the dispatch envelope for the unexpected
        logger.error("work_publish_error", error=str(exc))
        return failed(TOOL_NAME, exc)
