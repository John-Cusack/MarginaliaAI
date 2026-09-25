"""work_promote tool — make a plain note a work at revision 1."""

from __future__ import annotations

from typing import Any

import structlog

from research_engine.domain.errors import NotFoundError
from research_engine.mcp.errors import envelope, failed
from research_engine.services.works.drafting import ImportRefused

logger = structlog.get_logger()

TOOL_NAME = "work_promote"
TOOL_DESCRIPTION = (
    "Promote a plain markdown note to an authored work: create the slug and "
    "land the note as revision 1, returned as an ImportDiff — the server "
    "never reads or writes vault files, so pass the markdown inline. Notes "
    "need no front matter; headings, paragraphs, and lists become blocks. "
    "Citations come after promotion, so any {{cite:…}} marker is refused."
)
TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "slug": {"type": "string", "description": "Unique kebab-case slug."},
        "title": {"type": "string"},
        "work_type": {
            "type": "string",
            "description": "translation, essay, dossier, script, outline, or pack-defined.",
        },
        "markdown": {"type": "string"},
        "dry_run": {
            "type": "boolean",
            "description": "Compute the diff, write nothing.",
        },
    },
    "required": ["slug", "title", "work_type", "markdown"],
}


async def handler(
    container: Any,
    *,
    slug: str,
    title: str,
    work_type: str,
    markdown: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    service = container.work_export
    try:
        diff = await service.promote(
            slug=slug, title=title, work_type=work_type,
            markdown=markdown, dry_run=dry_run,
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
        logger.error("work_promote_error", error=str(exc))
        return failed(TOOL_NAME, exc)
