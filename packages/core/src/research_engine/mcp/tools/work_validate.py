"""work_validate tool — judge a revision's rows against a gate."""

from __future__ import annotations

from typing import Any

import structlog

from research_engine.domain.errors import NotFoundError
from research_engine.mcp.errors import envelope, failed

logger = structlog.get_logger()

TOOL_NAME = "work_validate"
TOOL_DESCRIPTION = (
    "Validate a work revision: markers against occurrences, tiers and "
    "waivers, span staleness and narrowing, edition identity, frozen-hash "
    "integrity. Gate none lists findings; freeze and publish fail on "
    "unwaived errors. Findings carry rule ids keyed by block_key and "
    "citation_key."
)
TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "slug": {"type": "string"},
        "revision": {"type": "integer", "description": "Revision number; omit for current."},
        "gate": {"type": "string", "enum": ["none", "freeze", "publish"]},
    },
    "required": ["slug"],
}


async def handler(
    container: Any,
    *,
    slug: str,
    revision: int | None = None,
    gate: str = "none",
) -> dict[str, Any]:
    service = container.work_validation
    try:
        report = await service.validate(slug=slug, revision=revision, gate=gate)  # type: ignore[arg-type]
        return report.model_dump(mode="json")
    except NotFoundError as exc:
        return envelope("not_found", str(exc), None)
    except ValueError as exc:
        return envelope("invalid_input", str(exc), None)
    except Exception as exc:  # noqa: BLE001 - the dispatch envelope for the unexpected
        logger.error("work_validate_error", error=str(exc))
        return failed(TOOL_NAME, exc)
