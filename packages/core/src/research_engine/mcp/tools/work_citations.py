"""work_citations tool -- which works cite this source."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog

from research_engine.services.works.citations import WorkCitationFinder

logger = structlog.get_logger()

TOOL_NAME = "work_citations"
TOOL_DESCRIPTION = (
    "Find the works citing a source: by document id, by edition key, or the "
    "works resting on a claim ref. Exactly one selector. In Phase 0 this scans "
    "the work files; once the Step 4 mirror exists the same call queries it "
    "and says so in `source`."
)
TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "document_id": {
            "type": "string",
            "format": "uuid",
            "description": "Only entries citing this document.",
        },
        "edition_key": {
            "type": "string",
            "description": "Only entries carrying this edition key.",
        },
        "claim_ref": {
            "type": "string",
            "description": "Only works resting on this claim ref.",
        },
    },
}


async def handler(
    container: Any,
    *,
    document_id: str | None = None,
    edition_key: str | None = None,
    claim_ref: str | None = None,
) -> dict[str, Any]:
    reader = getattr(container, "work_files", None)
    if reader is None:
        return {
            "error": {
                "code": "works_not_configured",
                "message": "RE_WORKS_DIR is not set, so no work file can be read.",
                "details": None,
            }
        }
    given = [name for name, value in
             (("document_id", document_id), ("edition_key", edition_key), ("claim_ref", claim_ref))
             if value is not None]
    if len(given) != 1:
        return {
            "error": {
                "code": "invalid_input",
                "message": f"Pass exactly one of document_id, edition_key, claim_ref (got {given})",
                "details": None,
            }
        }
    doc_uuid = None
    if document_id is not None:
        try:
            doc_uuid = UUID(document_id)
        except ValueError:
            return {
                "error": {
                    "code": "invalid_input",
                    "message": f"document_id is not a UUID: {document_id}",
                    "details": None,
                }
            }
    try:
        finder = WorkCitationFinder(reader.works_dir)
        result = await finder.find(
            document_id=doc_uuid, edition_key=edition_key, claim_ref=claim_ref
        )
        if getattr(container, "works_mirror_available", False):
            result["source"] = "mirror"
        return result
    except Exception as exc:  # noqa: BLE001 - the dispatch envelope for the unexpected
        logger.error("work_citations_error", error=str(exc))
        return {"error": {"code": "work_citations_failed", "message": str(exc), "details": None}}
