"""work_freeze tool — gate, waive, hash, and seal the current draft."""

from __future__ import annotations

from typing import Any

import structlog

from research_engine.domain.errors import NotFoundError
from research_engine.mcp.errors import envelope, failed
from research_engine.services.works.publication import FreezeBlocked, WaiverGiven

logger = structlog.get_logger()

TOOL_NAME = "work_freeze"
TOOL_DESCRIPTION = (
    "Freeze the work's current draft revision: validate at the freeze gate, "
    "record the given waivers as rows (who answers for what, and why), and "
    "seal the revision with its content hash. Blockers refuse the whole "
    "freeze, waivers included. A human act: only freeze what was reviewed."
)
TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "slug": {"type": "string"},
        "message": {"type": "string", "description": "Why this revision is sealed."},
        "waivers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "rule_id": {"type": "string"},
                    "subject": {"type": "string"},
                    "reason": {"type": "string"},
                    "actor": {"type": "string"},
                },
                "required": ["rule_id", "reason"],
            },
        },
    },
    "required": ["slug"],
}


async def handler(
    container: Any,
    *,
    slug: str,
    message: str | None = None,
    waivers: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    service = container.work_publication
    given: list[WaiverGiven] = []
    for waiver in waivers or []:
        if not isinstance(waiver, dict) or not waiver.get("rule_id") or not waiver.get("reason"):
            return envelope("invalid_input", "waivers need rule_id and reason", None)
        given.append(
            WaiverGiven(
                rule_id=str(waiver["rule_id"]),
                subject=waiver.get("subject"),
                reason=str(waiver["reason"]),
                actor=str(waiver.get("actor", "user")),
            )
        )
    try:
        sealed = await service.freeze(slug=slug, message=message, waivers=given)
        return sealed.model_dump(mode="json")
    except FreezeBlocked as exc:
        return envelope("validation_error", str(exc), {"blockers": exc.blockers})
    except NotFoundError as exc:
        return envelope("not_found", str(exc), None)
    except ValueError as exc:
        return envelope("invalid_input", str(exc), None)
    except Exception as exc:  # noqa: BLE001 - the dispatch envelope for the unexpected
        logger.error("work_freeze_error", error=str(exc))
        return failed(TOOL_NAME, exc)
