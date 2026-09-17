"""claim_audit tool — report mechanical claim-ledger failures."""

from __future__ import annotations

from typing import Any

import structlog

from research_engine.domain.claims import CLAIM_AUDIT_ASSURANCE
from research_engine.domain.errors import NotFoundError
from research_engine.mcp.errors import envelope, failed

logger = structlog.get_logger()

TOOL_NAME = "claim_audit"
TOOL_DESCRIPTION = (
    "Run the implemented mechanical checks over the whole claim ledger or only "
    "the named subject refs. Reports missing attribution, rebuttals aimed at "
    "unattributed claims, and public-ready claims with either failure open. "
    + CLAIM_AUDIT_ASSURANCE
)
TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "refs": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Optional subject claim refs. Omit for the full ledger.",
        }
    },
}


async def handler(
    container: Any,
    *,
    refs: list[str] | None = None,
) -> dict[str, Any]:
    try:
        report = await container.claim_audit_service.audit(refs)
        return report.model_dump(mode="json")
    except NotFoundError as exc:
        return envelope("not_found", str(exc), None)
    except ValueError as exc:
        return envelope("invalid_input", str(exc), None)
    except Exception as exc:  # noqa: BLE001 - unexpected tool failure envelope
        logger.error("claim_audit_error", error=str(exc))
        return failed(TOOL_NAME, exc)
