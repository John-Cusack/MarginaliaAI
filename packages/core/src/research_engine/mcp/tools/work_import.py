"""work_import tool — apply edited markdown as a new draft revision."""

from __future__ import annotations

from typing import Any

import structlog

from research_engine.domain.errors import NotFoundError
from research_engine.mcp.errors import envelope, failed
from research_engine.services.works.drafting import ImportRefused

logger = structlog.get_logger()

TOOL_NAME = "work_import"
TOOL_DESCRIPTION = (
    "Apply edited markdown text as a new draft revision of an authored work, "
    "returned as an ImportDiff — the server never reads or writes vault "
    "files, so pass the markdown inline. Export first with work_export and "
    "import only what that export produced: a file whose base stamp differs "
    "from the current revision is refused as stale, and nothing is written."
)
TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "slug": {"type": "string"},
        "markdown": {"type": "string"},
        "dry_run": {
            "type": "boolean",
            "description": "Compute the diff, write nothing.",
        },
    },
    "required": ["slug", "markdown"],
}


async def handler(
    container: Any,
    *,
    slug: str,
    markdown: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    service = container.work_export
    try:
        diff = await service.import_draft(
            slug=slug, markdown=markdown, dry_run=dry_run
        )
        return diff.model_dump(mode="json")
    except ImportRefused as exc:
        return envelope(
            "validation_error",
            exc.message,
            {"rule_id": exc.rule_id, **(exc.detail or {})},
        )
    except NotFoundError as exc:
        return envelope("not_found", str(exc), None)
    except ValueError as exc:
        return envelope("invalid_input", str(exc), None)
    except Exception as exc:  # noqa: BLE001 - the dispatch envelope for the unexpected
        logger.error("work_import_error", error=str(exc))
        return failed(TOOL_NAME, exc)
