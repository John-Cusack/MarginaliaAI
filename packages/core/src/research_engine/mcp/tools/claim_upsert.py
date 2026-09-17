"""claim_upsert tool — atomically write a claim and its grounded evidence."""

from __future__ import annotations

from typing import Any

import structlog
from pydantic import ValidationError

from research_engine.domain.claims import (
    AnchorInput,
    ClaimDraft,
    ClaimEdgeDraft,
    ClaimKind,
    ClaimRelation,
    ClaimStatus,
)
from research_engine.mcp.errors import envelope, failed
from research_engine.services.argument.claims import ClaimWriteRefused

logger = structlog.get_logger()

TOOL_NAME = "claim_upsert"
TOOL_DESCRIPTION = (
    "Create or update one addressable claim, its outgoing edges, and evidence "
    "anchors in one transaction. Every anchor is checked against canonical "
    "text before anything is written. not_found and no_canonical_text refuse "
    "the whole call; asserts anchors must name the person making the claim. "
    "The caller's typed quote is preserved while the shared source span owns "
    "the canonical slice."
)
TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ref": {"type": "string"},
        "statement": {"type": "string"},
        "kind": {"type": "string", "enum": [item.value for item in ClaimKind]},
        "status": {
            "type": "string",
            "enum": [item.value for item in ClaimStatus],
            "default": ClaimStatus.OPEN.value,
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "steelman": {"type": "string"},
        "public_ready": {"type": "boolean", "default": False},
        "academic_candidate": {"type": "boolean", "default": False},
        "attributes": {"type": "object"},
        "edges": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "target_ref": {"type": "string"},
                    "relation": {
                        "type": "string",
                        "enum": [item.value for item in ClaimRelation],
                    },
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "note": {"type": "string"},
                },
                "required": ["target_ref", "relation"],
            },
        },
        "anchors": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "role": {
                        "type": "string",
                        "enum": ["asserts", "supports", "rebuts", "context"],
                    },
                    "quote": {"type": "string"},
                    "document_id": {"type": "string", "format": "uuid"},
                    "person": {"type": "string"},
                    "edition_key": {"type": "string"},
                    "locator": {"type": "object"},
                },
                "required": ["role", "quote", "document_id"],
            },
        },
    },
    "required": ["ref", "statement", "kind"],
}


async def handler(
    container: Any,
    *,
    ref: str,
    statement: str,
    kind: str,
    status: str = ClaimStatus.OPEN.value,
    confidence: float | None = None,
    steelman: str | None = None,
    public_ready: bool = False,
    academic_candidate: bool = False,
    attributes: dict[str, Any] | None = None,
    edges: list[Any] | None = None,
    anchors: list[Any] | None = None,
) -> dict[str, Any]:
    try:
        claim = ClaimDraft(
            ref=ref,
            statement=statement,
            kind=kind,
            status=status,
            confidence=confidence,
            steelman=steelman,
            public_ready=public_ready,
            academic_candidate=academic_candidate,
            attributes=attributes or {},
        )
        edge_drafts = [ClaimEdgeDraft.model_validate(item) for item in edges or []]
        anchor_drafts = [AnchorInput.model_validate(item) for item in anchors or []]
    except ValidationError as exc:
        return envelope("invalid_input", "Invalid claim ledger input.", exc.errors())

    try:
        result = await container.claim_service.upsert(
            claim,
            edges=edge_drafts,
            anchors=anchor_drafts,
        )
        return result.model_dump(mode="json")
    except ClaimWriteRefused as exc:
        return envelope(exc.code, exc.message, exc.detail)
    except ValueError as exc:
        return envelope("invalid_input", str(exc), None)
    except Exception as exc:  # noqa: BLE001 - unexpected tool failure envelope
        logger.error("claim_upsert_error", error=str(exc))
        return failed(TOOL_NAME, exc)
